# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The closed proposal task registry, the Candidate Snapshot, and the termination task."""

from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from netbox_data_import.field_keys import (
    MAPPED_PEER_ROLE,
    SELECT_TERMINATION_TASK,
    TERMINATION_ROLE,
    termination_field_key,
)
from netbox_data_import.models import ImportProfile, TerminationResolution
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.proposal_tasks import (
    NO_CANDIDATES,
    TOO_MANY_CANDIDATES,
    CandidateSet,
    CandidateSnapshot,
    CandidateSnapshotEntry,
    UnknownProposalTask,
    UnusableCandidateSet,
    proposal_task,
    snapshot_from,
)
from netbox_data_import.termination_proposal import SelectTerminationTask, UnsupportedProposalRole
from netbox_data_import.tests.helpers import make_dcim_objects

User = get_user_model()


class FakeCandidate:
    """One retrieved object, carrying only what a snapshot entry reads from it."""

    def __init__(self, pk, name):
        self.pk = pk
        self.name = name

    def __str__(self):
        return self.name


def fake_set(count, total=None):
    """Return a retrieved set of *count* objects claiming an uncapped *total*."""
    objects = tuple(FakeCandidate(pk=index + 1, name=f"Ethernet 1/{index + 1}") for index in range(count))
    return CandidateSet(objects=objects, total=count if total is None else total)


def build(candidate_set, limit=64):
    """Snapshot *candidate_set* with a fixed object-type label."""
    return snapshot_from(candidate_set, label_for=lambda _c: "dcim.interface", name_for=str, limit=limit)


class CandidateSnapshotTest(SimpleTestCase):
    """A snapshot freezes the whole eligible set, or it refuses to exist."""

    def test_entries_carry_positional_opaque_identifiers(self):
        snapshot = build(fake_set(3))

        self.assertEqual(snapshot.candidate_ids, ("candidate-0001", "candidate-0002", "candidate-0003"))
        self.assertEqual(snapshot.entries[1].object_id, 2)
        self.assertEqual(snapshot.entries[1].object_type, "dcim.interface")
        self.assertEqual(snapshot.entries[1].display_name, "Ethernet 1/2")

    def test_an_empty_set_cannot_back_a_proposal(self):
        with self.assertRaises(UnusableCandidateSet) as caught:
            build(fake_set(0))

        self.assertEqual(caught.exception.reason, NO_CANDIDATES)

    def test_a_set_past_the_bound_is_refused(self):
        with self.assertRaises(UnusableCandidateSet) as caught:
            build(fake_set(65), limit=64)

        self.assertEqual(caught.exception.reason, TOO_MANY_CANDIDATES)

    def test_the_bound_itself_is_accepted(self):
        self.assertEqual(build(fake_set(64), limit=64).total, 64)

    def test_a_truncated_retrieval_never_establishes_freshness(self):
        """A page of 20 out of 48 would freeze a set that was never the eligible set."""
        with self.assertRaises(UnusableCandidateSet) as caught:
            build(fake_set(20, total=48))

        self.assertEqual(caught.exception.reason, TOO_MANY_CANDIDATES)

    def test_a_snapshot_survives_a_round_trip_through_the_row(self):
        snapshot = build(fake_set(3))

        restored = CandidateSnapshot.from_json(snapshot.as_json())

        self.assertTrue(snapshot.matches(restored))

    def test_a_renamed_candidate_no_longer_matches(self):
        """The model chose on the labels it was shown, so a changed label changed the evidence."""
        snapshot = build(fake_set(2))
        renamed = CandidateSnapshot(
            entries=(
                snapshot.entries[0],
                CandidateSnapshotEntry(
                    candidate_id=snapshot.entries[1].candidate_id,
                    object_type=snapshot.entries[1].object_type,
                    object_id=snapshot.entries[1].object_id,
                    display_name="Uplink to core",
                ),
            ),
            total=2,
        )

        self.assertFalse(snapshot.matches(renamed))

    def test_a_changed_membership_no_longer_matches(self):
        self.assertFalse(build(fake_set(2)).matches(build(fake_set(3))))


class ProposalTaskRegistryTest(SimpleTestCase):
    """The registry is closed: a task type exists because the plugin declared it."""

    def test_the_termination_task_is_registered(self):
        self.assertIsInstance(proposal_task(SELECT_TERMINATION_TASK), SelectTerminationTask)

    def test_an_unregistered_task_type_is_refused(self):
        with self.assertRaises(UnknownProposalTask):
            proposal_task("select_contact")


class SelectTerminationTaskTest(TestCase):
    """The one task type this delivery implements, against real NetBox objects."""

    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_superuser("proposal-task", "task@example.com", "testpass")
        cls.profile = ImportProfile.objects.create(
            name="Task Profile", source_adapter="trace_workbook", adapter_config={}
        )
        cls.site, _manufacturer, device_type, role = make_dcim_objects("Task")
        cls.device = Device.objects.create(name="TASK-SWITCH", site=cls.site, device_type=device_type, role=role)
        cls.interfaces = [
            Interface.objects.create(device=cls.device, name=f"Ethernet 1/{index}") for index in (1, 2, 3)
        ]
        cls.field_key = termination_field_key(
            device="TASK-SWITCH", cards="", port="Ethernet 1/1", kind="interface", role=TERMINATION_ROLE
        )
        cls.task = SelectTerminationTask()

    def reader(self):
        """Return a permission-scoped reader bound to the import target."""
        return NetBoxReader.for_actor(self.actor).for_target(site=self.site)

    def test_the_snapshot_offers_every_eligible_termination_on_the_device(self):
        snapshot = self.task.current(
            profile=self.profile, field_key=self.field_key, netbox_reader=self.reader(), limit=64
        )

        self.assertEqual(snapshot.total, 3)
        self.assertEqual([entry.display_name for entry in snapshot.entries], [str(port) for port in self.interfaces])
        self.assertEqual({entry.object_type for entry in snapshot.entries}, {"dcim.interface"})

    def test_a_device_with_more_ports_than_the_bound_is_refused(self):
        with self.assertRaises(UnusableCandidateSet) as caught:
            self.task.current(profile=self.profile, field_key=self.field_key, netbox_reader=self.reader(), limit=2)

        self.assertEqual(caught.exception.reason, TOO_MANY_CANDIDATES)

    def test_the_resolved_device_is_the_one_the_key_names(self):
        resolved = self.task.resolved_device(field_key=self.field_key, netbox_reader=self.reader())

        self.assertEqual(resolved, self.device)

    def test_a_key_naming_no_visible_device_resolves_to_nothing(self):
        absent = termination_field_key(
            device="NO-SUCH-DEVICE", cards="", port="Ethernet 1/1", kind="interface", role=TERMINATION_ROLE
        )

        self.assertIsNone(self.task.resolved_device(field_key=absent, netbox_reader=self.reader()))

    def test_the_mapped_peer_role_is_not_proposed_for(self):
        """Section 7.1 requests proposals for the termination role in this delivery."""
        peer_key = termination_field_key(
            device="TASK-SWITCH", cards="", port="Ethernet 1/1", kind="interface", role=MAPPED_PEER_ROLE
        )

        with self.assertRaises(UnsupportedProposalRole):
            self.task.current(profile=self.profile, field_key=peer_key, netbox_reader=self.reader(), limit=64)

    def test_writing_a_resolution_returns_only_its_id(self):
        snapshot = self.task.current(
            profile=self.profile, field_key=self.field_key, netbox_reader=self.reader(), limit=64
        )

        receipt = self.task.write_resolution(
            actor=self.actor, profile=self.profile, field_key=self.field_key, entry=snapshot.entries[1]
        )

        written = TerminationResolution.objects.get(pk=receipt.written_resolution_id)
        self.assertEqual(written.selected_object_id, self.interfaces[1].pk)
        self.assertEqual(written.field_key, self.field_key)

    def test_the_last_explicit_action_wins_for_one_key(self):
        """Acceptance upserts, so a second decision replaces the row instead of duplicating it."""
        snapshot = self.task.current(
            profile=self.profile, field_key=self.field_key, netbox_reader=self.reader(), limit=64
        )
        first = self.task.write_resolution(
            actor=self.actor, profile=self.profile, field_key=self.field_key, entry=snapshot.entries[0]
        )

        second = self.task.write_resolution(
            actor=self.actor, profile=self.profile, field_key=self.field_key, entry=snapshot.entries[2]
        )

        self.assertEqual(first.written_resolution_id, second.written_resolution_id)
        self.assertEqual(TerminationResolution.objects.filter(profile=self.profile).count(), 1)
        written = TerminationResolution.objects.get(pk=second.written_resolution_id)
        self.assertEqual(written.selected_object_id, self.interfaces[2].pk)

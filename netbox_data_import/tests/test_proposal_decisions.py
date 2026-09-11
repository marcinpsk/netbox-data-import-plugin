# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Read-time freshness and explicit operator decisions against real inventory."""

from threading import Event

from core.models import ObjectType
from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings

from netbox_data_import.field_keys import SELECT_TERMINATION_TASK, TERMINATION_ROLE, termination_field_key
from netbox_data_import.models import (
    ImportProfile,
    ProposalDecision,
    ProposalFailureReason,
    ProposalOutcome,
    ProposalStatus,
    TerminationResolution,
    locked_profile_policy,
)
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.object_permissions import ObjectPermissionDenied
from netbox_data_import.proposal_decisions import accept_proposal, proposal_staleness, reject_proposal
from netbox_data_import.proposal_tasks import proposal_task
from netbox_data_import.resolution_proposals import (
    cancel_proposal,
    claim_proposal,
    complete_proposal,
    fail_proposal,
    request_proposal,
)
from netbox_data_import.tests.helpers import (
    make_dcim_objects,
    run_on_separate_connection,
    user_with_object_permission,
    wait_until_a_lock_is_blocked,
)

User = get_user_model()


class DecisionInventory:
    def setUp(self):
        super().setUp()
        self.operator = User.objects.create_superuser("decision-operator", "operator@example.com", "testpass")
        self.profile = ImportProfile.objects.create(
            name="Decision Profile", source_adapter="trace_workbook", adapter_config={}
        )
        self.site, _, self.device_type, self.role = make_dcim_objects("Decision")
        self.device = Device.objects.create(
            name="Decision Device", site=self.site, device_type=self.device_type, role=self.role
        )
        self.ports = [Interface.objects.create(device=self.device, name=f"Ethernet 1/{i}") for i in (1, 2)]
        self.field_key = termination_field_key(
            device=self.device.name, cards="", port="Eth1", kind="interface", role=TERMINATION_ROLE
        )
        self.task = proposal_task(SELECT_TERMINATION_TASK)

    def reader(self, operator=None):
        return NetBoxReader.for_actor(operator or self.operator).for_target(site=self.site)

    def proposal(self, selection=0, outcome=ProposalOutcome.CANDIDATE, status=ProposalStatus.COMPLETED):
        snapshot = self.task.current(
            profile=self.profile, field_key=self.field_key, netbox_reader=self.reader(), limit=64
        )
        proposal = request_proposal(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
            source_evidence={"port": "Eth1"},
            resolved_device_type=ObjectType.objects.get_for_model(Device),
            resolved_device_id=self.device.pk,
            prompt_version=1,
            response_schema_version=1,
            candidate_snapshot=snapshot.as_json(),
            requested_by=self.operator,
        )
        if status == ProposalStatus.COMPLETED:
            entry = snapshot.entries[selection]
            claim_proposal(proposal.pk)
            complete_proposal(
                proposal.pk,
                outcome=outcome,
                explanation="The label matches.",
                selected_candidate_id=entry.candidate_id,
                selected_object_type=ObjectType.objects.get_for_model(Interface),
                selected_object_id=entry.object_id,
            )
        elif status == ProposalStatus.FAILED:
            fail_proposal(proposal.pk, reason=ProposalFailureReason.TIMEOUT)
        elif status == ProposalStatus.CANCELLED:
            cancel_proposal(proposal.pk)
        elif status == ProposalStatus.RUNNING:
            claim_proposal(proposal.pk)
        proposal.refresh_from_db()
        return proposal

    def accept(self, proposal, operator=None):
        actor = operator or self.operator
        return accept_proposal(proposal.pk, operator=actor, netbox_reader=self.reader(actor))

    def stale(self, proposal):
        return proposal_staleness(proposal, netbox_reader=self.reader())

    def assert_unwritten(self, proposal):
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "")
        self.assertIsNone(proposal.decided_at)
        self.assertIsNone(proposal.written_resolution_id)


class ProposalFreshnessTest(DecisionInventory, TestCase):
    def test_unchanged_inventory_is_fresh(self):
        proposal = self.proposal()
        state = self.stale(proposal)
        self.assertFalse(state.is_stale)
        self.assertFalse(state.resolved_device_changed)
        self.assertFalse(state.candidates_changed)

    def test_a_changed_resolved_device_is_stale_even_with_the_same_candidates(self):
        proposal = self.proposal()
        old_name = self.device.name
        self.device.name = "Former Device"
        self.device.save()
        replacement = Device.objects.create(name=old_name, site=self.site, device_type=self.device_type, role=self.role)
        Interface.objects.filter(device=self.device).update(device=replacement)
        state = self.stale(proposal)
        self.assertTrue(state.is_stale)
        self.assertTrue(state.resolved_device_changed)
        self.assertFalse(state.candidates_changed)
        self.assertFalse(self.accept(proposal))
        self.assert_unwritten(proposal)

    def assert_candidates_stale(self, proposal):
        state = self.stale(proposal)
        self.assertTrue(state.is_stale)
        self.assertFalse(state.resolved_device_changed)
        self.assertTrue(state.candidates_changed)
        self.assertFalse(self.accept(proposal))
        self.assert_unwritten(proposal)

    def test_an_added_candidate_is_stale(self):
        proposal = self.proposal()
        Interface.objects.create(device=self.device, name="Ethernet 1/3")
        self.assert_candidates_stale(proposal)

    def test_a_removed_candidate_is_stale(self):
        proposal = self.proposal()
        self.ports[0].delete()
        self.assert_candidates_stale(proposal)

    def test_a_renamed_candidate_is_stale(self):
        proposal = self.proposal()
        self.ports[0].name = "Ethernet 1/1 renamed"
        self.ports[0].save()
        self.assert_candidates_stale(proposal)

    def test_an_empty_candidate_set_is_stale(self):
        proposal = self.proposal()
        Interface.objects.filter(device=self.device).delete()
        self.assert_candidates_stale(proposal)

    def test_a_candidate_set_above_the_bound_is_stale(self):
        proposal = self.proposal()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 1}}):
            self.assert_candidates_stale(proposal)

    def test_staleness_is_recomputed_without_changing_the_proposal(self):
        proposal = self.proposal()
        before = proposal.last_updated
        extra = Interface.objects.create(device=self.device, name="Ethernet 1/3")
        self.assertTrue(self.stale(proposal).is_stale)
        extra.delete()
        self.assertFalse(self.stale(proposal).is_stale)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertEqual(proposal.last_updated, before)


class ProposalAcceptanceTest(DecisionInventory, TestCase):
    def test_fresh_acceptance_writes_and_links_the_resolution_for_another_operator(self):
        proposal = self.proposal()
        actor = User.objects.create_superuser("other-decider", "other@example.com", "testpass")
        self.assertTrue(self.accept(proposal, actor))
        row = TerminationResolution.objects.get(profile=self.profile)
        proposal.refresh_from_db()
        self.assertEqual((row.field_key, row.task_type), (self.field_key, SELECT_TERMINATION_TASK))
        self.assertEqual(row.selected_object_id, self.ports[0].pk)
        self.assertEqual(row.selected_object_type, ObjectType.objects.get_for_model(Interface))
        self.assertEqual(row.selected_display_name, str(self.ports[0]))
        self.assertEqual(proposal.written_resolution_id, row.pk)
        self.assertEqual(proposal.decision, ProposalDecision.ACCEPTED)
        self.assertEqual(proposal.decided_by, actor)
        self.assertIsNotNone(proposal.decided_at)
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)

    def test_no_match_cannot_be_accepted(self):
        proposal = self.proposal(outcome=ProposalOutcome.NO_MATCH)
        self.assertFalse(self.accept(proposal))
        self.assert_unwritten(proposal)

    def test_noncompleted_proposals_cannot_be_accepted(self):
        for status in (ProposalStatus.QUEUED, ProposalStatus.RUNNING, ProposalStatus.FAILED, ProposalStatus.CANCELLED):
            with self.subTest(status=status):
                proposal = self.proposal(status=status)
                self.assertFalse(self.accept(proposal))
                self.assert_unwritten(proposal)
                proposal.delete()

    def test_a_second_decision_is_refused_and_the_first_stands(self):
        proposal = self.proposal()
        self.assertTrue(self.accept(proposal))
        proposal.refresh_from_db()
        first = (proposal.decision, proposal.decided_by_id, proposal.decided_at, proposal.written_resolution_id)
        self.assertFalse(reject_proposal(proposal.pk, operator=self.operator))
        self.assertFalse(self.accept(proposal))
        proposal.refresh_from_db()
        self.assertEqual(
            (proposal.decision, proposal.decided_by_id, proposal.decided_at, proposal.written_resolution_id), first
        )

    def test_rejection_records_a_decision_and_allows_a_new_request(self):
        proposal = self.proposal(outcome=ProposalOutcome.NO_MATCH)
        actor = User.objects.create_superuser("reject-decider", "reject@example.com", "testpass")
        self.assertTrue(reject_proposal(proposal.pk, operator=actor))
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, ProposalDecision.REJECTED)
        self.assertEqual(proposal.decided_by, actor)
        self.assertIsNotNone(proposal.decided_at)
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertIsNone(proposal.written_resolution_id)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())
        self.assertEqual(self.proposal(status=ProposalStatus.QUEUED).status, ProposalStatus.QUEUED)

    def test_an_operator_without_resolution_permission_cannot_decide(self):
        proposal = self.proposal()
        actor = user_with_object_permission("no-resolution", [(Device, ["view"], {}), (Interface, ["view"], {})])
        with self.assertRaises(ObjectPermissionDenied):
            self.accept(proposal, actor)
        with self.assertRaises(ObjectPermissionDenied):
            reject_proposal(proposal.pk, operator=actor)
        self.assert_unwritten(proposal)

    def test_scoped_add_permission_cannot_write_outside_its_profile(self):
        proposal = self.proposal()
        actor = user_with_object_permission(
            "scoped-decider",
            [
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile_id": self.profile.pk + 1}),
            ],
        )
        with self.assertRaises(ObjectPermissionDenied):
            self.accept(proposal, actor)
        self.assert_unwritten(proposal)

    def test_rejection_takes_the_workspace_permission_not_the_resolution_one(self):
        """Rejection writes no Row Resolution, so it is scoped by the profile (specification 7.6)."""
        proposal = self.proposal()
        actor = user_with_object_permission(
            "workspace-rejecter",
            [
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (ImportProfile, ["change"], {"pk": self.profile.pk}),
                (TerminationResolution, ["add"], {"profile_id": self.profile.pk + 1}),
            ],
        )

        self.assertTrue(reject_proposal(proposal.pk, operator=actor))

        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, ProposalDecision.REJECTED)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_rejection_is_refused_outside_the_operators_profile_scope(self):
        proposal = self.proposal()
        actor = user_with_object_permission(
            "other-profile-rejecter",
            [
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (ImportProfile, ["change"], {"pk": self.profile.pk + 1}),
            ],
        )

        with self.assertRaises(ObjectPermissionDenied):
            reject_proposal(proposal.pk, operator=actor)

        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "")

    def test_scoped_add_permission_can_write_inside_its_profile(self):
        proposal = self.proposal()
        actor = user_with_object_permission(
            "allowed-decider",
            [
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile_id": self.profile.pk}),
            ],
        )
        self.assertTrue(self.accept(proposal, actor))
        self.assertEqual(TerminationResolution.objects.get(profile=self.profile).selected_object_id, self.ports[0].pk)

    def test_acceptance_cannot_use_another_operators_reader(self):
        proposal = self.proposal()
        actor = User.objects.create_user("other-reader", password="testpass")
        with self.assertRaises(ValueError):
            accept_proposal(proposal.pk, operator=actor, netbox_reader=self.reader())
        self.assert_unwritten(proposal)

    def test_a_rejected_candidate_cannot_be_accepted(self):
        proposal = self.proposal()
        self.assertTrue(reject_proposal(proposal.pk, operator=self.operator))
        self.assertFalse(self.accept(proposal))
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, ProposalDecision.REJECTED)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_updating_a_resolution_requires_change_permission(self):
        first = self.proposal()
        self.assertTrue(self.accept(first))
        second = self.proposal(selection=1)
        actor = user_with_object_permission(
            "add-only-decider",
            [
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {}),
            ],
        )
        with self.assertRaises(ObjectPermissionDenied):
            self.accept(second, actor)
        self.assertEqual(TerminationResolution.objects.get(profile=self.profile).selected_object_id, self.ports[0].pk)
        second.refresh_from_db()
        self.assertEqual(second.decision, "")

    def test_the_last_explicit_action_wins_across_proposals(self):
        first = self.proposal()
        second = self.proposal(selection=1)
        self.assertTrue(self.accept(first))
        self.assertTrue(self.accept(second))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(TerminationResolution.objects.filter(profile=self.profile).count(), 1)
        self.assertEqual(first.written_resolution_id, second.written_resolution_id)
        self.assertEqual(TerminationResolution.objects.get(profile=self.profile).selected_object_id, self.ports[1].pk)

    def test_a_profile_that_no_longer_supports_resolutions_refuses_the_write(self):
        proposal = self.proposal()
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="flat_workbook")
        with self.assertRaises(ValidationError):
            self.accept(proposal)
        self.assert_unwritten(proposal)


class ProposalDecisionConcurrencyTest(DecisionInventory, TransactionTestCase):
    def test_concurrent_acceptances_have_one_winner_and_the_loser_never_writes(self):
        proposal = self.proposal()
        holder_ready = Event()
        results = []
        loser_writes = []

        def record_writes(execute, sql, params, many, context):
            if sql.startswith(("INSERT", "UPDATE")) and '"netbox_data_import_terminationresolution"' in sql:
                loser_writes.append(sql)
            return execute(sql, params, many, context)

        def accept_under_the_lock():
            with locked_profile_policy(self.profile.pk):
                holder_ready.set()
                wait_until_a_lock_is_blocked(self)
                results.append(self.accept(proposal))

        with run_on_separate_connection(accept_under_the_lock):
            self.assertTrue(holder_ready.wait(timeout=10))
            with connection.execute_wrapper(record_writes):
                results.append(self.accept(proposal))

        self.assertCountEqual(results, [True, False])
        self.assertEqual(loser_writes, [])
        self.assertEqual(TerminationResolution.objects.filter(profile=self.profile).count(), 1)
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, ProposalDecision.ACCEPTED)
        self.assertEqual(proposal.written_resolution_id, TerminationResolution.objects.get(profile=self.profile).pk)

    def test_inventory_changed_while_acceptance_waits_is_refused_without_a_write(self):
        proposal = self.proposal()
        holder_ready = Event()

        def rename_under_the_lock():
            with locked_profile_policy(self.profile.pk):
                holder_ready.set()
                wait_until_a_lock_is_blocked(self)
                Interface.objects.filter(pk=self.ports[0].pk).update(name="Changed during acceptance")

        with run_on_separate_connection(rename_under_the_lock):
            self.assertTrue(holder_ready.wait(timeout=10))
            self.assertFalse(self.accept(proposal))

        self.assert_unwritten(proposal)

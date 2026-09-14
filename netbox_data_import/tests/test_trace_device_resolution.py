# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Profile-owned Device resolution for Source Traces."""

from io import BytesIO

from dcim.models import Device, Interface, Location, Rack
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from netbox_data_import.models import ImportProfile, SourceDocument, TraceDeviceResolution
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.object_permissions import ObjectPermissionDenied
from netbox_data_import.plan import Disposition
from netbox_data_import.profile_yaml import serialize_profile
from netbox_data_import.review_workspace import save_trace_device_resolution_and_replan
from netbox_data_import.trace_device_resolution import (
    DeviceEvidence,
    UNRESOLVED,
    eligible_trace_devices,
    resolve_trace_devices,
)
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path
from netbox_data_import.tests.helpers import trace_termination, trace_workbook_bytes, user_with_object_permission


class TraceDeviceResolutionModelTest(CableTopologyMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_a_trace_profile_accepts_one_canonical_source_device_key(self):
        resolution = TraceDeviceResolution(
            profile=self.profile,
            source_device_key="source device",
            selected_device_id=self.device_a.pk,
            selected_display_name=str(self.device_a),
        )

        resolution.full_clean()
        resolution.save()

        self.assertEqual(resolution.source_device_key, "source device")
        self.assertEqual(len(resolution.source_device_key_digest), 64)

    def test_a_noncanonical_source_device_key_is_rejected(self):
        resolution = TraceDeviceResolution(
            profile=self.profile,
            source_device_key=" Source  Device ",
            selected_device_id=self.device_a.pk,
            selected_display_name=str(self.device_a),
        )

        with self.assertRaisesMessage(ValidationError, "canonical source Device key"):
            resolution.full_clean()

    def test_a_flat_profile_cannot_own_a_trace_device_resolution(self):
        flat_profile = ImportProfile.objects.create(name="Flat Device Resolution", adapter_config={})
        resolution = TraceDeviceResolution(
            profile=flat_profile,
            source_device_key="source device",
            selected_device_id=self.device_a.pk,
            selected_display_name=str(self.device_a),
        )

        with self.assertRaises(ValidationError):
            resolution.full_clean()

    def test_the_mapping_changes_planning_but_stays_out_of_portable_profile_yaml(self):
        before = self.profile.planning_fingerprint

        TraceDeviceResolution.objects.create(
            profile=self.profile,
            source_device_key="source device",
            selected_device_id=self.device_a.pk,
            selected_display_name=str(self.device_a),
        )

        self.assertNotEqual(self.profile.planning_fingerprint, before)
        self.assertNotIn("trace_device_resolutions", serialize_profile(self.profile))


class TraceDeviceResolutionPlanningTest(CableTopologyMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_topology()
        cls.alias_port = Interface.objects.create(
            device=cls.device_a,
            name="eth9",
            type="1000base-t",
        )

    def save_alias(self, source_label="source alias"):
        return TraceDeviceResolution.objects.create(
            profile=self.profile,
            source_device_key=source_label,
            selected_device_id=self.device_a.pk,
            selected_display_name=str(self.device_a),
        )

    def alias_path(self, source_label="Source Alias"):
        return direct_path(
            from_end=trace_termination(source_label, "", "eth9", "Port"),
            to_end=trace_termination("DEV-B", "", "eth1", "Port"),
        )

    def test_a_saved_device_choice_resolves_a_different_port_in_a_later_file(self):
        self.save_alias()

        unit = self.unit(self.alias_path("  SOURCE   ALIAS  "))

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertNotIn("trace.device_unresolved", self.codes(unit))

    def test_a_stale_saved_choice_does_not_fall_back_to_a_new_exact_name_match(self):
        self.save_alias()
        self.device_a.delete()
        replacement = self.make_device("Source Alias")
        Interface.objects.create(device=replacement, name="eth9", type="1000base-t")

        unit = self.unit(self.alias_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("trace.device_resolution_stale", self.codes(unit))

    def test_an_unresolved_device_is_one_plan_question_for_all_of_its_ports(self):
        second_port = Interface.objects.create(device=self.device_b, name="eth2", type="1000base-t")
        first = self.alias_path()
        second = direct_path(
            from_end=trace_termination("SOURCE ALIAS", "", "eth8", "Port"),
            to_end=trace_termination("DEV-B", "", second_port.name, "Port"),
        )

        plan = self.plan(first, second)

        questions = [question for unit in plan.units for question in unit.display["trace"]["devices"]]
        alias_questions = [question for question in questions if question["key"] == "source alias"]
        self.assertEqual(len(alias_questions), 2)
        self.assertTrue(all(question["state"] == UNRESOLVED for question in alias_questions))


class TraceDeviceCandidateTest(CableTopologyMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_topology()
        cls.location = Location.objects.create(site=cls.site, name="Trace Room", slug="trace-room")
        cls.rack = Rack.objects.create(site=cls.site, location=cls.location, name="Trace Rack", u_height=42)
        cls.hinted = Device.objects.create(
            name="Candidate Z",
            site=cls.site,
            location=cls.location,
            rack=cls.rack,
            position=12,
            face="front",
            device_type=cls.device_type,
            role=cls.role,
        )
        cls.other = cls.make_device("Candidate A")
        cls.evidence = DeviceEvidence(
            key="source alias",
            labels=("Source Alias",),
            locations=("Trace Room",),
            racks=("Trace Rack",),
            u_positions=("12",),
        )

    def reader(self, actor=None):
        reader = NetBoxReader.for_actor(actor) if actor is not None else NetBoxReader.unrestricted()
        return reader.for_target(site=self.site)

    def test_placement_evidence_ranks_and_explains_but_does_not_resolve(self):
        page = eligible_trace_devices(reader=self.reader(), evidence=self.evidence, limit=20)
        outcome = resolve_trace_devices(
            profile=self.profile,
            reader=self.reader(),
            evidence={self.evidence.key: self.evidence},
        )[self.evidence.key]

        self.assertEqual(page.candidates[0].device, self.hinted)
        self.assertEqual(page.candidates[0].matched_hints, ("location", "rack", "U position"))
        self.assertEqual(outcome.state, UNRESOLVED)
        self.assertIsNone(outcome.device)

    def test_hidden_rack_and_location_do_not_affect_candidate_explanations(self):
        actor = user_with_object_permission(
            "trace-placement-scope",
            [
                (Device, ("view",), {"site_id": self.site.pk}),
                (Rack, ("view",), {"name": "Another Rack"}),
                (Location, ("view",), {"name": "Another Location"}),
            ],
        )

        page = eligible_trace_devices(reader=self.reader(actor), evidence=self.evidence, limit=20)
        candidate = next(item for item in page.candidates if item.device.pk == self.hinted.pk)

        self.assertEqual(candidate.matched_hints, ("U position",))
        self.assertNotIn("rack", candidate.conflicting_hints)
        self.assertNotIn("location", candidate.conflicting_hints)


class TraceDeviceResolutionWorkspaceTest(CableTopologyMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_topology()
        cls.alias_port = Interface.objects.create(device=cls.device_a, name="eth9", type="1000base-t")

    def start_alias_preview(self, source_label="Source Alias", port_name=None):
        self.client.force_login(self.actor)
        block = direct_path(
            from_end=trace_termination(source_label, "", port_name or self.alias_port.name, "Port"),
            to_end=trace_termination("DEV-B", "", "eth1", "Port"),
        )
        upload = BytesIO(trace_workbook_bytes(path_blocks=(block,)))
        upload.name = "trace-alias.xlsx"
        return self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )

    def test_an_unresolved_source_device_offers_the_device_picker(self):
        response = self.start_alias_preview()

        self.assertContains(response, "Choose Device")
        self.assertContains(response, "Source Alias")
        self.assertContains(response, reverse("plugins:netbox_data_import:trace_device_candidates"))

    def test_saving_a_device_choice_replans_and_persists_the_mapping(self):
        response = self.start_alias_preview()
        revision = response.context["preview_revision"]

        candidates = self.client.get(
            reverse("plugins:netbox_data_import:trace_device_candidates"),
            {"device_key": "source alias", "search": "DEV-A", "preview_revision": revision},
        )
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual([item["id"] for item in candidates.json()["candidates"]], [self.device_a.pk])

        saved = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_device"),
            {
                "device_key": "source alias",
                "device_id": self.device_a.pk,
                "search": "DEV-A",
                "preview_revision": revision,
            },
            follow=True,
        )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            TraceDeviceResolution.objects.get(profile=self.profile).selected_device_id,
            self.device_a.pk,
        )
        self.assertContains(saved, "manually resolved")

    def test_a_later_file_reuses_the_choice_for_another_port_and_label_spacing(self):
        response = self.start_alias_preview()
        self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_device"),
            {
                "device_key": "source alias",
                "device_id": self.device_a.pk,
                "search": "DEV-A",
                "preview_revision": response.context["preview_revision"],
            },
        )
        Interface.objects.create(device=self.device_a, name="eth10", type="1000base-t")

        later = self.start_alias_preview("  SOURCE   ALIAS  ", "eth10")

        self.assertContains(later, "manually resolved")
        selected = next(device for device in later.context["selected_trace"].devices if device["key"] == "source alias")
        self.assertEqual(selected["selected"], str(self.device_a))

    def test_the_candidate_endpoint_rejects_a_device_key_the_plan_did_not_author(self):
        response = self.start_alias_preview()

        candidates = self.client.get(
            reverse("plugins:netbox_data_import:trace_device_candidates"),
            {
                "device_key": "invented device",
                "preview_revision": response.context["preview_revision"],
            },
        )

        self.assertEqual(candidates.status_code, 400)
        self.assertIn("asked no question", candidates.json()["error"])

    def test_the_candidate_endpoint_rejects_an_old_preview_revision(self):
        self.start_alias_preview()

        candidates = self.client.get(
            reverse("plugins:netbox_data_import:trace_device_candidates"),
            {"device_key": "source alias", "preview_revision": "old-preview"},
        )

        self.assertEqual(candidates.status_code, 409)

    def test_a_stale_selection_does_not_disclose_its_saved_display_snapshot(self):
        TraceDeviceResolution.objects.create(
            profile=self.profile,
            source_device_key="source alias",
            selected_device_id=self.device_a.pk,
            selected_display_name="Hidden Device Snapshot",
        )
        self.device_a.delete()
        replacement = self.make_device("Source Alias")
        self.alias_port = Interface.objects.create(device=replacement, name="eth9", type="1000base-t")

        response = self.start_alias_preview()

        self.assertContains(response, "stale")
        self.assertNotContains(response, "Hidden Device Snapshot")


class TraceDeviceResolutionPermissionTest(CableTopologyMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_a_constrained_add_denial_rolls_back_the_device_resolution(self):
        from dcim.models import Site

        actor = user_with_object_permission(
            "trace-device-write-denied",
            [
                (Site, ("view",), {"pk": self.site.pk}),
                (Device, ("view",), {"pk": self.device_a.pk}),
                (TraceDeviceResolution, ("add",), {"source_device_key": "another device"}),
            ],
        )
        block = direct_path(
            from_end=trace_termination("Source Alias", "", "eth0", "Port"),
            to_end=trace_termination("DEV-B", "", "eth1", "Port"),
        )
        document = SourceDocument.store(
            profile=self.profile,
            content=trace_workbook_bytes(path_blocks=(block,)),
            uploaded_by=actor,
        )
        evidence = DeviceEvidence(
            key="source alias",
            labels=("Source Alias",),
            locations=(),
            racks=(),
            u_positions=(),
        )

        with self.assertRaises(ObjectPermissionDenied):
            save_trace_device_resolution_and_replan(
                profile=self.profile,
                source_document=document,
                actor=actor,
                planning_context={"site_id": self.site.pk, "location_id": None, "tenant_id": None},
                evidence=evidence,
                selected_device_id=self.device_a.pk,
                search="DEV-A",
                limit=20,
            )

        self.assertFalse(TraceDeviceResolution.objects.filter(profile=self.profile).exists())

    def test_a_constrained_change_denial_keeps_the_previous_device_resolution(self):
        from dcim.models import Site

        existing = TraceDeviceResolution.objects.create(
            profile=self.profile,
            source_device_key="source alias",
            selected_device_id=self.device_a.pk,
            selected_display_name=str(self.device_a),
        )
        actor = user_with_object_permission(
            "trace-device-change-denied",
            [
                (Site, ("view",), {"pk": self.site.pk}),
                (Device, ("view",), {"pk__in": (self.device_a.pk, self.device_b.pk)}),
                (
                    TraceDeviceResolution,
                    ("change",),
                    {"selected_device_id": self.device_a.pk},
                ),
            ],
        )
        block = direct_path(
            from_end=trace_termination("Source Alias", "", "eth0", "Port"),
            to_end=trace_termination("DEV-B", "", "eth1", "Port"),
        )
        document = SourceDocument.store(
            profile=self.profile,
            content=trace_workbook_bytes(path_blocks=(block,)),
            uploaded_by=actor,
        )
        evidence = DeviceEvidence(
            key="source alias",
            labels=("Source Alias",),
            locations=(),
            racks=(),
            u_positions=(),
        )

        with self.assertRaises(ObjectPermissionDenied):
            save_trace_device_resolution_and_replan(
                profile=self.profile,
                source_document=document,
                actor=actor,
                planning_context={"site_id": self.site.pk, "location_id": None, "tenant_id": None},
                evidence=evidence,
                selected_device_id=self.device_b.pk,
                search="DEV-B",
                limit=20,
            )

        existing.refresh_from_db()
        self.assertEqual(existing.selected_device_id, self.device_a.pk)

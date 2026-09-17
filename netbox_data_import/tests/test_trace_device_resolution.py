# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Profile-owned Device resolution for Source Traces."""

import re
from io import BytesIO

from dcim.models import Device, Interface, Location, Rack
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from netbox_data_import.cable_target import ELIGIBLE_TERMINATION_LIMIT
from netbox_data_import.field_keys import SELECT_TERMINATION_TASK, termination_field_key
from netbox_data_import.models import ImportProfile, SourceDocument, TerminationResolution, TraceDeviceResolution
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.object_permissions import ObjectPermissionDenied, clear_user_permission_caches
from netbox_data_import.plan import Disposition
from netbox_data_import.preview_row_actions import PREVIEW_PLAN_SESSION_KEY
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


class DeviceEvidenceSerializationTest(TestCase):
    evidence = {
        "key": "source device",
        "labels": ["Source Device"],
        "locations": [],
        "racks": [],
        "u_positions": [],
    }

    def test_serialized_evidence_rejects_a_non_mapping(self):
        with self.assertRaisesMessage(TypeError, "Device evidence must be an object"):
            DeviceEvidence.from_dict(["source device"])

    def test_serialized_evidence_rejects_a_non_string_key(self):
        with self.assertRaisesMessage(TypeError, "Device evidence key must be a string"):
            DeviceEvidence.from_dict({**self.evidence, "key": 17})

    def test_serialized_evidence_requires_every_field(self):
        damaged = {key: value for key, value in self.evidence.items() if key != "locations"}

        with self.assertRaisesMessage(ValueError, "Device evidence is missing fields: locations"):
            DeviceEvidence.from_dict(damaged)

    def test_serialized_evidence_rejects_unknown_fields(self):
        with self.assertRaisesMessage(ValueError, "Device evidence has unknown fields: typo"):
            DeviceEvidence.from_dict({**self.evidence, "typo": []})

    def test_serialized_evidence_rejects_invalid_fact_collections(self):
        cases = (
            ("labels", "Source Device"),
            ("locations", [17]),
            ("racks", {"Rack A"}),
            ("u_positions", None),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesMessage(TypeError, f"Device evidence {field} must be a list or tuple of strings"):
                    DeviceEvidence.from_dict({**self.evidence, field: value})


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

    def test_a_canonical_name_match_collapses_device_name_whitespace(self):
        self.device_a.name = "Source  Alias"
        self.device_a.save(update_fields=("name",))

        unit = self.unit(self.alias_path("Source Alias"))

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertNotIn("trace.device_unresolved", self.codes(unit))

    def test_a_canonical_name_match_uses_the_database_case_rules(self):
        self.device_a.name = "İdentity  Name"
        self.device_a.save(update_fields=("name",))

        unit = self.unit(self.alias_path("İdentity Name"))

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

    def test_a_stale_saved_choice_reports_the_new_exact_name_match_count(self):
        """The operator re-choosing needs to see that an exact-name Device now exists."""
        self.save_alias()
        self.device_a.delete()
        replacement = self.make_device("Source Alias")
        Interface.objects.create(device=replacement, name="eth9", type="1000base-t")

        unit = self.unit(self.alias_path())

        stale = next(item for item in unit.diagnostics if item.code == "trace.device_resolution_stale")
        self.assertEqual(stale.display["matches"], 1)

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

    def test_canonical_device_name_matching_has_priority_in_candidate_ranking(self):
        exact = self.make_device("Source  Alias")

        page = eligible_trace_devices(reader=self.reader(), evidence=self.evidence, limit=20)

        self.assertEqual(page.candidates[0].device, exact)
        self.assertIn("name", page.candidates[0].matched_hints)

    def test_canonical_placement_names_contribute_to_candidate_ranking(self):
        self.location.name = "Trace  Room"
        self.location.save(update_fields=("name",))
        self.rack.name = "Trace  Rack"
        self.rack.save(update_fields=("name",))
        evidence = DeviceEvidence(
            key="source alias",
            labels=("Source Alias",),
            locations=("Trace Room",),
            racks=("Trace Rack",),
            u_positions=(),
        )

        page = eligible_trace_devices(reader=self.reader(), evidence=evidence, limit=20)

        self.assertEqual(page.candidates[0].device, self.hinted)
        self.assertEqual(page.candidates[0].matched_hints, ("location", "rack"))

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

    def test_candidate_rows_lock_in_primary_key_order_but_keep_rank_order(self):
        locked_queries = []

        def capture_locked_query(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "FOR UPDATE" in sql:
                locked_queries.append(sql)
            return result

        with connection.execute_wrapper(capture_locked_query):
            page = eligible_trace_devices(reader=self.reader(), evidence=self.evidence, limit=20, lock_rows=True)

        self.assertEqual(page.candidates[0].device, self.hinted)
        self.assertEqual(len(locked_queries), 1)
        self.assertRegex(locked_queries[0], r'ORDER BY "dcim_device"\."id" ASC FOR UPDATE')

    def test_a_device_that_stops_matching_search_before_lock_is_not_returned(self):
        renamed = []

        def rename_after_ranked_ids_are_read(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if not renamed and 'FROM "dcim_device"' in sql and "LIMIT" in sql and "FOR UPDATE" not in sql:
                renamed.append(sql)
                Device.objects.filter(pk=self.hinted.pk).update(name="No Longer Eligible")
            return result

        with connection.execute_wrapper(rename_after_ranked_ids_are_read):
            page = eligible_trace_devices(
                reader=self.reader(),
                evidence=self.evidence,
                search="Candidate Z",
                limit=20,
                lock_rows=True,
            )

        self.assertTrue(renamed)
        self.assertEqual(page.candidates, ())


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

    def test_attention_questions_render_as_bordered_cards(self):
        response = self.start_alias_preview(port_name="absent-port")

        page = response.content.decode()
        devices = re.search(r"<section\b[^>]*data-trace-devices.*?</section>", page, re.DOTALL)
        terminations = re.search(r"<section\b[^>]*data-trace-terminations.*?</section>", page, re.DOTALL)
        self.assertIsNotNone(devices)
        self.assertIsNotNone(terminations)
        card_classes = r'<li class="[^"]*\bcard\b[^"]*\bndi-proposal-card\b[^"]*"'
        self.assertRegex(devices.group(), card_classes)
        self.assertRegex(terminations.group(), card_classes)

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

    def test_a_saved_device_choice_survives_a_later_load_and_re_read(self):
        def assert_saved_choice_is_rendered(result):
            devices = re.search(r"<section\b[^>]*data-trace-devices.*?</section>", result.content.decode(), re.DOTALL)
            self.assertIsNotNone(devices)
            manual = re.search(r"<div\b[^>]*data-trace-manual-devices.*?</table>", devices.group(), re.DOTALL)
            self.assertIsNotNone(manual)
            self.assertIn("Source Alias", manual.group())
            self.assertIn(str(self.device_a), manual.group())
            self.assertIn("manually resolved", manual.group())

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

        later = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        selected = next(device for device in later.context["selected_trace"].devices if device["key"] == "source alias")
        self.assertEqual(selected["state"], "manually resolved")
        assert_saved_choice_is_rendered(later)

        reread = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": later.context["preview_revision"]},
            follow=True,
        )
        selected = next(
            device for device in reread.context["selected_trace"].devices if device["key"] == "source alias"
        )
        self.assertEqual(selected["state"], "manually resolved")
        assert_saved_choice_is_rendered(reread)

    def test_the_proposed_topology_uses_an_unnamed_devices_display(self):
        unnamed = Device.objects.create(
            name=None,
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        Interface.objects.create(device=unnamed, name="eth7", type="1000base-t")
        TraceDeviceResolution.objects.create(
            profile=self.profile,
            source_device_key="source alias",
            selected_device_id=unnamed.pk,
            selected_display_name=str(unnamed),
        )

        response = self.start_alias_preview(port_name="eth7")

        page = response.content.decode()
        start = page.index("Proposed physical topology")
        proposed = page[start : page.index("</ol>", start)]
        self.assertIn(f"{unnamed} eth7", proposed)
        self.assertNotIn("None eth7", proposed)

    def test_a_saved_device_choice_stays_visible_outside_the_collapsed_disclosure(self):
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

        later = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        devices = re.search(r"<section\b[^>]*data-trace-devices.*?</section>", later.content.decode(), re.DOTALL)
        self.assertIsNotNone(devices)
        visible = devices.group().split("<details data-trace-resolved-devices", 1)[0]
        self.assertIn("Source Alias", visible)
        self.assertIn(str(self.device_a), visible)
        self.assertIn("manually resolved", visible)

    def test_an_attention_termination_names_its_resolved_device(self):
        response = self.start_alias_preview(port_name="absent-port")
        self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_device"),
            {
                "device_key": "source alias",
                "device_id": self.device_a.pk,
                "search": "DEV-A",
                "preview_revision": response.context["preview_revision"],
            },
        )

        later = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        terminations = re.search(
            r"<section\b[^>]*data-trace-terminations.*?</section>", later.content.decode(), re.DOTALL
        )
        self.assertIsNotNone(terminations)
        attention = terminations.group().split("<details data-trace-settled", 1)[0]
        self.assertIn("Source Alias absent-port", attention)
        self.assertIn(str(self.device_a), attention)

    def test_an_unoffered_device_choice_is_rejected_as_request_input(self):
        response = self.start_alias_preview()

        saved = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_device"),
            {
                "device_key": "source alias",
                "device_id": self.device_b.pk,
                "search": "DEV-A",
                "preview_revision": response.context["preview_revision"],
            },
            headers={"accept": "application/json"},
        )

        self.assertEqual(saved.status_code, 400)
        self.assertIn("not one of the eligible candidates", saved.json()["error"])
        self.assertFalse(TraceDeviceResolution.objects.filter(profile=self.profile).exists())

    def test_an_internal_device_resolution_value_error_is_not_request_input(self):
        response = self.start_alias_preview()
        failed_writes = []

        def fail_resolution_insert(execute, sql, params, many, context):
            if "netbox_data_import_tracedeviceresolution" in sql.lower() and sql.lstrip().upper().startswith("INSERT"):
                failed_writes.append(sql)
                raise ValueError("internal resolution write failure")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_resolution_insert):
            with self.assertRaisesMessage(ValueError, "internal resolution write failure"):
                self.client.post(
                    reverse("plugins:netbox_data_import:trace_resolve_device"),
                    {
                        "device_key": "source alias",
                        "device_id": self.device_a.pk,
                        "search": "DEV-A",
                        "preview_revision": response.context["preview_revision"],
                    },
                    headers={"accept": "application/json"},
                )

        self.assertTrue(failed_writes)
        self.assertFalse(TraceDeviceResolution.objects.filter(profile=self.profile).exists())

    def test_a_preview_lock_rolls_back_the_device_resolution(self):
        import uuid

        from core.choices import JobStatusChoices
        from core.models import Job

        from netbox_data_import.jobs import ImportJobRunner

        response = self.start_alias_preview()
        revision = response.context["preview_revision"]
        import_context = self.client.session["import_context"]
        retained = []
        decision_writes = []

        def retain_preview_after_initial_guard(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "netbox_data_import_tracedeviceresolution" in sql.lower() and sql.lstrip().upper().startswith(
                ("INSERT", "UPDATE")
            ):
                decision_writes.append(sql)
            if not retained and 'FROM "core_job"' in sql:
                retained.append(sql)
                Job.objects.create(
                    name=ImportJobRunner.name,
                    user=self.actor,
                    job_id=uuid.uuid4(),
                    status=JobStatusChoices.STATUS_PENDING,
                    data={
                        "job_type": ImportJobRunner.job_type,
                        "keeps_preview": True,
                        "profile_id": self.profile.pk,
                        "source_document_id": import_context["source_document_id"],
                    },
                )
            return result

        with connection.execute_wrapper(retain_preview_after_initial_guard):
            saved = self.client.post(
                reverse("plugins:netbox_data_import:trace_resolve_device"),
                {
                    "device_key": "source alias",
                    "device_id": self.device_a.pk,
                    "search": "DEV-A",
                    "preview_revision": revision,
                },
                headers={"accept": "application/json"},
            )

        self.assertTrue(retained)
        self.assertTrue(decision_writes)
        self.assertEqual(saved.status_code, 409)
        self.assertFalse(TraceDeviceResolution.objects.filter(profile=self.profile).exists())

    def test_the_candidate_endpoint_rejects_invalid_limits(self):
        response = self.start_alias_preview()

        for limit in ("not-an-integer", "-1", "0", str(ELIGIBLE_TERMINATION_LIMIT + 1)):
            with self.subTest(limit=limit):
                candidates = self.client.get(
                    reverse("plugins:netbox_data_import:trace_device_candidates"),
                    {
                        "device_key": "source alias",
                        "limit": limit,
                        "preview_revision": response.context["preview_revision"],
                    },
                )

                self.assertEqual(candidates.status_code, 400)
                # A bare 400 would still pass if a later regression refused the request elsewhere.
                self.assertEqual(
                    candidates.json()["error"],
                    f"Candidate limit must be an integer from 1 to {ELIGIBLE_TERMINATION_LIMIT}.",
                )

    def test_the_candidate_endpoint_rejects_malformed_device_evidence(self):
        response = self.start_alias_preview()
        session = self.client.session
        plan = session[PREVIEW_PLAN_SESSION_KEY]
        question = next(
            device
            for unit in plan["units"]
            for device in ((unit.get("display") or {}).get("trace") or {}).get("devices", ())
            if device.get("key") == "source alias"
        )
        question["labels"] = "Source Alias"
        session[PREVIEW_PLAN_SESSION_KEY] = plan
        session.save()

        candidates = self.client.get(
            reverse("plugins:netbox_data_import:trace_device_candidates"),
            {
                "device_key": "source alias",
                "preview_revision": response.context["preview_revision"],
            },
        )

        self.assertEqual(candidates.status_code, 400)
        self.assertEqual(candidates.json()["error"], "That Device cannot be resolved here.")

    def test_an_internal_candidate_type_error_is_not_request_input(self):
        response = self.start_alias_preview()
        failed_reads = []

        def fail_candidate_read(execute, sql, params, many, context):
            if 'FROM "dcim_device"' in sql:
                failed_reads.append(sql)
                raise TypeError("internal candidate read failure")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_candidate_read):
            with self.assertRaisesMessage(TypeError, "internal candidate read failure"):
                self.client.get(
                    reverse("plugins:netbox_data_import:trace_device_candidates"),
                    {
                        "device_key": "source alias",
                        "preview_revision": response.context["preview_revision"],
                    },
                )

        self.assertTrue(failed_reads)

    def test_the_summary_counts_only_saved_decisions_the_actor_can_view(self):
        from core.models import ObjectType
        from dcim.models import Site

        self.start_alias_preview()
        preview_session = {key: value for key, value in self.client.session.items() if key.startswith("import_")}
        object_type = ObjectType.objects.get_for_model(Interface)
        visible_field_key = termination_field_key(device="DEV-A", cards="", port="eth0", kind="interface")
        hidden_field_key = termination_field_key(device="DEV-B", cards="", port="eth1", kind="interface")
        for field_key, selected in ((visible_field_key, self.eth0), (hidden_field_key, self.eth1)):
            TerminationResolution.objects.create(
                profile=self.profile,
                task_type=SELECT_TERMINATION_TASK,
                field_key=field_key,
                selected_object_type=object_type,
                selected_object_id=selected.pk,
                selected_display_name=str(selected),
            )
        for key, selected in (("visible device", self.device_a), ("hidden device", self.device_b)):
            TraceDeviceResolution.objects.create(
                profile=self.profile,
                source_device_key=key,
                selected_device_id=selected.pk,
                selected_display_name=str(selected),
            )
        actor = user_with_object_permission(
            "trace-summary-scope",
            [
                (ImportProfile, ("view", "change"), {"pk": self.profile.pk}),
                (Site, ("view",), {"pk": self.site.pk}),
                (Device, ("view",), {"site_id": self.site.pk}),
                (Interface, ("view",), {}),
                (TerminationResolution, ("view",), {"field_key": visible_field_key}),
                (TraceDeviceResolution, ("view",), {"source_device_key": "visible device"}),
            ],
        )
        self.client.force_login(actor)
        session = self.client.session
        session.update(preview_session)
        session.save()

        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(workspace.context["summary"]["saved_decisions"], 2)

    def test_saving_a_device_choice_refuses_an_adapter_this_release_dropped(self):
        """The POST discards an unusable preview before it writes the Device decision."""
        response = self.start_alias_preview()
        revision = response.context["preview_revision"]
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired-adapter")

        saved = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_device"),
            {
                "device_key": "source alias",
                "device_id": self.device_a.pk,
                "search": "DEV-A",
                "preview_revision": revision,
            },
        )

        self.assertRedirects(
            saved,
            reverse("plugins:netbox_data_import:import_setup"),
            fetch_redirect_response=False,
        )
        self.assertFalse(TraceDeviceResolution.objects.filter(profile=self.profile).exists())
        self.assertFalse(self.client.session["import_preview_pending"])

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

    def test_the_workspace_disables_device_actions_without_resolution_write_permission(self):
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        TraceDeviceResolution.objects.create(
            profile=self.profile,
            source_device_key="dev-b",
            selected_device_id=self.device_b.pk,
            selected_display_name=str(self.device_b),
        )
        self.client.force_login(self.actor)
        block = direct_path(
            from_end=trace_termination("Source Alias", "", "eth0", "Port"),
            to_end=trace_termination("DEV-B", "", "eth1", "Port"),
        )
        upload = BytesIO(trace_workbook_bytes(path_blocks=(block,)))
        upload.name = "trace-permissions.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
        )
        self.actor.is_superuser = False
        self.actor.save(update_fields=("is_superuser",))
        broad_permission = ObjectPermission.objects.create(
            name="Trace workspace except Device resolution writes",
            actions=["view", "add", "change", "delete"],
            constraints={},
        )
        trace_resolution_type = ContentType.objects.get_for_model(TraceDeviceResolution)
        broad_permission.object_types.add(*ContentType.objects.exclude(pk=trace_resolution_type.pk))
        broad_permission.users.add(self.actor)

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You do not have permission to save a Device resolution.")
        self.assertContains(response, 'data-trace-device-picker="source alias" disabled')
        self.assertContains(response, 'data-trace-device-picker="dev-b" disabled')

        scoped_permission = ObjectPermission.objects.create(
            name="Another source Device resolution only",
            actions=["add"],
            constraints={"source_device_key": "another device"},
        )
        scoped_permission.object_types.add(trace_resolution_type)
        scoped_permission.users.add(self.actor)
        clear_user_permission_caches(self.actor)

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-trace-device-picker="source alias" disabled')

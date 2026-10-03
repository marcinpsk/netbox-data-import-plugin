# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Source Location paths: the profile mapping, the Device candidate evidence, and the workspace list."""

import re
from io import BytesIO

from dcim.models import Cable, Device, FrontPort, Interface, Location, Rack, RearPort, Site
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import (
    CableClassMapping,
    ImportProfile,
    PreviewCoordinator,
    PreviewState,
    TraceDeviceResolution,
    TraceLocationResolution,
)
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.profile_yaml import serialize_profile
from netbox_data_import.trace_device_resolution import CandidateFact, DeviceEvidence, eligible_trace_devices
from netbox_data_import.tests.helpers import (
    retired_claim,
    executed_sql,
    preview_claim,
    preview_coordinator,
    seed_preview,
    stored_plan,
    trace_endpoint_line,
    trace_segment,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
    user_with_object_permission,
)
from netbox_data_import.tests.test_cable_module import CableTopologyMixin
from netbox_data_import.views import CANDIDATE_OFFSET_INVALID, CANDIDATE_OFFSET_MAX

SOURCE_PATH = "Region >> Building (X) >> 1st Floor >> DH4 >> T"
OTHER_PATH = "Region >> Building (X) >> 1st Floor >> DH5"


def located_path(location, *, source_label="SRV Alias", rack="", u_position="", to_port="eth1"):
    """Return one path block whose source Device states the given placement facts."""
    from_end = trace_termination(source_label, "", "eth0", "Port")
    to_end = trace_termination("DEV-B", "", to_port, "Port")
    return (
        trace_endpoint_line(from_end),
        trace_endpoint_line(to_end),
        (trace_segment(from_end, "Patch", to_end, corroboration=(u_position, rack, location)),),
    )


class LocationTreeMixin(CableTopologyMixin):
    """A real Location tree in the cable topology Site, with one Device on each placement."""

    @classmethod
    def build_location_tree(cls):
        """Create Building > 1st Floor > DH4 > T and DH5, and a Device in each placement."""
        cls.build_topology()
        cls.building = Location.objects.create(site=cls.site, name="Building X", slug="building-x")
        cls.floor = Location.objects.create(site=cls.site, parent=cls.building, name="1st Floor", slug="first-floor")
        cls.hall = Location.objects.create(site=cls.site, parent=cls.floor, name="DH4", slug="dh4")
        cls.row = Location.objects.create(site=cls.site, parent=cls.hall, name="T", slug="t")
        cls.other_hall = Location.objects.create(site=cls.site, parent=cls.floor, name="DH5", slug="dh5")
        cls.in_hall = cls.placed_device("Server In Hall", location=cls.hall)
        cls.in_row = cls.placed_device("Server In Row", location=cls.row)
        cls.in_other_hall = cls.placed_device("Server In Other Hall", location=cls.other_hall)

    @classmethod
    def placed_device(cls, name, **placement):
        """Create one Device at the shared Site with the given placement."""
        return Device.objects.create(name=name, site=cls.site, device_type=cls.device_type, role=cls.role, **placement)

    def map_path(self, path, location, display="Mapped Location Snapshot"):
        """Store one Location mapping for this profile, the way the workspace writer does."""
        return TraceLocationResolution.objects.create(
            profile=self.profile,
            source_location_key=" ".join(path.split()).upper(),
            source_location_path=path,
            selected_location_id=location.pk,
            selected_display_name=display,
        )

    def evidence(self, *locations, racks=(), u_positions=(), label="SRV Alias"):
        """Return the Device evidence one source label carries."""
        return DeviceEvidence(
            key=" ".join(label.split()).upper(),
            labels=(label,),
            locations=locations,
            racks=racks,
            u_positions=u_positions,
        )

    def candidates(self, evidence, actor=None, *, location=None):
        """Return every candidate the scoped picker offers, keyed by Device."""
        reader = NetBoxReader.for_actor(actor) if actor is not None else NetBoxReader.unrestricted()
        page = eligible_trace_devices(
            profile=self.profile,
            reader=reader.for_target(site=self.site, location=location),
            evidence=evidence,
            limit=50,
        )
        return page

    @staticmethod
    def facts(candidate, fact):
        """Return the matched and conflicting explanations one candidate gives for one fact."""
        return (
            [item for item in candidate.matched if item.fact == fact],
            [item for item in candidate.conflicting if item.fact == fact],
        )

    @staticmethod
    def candidate_for(page, device):
        """Return the one candidate that offers *device*."""
        return next(candidate for candidate in page.candidates if candidate.device.pk == device.pk)

    @staticmethod
    def order(page):
        """Return the offered Devices in rank order."""
        return [candidate.device.pk for candidate in page.candidates]

    def unmapped_order(self, actor, path=SOURCE_PATH):
        """Return the rank order *actor* sees once no mapping exists, as if the evidence never existed."""
        TraceLocationResolution.objects.filter(profile=self.profile).delete()
        return self.order(self.candidates(self.evidence(path), actor))


class TraceLocationResolutionModelTest(LocationTreeMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_location_tree()

    def test_a_trace_profile_accepts_one_canonical_source_location_key(self):
        resolution = TraceLocationResolution(
            profile=self.profile,
            source_location_key="REGION >> BUILDING (X) >> 1ST FLOOR >> DH4 >> T",
            source_location_path=SOURCE_PATH,
            selected_location_id=self.hall.pk,
            selected_display_name=str(self.hall),
        )

        resolution.full_clean()
        resolution.save()

        self.assertEqual(len(resolution.source_location_key_digest), 64)

    def test_a_legacy_location_resolution_can_be_validated_and_updated(self):
        """A saved mapping can lack the source spelling introduced after it was stored."""
        resolution = TraceLocationResolution.objects.create(
            profile=self.profile,
            source_location_key="SOURCE LOCATION",
            selected_location_id=self.hall.pk,
            selected_display_name=str(self.hall),
        )
        resolution = TraceLocationResolution.objects.get(pk=resolution.pk)
        resolution.selected_display_name = "Updated location display"

        resolution.full_clean()
        resolution.save()

        stored = TraceLocationResolution.objects.get(pk=resolution.pk)
        self.assertEqual(stored.selected_display_name, "Updated location display")
        self.assertEqual(stored.source_location_path, "")

    def test_a_saved_location_resolution_rejects_a_nonempty_spelling_of_another_key(self):
        resolution = TraceLocationResolution.objects.create(
            profile=self.profile,
            source_location_key="SOURCE LOCATION",
            selected_location_id=self.hall.pk,
            selected_display_name=str(self.hall),
        )
        resolution.source_location_path = "Another source"

        with self.assertRaises(ValidationError) as caught:
            resolution.full_clean()

        self.assertIn("source_location_path", caught.exception.message_dict)

    def test_the_kept_source_path_has_to_state_the_key(self):
        """A path of another key would move the mapping to that key at the next rekey."""
        for path in ("", "Region >> Building (X)"):
            with self.subTest(path=path):
                resolution = TraceLocationResolution(
                    profile=self.profile,
                    source_location_key="REGION >> BUILDING (X) >> 1ST FLOOR >> DH4 >> T",
                    source_location_path=path,
                    selected_location_id=self.hall.pk,
                    selected_display_name=str(self.hall),
                )

                with self.assertRaisesMessage(ValidationError, "source path of this Location key"):
                    resolution.full_clean()

    def test_a_noncanonical_or_empty_source_location_key_is_rejected(self):
        for key in (SOURCE_PATH, "  ", ""):
            with self.subTest(key=key):
                resolution = TraceLocationResolution(
                    profile=self.profile,
                    source_location_key=key,
                    selected_location_id=self.hall.pk,
                    selected_display_name=str(self.hall),
                )

                with self.assertRaisesMessage(ValidationError, "canonical source Location key"):
                    resolution.full_clean()

    def test_a_flat_profile_cannot_own_a_trace_location_resolution(self):
        flat_profile = ImportProfile.objects.create(name="Flat Location Resolution", adapter_config={})
        resolution = TraceLocationResolution(
            profile=flat_profile,
            source_location_key="DH4",
            selected_location_id=self.hall.pk,
            selected_display_name=str(self.hall),
        )

        with self.assertRaises(ValidationError):
            resolution.full_clean()

    def test_a_mapping_change_changes_the_profile_fingerprint(self):
        before = self.profile.planning_fingerprint
        mapping = self.map_path(SOURCE_PATH, self.hall)
        mapped = self.profile.planning_fingerprint

        mapping.selected_location_id = self.other_hall.pk
        mapping.save()

        self.assertNotEqual(mapped, before)
        self.assertNotEqual(self.profile.planning_fingerprint, mapped)

    def test_portable_profile_yaml_omits_the_whole_section(self):
        self.map_path(SOURCE_PATH, self.hall)

        document = serialize_profile(self.profile)

        self.assertNotIn("trace_location_resolutions", document)


class SourceLocationEvidenceTest(LocationTreeMixin, TestCase):
    """Location evidence compares only through a mapping, never by a NetBox Location name."""

    @classmethod
    def setUpTestData(cls):
        cls.build_location_tree()

    def test_an_unmapped_source_path_is_neither_a_match_nor_a_conflict(self):
        """An unmapped path gives no Location evidence, so no candidate reports a Location difference."""
        page = self.candidates(self.evidence(SOURCE_PATH))

        for device in (self.in_hall, self.in_row, self.in_other_hall):
            with self.subTest(device=device.name):
                self.assertEqual(self.facts(self.candidate_for(page, device), "location"), ([], []))

    def test_a_netbox_location_named_like_the_whole_path_is_not_a_match(self):
        """The path is opaque, so even an identical Location name gives no evidence."""
        literal = Location.objects.create(site=self.site, name=SOURCE_PATH[:100], slug="literal")
        device = self.placed_device("Server In Literal", location=literal)

        page = self.candidates(self.evidence(SOURCE_PATH[:100]))

        self.assertEqual(self.facts(self.candidate_for(page, device), "location"), ([], []))

    def test_a_mapped_path_matches_its_location_and_every_descendant(self):
        self.map_path(SOURCE_PATH, self.hall)

        page = self.candidates(self.evidence(SOURCE_PATH))

        self.assertEqual(
            self.facts(self.candidate_for(page, self.in_hall), "location"),
            ([CandidateFact("location", source=SOURCE_PATH, mapped="DH4", netbox="DH4")], []),
        )
        self.assertEqual(
            self.facts(self.candidate_for(page, self.in_row), "location"),
            ([CandidateFact("location", source=SOURCE_PATH, mapped="DH4", netbox="T")], []),
        )
        ranked = [candidate.device for candidate in page.candidates]
        self.assertLess(ranked.index(self.in_row), ranked.index(self.in_other_hall))
        self.assertLess(ranked.index(self.in_hall), ranked.index(self.in_other_hall))

    def test_a_mapped_path_conflicts_outside_its_subtree_with_all_three_values(self):
        self.map_path(SOURCE_PATH, self.hall)

        page = self.candidates(self.evidence(SOURCE_PATH))

        self.assertEqual(
            self.facts(self.candidate_for(page, self.in_other_hall), "location"),
            ([], [CandidateFact("location", source=SOURCE_PATH, mapped="DH4", netbox="DH5")]),
        )

    def test_fewer_conflicts_rank_first_among_equal_matches(self):
        """A Device with no placement has nothing to conflict with, so it ranks above a conflict."""
        self.map_path(SOURCE_PATH, self.hall)
        unplaced = self.placed_device("Server Unplaced")

        ranked = [candidate.device for candidate in self.candidates(self.evidence(SOURCE_PATH)).candidates]

        self.assertLess(ranked.index(unplaced), ranked.index(self.in_other_hall))

    def test_the_rack_location_counts_only_when_the_device_has_none_of_its_own(self):
        """Ranking and explanation use one placement rule, so they cannot disagree."""
        self.map_path(SOURCE_PATH, self.hall)
        rack = Rack.objects.create(site=self.site, location=self.hall, name="Hall Rack", u_height=42)
        own_location_wins = self.placed_device("Server Own Location", rack=rack)
        Device.objects.filter(pk=own_location_wins.pk).update(location=self.other_hall)
        rack_fallback = self.placed_device("Server Rack Fallback", rack=rack)
        Device.objects.filter(pk=rack_fallback.pk).update(location=None)

        page = self.candidates(self.evidence(SOURCE_PATH))

        self.assertEqual(
            self.facts(self.candidate_for(page, own_location_wins), "location"),
            ([], [CandidateFact("location", source=SOURCE_PATH, mapped="DH4", netbox="DH5")]),
        )
        self.assertEqual(
            self.facts(self.candidate_for(page, rack_fallback), "location"),
            ([CandidateFact("location", source=SOURCE_PATH, mapped="DH4", netbox="DH4")], []),
        )
        ranked = [candidate.device for candidate in page.candidates]
        self.assertLess(ranked.index(rack_fallback), ranked.index(own_location_wins))

    def test_a_hidden_own_location_gives_no_evidence_and_no_rack_fallback(self):
        secret = Location.objects.create(site=self.site, name="Secret Room", slug="secret-room")
        rack = Rack.objects.create(site=self.site, location=self.hall, name="Hall Rack", u_height=42)
        device = self.placed_device("Server Secret", rack=rack)
        Device.objects.filter(pk=device.pk).update(location=secret)
        self.map_path(SOURCE_PATH, self.hall)
        actor = user_with_object_permission(
            "location-hidden-own",
            [
                (Device, ("view",), {"site_id": self.site.pk}),
                (Rack, ("view",), {}),
                (Location, ("view",), {"name__in": ["DH4", "DH5", "T", "1st Floor", "Building X"]}),
                (TraceLocationResolution, ("view",), {}),
            ],
        )

        page = self.candidates(self.evidence(SOURCE_PATH), actor)

        candidate = self.candidate_for(page, device)
        self.assertEqual(self.facts(candidate, "location"), ([], []))
        self.assertNotIn("Secret Room", str(candidate))
        # The order must be the order of a Device with no placement at all.
        Device.objects.filter(pk=device.pk).update(location=None, rack=None)
        self.assertEqual(self.order(page), self.order(self.candidates(self.evidence(SOURCE_PATH), actor)))

    def test_a_hidden_intermediate_location_does_not_break_containment(self):
        path = "Campus >> Level 1 >> Row T"
        self.map_path(path, self.floor)
        actor = user_with_object_permission(
            "location-hidden-intermediate",
            [
                (Device, ("view",), {"site_id": self.site.pk}),
                (Location, ("view",), {"name__in": ["1st Floor", "T", "DH5"]}),
                (TraceLocationResolution, ("view",), {}),
            ],
        )

        page = self.candidates(self.evidence(path), actor)

        candidate = self.candidate_for(page, self.in_row)
        self.assertEqual(
            self.facts(candidate, "location"),
            ([CandidateFact("location", source=path, mapped="1st Floor", netbox="T")], []),
        )
        self.assertNotIn("DH4", str(candidate))
        # A Device whose own Location is hidden has no visible placement at all.
        self.assertEqual(self.facts(self.candidate_for(page, self.in_hall), "location"), ([], []))

    def test_two_paths_on_one_label_score_once_and_explain_both(self):
        self.map_path(SOURCE_PATH, self.hall)
        self.map_path(OTHER_PATH, self.other_hall)
        rack = Rack.objects.create(site=self.site, name="Source Rack", u_height=42)
        rack_only = self.placed_device("A Rack Only", rack=rack)

        page = self.candidates(self.evidence(SOURCE_PATH, OTHER_PATH, racks=("Source Rack",)))

        candidate = self.candidate_for(page, self.in_hall)
        self.assertEqual(
            self.facts(candidate, "location"),
            (
                [CandidateFact("location", source=SOURCE_PATH, mapped="DH4", netbox="DH4")],
                [CandidateFact("location", source=OTHER_PATH, mapped="DH5", netbox="DH4")],
            ),
        )
        # One location score, one location conflict: the rack-only Device has one score and no conflict.
        ranked = [item.device for item in page.candidates]
        self.assertLess(ranked.index(rack_only), ranked.index(self.in_hall))

    def test_nested_mapped_paths_add_one_location_score(self):
        """A Device inside both mapped subtrees still has one Location score, not two."""
        self.map_path(SOURCE_PATH, self.hall)
        self.map_path(OTHER_PATH, self.building)
        rack = Rack.objects.create(site=self.site, name="Source Rack", u_height=42)
        rack_only = self.placed_device("A Rack Only", rack=rack)
        in_both = self.placed_device("Z In Both", location=self.hall)

        page = self.candidates(self.evidence(SOURCE_PATH, OTHER_PATH, racks=("Source Rack",)))

        self.assertEqual(len(self.facts(self.candidate_for(page, in_both), "location")[0]), 2)
        ranked = [item.device for item in page.candidates]
        self.assertLess(ranked.index(rack_only), ranked.index(in_both))

    def test_a_stale_mapping_acts_unmapped(self):
        # Sorts after every placed Device by name, so one Location conflict would reorder the page.
        self.placed_device("Zz Unplaced")
        gone = Location.objects.create(site=self.site, name="Gone Room", slug="gone-room")
        moved = Location.objects.create(site=self.site, name="Moved Room", slug="moved-room")
        hidden = Location.objects.create(site=self.site, parent=self.hall, name="Hidden Room", slug="hidden-room")
        other_site = Site.objects.create(name="Other Location Site", slug="other-location-site")
        cases = (
            ("deleted", gone, lambda: Location.objects.filter(pk=gone.pk).delete()),
            ("moved", moved, lambda: Location.objects.filter(pk=moved.pk).update(site=other_site)),
            ("hidden", hidden, lambda: None),
        )
        actor = user_with_object_permission(
            "location-stale",
            [
                (Device, ("view",), {"site_id": self.site.pk}),
                (Location, ("view",), {"name__in": ["DH4", "DH5", "T", "Moved Room"]}),
                (TraceLocationResolution, ("view",), {}),
            ],
        )
        for name, location, make_stale in cases:
            with self.subTest(case=name):
                TraceLocationResolution.objects.filter(profile=self.profile).delete()
                self.map_path(SOURCE_PATH, location)
                make_stale()

                page = self.candidates(self.evidence(SOURCE_PATH), actor)

                for device in (self.in_hall, self.in_other_hall):
                    self.assertEqual(self.facts(self.candidate_for(page, device), "location"), ([], []))
                self.assertEqual(self.order(page), self.unmapped_order(actor))

    def test_a_mapping_row_the_actor_cannot_view_gives_no_evidence(self):
        self.map_path(SOURCE_PATH, self.hall)
        actor = user_with_object_permission(
            "location-row-hidden",
            [
                (Device, ("view",), {"site_id": self.site.pk}),
                (Location, ("view",), {}),
                (TraceLocationResolution, ("view",), {"source_location_key": "another path"}),
            ],
        )

        page = self.candidates(self.evidence(SOURCE_PATH), actor)

        self.assertEqual(self.facts(self.candidate_for(page, self.in_other_hall), "location"), ([], []))
        self.assertEqual(self.facts(self.candidate_for(page, self.in_hall), "location"), ([], []))
        self.assertEqual(self.order(page), self.unmapped_order(actor))

    def test_the_import_location_ranks_after_name_and_source_evidence_and_never_filters(self):
        rack = Rack.objects.create(site=self.site, name="Source Rack", u_height=42)
        exact = self.placed_device("SRV Alias")
        rack_match = self.placed_device("Z Rack Match", rack=rack)
        unhinted = self.placed_device("A Unhinted")

        page = self.candidates(self.evidence(racks=("Source Rack",)), location=self.floor)

        ranked = [candidate.device for candidate in page.candidates]
        self.assertEqual(ranked[:2], [exact, rack_match])
        self.assertLess(ranked.index(self.in_row), ranked.index(unhinted))
        self.assertEqual(page.total, Device.objects.filter(site=self.site).count())
        hinted = self.candidate_for(page, self.in_row)
        self.assertEqual((hinted.import_location.location, hinted.import_location.netbox), ("1st Floor", "T"))
        self.assertEqual(hinted.conflicting, ())
        self.assertIsNone(self.candidate_for(page, unhinted).import_location)

    def test_the_import_location_never_conflicts_with_a_device_outside_it(self):
        page = self.candidates(self.evidence(SOURCE_PATH), location=self.other_hall)

        outside = self.candidate_for(page, self.in_hall)
        self.assertIsNone(outside.import_location)
        self.assertEqual(outside.conflicting, ())

    def test_matched_and_conflicting_facts_carry_both_values(self):
        rack = Rack.objects.create(site=self.site, name="Source Rack", u_height=42)
        other_rack = Rack.objects.create(site=self.site, name="Other Rack", u_height=42)
        matching = self.placed_device("Server Matching", rack=rack, position=12, face="front")
        differing = self.placed_device("Server Differing", rack=other_rack, position=14, face="front")

        page = self.candidates(self.evidence(racks=("Source Rack",), u_positions=("12",)))

        self.assertEqual(
            self.candidate_for(page, matching).matched,
            (
                CandidateFact("rack", source="Source Rack", netbox="Source Rack"),
                CandidateFact("U position", source="12", netbox="12"),
            ),
        )
        self.assertEqual(
            self.candidate_for(page, differing).conflicting,
            (
                CandidateFact("rack", source="Source Rack", netbox="Other Rack"),
                CandidateFact("U position", source="12", netbox="14"),
            ),
        )


def _workspace_grants(*, mapping_actions=("view", "add", "change", "delete"), mapping_constraints=None):
    """Return every grant a constrained operator needs to review a trace import end to end."""
    return [
        (ImportProfile, ("view", "change"), {}),
        (Site, ("view",), {}),
        (Location, ("view",), {}),
        (Rack, ("view",), {}),
        (Device, ("view",), {}),
        (Interface, ("view",), {}),
        (FrontPort, ("view",), {}),
        (RearPort, ("view",), {}),
        (Cable, ("view",), {}),
        (CableClassMapping, ("view",), {}),
        (TraceDeviceResolution, ("view", "add", "change"), {}),
        (TraceLocationResolution, mapping_actions, mapping_constraints),
    ]


class LocationWorkspaceMixin(LocationTreeMixin):
    """Drive the real setup, workspace, picker and mapping views."""

    def open_workspace(self, *blocks, client=None, location=None):
        """Upload the path blocks through the real setup flow and return the workspace page."""
        client = client or self.client
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks or (located_path(SOURCE_PATH),)))
        upload.name = "traces.xlsx"
        data = {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload}
        if location is not None:
            data["location"] = location.pk
        setup = upload_preview(client, data, follow=True)
        self.assertEqual(setup.status_code, 200)
        return client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def stale_claim(self, client=None):
        """Return the claim a successful re-read retired."""
        return retired_claim(client or self.client)

    def seed_as(self, actor):
        """Log *actor* in and give that session the current upload, planned as *actor*."""
        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.models import SourceDocument

        coordinator = preview_coordinator(self.client)
        document = SourceDocument.objects.get(pk=coordinator.source_document_id)
        context = dict(coordinator.context)
        self.client.force_login(actor)
        planning_context = {key: context[key] for key in ("site_id", "location_id", "tenant_id")}
        plan = ImportEngine.plan(self.profile, document, actor, planning_context)
        seed_preview(self.client, profile=self.profile, document=document, plan=plan, context=context)

    def post_mapping(self, client=None, *, as_json=True, **data):
        """Post one Location mapping command through the workspace endpoint."""
        client = client or self.client
        for key, value in preview_claim(client).items():
            data.setdefault(key, value)
        data.setdefault("location_key", " ".join(SOURCE_PATH.split()).upper())
        return client.post(
            reverse("plugins:netbox_data_import:trace_location_mapping"),
            data,
            headers={"accept": "application/json"} if as_json else {},
        )

    def device_candidates(self, client=None, device_key="SRV ALIAS"):
        """Return the JSON page the Device picker reads."""
        client = client or self.client
        response = client.get(
            reverse("plugins:netbox_data_import:trace_device_candidates"),
            {"device_key": device_key, **preview_claim(client)},
        )
        self.assertEqual(response.status_code, 200, response.content)
        return {item["id"]: item for item in response.json()["candidates"]}

    def location_candidates(self, client=None, **params):
        """Ask the shared Location picker endpoint the way the picker asks."""
        client = client or self.client
        params.setdefault("location_key", " ".join(SOURCE_PATH.split()).upper())
        for key, value in preview_claim(client).items():
            params.setdefault(key, value)
        return client.get(reverse("plugins:netbox_data_import:trace_location_candidates"), params)

    @staticmethod
    def mapping_row(response, key=None):
        """Return the workspace row for one source Location path."""
        key = key or " ".join(SOURCE_PATH.split()).upper()
        return next(row for row in response.context["location_mappings"] if row.key == key)


class LocationWorkspaceTest(LocationWorkspaceMixin, TestCase):
    """End to end through the real views, as a superuser."""

    @classmethod
    def setUpTestData(cls):
        cls.build_location_tree()

    def setUp(self):
        self.client.force_login(self.actor)

    def test_the_users_case_shows_no_location_conflict_without_a_mapping(self):
        self.open_workspace()

        candidates = self.device_candidates()

        for device in (self.in_hall, self.in_row, self.in_other_hall):
            with self.subTest(device=device.name):
                self.assertEqual(
                    [fact for fact in candidates[device.pk]["conflicting_facts"] if fact["fact"] == "location"],
                    [],
                )

    def test_the_workspace_lists_each_source_path_once_independent_of_the_selected_trace(self):
        second = located_path(SOURCE_PATH.replace(" >> ", "  >>  "), source_label="DEV-A", to_port="eth2")
        third = located_path(OTHER_PATH, source_label="SRV Other", to_port="eth3")

        response = self.open_workspace(located_path(SOURCE_PATH), second, third)

        self.assertEqual(
            [(row.path, row.state) for row in response.context["location_mappings"]],
            [(SOURCE_PATH, "unmapped"), (OTHER_PATH, "unmapped")],
        )
        self.assertContains(response, "data-trace-location-mappings")

    def test_separator_spelling_is_part_of_the_key(self):
        compact = SOURCE_PATH.replace(" >> ", ">>")

        response = self.open_workspace(
            located_path(SOURCE_PATH), located_path(compact, source_label="DEV-A", to_port="eth2")
        )

        self.assertEqual(
            sorted(row.path for row in response.context["location_mappings"]),
            sorted([SOURCE_PATH, compact]),
        )

    def test_a_batch_with_no_source_path_says_so(self):
        response = self.open_workspace(located_path(""))

        self.assertEqual(response.context["location_mappings"], [])
        self.assertContains(response, "No source Location paths")

    def test_a_site_with_no_visible_location_says_so_separately(self):
        Device.objects.filter(location__isnull=False).update(location=None)
        Location.objects.all().delete()

        response = self.open_workspace()

        self.assertContains(response, "No Location in this Site is visible to you.")
        self.assertNotContains(response, "No source Location paths")
        row = self.mapping_row(response)
        self.assertEqual(row.save_reason, "No Location in this Site is visible to you.")

    def test_saving_a_mapping_replans_and_explains_the_candidates(self):
        opened = self.open_workspace()
        before = preview_coordinator(self.client).revision

        saved = self.post_mapping(location_id=self.hall.pk, trace=opened.context["selected_trace"].identity)

        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["preview_state"], "replanned")
        stored = TraceLocationResolution.objects.get(profile=self.profile)
        self.assertEqual((stored.selected_location_id, stored.selected_display_name), (self.hall.pk, "DH4"))
        self.assertEqual(stored.source_location_path, SOURCE_PATH)
        self.assertEqual(preview_coordinator(self.client).revision, before + 1)
        page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        row = self.mapping_row(page)
        self.assertEqual((row.state, row.location), ("mapped", "DH4"))
        candidates = self.device_candidates()
        self.assertEqual(
            candidates[self.in_row.pk]["matched_facts"],
            [{"fact": "location", "source": SOURCE_PATH, "mapped": "DH4", "netbox": "T"}],
        )
        self.assertEqual(
            candidates[self.in_other_hall.pk]["conflicting_facts"],
            [{"fact": "location", "source": SOURCE_PATH, "mapped": "DH4", "netbox": "DH5"}],
        )

    def test_clearing_a_mapping_removes_it_and_replans(self):
        self.open_workspace()
        self.post_mapping(location_id=self.hall.pk)
        mapped_fingerprint = stored_plan(self.client)["profile_fingerprint"]
        before = preview_coordinator(self.client).revision

        cleared = self.post_mapping(clear="1")

        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.assertFalse(TraceLocationResolution.objects.filter(profile=self.profile).exists())
        self.assertEqual(preview_coordinator(self.client).revision, before + 1)
        # The stored plan was planned without the mapping, so the workspace reads it as current.
        replanned = stored_plan(self.client)["profile_fingerprint"]
        self.assertNotEqual(replanned, mapped_fingerprint)
        self.assertEqual(replanned, ImportProfile.objects.get(pk=self.profile.pk).planning_fingerprint)
        page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertFalse(page.context["drift"])
        self.assertEqual(self.mapping_row(page).state, "unmapped")
        candidates = self.device_candidates()
        for device in (self.in_row, self.in_other_hall):
            facts = candidates[device.pk]["matched_facts"] + candidates[device.pk]["conflicting_facts"]
            self.assertEqual([fact for fact in facts if fact["fact"] == "location"], [], device.name)

    def test_one_location_picker_serves_every_source_path(self):
        """Each row opens the one shared picker; no row renders its own list of Locations."""
        import re

        from django.utils.html import escape

        second = located_path(OTHER_PATH, source_label="SRV Other", to_port="eth3")
        response = self.open_workspace(located_path(SOURCE_PATH), second)

        html = response.content.decode()
        card = re.search(r"<div[^>]*data-trace-location-mappings.*?<div class=\"row g-3\">", html, re.DOTALL)
        self.assertIsNotNone(card)
        self.assertNotIn("<select", card.group())
        self.assertEqual(html.count('id="traceLocationPicker"'), 1)
        for row in response.context["location_mappings"]:
            self.assertIn(f'data-trace-location-picker="{escape(row.key)}"', html)

    def test_the_workspace_reads_no_location_list_to_render(self):
        """The page asks only whether a Location is visible; the picker pages the rest on demand."""
        from django.db.models.signals import post_init

        for number in range(30):
            Location.objects.create(site=self.site, name=f"Bulk {number:02}", slug=f"bulk-{number:02}")
        self.open_workspace()
        materialized = []

        def count(sender, instance, **kwargs):
            materialized.append(instance)

        post_init.connect(count, sender=Location)
        try:
            response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        finally:
            post_init.disconnect(count, sender=Location)

        self.assertEqual(response.status_code, 200)
        self.assertLess(len(materialized), 5, [str(location) for location in materialized])

    def test_the_location_picker_pages_and_searches_the_visible_locations_of_the_site(self):
        other_site = Site.objects.create(name="Other Picker Site", slug="other-picker-site")
        Location.objects.create(site=other_site, name="DH9 Elsewhere", slug="dh9-elsewhere")
        self.open_workspace()

        first_page = self.location_candidates(limit=2).json()
        searched = self.location_candidates(search="  dh ").json()

        self.assertEqual(
            (first_page["shown"], first_page["total"]), (2, Location.objects.filter(site=self.site).count())
        )
        self.assertEqual([item["name"] for item in first_page["candidates"]], ["1st Floor", "Building X"])
        self.assertEqual(
            searched["candidates"],
            [
                {"id": self.hall.pk, "name": "DH4", "parent": "1st Floor"},
                {"id": self.other_hall.pk, "name": "DH5", "parent": "1st Floor"},
            ],
        )
        self.assertEqual(searched["total"], 2)

    def test_the_three_pickers_scroll_only_their_candidate_list(self):
        """Search, count, pages and Save stay in view, and each picker places them the same way."""
        page = self.open_workspace().content.decode()

        for prefix in ("traceDevice", "traceLocation", "traceTermination"):
            with self.subTest(picker=prefix):
                dialog = re.search(rf'<div class="modal" id="{prefix}Picker".*?id="{prefix}Submit"', page, re.DOTALL)
                self.assertIsNotNone(dialog)
                markup = dialog.group()
                self.assertIn('class="modal-dialog modal-lg modal-dialog-scrollable"', markup)
                self.assertIn('class="d-flex flex-column overflow-hidden"', markup)
                self.assertIn('<div class="modal-body d-flex flex-column overflow-hidden">', markup)
                self.assertIn(f'class="list-group overflow-auto ndi-picker-list" id="{prefix}Candidates"', markup)
                self.assertRegex(markup, rf'<div class="modal-footer">\s*<nav class="me-auto" id="{prefix}Pages"')

    def test_the_location_picker_reaches_the_twenty_first_location_of_one_name(self):
        """Twenty-one visible 'Room' Locations under different parents fill more than one page."""
        rooms = []
        for number in range(1, 22):
            hall = Location.objects.create(site=self.site, name=f"Hall {number:02}", slug=f"hall-{number:02}")
            rooms.append(Location.objects.create(site=self.site, parent=hall, name="Room", slug=f"room-{number:02}"))
        self.open_workspace()

        first = self.location_candidates(search="Room").json()
        second = self.location_candidates(search="Room", offset=20).json()
        saved = self.post_mapping(location_id=rooms[-1].pk)

        self.assertEqual((first["shown"], first["total"], first["offset"]), (20, 21, 0))
        self.assertEqual(
            (second["candidates"], second["shown"], second["total"], second["offset"]),
            ([{"id": rooms[-1].pk, "name": "Room", "parent": "Hall 21"}], 1, 21, 20),
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(TraceLocationResolution.objects.get(profile=self.profile).selected_location_id, rooms[-1].pk)

    def test_the_location_picker_refuses_what_the_preview_did_not_ask(self):
        self.open_workspace()
        cases = (
            ({"location_key": "invented path"}, 400, "This preview carries no such source Location path."),
            ({"search": "x" * 201}, 400, "Location search must be 200 characters or fewer."),
            ({"limit": "0"}, 400, "Candidate limit must be an integer from 1 to 20."),
            ({"offset": "-1"}, 400, CANDIDATE_OFFSET_INVALID),
            ({"offset": "next"}, 400, CANDIDATE_OFFSET_INVALID),
            ({"offset": str(CANDIDATE_OFFSET_MAX + 1)}, 400, CANDIDATE_OFFSET_INVALID),
            ({"offset": str(2**63)}, 400, CANDIDATE_OFFSET_INVALID),
        )
        for params, status, error in cases:
            with self.subTest(params=params):
                response = self.location_candidates(**params)

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"ok": False, "error": error})

    def test_the_location_picker_refuses_a_stale_claim(self):
        from netbox_data_import.preview_coordinator import STALE_PREVIEW

        self.open_workspace()

        response = self.location_candidates(**self.stale_claim())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"ok": False, "error": STALE_PREVIEW, "code": "preview_stale"})

    def test_the_largest_offset_reads_an_empty_location_page(self):
        """The bound leaves room for one page, and a page past the count is empty."""
        self.open_workspace()

        with executed_sql() as statements:
            response = self.location_candidates(offset=CANDIDATE_OFFSET_MAX)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual((payload["candidates"], payload["offset"]), ([], CANDIDATE_OFFSET_MAX))
        self.assertGreater(payload["total"], 0)
        self.assertEqual([sql for sql in statements if "OFFSET" in sql], [])

    def test_a_path_the_preview_never_carried_is_refused(self):
        self.open_workspace()

        refused = self.post_mapping(location_key="invented path", location_id=self.hall.pk)

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"], "This preview carries no such source Location path.")
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_a_location_outside_the_site_is_refused(self):
        other_site = Site.objects.create(name="Other Mapping Site", slug="other-mapping-site")
        elsewhere = Location.objects.create(site=other_site, name="Elsewhere", slug="elsewhere")
        self.open_workspace()

        for location_id in (elsewhere.pk, "not-a-number", 987654321):
            with self.subTest(location_id=location_id):
                refused = self.post_mapping(location_id=location_id)

                self.assertEqual(refused.status_code, 400)
                self.assertEqual(refused.json()["error"], "Choose a visible Location in the selected Site.")
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_a_stale_mapping_is_marked_and_never_shows_its_snapshot(self):
        gone = Location.objects.create(site=self.site, name="Gone Room", slug="gone-room")
        self.map_path(SOURCE_PATH, gone, display="Hidden Snapshot Name")
        Location.objects.filter(pk=gone.pk).delete()

        response = self.open_workspace()

        row = self.mapping_row(response)
        self.assertEqual((row.state, row.location), ("stale", ""))
        self.assertNotContains(response, "Hidden Snapshot Name")
        self.assertTrue(TraceLocationResolution.objects.filter(profile=self.profile).exists())

    def test_a_mapping_that_moved_under_this_preview_refuses_the_save(self):
        """Two sessions on one profile: the revision is per session, the fingerprint is not."""
        self.open_workspace()
        other = Client()
        other.force_login(self.actor)
        self.open_workspace(client=other)
        self.post_mapping(client=other, location_id=self.other_hall.pk)

        refused = self.post_mapping(location_id=self.hall.pk)

        self.assertEqual(refused.status_code, 409)
        self.assertIn("policy changed since this preview was planned", refused.json()["error"])
        self.assertEqual(
            TraceLocationResolution.objects.get(profile=self.profile).selected_location_id,
            self.other_hall.pk,
        )
        cleared = self.post_mapping(clear="1")
        self.assertEqual(cleared.status_code, 409)
        self.assertTrue(TraceLocationResolution.objects.filter(profile=self.profile).exists())

    def test_a_device_decision_against_a_moved_policy_is_refused(self):
        """The Device writer compares the reviewed fingerprint like every other policy writer."""
        self.open_workspace()
        other = Client()
        other.force_login(self.actor)
        self.open_workspace(client=other)
        self.post_mapping(client=other, location_id=self.hall.pk)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_device"),
            {
                "device_key": "SRV ALIAS",
                "device_id": self.in_hall.pk,
                "search": "",
                **preview_claim(self.client),
            },
            headers={"accept": "application/json"},
        )

        self.assertEqual(refused.status_code, 409)
        self.assertIn("policy changed since this preview was planned", refused.json()["error"])
        self.assertFalse(TraceDeviceResolution.objects.filter(profile=self.profile).exists())

    def test_a_save_with_a_stale_claim_writes_nothing(self):
        self.open_workspace()
        stale = self.stale_claim()

        refused = self.post_mapping(location_id=self.hall.pk, **stale)
        refused_form = self.post_mapping(location_id=self.hall.pk, as_json=False, **stale)

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "preview_stale")
        self.assertEqual(refused_form.status_code, 409)
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_a_clear_with_a_stale_claim_writes_nothing(self):
        self.map_path(SOURCE_PATH, self.hall)
        self.open_workspace()
        stale = self.stale_claim()
        before = list(TraceLocationResolution.objects.values())

        refused = self.post_mapping(clear="1", **stale)
        refused_form = self.post_mapping(clear="1", as_json=False, **stale)

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "preview_stale")
        self.assertEqual(refused_form.status_code, 409)
        self.assertEqual(list(TraceLocationResolution.objects.values()), before)

    def test_the_saved_decisions_count_includes_location_mappings(self):
        self.map_path(SOURCE_PATH, self.hall)

        response = self.open_workspace()

        self.assertEqual(response.context["summary"]["saved_decisions"], 1)


class ImportLocationEvidenceTest(LocationWorkspaceMixin, TestCase):
    """The import-page Location ranks trace candidates and is never a required target for them."""

    @classmethod
    def setUpTestData(cls):
        cls.build_location_tree()

    def setUp(self):
        self.client.force_login(self.actor)

    def test_the_candidate_endpoint_names_the_import_location_hint(self):
        self.open_workspace(location=self.floor)

        candidates = self.device_candidates()

        self.assertEqual(candidates[self.in_row.pk]["import_location"], {"location": "1st Floor", "netbox": "T"})
        self.assertIsNone(candidates[self.device_a.pk]["import_location"])

    def test_a_trace_preview_survives_its_import_location_going(self):
        moved_site = Site.objects.create(name="Moved Location Site", slug="moved-location-site")
        cases = (
            ("deleted", lambda location: Location.objects.filter(pk=location.pk).delete()),
            ("moved", lambda location: Location.objects.filter(pk=location.pk).update(site=moved_site)),
        )
        for name, make_unavailable in cases:
            with self.subTest(case=name):
                location = Location.objects.create(site=self.site, name=f"Import {name}", slug=f"import-{name}")
                self.open_workspace(location=location)
                make_unavailable(location)

                response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)
                self.assertContains(response, "The import Location is no longer available")
                self.assertTrue(all(item["import_location"] is None for item in self.device_candidates().values()))

    def test_a_trace_preview_survives_its_import_location_being_hidden(self):
        hidden = Location.objects.create(site=self.site, name="Import Hidden", slug="import-hidden")
        grants = _workspace_grants()
        grants[2] = (Location, ("view",), {"name__in": ["DH4", "DH5", "T", "1st Floor", "Building X"]})
        actor = user_with_object_permission("import-location-hidden", grants)
        self.open_workspace(location=hidden)
        self.seed_as(actor)

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The import Location is no longer available")
        self.assertNotContains(response, "Import Hidden")

    def test_a_lost_tenant_still_sends_the_trace_workspace_to_setup(self):
        """Only the import Location became evidence; the rest of the target stays required."""
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Workspace Tenant", slug="workspace-tenant")
        upload = BytesIO(trace_workbook_bytes(path_blocks=(located_path(SOURCE_PATH),)))
        upload.name = "traces.xlsx"
        upload_preview(
            self.client, {"profile": self.profile.pk, "site": self.site.pk, "tenant": tenant.pk, "excel_file": upload}
        )
        tenant.delete()

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertRedirects(
            response, reverse("plugins:netbox_data_import:import_setup"), fetch_redirect_response=False
        )
        # A page load only reads, so the preview stays until a command replaces it.
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)

    def test_setup_refuses_a_location_outside_the_selected_site(self):
        other_site = Site.objects.create(name="Setup Other Site", slug="setup-other-site")
        elsewhere = Location.objects.create(site=other_site, name="Setup Elsewhere", slug="setup-elsewhere")
        upload = BytesIO(trace_workbook_bytes(path_blocks=(located_path(SOURCE_PATH),)))
        upload.name = "traces.xlsx"

        response = upload_preview(
            self.client,
            {"profile": self.profile.pk, "site": self.site.pk, "location": elsewhere.pk, "excel_file": upload},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not belong to the selected site")
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.EMPTY)


class ImportLocationExecutionTest(LocationWorkspaceMixin, TransactionTestCase):
    """An execution reserves its audit row in its own transaction, so this case cannot run inside one."""

    def setUp(self):
        self.build_location_tree()
        self.client.force_login(self.actor)

    def test_a_trace_sync_proceeds_after_its_import_location_goes(self):
        """The Cable Target Module never writes into the import Location, so the plan stays executable."""
        import uuid

        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.models import SourceDocument
        from netbox_data_import.plan import Disposition, ImportPlan

        location = Location.objects.create(site=self.site, name="Import Executed", slug="import-executed")
        self.open_workspace(located_path(SOURCE_PATH, source_label="DEV-A"), location=location)
        Location.objects.filter(pk=location.pk).delete()
        plan = ImportPlan.from_dict(stored_plan(self.client))

        execution = ImportEngine.execute(
            self.profile,
            SourceDocument.objects.get(profile=self.profile),
            plan.to_dict(),
            [unit.identity for unit in plan.units if unit.disposition == Disposition.ACTIONABLE],
            str(uuid.uuid4()),
            self.actor,
        )

        self.assertEqual(execution.outcome, "succeeded")
        self.assertEqual(Cable.objects.count(), 1)


class LocationMappingPermissionTest(LocationWorkspaceMixin, TestCase):
    """Row view gates disclosure; add, change and delete are checked separately on the server."""

    @classmethod
    def setUpTestData(cls):
        cls.build_location_tree()

    def login(self, name, **grants):
        """Log in an operator whose Location mapping grant is the one given."""
        actor = user_with_object_permission(name, _workspace_grants(**grants))
        self.client.force_login(actor)
        return actor

    def test_view_only_disables_save_with_its_reason_and_the_server_refuses(self):
        self.login("mapping-view-only", mapping_actions=("view",))
        response = self.open_workspace()

        row = self.mapping_row(response)
        self.assertEqual(row.save_reason, "You do not have permission to save this Location mapping.")
        self.assertContains(response, "You do not have permission to save this Location mapping.")
        refused = self.post_mapping(location_id=self.hall.pk)

        self.assertEqual(refused.status_code, 403)
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_an_add_constraint_admits_only_its_own_target(self):
        self.login(
            "mapping-add-constrained",
            mapping_actions=("view", "add"),
            mapping_constraints={"selected_location_id": self.hall.pk},
        )
        self.open_workspace()

        refused = self.post_mapping(location_id=self.other_hall.pk)
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(TraceLocationResolution.objects.exists())

        saved = self.post_mapping(location_id=self.hall.pk)
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(TraceLocationResolution.objects.get().selected_location_id, self.hall.pk)

    def test_change_is_checked_apart_from_add(self):
        existing = self.map_path(SOURCE_PATH, self.hall)
        self.login("mapping-no-change", mapping_actions=("view", "add", "delete"))
        response = self.open_workspace()
        self.assertEqual(
            self.mapping_row(response).save_reason, "You do not have permission to save this Location mapping."
        )

        refused = self.post_mapping(location_id=self.other_hall.pk)

        self.assertEqual(refused.status_code, 403)
        existing.refresh_from_db()
        self.assertEqual(existing.selected_location_id, self.hall.pk)

    def test_a_change_constraint_refuses_moving_the_row_outside_it(self):
        existing = self.map_path(SOURCE_PATH, self.hall)
        self.login(
            "mapping-change-constrained",
            mapping_actions=("view", "change"),
            mapping_constraints={"selected_location_id": self.hall.pk},
        )
        self.open_workspace()

        refused = self.post_mapping(location_id=self.other_hall.pk)

        self.assertEqual(refused.status_code, 403)
        existing.refresh_from_db()
        self.assertEqual(existing.selected_location_id, self.hall.pk)

    def test_delete_is_checked_apart_from_change(self):
        self.map_path(SOURCE_PATH, self.hall)
        self.login("mapping-no-delete", mapping_actions=("view", "add", "change"))
        response = self.open_workspace()
        self.assertEqual(
            self.mapping_row(response).clear_reason, "You do not have permission to clear this Location mapping."
        )
        self.assertContains(response, "You do not have permission to clear this Location mapping.")

        refused = self.post_mapping(clear="1")

        self.assertEqual(refused.status_code, 403)
        self.assertTrue(TraceLocationResolution.objects.exists())

    def test_delete_alone_clears_the_mapping(self):
        self.map_path(SOURCE_PATH, self.hall)
        self.login("mapping-delete", mapping_actions=("view", "delete"))
        response = self.open_workspace()
        self.assertEqual(self.mapping_row(response).clear_reason, "")

        cleared = self.post_mapping(clear="1")

        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_the_location_picker_offers_only_visible_locations_and_names_only_a_visible_parent(self):
        actor = user_with_object_permission(
            "location-picker-scoped",
            [
                *[grant for grant in _workspace_grants() if grant[0] is not Location],
                (Location, ("view",), {"name__in": ["DH4", "DH5", "T"]}),
            ],
        )
        self.client.force_login(actor)
        self.open_workspace()

        offered = self.location_candidates().json()

        self.assertEqual(
            offered["candidates"],
            [
                {"id": self.hall.pk, "name": "DH4", "parent": ""},
                {"id": self.other_hall.pk, "name": "DH5", "parent": ""},
                {"id": self.row.pk, "name": "T", "parent": "DH4"},
            ],
        )
        self.assertEqual(offered["total"], 3)

    def test_a_hidden_row_discloses_nothing_and_is_never_overwritten_blind(self):
        existing = self.map_path(SOURCE_PATH, self.hall, display="Hidden Row Snapshot")
        self.login(
            "mapping-row-hidden",
            mapping_actions=("view", "add", "change", "delete"),
            mapping_constraints={"source_location_key": "another path"},
        )
        response = self.open_workspace()

        row = self.mapping_row(response)
        self.assertEqual((row.state, row.location), ("hidden", ""))
        self.assertEqual(row.save_reason, "You cannot change a policy you cannot view.")
        self.assertEqual(row.clear_reason, "You cannot change a policy you cannot view.")
        self.assertNotContains(response, "Hidden Row Snapshot")
        conflicts = [
            fact
            for fact in self.device_candidates()[self.in_other_hall.pk]["conflicting_facts"]
            if fact["fact"] == "location"
        ]
        self.assertEqual(conflicts, [])

        for data in ({"location_id": self.other_hall.pk}, {"clear": "1"}):
            with self.subTest(data=data):
                refused = self.post_mapping(**data)

                self.assertEqual(refused.status_code, 400)
                self.assertEqual(refused.json()["error"], "You cannot change a policy you cannot view.")
                existing.refresh_from_db()
                self.assertEqual(existing.selected_location_id, self.hall.pk)


class LocationMappingRefusalTest(LocationWorkspaceMixin, TestCase):
    """Every refusal leaves the profile policy untouched."""

    @classmethod
    def setUpTestData(cls):
        cls.build_location_tree()

    def setUp(self):
        self.client.force_login(self.actor)

    def retained_sync_job(self, document_id):
        """Create the pending Job a trace sync that keeps this preview would hold."""
        import uuid

        from core.choices import JobStatusChoices
        from core.models import Job

        from netbox_data_import.jobs import ImportJobRunner

        return Job.objects.create(
            name=ImportJobRunner.name,
            user=self.actor,
            job_id=uuid.uuid4(),
            status=JobStatusChoices.STATUS_PENDING,
            data={
                "job_type": ImportJobRunner.job_type,
                "keeps_preview": True,
                "profile_id": self.profile.pk,
                "source_document_id": document_id,
            },
        )

    def test_without_a_preview_the_command_is_refused(self):
        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_location_mapping"),
            {"location_key": "DH4", "location_id": self.hall.pk},
        )

        self.assertEqual(refused.status_code, 409)
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_a_retained_sync_refuses_the_command(self):
        from netbox_data_import.preview_coordinator import RETAINED_SYNC_BLOCK_REASON

        self.open_workspace()
        coordinator = preview_coordinator(self.client)
        job = self.retained_sync_job(coordinator.source_document_id)
        PreviewCoordinator.objects.filter(pk=coordinator.pk).update(state=PreviewState.SYNC_PENDING, job_id=job.pk)

        refused = self.post_mapping(location_id=self.hall.pk)

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"], RETAINED_SYNC_BLOCK_REASON)
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_an_adapter_this_release_dropped_refuses_the_command(self):
        self.open_workspace()
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired-adapter")

        refused = self.post_mapping(location_id=self.hall.pk)

        self.assertEqual(refused.status_code, 409)
        self.assertIn("retired-adapter", refused.json()["error"])
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_a_lost_import_target_returns_to_setup(self):
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Mapping Tenant", slug="mapping-tenant")
        upload = BytesIO(trace_workbook_bytes(path_blocks=(located_path(SOURCE_PATH),)))
        upload.name = "traces.xlsx"
        upload_preview(
            self.client, {"profile": self.profile.pk, "site": self.site.pk, "tenant": tenant.pk, "excel_file": upload}
        )
        tenant.delete()

        refused = self.post_mapping(location_id=self.hall.pk, as_json=False)

        self.assertRedirects(refused, reverse("plugins:netbox_data_import:import_setup"), fetch_redirect_response=False)
        self.assertFalse(TraceLocationResolution.objects.exists())

    def test_a_row_whose_digest_names_another_key_is_refused(self):
        from netbox_data_import.trace_location_resolution import trace_location_mappings

        row = self.map_path(SOURCE_PATH, self.hall)
        TraceLocationResolution.objects.filter(pk=row.pk).update(source_location_key="another path")

        with self.assertRaisesMessage(ValueError, "digest does not match"):
            trace_location_mappings(
                profile=self.profile,
                reader=NetBoxReader.unrestricted().for_target(site=self.site),
                keys=(row.source_location_key,),
            )

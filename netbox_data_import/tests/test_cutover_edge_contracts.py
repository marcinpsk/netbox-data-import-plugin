# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Small boundary contracts retained after the import-engine cutover."""

from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase

from netbox_data_import.device_field_review import DeviceFieldReviewer
from netbox_data_import.device_identity import DeviceTypeIdentityResolver
from netbox_data_import.import_engine import EngineConfigurationError, ImportEngine, SelectionError, _resolution_section
from netbox_data_import.ip_assignment import IPAssignmentError, IPTarget, already_assigned, parse_address
from netbox_data_import.models import (
    ColumnMapping,
    DeviceImportSource,
    DeviceTypeMapping,
    ExecutionOutcome,
    FailureReason,
    IgnoredFieldDifference,
    ImportExecution,
    ImportProfile,
    ManufacturerMapping,
    SourceDocument,
    SourceResolution,
    _validated_contact_id,
    validate_contact_candidate_resolution,
)
from netbox_data_import.netbox_reader import PlanningTargetUnavailable
from netbox_data_import.object_permissions import ObjectPermissionDenied
from netbox_data_import.plan import Disposition, ImportPlan, PlannedChange, SynchronizationUnit
from netbox_data_import.preview_row_actions import current_preview_revision
from netbox_data_import.source_resolution import derive_effective_rows
from netbox_data_import.target_modules import PreconditionFailed
from netbox_data_import.tests.helpers import workbook_bytes
from netbox_data_import.values import comparison_key, normalize_for_compare, source_position


class ValueAndReviewBoundaryTest(SimpleTestCase):
    """Plan display and field snapshots contain stable JSON-friendly scalar values."""

    def test_value_normalization_handles_fractional_invalid_and_nonfinite_inputs(self):
        """Numeric source boundaries preserve fractions and reject unsafe values."""
        self.assertEqual(source_position("7.5"), 7.5)
        self.assertIsNone(source_position("Infinity"))
        self.assertEqual(normalize_for_compare("7.5"), "7.5")
        marker = object()
        self.assertEqual(normalize_for_compare(marker), str(marker))
        self.assertEqual(comparison_key("serial", None), "")
        self.assertEqual(comparison_key("serial", " value "), "value")

    def test_ip_target_exposes_placement_and_refuses_a_missing_held_row(self):
        """IP presentation distinguishes an intended interface from an existing assignment."""
        interface = SimpleNamespace(name="mgmt")
        pending = IPTarget(address="198.18.0.10/32", interface=interface, existing=None, already_held=False)
        self.assertEqual(pending.placement, "would go to mgmt")
        with self.assertRaises(IPAssignmentError):
            _ = pending.held

        held = IPTarget(
            address="198.18.0.10/32",
            interface=interface,
            existing=SimpleNamespace(pk=1),
            already_held=True,
        )
        self.assertIn("already on mgmt", held.placement)
        self.assertEqual(held.summary, "198.18.0.10/32 on mgmt")
        self.assertEqual(parse_address(""), None)

    def test_field_review_public_api_normalizes_fallback_and_location_values(self):
        """Every review value reaches snapshots through the shared field registry."""
        rack = SimpleNamespace(name="rack-a", location_id=7, location="Room A")
        device = SimpleNamespace(
            rack_id=1,
            rack=rack,
            position=Decimal("5.0"),
            name="stored-name",
            tenant=None,
        )

        rack_snapshot = DeviceFieldReviewer.current_snapshot(device, "rack_name")
        self.assertEqual(rack_snapshot, {"canonical": "7:rack-a", "display": "Room A / rack-a"})
        self.assertIsNone(DeviceFieldReviewer.current_snapshot(device, "unknown"))

        definition = DeviceFieldReviewer.definition("device_type")
        assert definition is not None
        self.assertEqual(definition.snapshot("raw-type"), {"canonical": "raw-type", "display": "raw-type"})
        serial_definition = DeviceFieldReviewer.definition("serial")
        position_definition = DeviceFieldReviewer.definition("u_position")
        assert serial_definition is not None
        assert position_definition is not None
        self.assertEqual(serial_definition.snapshot(None), {"canonical": "", "display": ""})
        self.assertEqual(position_definition.snapshot(1.5), {"canonical": "1.5", "display": "1.5"})
        differences = DeviceFieldReviewer.field_diff(
            device,
            {"u_position": object(), "device_name": "new-name", "u_height": None},
            include_informational=True,
        )
        self.assertIn("u_position", differences)
        self.assertIn("device_name", differences)
        self.assertNotIn("device_name", DeviceFieldReviewer.field_diff(device, {"device_name": "new-name"}))

    def test_preview_revision_is_created_once_for_a_new_session(self):
        """A preview session gets one stable revision until an action retires it."""
        session = {}

        first = current_preview_revision(session)

        self.assertEqual(current_preview_revision(session), first)


class IdentityAndResolutionBoundaryTest(TestCase):
    """Identity mappings and saved source resolutions have deterministic precedence."""

    def test_duplicate_mapping_rows_keep_the_first_exact_identity(self):
        """Batch indexes do not let a later duplicate policy row replace the first one."""
        first = SimpleNamespace(
            source_make="Make",
            source_model="Model",
            netbox_manufacturer_slug="first-make",
            netbox_device_type_slug="first-model",
        )
        second = SimpleNamespace(
            source_make="Make",
            source_model="Model",
            netbox_manufacturer_slug="second-make",
            netbox_device_type_slug="second-model",
        )
        resolver = DeviceTypeIdentityResolver([first, second], [])

        self.assertEqual(resolver.resolve("Make", "Model"), ("first-make", "first-model", True))

    def test_mapping_identity_is_case_insensitive_on_both_sides(self):
        """Source casing cannot bypass an explicit Device Type or manufacturer mapping."""
        profile = ImportProfile.objects.create(name="Persisted Identity Mapping Profile")
        DeviceTypeMapping.objects.create(
            profile=profile,
            source_make="Dell",
            source_model="R660",
            netbox_manufacturer_slug="mapped-make",
            netbox_device_type_slug="mapped-type",
        )
        ManufacturerMapping.objects.create(
            profile=profile,
            source_make="ACME",
            netbox_manufacturer_slug="mapped-manufacturer",
        )
        resolver = DeviceTypeIdentityResolver.for_profile(profile)

        self.assertEqual(resolver.resolve("dell", "r660"), ("mapped-make", "mapped-type", True))
        self.assertEqual(
            resolver.resolve("acme", "widget"),
            ("mapped-manufacturer", "acme-widget", False),
        )

    def test_resolution_copies_conflicts_and_clears_an_omitted_mapped_value(self):
        """A resolution changes a detached row and clears its superseded target value."""
        profile = ImportProfile.objects.create(name="Resolution Boundary Profile")
        ColumnMapping.objects.create(profile=profile, source_column="Raw", target_field="serial")
        SourceResolution.objects.create(
            profile=profile,
            source_id="BOUNDARY-1",
            source_column="Raw",
            original_value="OLD",
            resolved_fields={"device_name": "resolved-name"},
        )
        rows = [
            {
                "source_id": "BOUNDARY-1",
                "Raw": "OLD",
                "serial": "OLD",
                "_conflicts": {"device_name": ["a", "b"], "serial": ["OLD"]},
            }
        ]

        effective = derive_effective_rows(rows, profile)

        self.assertEqual(effective[0]["device_name"], "resolved-name")
        self.assertIsNone(effective[0]["serial"])
        self.assertEqual(effective[0]["_conflicts"], {"serial": ["OLD"]})
        self.assertNotIn("device_name", rows[0])

    def test_a_legacy_non_mapping_resolution_does_not_break_planning(self):
        """A malformed stored JSON value is ignored instead of reaching dict operations."""
        profile = ImportProfile.objects.create(name="Malformed Resolution Boundary Profile")
        SourceResolution.objects.create(
            profile=profile,
            source_id="BOUNDARY-2",
            source_column="Raw",
            original_value="OLD",
            resolved_fields=["not", "a", "mapping"],
        )
        rows = [{"source_id": "BOUNDARY-2", "device_name": "unchanged"}]

        self.assertEqual(derive_effective_rows(rows, profile), rows)

    def test_resolution_validation_rejects_reserved_and_unknown_target_fields(self):
        """A saved decision cannot name planning internals or fields outside the catalog."""
        profile = ImportProfile.objects.create(name="Resolution Key Validation Profile")
        invalid_fields = ({"source_id": "REPLACED"}, {"_conflicts": {}}, {"unknown": "value"})

        for resolved_fields in invalid_fields:
            resolution = SourceResolution(
                profile=profile,
                source_id="BOUNDARY-3",
                source_column="Raw",
                original_value="OLD",
                resolved_fields=resolved_fields,
            )
            with self.subTest(resolved_fields=resolved_fields), self.assertRaises(ValidationError):
                resolution.full_clean()

    def test_a_legacy_resolution_cannot_replace_planning_internals(self):
        """Planning ignores a stored mapping that predates resolution-key validation."""
        profile = ImportProfile.objects.create(name="Legacy Resolution Key Profile")
        SourceResolution.objects.create(
            profile=profile,
            source_id="BOUNDARY-4",
            source_column="Raw",
            original_value="OLD",
            resolved_fields={"source_id": "REPLACED", "_conflicts": {}},
        )
        rows = [
            {
                "source_id": "BOUNDARY-4",
                "device_name": "unchanged",
                "_conflicts": {"device_name": ["a", "b"]},
            }
        ]

        self.assertEqual(derive_effective_rows(rows, profile), rows)

    def test_contact_id_validation_rejects_values_integer_coercion_would_reshape(self):
        """Only positive integral Contact IDs cross the saved-resolution boundary."""
        for value in (True, 1.5, "not-an-id", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                _validated_contact_id(value)
        self.assertEqual(_validated_contact_id("2"), 2)

    def test_contact_candidate_validation_rejects_each_malformed_section(self):
        """Saved Contact policy has one strict shape before a resolver can consume it."""
        base = {
            "contact_resolution_applied": True,
            "contact_field_sources": {"name": "Name", "email": "Email"},
            "contact_field_values": {},
            "contact_id": None,
        }
        invalid = (
            {},
            {**base, "contact_resolution_applied": False},
            {**base, "contact_field_sources": {"unknown": "Name"}},
            {**base, "contact_field_sources": {"name": ""}},
            {**base, "contact_field_sources": {"name": "Missing", "email": "Email"}},
            {**base, "contact_field_values": {"unknown": "value"}},
            {**base, "contact_field_values": {"name": " "}, "contact_field_sources": {}},
            {**base, "contact_field_values": {"name": "Literal"}},
            {
                **base,
                "contact_field_sources": {"email": "Email"},
                "contact_field_values": {},
            },
            {
                **base,
                "contact_field_sources": {"name": "Name"},
                "contact_field_values": {},
            },
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                validate_contact_candidate_resolution(fields, "email", {"Name", "Email"})

    def test_unknown_adapter_and_retained_model_display_paths_are_explicit(self):
        """Corrupt adapter state fails, while retained audit and provenance rows stay readable."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        unknown = ImportProfile(name="Unknown Adapter Profile", source_adapter="missing", adapter_config={})
        self.assertEqual(unknown.adapter_config_display, [])
        with self.assertRaises(ValidationError):
            unknown.clean()
        with self.assertRaises(ValidationError):
            unknown.save()

        profile = ImportProfile.objects.create(name="Retained Display Profile")
        document = SourceDocument.store(profile=profile, content=b"stored-source")
        self.assertTrue(str(document).startswith("upload ("))

        site = Site.objects.create(name="Retained Display Site", slug="retained-display-site")
        manufacturer = Manufacturer.objects.create(name="Retained Display Make", slug="retained-display-make")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Retained Display Model",
            slug="retained-display-model",
            u_height=1,
        )
        role = DeviceRole.objects.create(name="Retained Display Role", slug="retained-display-role")
        device = Device.objects.create(
            name="retained-display-device",
            site=site,
            device_type=device_type,
            role=role,
        )
        ignored = IgnoredFieldDifference.objects.create(
            profile=profile,
            source_id="RETAINED-1",
            netbox_device_id=device.pk,
            target_field="serial",
        )
        provenance = DeviceImportSource.objects.create(device=device, profile=profile)
        self.assertIn("RETAINED-1/serial", str(ignored))
        self.assertIn("(no source ID)", str(provenance))


class CoordinatorDefensiveContractTest(TestCase):
    """Coordinator failure classification remains complete and deterministic."""

    def test_missing_resolution_policy_fails_fast(self):
        """The engine cannot apply saved policy without its catalog declaration."""
        from netbox_data_import import catalog

        section = catalog._SECTIONS_BY_KEY.pop("source_resolutions")
        self.addCleanup(catalog._SECTIONS_BY_KEY.__setitem__, "source_resolutions", section)

        with self.assertRaises(EngineConfigurationError):
            _resolution_section()

    def test_every_expected_failure_has_an_audit_reason(self):
        """Typed target and database failures map to stable audit reasons."""
        cases = (
            (ObjectPermissionDenied("denied"), FailureReason.PERMISSION),
            (ValidationError("invalid"), FailureReason.VALIDATION),
            (DatabaseError("database"), FailureReason.DATABASE),
            (PlanningTargetUnavailable("target"), FailureReason.PLANNING),
            (RuntimeError("unknown"), FailureReason.PLANNING),
            (PreconditionFailed("changed"), FailureReason.PRECONDITION),
        )
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(ImportEngine._failure_reason(failure), expected)

    def test_duplicate_selection_and_a_concurrent_failure_marker_are_bounded(self):
        """A unit is selected once, and an already-finished audit row stays finished."""
        plan = ImportPlan(
            units=(SynchronizationUnit(identity="device:one", disposition=Disposition.ACTIONABLE),),
            source_fingerprint="0" * 64,
            profile_fingerprint="1" * 64,
            actor="1",
        )
        with self.assertRaises(SelectionError):
            ImportEngine._selected_units(plan, plan, ["device:one", "device:one"])

        execution = ImportExecution.objects.create(
            profile=ImportProfile.objects.create(name="Finished Audit Profile"),
            outcome=ExecutionOutcome.PENDING,
        )
        execution.mark_succeeded(applied_changes={"changes": ["device:one:create"], "deleted": []})
        ImportEngine._mark_failed(execution, reason=FailureReason.PLANNING)
        execution.refresh_from_db()
        self.assertEqual(execution.outcome, ExecutionOutcome.SUCCEEDED)

    def test_an_unregistered_runtime_is_refused_before_any_write(self):
        """The coordinator cannot silently skip an executable change without a runtime."""
        from netbox_data_import import target_modules
        from dcim.models import Site

        site = Site.objects.create(name="Runtime Boundary Site", slug="runtime-boundary-site")
        actor = get_user_model().objects.create_superuser(
            username="runtime-boundary-operator",
            email="runtime-boundary@example.invalid",
            password="testpass",
        )
        profile = ImportProfile.objects.create(
            name="Runtime Boundary Profile",
            adapter_config={"sheet_name": "Data"},
        )
        document = SourceDocument.store(
            profile=profile,
            content=workbook_bytes(["Source ID"], [["GHOST-1"]]),
            uploaded_by=actor,
        )
        change = PlannedChange(
            identity="ghost:one:create",
            target_module="ghost",
            operation="create",
            payload={},
        )
        unit = SynchronizationUnit(
            identity="ghost:one",
            disposition=Disposition.ACTIONABLE,
            changes=(change,),
        )

        class GhostPlanningRuntime:
            @staticmethod
            def plan(*args):
                return [unit]

            @staticmethod
            def apply(*args):
                raise AssertionError("the unregistered ghost runtime cannot be applied")

        runtime = target_modules.MODULE_RUNTIMES["rack"]
        target_modules.MODULE_RUNTIMES["rack"] = GhostPlanningRuntime()
        self.addCleanup(target_modules.MODULE_RUNTIMES.__setitem__, "rack", runtime)
        planning_context = {"site_id": site.pk, "location_id": None, "tenant_id": None}
        plan = ImportEngine.plan(profile, document, actor, planning_context)
        execution = ImportExecution.objects.create(profile=profile, outcome=ExecutionOutcome.PENDING)
        with self.assertRaises(EngineConfigurationError):
            ImportEngine._write_selection(
                execution,
                profile,
                document,
                plan,
                ["ghost:one"],
                actor,
                None,
            )
        execution.refresh_from_db()
        self.assertEqual(execution.outcome, ExecutionOutcome.PENDING)
        self.assertIsNone(execution.applied_changes)

    def test_invalid_current_ip_state_is_not_treated_as_settled(self):
        """Malformed target data cannot make an address precondition look satisfied."""
        device = SimpleNamespace(pk=1, primary_ip4=SimpleNamespace(address="invalid", pk=1))
        self.assertFalse(already_assigned(device, "primary_ip4", "198.18.0.1/32"))

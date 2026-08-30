# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Import Engine coordinates stored sources, adapters, readers, and Target Modules."""

from io import BytesIO

import openpyxl
from django.test import TestCase

import netbox_data_import.adapters as adapter_registry
from netbox_data_import.adapters import (
    FlatWorkbookAdapter,
    SourceBatch,
    SourceDiagnostic,
    UnknownSourceAdapter,
)
from netbox_data_import.catalog import TARGET_MODULES, TargetModuleKey
from netbox_data_import.import_engine import ImportEngine, StaleSourceDocument
from netbox_data_import.netbox_reader import PlanningTargetUnavailable
from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    IgnoredDevice,
    ImportProfile,
    SourceDocument,
    SourceResolution,
)
from netbox_data_import.plan import Disposition, ImportPlan, Severity, executable_units, merge_changes
from netbox_data_import.target_modules import MODULE_RUNTIMES, runtime_for
from netbox_data_import.tests.helpers import make_dcim_objects, user_with_object_permission


def _workbook(*rows) -> bytes:
    """Return one stored-source workbook with the coordinator test columns."""
    book = openpyxl.Workbook()
    sheet = book.worksheets[0]
    sheet.title = "Data"
    sheet.append(["Source ID", "Class", "Name", "Rack", "Make", "Model", "Height"])
    for row in rows:
        sheet.append(list(row))
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class _DiagnosticFlatWorkbookAdapter(FlatWorkbookAdapter):
    """Return a real flat batch with one adapter diagnostic."""

    @classmethod
    def interpret(cls, content, adapter_config, *, collect_unused=False) -> SourceBatch:
        """Interpret the content and attach one source warning."""
        batch = super().interpret(content, adapter_config, collect_unused=collect_unused)
        return SourceBatch(
            output_kinds=batch.output_kinds,
            rows=batch.rows,
            diagnostics=(
                SourceDiagnostic(
                    code="flat_workbook.uncertain_value", message="Check this source value.", row_number=2
                ),
            ),
            unused_columns=batch.unused_columns,
        )


class ImportEngineTestDataMixin:
    """Create the stored workbook, profile policy, target state, and actor for coordinator tests."""

    def setUp(self):
        """Create the profile policy and target state used by each plan."""
        from dcim.models import Rack

        self.site, self.manufacturer, self.device_type, self.role = make_dcim_objects("Coordinator")
        # The identity resolver derives '<manufacturer>-<model>', so a row only resolves to that slug.
        self.device_type.slug = f"{self.manufacturer.slug}-{self.device_type.slug}"
        self.device_type.save(update_fields=["slug"])
        self.rack = Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        self.profile = ImportProfile.objects.create(
            name="Coordinator Profile",
            adapter_config={"sheet_name": "Data", "update_existing": True},
        )
        for source_column, target_field in (
            ("Source ID", "source_id"),
            ("Class", "device_class"),
            ("Name", "device_name"),
            ("Rack", "rack_name"),
            ("Make", "make"),
            ("Model", "model"),
            ("Height", "u_height"),
        ):
            ColumnMapping.objects.create(
                profile=self.profile,
                source_column=source_column,
                target_field=target_field,
            )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            role_slug=self.role.slug,
        )
        self.content = _workbook(
            ("R-1", "Cabinet", "", self.rack.name, "", "", 42),
            (
                "D-1",
                "Server",
                "server-a",
                self.rack.name,
                self.manufacturer.name,
                self.device_type.model,
                1,
            ),
        )
        self.document = SourceDocument.store(profile=self.profile, content=self.content, filename="source.xlsx")
        self.planning_context = {"site_id": self.site.pk, "location_id": None, "tenant_id": None}
        self.actor = user_with_object_permission("coordinator-operator", self._planning_grants())

    @staticmethod
    def _planning_grants(overrides=None):
        """Return the object permissions an operator needs to plan against this target."""
        from dcim.models import Device, Rack, Site

        grants = {Site: {}, Rack: {}, Device: {}}
        grants.update(overrides or {})
        return [
            (
                model,
                ("view",) if model is Site else ("view", "add", "change"),
                constraints,
            )
            for model, constraints in grants.items()
        ]

    def _plan(self, document=None, actor=None, planning_context=None):
        """Plan the default document with the shared target context."""
        return ImportEngine.plan(
            self.profile,
            document or self.document,
            actor or self.actor,
            planning_context or self.planning_context,
        )


class ImportEnginePlanTest(ImportEngineTestDataMixin, TestCase):
    """Planning uses one stored source and produces one deterministic target-neutral plan."""

    def test_one_batch_fans_out_in_catalog_order(self):
        """One interpreted batch supplies units from both implemented modules."""
        plan = self._plan()

        self.assertEqual(
            [unit.identity for unit in plan.units],
            ["device:source:D-1", "rack:source:R-1"],
        )
        self.assertEqual(plan.actor, str(self.actor.pk))
        self.assertEqual(plan.planning_context, self.planning_context)
        # A blocked device row would also carry these identities, so prove the row really planned.
        self.assertEqual(plan.unit("device:source:D-1").changes[0].operation, "create")

    def test_a_device_can_use_the_rack_its_batch_creates(self):
        """One stored workbook can create a rack and place a device in it."""
        self.rack.delete()

        unit = self._plan().unit("device:source:D-1")

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE, unit.diagnostics)
        self.assertNotIn("device.rack_missing", [diagnostic.code for diagnostic in unit.diagnostics])

    def test_a_device_change_runs_after_its_batch_rack_change(self):
        """The rack change identity orders the two independent target modules."""
        self.rack.delete()

        plan = self._plan()
        rack_change = plan.unit("rack:source:R-1").changes[0]
        device_change = plan.unit("device:source:D-1").changes[0]
        ordered = merge_changes(executable_units(plan.units))

        self.assertEqual(device_change.dependencies, (rack_change.identity,))
        self.assertIsNone(device_change.payload["rack_id"])
        self.assertEqual(device_change.payload["rack_name"], "rack-a")
        self.assertEqual([change.identity for change in ordered], [rack_change.identity, device_change.identity])

    def test_merged_rack_and_device_changes_apply_in_dependency_order(self):
        """Applying the merged changes places the device in the new rack."""
        from django.contrib.auth import get_user_model
        from django.db import transaction

        from dcim.models import Device, Rack

        from netbox_data_import.netbox_reader import NetBoxReader
        from netbox_data_import.target_modules import ExecutionContext

        self.rack.delete()
        actor = get_user_model().objects.create_superuser(
            username="coordinator-writer",
            email="coordinator-writer@example.invalid",
            password="testpass",
        )
        plan = self._plan(actor=actor)
        context = ExecutionContext(
            actor=actor,
            reader=NetBoxReader.for_actor(actor).for_planning_context(self.planning_context),
            profile=self.profile,
        )

        with transaction.atomic():
            for change in merge_changes(executable_units(plan.units)):
                runtime = runtime_for(change.target_module)
                self.assertIsNotNone(runtime)
                runtime.apply(change, context)

        rack = Rack.objects.get(name="rack-a", site=self.site)
        device = Device.objects.get(name="server-a", site=self.site)
        self.assertEqual(device.rack_id, rack.pk)

    def test_saved_source_resolution_supplies_a_missing_device_name(self):
        """A saved row decision supplies a name before the Device Module plans."""
        document = SourceDocument.store(
            profile=self.profile,
            content=_workbook(
                (
                    "D-RESOLVED",
                    "Server",
                    "",
                    self.rack.name,
                    self.manufacturer.name,
                    self.device_type.model,
                    1,
                )
            ),
            filename="resolved-source.xlsx",
        )
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="D-RESOLVED",
            source_column="Name",
            original_value="",
            resolved_fields={"device_name": "resolved-server"},
        )

        unit = self._plan(document).unit("device:source:D-RESOLVED")

        self.assertNotIn("device.missing_name", [diagnostic.code for diagnostic in unit.diagnostics])
        self.assertEqual(unit.changes[0].operation, "create")

    def test_deleting_source_resolution_restores_the_missing_name_diagnostic(self):
        """Deleting the saved row decision makes the unchanged source row invalid again."""
        document = SourceDocument.store(
            profile=self.profile,
            content=_workbook(
                (
                    "D-DELETED",
                    "Server",
                    "",
                    self.rack.name,
                    self.manufacturer.name,
                    self.device_type.model,
                    1,
                )
            ),
            filename="deleted-resolution-source.xlsx",
        )
        resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="D-DELETED",
            source_column="Name",
            original_value="",
            resolved_fields={"device_name": "temporary-name"},
        )
        resolved = self._plan(document).unit("device:source:D-DELETED")

        resolution.delete()
        unresolved = self._plan(document).unit("device:source:D-DELETED")

        self.assertEqual(resolved.changes[0].operation, "create")
        self.assertEqual([diagnostic.code for diagnostic in unresolved.diagnostics], ["device.missing_name"])
        self.assertEqual(unresolved.changes, ())

    def test_replanning_unchanged_input_is_deterministic(self):
        """Equivalent inputs keep the plan fingerprint and unit order."""
        first = self._plan()
        second = self._plan()

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(
            [unit.identity for unit in first.units],
            [unit.identity for unit in second.units],
        )

    def test_source_resolution_replanning_keeps_the_stored_source_pristine(self):
        """Resolution planning is repeatable and leaves the stored workbook unchanged."""
        content = _workbook(
            (
                "D-PRISTINE",
                "Server",
                "",
                self.rack.name,
                self.manufacturer.name,
                self.device_type.model,
                1,
            )
        )
        document = SourceDocument.store(
            profile=self.profile,
            content=content,
            filename="pristine-source.xlsx",
        )
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="D-PRISTINE",
            source_column="Name",
            original_value="",
            resolved_fields={"device_name": "pristine-server"},
        )

        first = self._plan(document)
        second = self._plan(document)
        document.refresh_from_db()

        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(bytes(document.content), content)

    def test_policy_changes_invalidate_the_plan_and_every_unit(self):
        """A policy edit changes the profile, plan, and selection fingerprints."""
        before = self._plan()
        before_profile = self.profile.planning_fingerprint
        before_units = {unit.identity: before.unit_fingerprint(unit.identity) for unit in before.units}

        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1", device_name="server-a")
        IgnoredDevice.objects.create(profile=self.profile, source_id="R-1", device_name="rack-a")
        after = self._plan()

        self.assertNotEqual(before_profile, self.profile.planning_fingerprint)
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(set(before_units), {unit.identity for unit in after.units})
        for unit in after.units:
            self.assertNotEqual(before_units[unit.identity], after.unit_fingerprint(unit.identity))

        stable = self.profile.planning_fingerprint
        self.profile.ignored_devices.all().delete()
        IgnoredDevice.objects.create(profile=self.profile, source_id="R-1", device_name="rack-a")
        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1", device_name="server-a")
        self.assertEqual(stable, self.profile.planning_fingerprint)

    def test_saving_source_resolution_changes_plan_and_unit_fingerprints(self):
        """A saved row decision invalidates the plan and its affected unit selection."""
        before = self._plan()

        SourceResolution.objects.create(
            profile=self.profile,
            source_id="D-1",
            source_column="Name",
            original_value="server-a",
            resolved_fields={"device_name": "resolved-server-a"},
        )
        after = self._plan()

        self.assertNotEqual(before.profile_fingerprint, after.profile_fingerprint)
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertNotEqual(
            before.unit_fingerprint("device:source:D-1"),
            after.unit_fingerprint("device:source:D-1"),
        )

    def test_policy_fingerprint_is_independent_of_database_row_order(self):
        """Equivalent policy rows fingerprint equally in any insertion order."""
        ColumnMapping.objects.create(profile=self.profile, source_column="Serial A", target_field="serial")
        ColumnMapping.objects.create(profile=self.profile, source_column="Serial B", target_field="serial")
        first = self.profile.planning_fingerprint

        self.profile.column_mappings.filter(target_field="serial").delete()
        ColumnMapping.objects.create(profile=self.profile, source_column="Serial B", target_field="serial")
        ColumnMapping.objects.create(profile=self.profile, source_column="Serial A", target_field="serial")

        self.assertEqual(first, self.profile.planning_fingerprint)

    def test_source_fingerprint_depends_on_content_not_upload_identity(self):
        """An equal re-upload keeps the plan while changed content invalidates it."""
        same = SourceDocument.store(profile=self.profile, content=self.content, filename="same.xlsx")
        different = SourceDocument.store(
            profile=self.profile,
            content=_workbook(("R-2", "Cabinet", "", "rack-b", "", "", 42)),
            filename="different.xlsx",
        )

        self.assertEqual(self._plan().fingerprint, self._plan(same).fingerprint)
        self.assertNotEqual(self._plan().fingerprint, self._plan(different).fingerprint)

    def test_a_deleted_source_document_is_stale(self):
        """Planning requires the stored bytes to remain available."""
        self.document.delete()

        with self.assertRaises(StaleSourceDocument):
            self._plan()

    def test_an_unregistered_source_adapter_is_rejected(self):
        """A profile cannot plan through an adapter this release does not register."""
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired_adapter")
        self.profile.refresh_from_db()

        with self.assertRaisesMessage(UnknownSourceAdapter, "retired_adapter"):
            self._plan()

    def test_the_reader_is_bound_to_the_actor(self):
        """A device hidden from the actor is refused, never duplicated."""
        from dcim.models import Device

        Device.objects.create(
            name="server-a",
            site=self.site,
            rack=self.rack,
            device_type=self.device_type,
            role=self.role,
        )
        actor = user_with_object_permission(
            "coordinator-actor",
            self._planning_grants({Device: {"name": "another-device"}}),
        )

        unrestricted = self._plan().unit("device:source:D-1")
        scoped_plan = self._plan(actor=actor)
        scoped = scoped_plan.unit("device:source:D-1")

        self.assertEqual(unrestricted.changes[0].operation, "update")
        self.assertEqual(scoped.disposition, Disposition.INVALID)
        self.assertEqual(scoped.diagnostics[0].code, "device.inaccessible_match")
        self.assertEqual(scoped_plan.actor, str(actor.pk))

    def test_an_adapter_diagnostic_becomes_a_plan_diagnostic(self):
        """The coordinator keeps a source warning outside every unit."""
        original = adapter_registry._ADAPTERS_BY_KEY[FlatWorkbookAdapter.key]
        adapter_registry._ADAPTERS_BY_KEY[FlatWorkbookAdapter.key] = _DiagnosticFlatWorkbookAdapter
        self.addCleanup(adapter_registry._ADAPTERS_BY_KEY.__setitem__, FlatWorkbookAdapter.key, original)

        plan = self._plan()

        self.assertEqual(len(plan.diagnostics), 1)
        self.assertEqual(plan.diagnostics[0].code, "flat_workbook.uncertain_value")
        self.assertEqual(plan.diagnostics[0].severity, Severity.WARNING)
        self.assertEqual(plan.diagnostics[0].identities, ())
        self.assertEqual(
            plan.diagnostics[0].display,
            {"message": "Check this source value.", "row_number": 2},
        )
        self.assertTrue(all(plan.diagnostics[0] not in unit.diagnostics for unit in plan.units))

    def test_a_document_another_profile_owns_is_refused(self):
        """A stored source belongs to the profile it was uploaded for."""
        other = ImportProfile.objects.create(name="Other Profile", adapter_config={"sheet_name": "Data"})
        borrowed = SourceDocument.store(profile=other, content=self.content, filename="borrowed.xlsx")

        with self.assertRaises(StaleSourceDocument):
            self._plan(borrowed)

    def test_planning_without_an_actor_is_refused(self):
        """Planning is permission-scoped, so an unscoped read is never an accident."""
        with self.assertRaises(ValueError):
            ImportEngine.plan(self.profile, self.document, None, self.planning_context)

    def test_a_target_the_actor_cannot_view_is_refused(self):
        """The planning target resolves through the actor's own scope."""
        from dcim.models import Site

        blinkered = user_with_object_permission(
            "coordinator-blinkered",
            self._planning_grants({Site: {"name": "elsewhere"}}),
        )

        with self.assertRaises(PlanningTargetUnavailable):
            self._plan(actor=blinkered)

    def test_a_planning_context_with_no_site_is_refused(self):
        """The import writes into one site, so the context has to name it."""
        with self.assertRaises(PlanningTargetUnavailable):
            self._plan(planning_context={"site_id": None, "location_id": None, "tenant_id": None})

    def test_a_location_outside_the_target_site_is_refused(self):
        """A visible location from another site cannot form an import target with this site."""
        from dcim.models import Location, Site
        from django.contrib.auth import get_user_model

        other_site = Site.objects.create(name="Other Coordinator Site", slug="other-coordinator-site")
        other_location = Location.objects.create(
            name="Other Coordinator Location",
            slug="other-coordinator-location",
            site=other_site,
        )
        actor = get_user_model().objects.create_superuser(
            username="coordinator-cross-site",
            email="coordinator-cross-site@example.invalid",
            password="testpass",
        )

        with self.assertRaisesMessage(PlanningTargetUnavailable, "does not belong"):
            self._plan(
                actor=actor,
                planning_context={
                    "site_id": self.site.pk,
                    "location_id": other_location.pk,
                    "tenant_id": None,
                },
            )

    def test_two_profiles_holding_one_policy_do_not_share_a_fingerprint(self):
        """Section 4.5 invalidates every selection when the Import Profile changes."""
        twin = ImportProfile.objects.create(
            name="Twin Profile",
            adapter_config=dict(self.profile.adapter_config),
        )

        self.assertNotEqual(self.profile.planning_fingerprint, twin.planning_fingerprint)

    def test_a_module_returning_a_dangling_dependency_fails_planning(self):
        """Section 4.4 makes the merged graph the coordinator's to reject."""
        from netbox_data_import import target_modules as runtimes
        from netbox_data_import.plan import Disposition, PlanInvalid, PlannedChange, SynchronizationUnit

        class _DanglingModule:
            """Return one unit whose change depends on a change no unit supplies."""

            key = runtimes.RackModule.key
            consumes = runtimes.RackModule.consumes

            def plan(self, source_batch, profile, catalog, netbox_reader):
                """Return the one unreferenceable unit this test needs."""
                return [
                    SynchronizationUnit(
                        identity="rack:source:R-1",
                        disposition=Disposition.ACTIONABLE,
                        changes=(
                            PlannedChange(
                                identity="rack:source:R-1:create",
                                target_module=self.key,
                                operation="create",
                                payload={},
                                dependencies=("rack:source:missing:create",),
                            ),
                        ),
                    )
                ]

        original = runtimes.MODULE_RUNTIMES[runtimes.RackModule.key]
        runtimes.MODULE_RUNTIMES[runtimes.RackModule.key] = _DanglingModule()
        self.addCleanup(runtimes.MODULE_RUNTIMES.__setitem__, runtimes.RackModule.key, original)

        with self.assertRaisesMessage(PlanInvalid, "rack:source:missing:create"):
            self._plan()

    def test_a_real_plan_round_trips_with_its_fingerprint(self):
        """The coordinator output survives the storage-neutral serialization seam."""
        plan = self._plan()

        restored = ImportPlan.from_dict(plan.to_dict())

        self.assertEqual(restored, plan)
        self.assertEqual(restored.fingerprint, plan.fingerprint)


class TargetModuleRuntimeRegistryTest(TestCase):
    """The runtime registry cannot drift from the static Target Module declarations."""

    def test_runtime_keys_are_declared_and_every_implemented_module_is_wired(self):
        """Catalog declarations and runtime availability agree for this release."""
        declared_key_values = {
            value
            for name, value in vars(TargetModuleKey).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        implemented = {module.key for module in TARGET_MODULES if module.implemented}

        self.assertLessEqual(set(MODULE_RUNTIMES), declared_key_values)
        self.assertLessEqual(implemented, set(MODULE_RUNTIMES))
        for key, runtime in MODULE_RUNTIMES.items():
            self.assertIs(runtime_for(key), runtime)

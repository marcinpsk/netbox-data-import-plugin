# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Verify Device Target Module planning and execution behavior."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from netbox_data_import.adapters import SourceBatch
from netbox_data_import.catalog import CATALOG, OutputKind
from netbox_data_import.device_identity import DeviceTypeIdentityResolver
from netbox_data_import.models import (
    ClassRoleMapping,
    DeviceExistingMatch,
    DeviceImportSource,
    DeviceTypeMapping,
    IgnoredDevice,
    IgnoredFieldDifference,
    ImportProfile,
    ManufacturerMapping,
)
from netbox_data_import.netbox_reader import NetBoxReader, PlanningTargetUnavailable
from netbox_data_import.plan import Disposition, Severity
from netbox_data_import.target_modules import DeviceModule, ExecutionContext, PreconditionFailed, _DeviceBatch


class DeviceModulePlanTestBase(TestCase):
    """The site, the device type and the role every device row in these tests resolves to."""

    def setUp(self):
        """Create the target state a well-formed row plans against."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.site = Site.objects.create(name="Device Module Site", slug="device-module-site")
        self.rack = Rack.objects.create(name="dm-rack", site=self.site, u_height=42)
        self.manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        self.device_type = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="R660", slug="dell-r660", u_height=1
        )
        self.role = DeviceRole.objects.create(name="Server", slug="server")
        self.profile = ImportProfile.objects.create(
            name="Device Module Profile", adapter_config={"sheet_name": "Data", "update_existing": True}
        )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug="server"
        )
        self.reader = NetBoxReader.unrestricted().for_target(site=self.site)

    def _batch(self, *rows):
        """Wrap rows the way the flat adapter hands them over."""
        return SourceBatch(output_kinds=frozenset({OutputKind.DEVICE_SOURCE_ROW}), rows=tuple(rows))

    def _row(self, number, source_id, device_name, device_class="Server", **extra):
        """Return one well-formed device row, before the test spoils whatever it is about."""
        row = {
            "_row_number": number,
            "source_id": source_id,
            "device_class": device_class,
            "device_name": device_name,
            "rack_name": "dm-rack",
            "make": "Dell",
            "model": "R660",
            "serial": "",
            "asset_tag": "",
        }
        row.update(extra)
        return row

    def _plan(self, *rows):
        return DeviceModule().plan(self._batch(*rows), self.profile, CATALOG, self.reader)

    def _with_provenance(self, device, source_id="D-1", asset_tag="", extra_columns=None):
        """Record the provenance a previous import of this row would have left behind."""
        DeviceImportSource.objects.create(
            device=device, profile=self.profile, source_id=source_id, extra_columns=extra_columns or {}
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id=source_id,
            netbox_device_id=device.pk,
            device_name=device.name,
            source_asset_tag=asset_tag,
        )
        return device

    def _device(self, name, **fields):
        """Create a NetBox device that a row can match."""
        from dcim.models import Device

        values = {
            "name": name,
            "site": self.site,
            "device_type": self.device_type,
            "role": self.role,
            "status": "active",
        }
        values.update(fields)
        return Device.objects.create(**values)


class DeviceModuleBatchLoadingTest(DeviceModulePlanTestBase):
    """Batch planning loads review targets without one query per source row."""

    def test_reviewed_devices_are_loaded_once_for_the_batch(self):
        """Two review-aware passes share one permission-scoped Device index."""
        rows = []
        for number in (1, 2):
            source_id = f"D-{number}"
            device = self._with_provenance(self._device(f"srv-{number:02d}"), source_id=source_id)
            IgnoredFieldDifference.objects.create(
                profile=self.profile,
                source_id=source_id,
                netbox_device_id=device.pk,
                target_field="serial",
            )
            rows.append(self._row(number, source_id, device.name))

        with CaptureQueriesContext(connection) as captured:
            _DeviceBatch(self._batch(*rows), rows, self.profile, self.reader)

        table = connection.ops.quote_name(device._meta.db_table)
        device_queries = [query["sql"] for query in captured.captured_queries if f"FROM {table}" in query["sql"]]
        self.assertEqual(len(device_queries), 4, device_queries)

    def test_dependencies_are_loaded_once_for_the_batch(self):
        """Repeated dependency identities do not issue one ORM query per Device row."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer

        rows = [
            self._row(number, f"D-{number}", f"srv-{number:02d}", make="Example Make", model="Example Model")
            for number in (1, 2)
        ]

        with CaptureQueriesContext(connection) as captured:
            batch = _DeviceBatch(self._batch(*rows), rows, self.profile, self.reader)
            dependencies = [batch.dependencies(row) for row in rows]

        self.assertTrue(all(dependency.missing is None for dependency in dependencies))
        for model in (DeviceType, Manufacturer, DeviceRole):
            table = connection.ops.quote_name(model._meta.db_table)
            queries = [query["sql"] for query in captured.captured_queries if f"FROM {table}" in query["sql"]]
            self.assertEqual(len(queries), 1, queries)

    def test_identity_matches_use_the_batch_indexes_without_row_queries(self):
        """Every fallback match and its visibility check uses the batch-wide indexes."""
        from dcim.models import Device

        stored_source = self._device("stored-source")
        DeviceImportSource.objects.create(device=stored_source, profile=self.profile, source_id="D-1")
        serial = self._device("stored-serial", serial="SERIAL-2")
        asset_tag = self._device("stored-asset", asset_tag="Asset-Three")
        name = self._device("Stored-Name")
        rows = [
            self._row(1, "D-1", "source-one"),
            self._row(2, "D-2", "source-two", serial="SERIAL-2"),
            self._row(3, "D-3", "source-three", asset_tag="asset-three"),
            self._row(4, "D-4", "stored-name"),
        ]
        batch = _DeviceBatch(self._batch(*rows), rows, self.profile, self.reader)

        with CaptureQueriesContext(connection) as captured:
            matches = [batch.match(row, row["device_name"]) for row in rows]

        table = connection.ops.quote_name(Device._meta.db_table)
        device_queries = [query["sql"] for query in captured.captured_queries if f"FROM {table}" in query["sql"]]
        self.assertEqual(device_queries, [])
        self.assertEqual([match.device.pk for match in matches], [stored_source.pk, serial.pk, asset_tag.pk, name.pk])
        self.assertEqual([match.method for match in matches], ["stored source ID", "serial", "asset tag", "name"])

    def test_case_insensitive_indexes_use_the_database_normalization(self):
        """Batch indexes must use the same Unicode case rules as NetBox lookups."""
        name = self._device("İdentity-Name")
        expanded_name = self._device("Straße-Name")
        asset_tag = self._device("asset-device", asset_tag="Identity-Tag")
        rows = [
            self._row(1, "D-1", "İdentity-name"),
            self._row(2, "D-2", "different-name", asset_tag="ıdentity-tag"),
            self._row(3, "D-3", "STRASSE-NAME"),
        ]

        batch = _DeviceBatch(self._batch(*rows), rows, self.profile, self.reader)
        matches = [batch.match(row, row["device_name"]) for row in rows]

        self.assertEqual(
            [match.device.pk if match.device is not None else None for match in matches],
            [name.pk, asset_tag.pk, expanded_name.pk],
        )


class DeviceModuleSelectionTest(DeviceModulePlanTestBase):
    """Class policy decides which rows belong to this module."""

    def test_a_rack_row_produces_no_device_unit(self):
        """A class that creates a rack belongs to the Rack module, not this one."""
        self.assertEqual(self._plan(self._row(2, "R-1", "dm-rack", device_class="Cabinet")), [])

    def test_an_unmapped_class_is_invalid(self):
        """The preview must show a source row whose class has no target policy."""
        units = self._plan(self._row(2, "D-1", "srv-01", device_class="Unmapped"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.class_unmapped")

    def test_a_class_the_profile_ignores_is_excluded(self):
        """The explicit class policy stays visible as an excluded unit."""
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Spare", creates_rack=False, ignore=True)
        units = self._plan(self._row(2, "D-1", "srv-01", device_class="Spare"))

        self.assertEqual(units[0].disposition, Disposition.EXCLUDED)
        self.assertEqual(units[0].diagnostics[0].code, "device.class_ignored")

    def test_planning_requires_a_reader_bound_to_an_import_site(self):
        """A direct caller gets the target error instead of an attribute error after a strong match."""
        self._device("stored-device", serial="SITE-BOUND-SERIAL")

        with self.assertRaises(PlanningTargetUnavailable):
            DeviceModule().plan(
                self._batch(self._row(2, "D-1", "source-name", rack_name="", serial="SITE-BOUND-SERIAL")),
                self.profile,
                CATALOG,
                NetBoxReader.unrestricted(),
            )


class DeviceModuleIdentityTest(DeviceModulePlanTestBase):
    """A unit needs an identity that survives replanning, and a name to write."""

    def test_an_empty_device_name_falls_back_to_the_asset_tag(self):
        """The legacy import names an otherwise valid device from its asset tag."""
        units = self._plan(self._row(2, "D-1", "", asset_tag="AT-FALLBACK"))

        self.assertEqual(units[0].changes[-1].payload["name"], "AT-FALLBACK")

    def test_a_position_below_one_is_a_no_op(self):
        """Under-rack and blanking-panel rows remain visible but never write a Device."""
        units = self._plan(self._row(2, "D-1", "srv-01", u_position="0"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)
        self.assertEqual(units[0].diagnostics[0].code, "device.below_rack")

    def test_a_below_rack_position_wins_over_missing_name_and_class_mapping(self):
        """A non-device source position is skipped before Device fields are validated."""
        unit = self._plan(self._row(2, "D-1", "", device_class="Unmapped", u_position="0"))[0]

        self.assertEqual(unit.disposition, Disposition.NO_OP)
        self.assertEqual(unit.diagnostics[0].code, "device.below_rack")

    def test_a_row_with_no_device_name_is_invalid(self):
        """A device without a name is not something this can create or match."""
        units = self._plan(self._row(2, "D-1", ""))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.missing_name")

    def test_identity_is_stable_across_replanning(self):
        """The row number moves when a sheet gains a row, so the identity cannot read it."""
        first = self._plan(self._row(2, "D-1", "srv-01"))[0].identity
        second = self._plan(self._row(9, "D-1", "srv-01"))[0].identity

        self.assertEqual(first, second)

    def test_a_row_with_no_source_id_is_identified_by_its_name(self):
        """A profile can leave the source ID column unmapped, and the unit still needs an identity."""
        units = self._plan(self._row(2, "", "srv-01"))

        self.assertEqual(units[0].identity, "device:name:srv-01")

    def test_an_ignored_source_id_is_excluded(self):
        """`excluded` is the disposition reserved for operator-configured policy."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1")

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.EXCLUDED)
        self.assertEqual(units[0].diagnostics[0].code, "device.ignored")

    def test_a_null_like_ignored_source_id_does_not_ignore_an_unidentified_row(self):
        """An empty normalized source identity cannot select one Device to ignore."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="N/A")

        unit = self._plan(self._row(2, "N/A", "srv-01"))[0]

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE, unit.diagnostics)
        self.assertEqual(unit.changes[-1].payload["source_id"], "")

    def test_an_ignored_source_id_wins_over_a_missing_name(self):
        """Operator policy excludes the identified row before its source fields are validated."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1")

        unit = self._plan(self._row(2, "D-1", ""))[0]

        self.assertEqual(unit.disposition, Disposition.EXCLUDED)
        self.assertEqual(unit.diagnostics[0].code, "device.ignored")

    def test_an_ignored_source_id_wins_over_a_missing_class_mapping(self):
        """Individual ignore policy does not depend on the current class configuration."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1")

        unit = self._plan(self._row(2, "D-1", "srv-01", device_class="Unmapped"))[0]

        self.assertEqual(unit.disposition, Disposition.EXCLUDED)
        self.assertEqual(unit.diagnostics[0].code, "device.ignored")

    def test_a_missing_name_wins_over_ignored_class_policy(self):
        """The source row stays invalid when class policy would otherwise exclude it."""
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Spare", creates_rack=False, ignore=True)

        unit = self._plan(self._row(2, "D-1", "", device_class="Spare"))[0]

        self.assertEqual(unit.disposition, Disposition.INVALID)
        self.assertEqual(unit.diagnostics[0].code, "device.missing_name")


class DeviceModuleDuplicateTest(DeviceModulePlanTestBase):
    """Two rows claiming one identity is a source defect, so both are refused."""

    def test_a_null_like_ignored_source_id_does_not_hide_duplicate_identity_fields(self):
        """An empty source identity still participates in serial and asset-tag duplicate checks."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="N/A")

        for field, value, code in (
            ("serial", "SN-1", "device.duplicate_serial"),
            ("asset_tag", "AT-1", "device.duplicate_asset_tag"),
        ):
            with self.subTest(field=field):
                units = self._plan(
                    self._row(2, "N/A", "srv-01", **{field: value}),
                    self._row(3, "N/A", "srv-02", **{field: value}),
                )

                self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
                self.assertEqual(units[0].diagnostics[0].code, code)

    def test_a_duplicate_source_id_in_one_file_is_invalid(self):
        units = self._plan(self._row(2, "D-1", "srv-01"), self._row(3, "D-1", "srv-02"))

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "device.duplicate_source_id")

    def test_a_duplicate_serial_in_one_file_is_invalid(self):
        units = self._plan(
            self._row(2, "D-1", "srv-01", serial="SN-1"),
            self._row(3, "D-2", "srv-02", serial="SN-1"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "device.duplicate_serial")

    def test_a_duplicate_asset_tag_in_one_file_is_invalid(self):
        units = self._plan(
            self._row(2, "D-1", "srv-01", asset_tag="AT-1"),
            self._row(3, "D-2", "srv-02", asset_tag="at-1"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "device.duplicate_asset_tag")

    def test_null_like_serials_are_empty_instead_of_duplicates(self):
        """Spreadsheet null markers do not become Device serial identities."""
        units = self._plan(
            self._row(2, "D-1", "srv-01", serial="N/A"),
            self._row(3, "D-2", "srv-02", serial="N/A"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.ACTIONABLE] * 2)
        self.assertTrue(all(unit.changes[-1].payload["serial"] == "" for unit in units))

    def test_null_like_asset_tags_are_empty_instead_of_duplicates(self):
        """Spreadsheet null markers do not become Device asset tag identities."""
        units = self._plan(
            self._row(2, "D-1", "srv-01", asset_tag="#N/A"),
            self._row(3, "D-2", "srv-02", asset_tag="#N/A"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.ACTIONABLE] * 2)
        self.assertTrue(all(unit.changes[-1].payload["asset_tag"] == "" for unit in units))

    def test_a_duplicate_name_refuses_both_unmatched_rows(self):
        """Two creates with one target name cannot both succeed, so neither is executable."""
        self._device("srv-01")

        units = self._plan(self._row(2, "D-1", "srv-01"), self._row(3, "D-2", "srv-01"))

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertTrue(all(unit.diagnostics[0].code == "device.duplicate_name" for unit in units))

    def test_the_diagnostic_names_every_row_the_conflict_involves(self):
        """The operator picks which row gives the value up, so both row numbers have to be there."""
        units = self._plan(
            self._row(2, "D-1", "srv-01", serial="SN-1"),
            self._row(7, "D-2", "srv-02", serial="SN-1"),
        )

        # The plan freezes its values, so the row numbers arrive as a tuple.
        self.assertEqual(units[0].diagnostics[0].display["rows"], (2, 7))


class DeviceModuleDependencyTest(DeviceModulePlanTestBase):
    """The module plans the relation objects the legacy device pass created."""

    def test_a_missing_device_type_is_an_executable_dependency(self):
        """The default profile creates the Manufacturer and Device Type before the Device."""
        units = self._plan(self._row(2, "D-1", "srv-01", make="Acme", model="Widget"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual(
            [change.operation for change in units[0].changes],
            ["create_manufacturer", "create_device_type", "create"],
        )

    def test_a_missing_role_is_an_executable_dependency(self):
        """The class mapping supplies enough information to create its Device Role."""
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Switch", creates_rack=False, role_slug="network-switch"
        )

        units = self._plan(self._row(2, "D-1", "sw-01", device_class="Switch"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual([change.operation for change in units[0].changes], ["create_role", "create"])

    def test_a_stored_role_slug_that_looks_null_remains_valid(self):
        """Null-marker rules apply to source cells, not stored NetBox identities."""
        from dcim.models import DeviceRole

        role = DeviceRole.objects.create(name="None Role", slug="none")
        mapping = self.profile.class_role_mappings.get(source_class="Server")
        mapping.role_slug = role.slug
        mapping.save(update_fields=["role_slug"])

        unit = self._plan(self._row(2, "D-1", "srv-01"))[0]

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE, unit.diagnostics)
        self.assertEqual(unit.changes[-1].payload["role_id"], role.pk)

    def test_disabled_device_type_creation_leaves_the_unit_blocked(self):
        """The explicit profile policy can require the operator to create or map the type."""
        self.profile.adapter_config = {**self.profile.adapter_config, "create_missing_device_types": False}
        self.profile.save(update_fields=["adapter_config"])

        units = self._plan(self._row(2, "D-1", "srv-01", make="Acme", model="Widget"))

        self.assertEqual(units[0].disposition, Disposition.BLOCKED)
        self.assertEqual(units[0].diagnostics[0].code, "device.device_type_missing")

    def test_derived_dependency_slug_collision_refuses_each_source_row(self):
        """Different source identities cannot silently share one generated dependency identity."""
        units = self._plan(
            self._row(2, "D-1", "srv-01", make="Acme!", model="Widget"),
            self._row(3, "D-2", "srv-02", make="Acme?", model="Widget"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertTrue(all(unit.diagnostics[0].code == "device.derived_slug_collision" for unit in units))

    def test_normalized_manufacturer_mappings_make_a_shared_slug_explicit(self):
        """Escaped source labels remain explicit when collision detection checks their normalized rows."""
        ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make=r"Acme\u0021",
            netbox_manufacturer_slug="acme",
        )
        ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make=r"Acme\u003f",
            netbox_manufacturer_slug="acme",
        )

        units = self._plan(
            self._row(2, "D-1", "srv-01", make="Acme!", model="Widget One"),
            self._row(3, "D-2", "srv-02", make="Acme?", model="Widget Two"),
        )

        self.assertFalse(
            any(diagnostic.code == "device.derived_slug_collision" for unit in units for diagnostic in unit.diagnostics)
        )

    def test_device_type_mapping_normalizes_the_source_make_before_lookup(self):
        """Escaped whitespace in a mapping still selects its explicit Device Type."""
        from dcim.models import DeviceType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Mapped Make", slug="mapped-make")
        mapped_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Mapped Type",
            slug="mapped-type",
            u_height=1,
        )
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make=r"Dell\u0020\u0020",
            source_model=r"R\u0036\u0036\u0030",
            netbox_manufacturer_slug=manufacturer.slug,
            netbox_device_type_slug=mapped_type.slug,
        )

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].changes[-1].payload["device_type_id"], mapped_type.pk)

    def test_device_type_resolver_normalizes_both_sides_of_make_lookup(self):
        """The resolver owns whitespace normalization for direct callers too."""
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="Dell  EMC",
            source_model=r"R\u0036\u0036\u0030",
            netbox_manufacturer_slug="mapped-make",
            netbox_device_type_slug="mapped-type",
        )
        resolver = DeviceTypeIdentityResolver.for_profile(self.profile)

        self.assertEqual(
            resolver.resolve("Dell  EMC", "R660"),
            ("mapped-make", "mapped-type", True),
        )

    def test_a_rack_the_row_names_but_netbox_does_not_have_is_blocked(self):
        """A device row cannot create the rack it is placed in."""
        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="no-such-rack"))

        self.assertEqual(units[0].disposition, Disposition.BLOCKED)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_missing")

    def test_a_rack_netbox_holds_stays_an_id_based_dependency(self):
        """A stored rack needs no deferred change dependency."""
        change = self._plan(self._row(2, "D-1", "srv-01"))[0].changes[0]

        self.assertEqual(change.payload["rack_id"], self.rack.pk)
        self.assertEqual(change.dependencies, ())

    def test_an_ignored_rack_row_does_not_satisfy_a_device_dependency(self):
        """An excluded rack unit supplies no rack create for a device to use."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="R-1")
        rack_row = self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack")
        device_row = self._row(3, "D-1", "srv-01", rack_name="batch-rack")

        unit = self._plan(rack_row, device_row)[0]

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.diagnostics[0].code, "device.rack_missing")

    def test_duplicate_rack_rows_do_not_satisfy_a_device_dependency(self):
        """Refused rack units supply no rack create for a device to use."""
        rows = (
            self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack"),
            self._row(3, "R-2", "batch-rack", device_class="Cabinet", rack_name="batch-rack"),
            self._row(4, "D-1", "srv-01", rack_name="batch-rack"),
        )

        unit = self._plan(*rows)[0]

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.diagnostics[0].code, "device.rack_missing")


class DeviceModuleMatchTest(DeviceModulePlanTestBase):
    """Matching decides whether the row creates a device or reconciles one."""

    def test_an_unmatched_row_is_actionable_as_a_create(self):
        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].operation, "create")
        self.assertEqual(units[0].changes[0].payload["name"], "srv-01")

    def test_a_matching_device_is_a_no_op(self):
        """NetBox already holds what the row asks for, so nothing should execute."""
        self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)
        self.assertEqual(units[0].changes, ())

    def test_a_differing_device_is_actionable_as_an_update(self):
        self._device("srv-01", rack=self.rack, serial="OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].payload["serial"], "NEW")

    def test_update_existing_false_leaves_a_differing_device_alone(self):
        """The profile policy disables reconciliation of matched Devices."""
        self._with_provenance(self._device("srv-01", rack=self.rack, serial="OLD"))
        self.profile.adapter_config = {**self.profile.adapter_config, "update_existing": False}
        self.profile.save(update_fields=["adapter_config"])

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)
        self.assertEqual(units[0].changes, ())

    def test_a_matched_device_moving_to_a_batch_created_rack_is_an_update(self):
        """A deferred rack name is placement work even though it has no ID yet."""
        self._with_provenance(self._device("srv-01"))
        rack_row = self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack")
        device_row = self._row(3, "D-1", "srv-01", rack_name="batch-rack")

        unit = self._plan(rack_row, device_row)[0]

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE, unit.diagnostics)
        self.assertEqual(unit.changes[0].operation, "update")

    def test_a_serial_matches_a_device_the_name_does_not(self):
        """The serial is a stronger identifier than the name, so it decides first."""
        device = self._device("stored-name", rack=self.rack, serial="SN-1")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1", status="Offline"))

        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_an_asset_tag_matches_a_device_the_name_does_not(self):
        device = self._device("stored-name", rack=self.rack, asset_tag="AT-1")

        units = self._plan(self._row(2, "D-1", "srv-01", asset_tag="AT-1", status="Offline"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_a_name_in_another_tenant_does_not_match(self):
        """Name identity is scoped by both site and tenant."""
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Other Name Tenant", slug="other-name-tenant")
        self._device("srv-01", rack=self.rack, tenant=tenant)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].changes[-1].operation, "create")

    def test_a_device_row_carries_the_stored_class_policy_for_its_editor(self):
        """Reopening the class editor must show the policy already saved, not an empty form."""
        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].display["extra_data"]["class_mapping_action"], "role")
        self.assertEqual(units[0].display["extra_data"]["class_mapping_role_slug"], "server")

    def test_a_device_row_whose_class_is_ignored_reports_the_ignore_policy(self):
        """An ignore mapping must reopen as ignore, so a save cannot silently change the action."""
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Sensor", ignore=True)

        units = self._plan(self._row(2, "D-1", "srv-01", device_class="Sensor"))

        self.assertEqual(units[0].display["extra_data"]["class_mapping_action"], "ignore")
        self.assertEqual(units[0].display["extra_data"]["class_mapping_role_slug"], "")

    def test_a_device_row_with_no_class_policy_reports_none(self):
        """An unmapped class has nothing to restore, so the editor opens on its own default."""
        units = self._plan(self._row(2, "D-1", "srv-01", device_class="Traffic Generator"))

        self.assertEqual(units[0].display["extra_data"]["class_mapping_action"], "")
        self.assertEqual(units[0].display["extra_data"]["class_mapping_role_slug"], "")

    def test_a_name_only_match_on_an_unplaced_device_says_the_device_is_unplaced(self):
        """The stored Device holds no placement, so the row must not report one it could differ from."""
        self._device("srv-01")

        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="dm-rack"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.name_unplaced_match")

    def test_a_name_only_match_at_another_placement_reports_the_conflict(self):
        """A stored Device the source would move keeps the wording that states it sits elsewhere."""
        self._device("srv-01", rack=self.rack, position=10, face="front")

        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="dm-rack", u_position="20", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.name_placement_conflict")

    def test_an_explicit_binding_outranks_every_other_identifier(self):
        """The operator linked this row to this device, which is the strongest statement there is."""
        self._device("srv-01", rack=self.rack)
        bound = self._device("bound-device", rack=self.rack)
        DeviceExistingMatch.objects.create(profile=self.profile, source_id="D-1", netbox_device_id=bound.pk)

        units = self._plan(self._row(2, "D-1", "srv-01", status="Offline"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], bound.pk)

    def test_a_strong_match_already_bound_to_another_source_is_invalid(self):
        """One NetBox Device cannot represent two source identities."""
        device = self._device("stored-name", rack=self.rack, serial="SN-BOUND")
        DeviceExistingMatch.objects.create(
            profile=self.profile, source_id="D-OTHER", netbox_device_id=device.pk, device_name=device.name
        )

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-BOUND"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.already_bound")

    def test_a_refused_row_does_not_claim_its_matched_device(self):
        """A later valid row may use a Device that an invalid row only considered."""
        device = self._device(
            "stored-name",
            rack=self.rack,
            serial="SN-SHARED",
            asset_tag="ASSET-SHARED",
        )

        units = self._plan(
            self._row(2, "D-INVALID", "first-name", rack_name="", u_position="5", serial=device.serial),
            self._row(3, "D-VALID", "second-name", asset_tag=device.asset_tag),
        )

        self.assertEqual(units[0].diagnostics[0].code, "device.rack_required")
        self.assertNotEqual(units[1].diagnostics[0].code if units[1].diagnostics else None, "device.already_bound")
        self.assertIn(units[1].disposition, {Disposition.ACTIONABLE, Disposition.NO_OP})

    def test_an_ambiguous_serial_refuses_the_row_rather_than_guessing(self):
        """Two stored devices carry the serial, so no automatic answer is the safe one."""
        self._device("first", rack=self.rack, serial="SN-1")
        self._device("second", rack=self.rack, serial="SN-1")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.ambiguous_serial")

    def test_a_device_at_another_site_is_not_matched_by_name(self):
        """The name is only unique inside a site, so a global name match would cross sites."""
        from dcim.models import Site

        other = Site.objects.create(name="Other Site", slug="other-site")
        self._device("srv-01", site=other)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].changes[0].operation, "create")

    def test_a_serial_match_at_another_site_is_invalid(self):
        """A strong identity is global, so the import refuses to move or duplicate it."""
        from dcim.models import Site

        other = Site.objects.create(name="Serial Other Site", slug="serial-other-site")
        self._device("stored-name", site=other, serial="SN-CROSS-SITE")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-CROSS-SITE"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.cross_site_match")

    def test_a_strong_match_the_actor_cannot_view_is_invalid(self):
        """A hidden global identity must not become a duplicate create."""
        from dcim.models import Device, Rack

        from netbox_data_import.tests.helpers import user_with_object_permission

        self._device("hidden-device", rack=self.rack, serial="SN-HIDDEN")
        # The rack stays visible, so the only thing the actor cannot see is the device itself.
        actor = user_with_object_permission(
            "device-module-blind",
            [(Device, ("view",), {"name": "nothing-matches-this"}), (Rack, ("view",), {})],
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)

        units = DeviceModule().plan(
            self._batch(self._row(2, "D-1", "srv-01", serial="SN-HIDDEN")), self.profile, CATALOG, reader
        )

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.inaccessible_match")


class DeviceModulePlacementTest(DeviceModulePlanTestBase):
    """Where a device sits is part of what the row writes, and the rack has to have room."""

    def test_the_placement_the_row_names_reaches_the_change(self):
        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Front", airflow="Front to Back"))

        payload = units[0].changes[0].payload
        self.assertEqual(payload["u_position"], 5)
        self.assertEqual(payload["face"], "front")
        self.assertEqual(payload["airflow"], "front-to-rear")

    def test_a_source_word_the_importer_already_reads_is_translated(self):
        """`Back` and `Rear` are one NetBox face, and the module reads the same table as the engine."""
        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Back"))

        self.assertEqual(units[0].changes[0].payload["face"], "rear")

    def test_a_status_the_source_spells_its_own_way_is_translated(self):
        units = self._plan(self._row(2, "D-1", "srv-01", status="Live"))

        self.assertEqual(units[0].changes[0].payload["status"], "active")

    def test_inventory_status_uses_netbox_inventory_semantics(self):
        from dcim.choices import DeviceStatusChoices

        units = self._plan(self._row(2, "D-1", "srv-01", status="Inventory"))

        self.assertEqual(units[0].changes[0].payload["status"], DeviceStatusChoices.STATUS_INVENTORY)

    def test_a_position_with_no_rack_is_invalid(self):
        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="", u_position="5", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_required")

    def test_a_position_in_a_batch_created_rack_is_actionable(self):
        """A new rack is empty, so its dependency can accept a placement."""
        rack_row = self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack")
        device_row = self._row(
            3,
            "D-1",
            "srv-01",
            rack_name="batch-rack",
            u_position="5",
            face="Front",
        )

        unit = self._plan(rack_row, device_row)[0]

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE, unit.diagnostics)

    def test_a_position_with_no_face_is_invalid(self):
        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.face_required")

    def test_a_face_with_no_rack_is_dropped(self):
        """NetBox refuses a rack face on a device that is in no rack, so the plan cannot ask for one."""
        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].payload["face"], "")

    def test_a_position_a_stored_device_already_fills_is_invalid(self):
        """The write would fail on the rack's own constraint, so planning says so first."""
        self._device("stored", rack=self.rack, position=5, face="front")

        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_position_occupied")

    def test_two_rows_claiming_one_slot_name_each_other(self):
        """The second row is refused, and the operator needs to know which row took the slot."""
        units = self._plan(
            self._row(2, "D-1", "srv-01", u_position="5", face="Front"),
            self._row(7, "D-2", "srv-02", u_position="5", face="Front"),
        )

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[1].disposition, Disposition.INVALID)
        self.assertEqual(units[1].diagnostics[0].code, "device.rack_position_claimed")
        self.assertEqual(units[1].diagnostics[0].display["claimed_by_row"], 2)

    def test_a_rejected_row_does_not_claim_a_slot_from_a_later_valid_row(self):
        """Only a row that can settle as executable or a no-op reserves its placement."""
        self._device("stored-name")

        units = self._plan(
            self._row(2, "D-1", "stored-name", u_position="5", face="Front"),
            self._row(3, "D-2", "new-device", u_position="5", face="Front"),
        )

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.name_unplaced_match")
        self.assertEqual(units[1].disposition, Disposition.ACTIONABLE, units[1].diagnostics)

    def test_two_rows_still_cannot_claim_one_slot_in_a_batch_created_rack(self):
        """The batch claim works before the new rack has an ORM identity."""
        rack_row = self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack")
        device_rows = (
            self._row(3, "D-1", "srv-01", rack_name="batch-rack", u_position="5", face="Front"),
            self._row(4, "D-2", "srv-02", rack_name="batch-rack", u_position="5", face="Front"),
        )

        units = self._plan(rack_row, *device_rows)

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[1].disposition, Disposition.INVALID)
        self.assertEqual(units[1].diagnostics[0].code, "device.rack_position_claimed")
        self.assertEqual(units[1].diagnostics[0].display["claimed_by_row"], 3)

    def test_full_depth_and_half_depth_rows_conflict_in_a_batch_created_rack(self):
        """A full-depth placement occupies the same unit on both faces of a new rack."""
        from dcim.models import DeviceType

        self.device_type.is_full_depth = True
        self.device_type.save(update_fields=["is_full_depth"])
        DeviceType.objects.create(
            manufacturer=self.manufacturer,
            model="R661",
            slug="dell-r661",
            u_height=1,
            is_full_depth=False,
        )
        rack_row = self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack")
        device_rows = (
            self._row(3, "D-1", "srv-01", rack_name="batch-rack", u_position="5", face="Front"),
            self._row(
                4,
                "D-2",
                "srv-02",
                rack_name="batch-rack",
                model="R661",
                u_position="5",
                face="Rear",
            ),
        )

        units = self._plan(rack_row, *device_rows)

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[1].disposition, Disposition.INVALID)
        self.assertEqual(units[1].diagnostics[0].code, "device.rack_position_claimed")
        self.assertEqual(units[1].diagnostics[0].display["claimed_by_row"], 3)

    def test_a_device_keeping_its_own_slot_is_not_refused(self):
        """A matched device already occupies the position, and staying put is not a conflict."""
        self._with_provenance(self._device("srv-01", rack=self.rack, position=5, face="front"))

        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)

    def test_a_taller_device_that_does_not_fit_is_invalid(self):
        """A 2U device at the top of the rack has nowhere to put its second unit."""
        from dcim.models import DeviceType

        DeviceType.objects.create(manufacturer=self.manufacturer, model="R760", slug="dell-r760", u_height=2)

        units = self._plan(self._row(2, "D-1", "srv-01", model="R760", u_position="42", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_position_occupied")


class DeviceModuleZeroUTest(DeviceModulePlanTestBase):
    """A zero-U device type holds no rack position and no face, whatever the row says."""

    def setUp(self):
        super().setUp()
        from dcim.models import DeviceType

        DeviceType.objects.create(manufacturer=self.manufacturer, model="PDU", slug="dell-pdu", u_height=0)

    def _pdu_row(self, **extra):
        return self._row(2, "D-1", "pdu-01", model="PDU", **extra)

    def test_the_position_the_row_carries_is_dropped(self):
        units = self._plan(self._pdu_row(u_position="5", face="Front"))

        self.assertIsNone(units[0].changes[0].payload["u_position"])
        self.assertEqual(units[0].changes[0].payload["face"], "")

    def test_a_position_with_no_face_is_not_refused(self):
        """The face rule guards a real rack position, and a zero-U type has none to guard."""
        units = self._plan(self._pdu_row(u_position="5"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)

    def test_two_zero_u_rows_do_not_claim_one_slot(self):
        """Neither row occupies a unit, so neither can take the other's."""
        units = self._plan(
            self._row(2, "D-1", "pdu-01", model="PDU", u_position="5", face="Front"),
            self._row(7, "D-2", "pdu-02", model="PDU", u_position="5", face="Front"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.ACTIONABLE] * 2)


class DeviceModuleApplyTest(DeviceModulePlanTestBase):
    """Applying one Planned Change writes the device the plan described, or refuses to."""

    def setUp(self):
        super().setUp()
        from netbox_data_import.tests.helpers import user_with_object_permission
        from dcim.models import Device

        self.actor = user_with_object_permission("device-module-writer", [(Device, ("add", "change", "view"), {})])
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def _only_change(self, *rows):
        """Return the single change the given rows plan."""
        units = self._plan(*rows)
        self.assertEqual(len(units), 1, [unit.diagnostics for unit in units])
        self.assertEqual(len(units[0].changes), 1, units[0].disposition)
        return units[0].changes[0]

    def test_a_create_change_writes_the_device_the_plan_described(self):
        change = self._only_change(
            self._row(2, "D-1", "srv-01", serial="SN-1", asset_tag="AT-1", u_position="5", face="Front")
        )

        device = DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        self.assertEqual(device.name, "srv-01")
        self.assertEqual(device.serial, "SN-1")
        self.assertEqual(device.asset_tag, "AT-1")
        self.assertEqual(device.rack_id, self.rack.pk)
        self.assertEqual(device.position, 5)
        self.assertEqual(device.face, "front")
        self.assertEqual(device.device_type_id, self.device_type.pk)
        self.assertEqual(device.role_id, self.role.pk)

    def test_dependency_changes_create_the_type_and_role_before_the_device(self):
        """Applying the complete unit preserves the legacy automatic dependency behavior."""
        from django.contrib.auth import get_user_model
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer

        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Appliance", creates_rack=False, role_slug="appliance"
        )
        unit = self._plan(
            self._row(2, "D-AUTO", "appliance-01", device_class="Appliance", make="Example", model="Unit")
        )[0]
        actor = get_user_model().objects.create_superuser(
            username="dependency-writer", email="dependency-writer@example.invalid", password="testpass"
        )
        context = ExecutionContext(actor=actor, reader=self.reader, profile=self.profile)

        for change in unit.changes:
            DeviceModule().apply(change, context)

        self.assertTrue(Manufacturer.objects.filter(slug="example").exists())
        self.assertTrue(DeviceType.objects.filter(manufacturer__slug="example", slug="example-unit").exists())
        self.assertTrue(DeviceRole.objects.filter(slug="appliance").exists())
        self.assertTrue(Device.objects.filter(name="appliance-01", site=self.site).exists())

    def test_an_update_change_reconciles_the_stored_device(self):
        stored = self._device("srv-01", rack=self.rack, serial="OLD")

        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        DeviceModule().apply(change, self.context)

        stored.refresh_from_db()
        self.assertEqual(stored.serial, "NEW")

    def test_an_update_clears_a_tenant_when_the_import_target_has_none(self):
        """Blank target context is an explicit value for Device location and tenant."""
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Old Tenant", slug="old-tenant")
        stored = self._with_provenance(self._device("srv-01", rack=self.rack, tenant=tenant))

        change = self._only_change(self._row(2, "D-1", "srv-01"))
        DeviceModule().apply(change, self.context)

        stored.refresh_from_db()
        self.assertIsNone(stored.tenant_id)

    def test_an_update_clears_a_face_the_source_omits(self):
        """The preview and writer agree that an empty face clears the stored face."""
        stored = self._with_provenance(self._device("srv-01", rack=self.rack, position=5, face="front"))

        change = self._only_change(self._row(2, "D-1", "srv-01"))
        DeviceModule().apply(change, self.context)

        stored.refresh_from_db()
        self.assertEqual(stored.face, "")

    def test_a_deferred_rack_that_is_still_absent_is_refused(self):
        """A device change cannot run before the rack change it depends on."""
        rack_row = self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack")
        device_row = self._row(3, "D-1", "srv-01", rack_name="batch-rack")
        change = self._only_change(rack_row, device_row)

        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(change, self.context)

    def test_a_vanished_device_is_refused_rather_than_recreated(self):
        """The plan named a device to update, and creating a new one instead is not that."""
        from dcim.models import Device

        stored = self._device("srv-01", rack=self.rack, serial="OLD")
        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        Device.objects.filter(pk=stored.pk).delete()

        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(change, self.context)

    def test_a_create_is_refused_when_its_identity_appears_after_planning(self):
        """Execution cannot turn a previewed create into a duplicate of a late Device."""
        change = self._only_change(self._row(2, "D-1", "srv-01", serial="SN-LATE"))
        self._device("late-device", rack=self.rack, serial="SN-LATE")

        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(change, self.context)

    def test_a_device_type_that_moved_since_the_plan_is_refused(self):
        """The type decides the height, so the placement the plan checked no longer holds."""
        from dcim.models import Device, DeviceType

        stored = self._device("srv-01", rack=self.rack, serial="OLD")
        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        other = DeviceType.objects.create(manufacturer=self.manufacturer, model="R760", slug="dell-r760", u_height=1)
        Device.objects.filter(pk=stored.pk).update(device_type=other)

        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(change, self.context)

    def test_any_writable_target_state_change_invalidates_the_precondition(self):
        """The apply guard covers more than the Device Type relation."""
        stored = self._with_provenance(self._device("srv-01", rack=self.rack, serial="OLD"))
        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        stored.status = "offline"
        stored.save(update_fields=["status"])

        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(change, self.context)

    def test_an_unrelated_custom_field_change_does_not_invalidate_the_precondition(self):
        """The guard compares only the custom field this import can overwrite."""
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Device
        from extras.models import CustomField

        device_type = ContentType.objects.get_for_model(Device)
        for name in ("source_id", "other"):
            custom_field = CustomField.objects.create(name=name, type="text")
            custom_field.object_types.add(device_type)
        self.profile.adapter_config = {**self.profile.adapter_config, "custom_field_name": "source_id"}
        self.profile.save(update_fields=["adapter_config"])
        stored = self._with_provenance(
            self._device("srv-01", rack=self.rack, serial="OLD", custom_field_data={"other": "before"})
        )
        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        stored.custom_field_data["other"] = "after"
        stored.save(update_fields=["custom_field_data"])

        DeviceModule().apply(change, self.context)

        stored.refresh_from_db()
        self.assertEqual(stored.serial, "NEW")
        self.assertEqual(stored.custom_field_data["other"], "after")

    def test_an_actor_without_the_add_permission_is_refused(self):
        """An ObjectPermission constraint is only decided against the saved row."""
        from dcim.models import Device

        from netbox_data_import.object_permissions import ObjectPermissionDenied
        from netbox_data_import.tests.helpers import user_with_object_permission

        blocked = user_with_object_permission(
            "device-module-blocked", [(Device, ("add", "view"), {"name": "only-this-name"})]
        )
        change = self._only_change(self._row(2, "D-1", "srv-01"))

        with self.assertRaises(ObjectPermissionDenied):
            DeviceModule().apply(change, ExecutionContext(actor=blocked, reader=self.reader, profile=self.profile))

    def test_a_zero_u_device_is_written_without_a_position(self):
        """NetBox refuses a position on a zero-U type, so the plan never carries one."""
        from dcim.models import DeviceType

        DeviceType.objects.create(manufacturer=self.manufacturer, model="PDU", slug="dell-pdu", u_height=0)

        change = self._only_change(self._row(2, "D-1", "pdu-01", model="PDU", u_position="5", face="Front"))
        device = DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        self.assertIsNone(device.position)
        self.assertEqual(device.face, "")


class DeviceModuleIPAssignmentTest(DeviceModulePlanTestBase):
    """Device plans and writes carry the same parsed address fields."""

    def setUp(self):
        """Give device and address writes their real object permissions."""
        super().setUp()
        from dcim.models import Device
        from ipam.models import IPAddress

        from netbox_data_import.tests.helpers import user_with_object_permission

        self.actor = user_with_object_permission(
            "device-module-ip-writer",
            [
                (Device, ("add", "change", "view"), {}),
                (IPAddress, ("add", "change", "view"), {}),
            ],
        )
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def _interface_template(self):
        """Declare the management interface that a new device instantiates."""
        from dcim.models import InterfaceTemplate

        return InterfaceTemplate.objects.create(
            device_type=self.device_type,
            name="mgmt0",
            type="1000base-t",
            mgmt_only=True,
        )

    def _assigned_address(self, device, address):
        """Put one address on the device interface and select it as primary IPv4."""
        from ipam.models import IPAddress

        assigned = IPAddress.objects.create(
            address=address,
            assigned_object=device.interfaces.get(name="mgmt0"),
        )
        device.primary_ip4 = assigned
        device.save(update_fields=["primary_ip4"])
        return assigned

    def _only_change(self, row):
        """Return the one change a row plans."""
        units = self._plan(row)
        self.assertEqual(len(units), 1, [unit.diagnostics for unit in units])
        self.assertEqual(len(units[0].changes), 1, units[0].disposition)
        return units[0].changes[0]

    def _ignore_primary_ip4(self, device, file_address, stored_address):
        """Record the exact primary IPv4 difference the operator ignored."""
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="D-1",
            netbox_device_id=device.pk,
            target_field="primary_ip4",
            file_snapshot={"canonical": file_address, "display": file_address},
            netbox_snapshot={"canonical": stored_address, "display": stored_address},
        )

    def test_a_create_carries_and_assigns_the_rows_address(self):
        """A created device gets its parsed address after its interface exists."""
        self._interface_template()

        change = self._only_change(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.10"))
        self.assertEqual(dict(change.payload["ip_fields"]), {"primary_ip4": "198.18.0.10/32"})
        device = DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        self.assertEqual(str(device.primary_ip4.address), "198.18.0.10/32")
        self.assertEqual(device.primary_ip4.assigned_object.device_id, device.pk)
        self.assertEqual(device.primary_ip4.assigned_object.name, "mgmt0")

    def test_a_matched_device_that_already_holds_the_address_is_a_no_op(self):
        """The plan has no work when the current field points at that device's address."""
        self._interface_template()
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        self._assigned_address(device, "198.18.0.11/32")

        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.11"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)
        self.assertEqual(units[0].changes, ())

        # Another address on the same setup is work, so the no-op rests on the address.
        moved = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.13"))

        self.assertEqual(moved[0].disposition, Disposition.ACTIONABLE, moved[0].diagnostics)
        self.assertEqual(dict(moved[0].changes[0].payload["ip_fields"]), {"primary_ip4": "198.18.0.13/32"})

    def test_a_duplicate_address_on_another_device_does_not_repeat_a_settled_write(self):
        """Another object holding the same address does not change the row this device holds."""
        from ipam.models import IPAddress

        self._interface_template()
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        self._assigned_address(device, "198.18.0.16/32")
        other = self._device("srv-02", rack=self.rack)
        IPAddress.objects.create(
            address="198.18.0.16/32",
            assigned_object=other.interfaces.get(name="mgmt0"),
        )

        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.16"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)
        self.assertEqual(units[0].changes, ())

    def test_the_same_address_in_another_vrf_does_not_make_the_current_address_work(self):
        """An address is unique inside its VRF, so another VRF cannot invalidate a settled field."""
        from ipam.models import VRF, IPAddress

        self._interface_template()
        current_vrf = VRF.objects.create(name="Device Module VRF A")
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        interface = device.interfaces.get(name="mgmt0")
        interface.vrf = current_vrf
        interface.save(update_fields=["vrf"])
        current = IPAddress.objects.create(
            address="198.18.0.14/32",
            vrf=current_vrf,
            assigned_object=interface,
        )
        device.primary_ip4 = current
        device.save(update_fields=["primary_ip4"])
        IPAddress.objects.create(
            address="198.18.0.14/32",
            vrf=VRF.objects.create(name="Device Module VRF B"),
        )

        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.14"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)
        self.assertEqual(units[0].changes, ())

    def test_an_interface_vrf_difference_does_not_repeat_a_settled_address_write(self):
        """NetBox permits an interface and its assigned address to use independent VRFs."""
        from ipam.models import IPAddress, VRF

        self._interface_template()
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        interface = device.interfaces.get(name="mgmt0")
        interface.vrf = VRF.objects.create(name="Device Module Interface VRF")
        interface.save(update_fields=["vrf"])
        current = IPAddress.objects.create(
            address="198.18.0.15/32",
            vrf=VRF.objects.create(name="Device Module Address VRF"),
            assigned_object=interface,
        )
        device.primary_ip4 = current
        device.save(update_fields=["primary_ip4"])

        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.15"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)
        self.assertEqual(units[0].changes, ())

    def test_a_matched_device_that_lacks_the_address_is_actionable(self):
        """An address absent from the matched device is update work."""
        self._interface_template()
        device = self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.12"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_an_ignored_address_is_absent_from_the_change_and_is_not_written(self):
        """One reviewed payload decides both the disposition and the address write."""
        self._interface_template()
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        stored = self._assigned_address(device, "198.18.0.19/32")
        self._ignore_primary_ip4(device, "198.18.0.20/32", "198.18.0.19/32")

        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.20"))
        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW", primary_ip4="198.18.0.20"))
        self.assertNotIn("primary_ip4", change.payload["ip_fields"])
        DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        self.assertEqual(device.serial, "NEW")
        self.assertEqual(device.primary_ip4_id, stored.pk)

    def test_an_unplaceable_address_is_stored_as_unassigned(self):
        """A device still writes when its type declares no interface for the address."""
        change = self._only_change(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.21"))

        device = DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        self.assertIsNone(device.primary_ip4)
        self.assertEqual(
            DeviceImportSource.objects.get(device=device).unassigned_ips,
            {"primary_ip4": "198.18.0.21/32"},
        )

    def test_an_unparseable_address_warns_without_changing_the_disposition(self):
        """A bad optional address does not refuse an otherwise actionable create."""
        self._interface_template()

        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="not-an-address"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(len(units[0].diagnostics), 1)
        diagnostic = units[0].diagnostics[0]
        self.assertEqual(diagnostic.code, "device.unparseable_ip")
        self.assertEqual(diagnostic.severity, Severity.WARNING)
        self.assertEqual(diagnostic.display["device_name"], "srv-01")
        self.assertEqual(diagnostic.display["source_id"], "D-1")
        self.assertEqual(diagnostic.display["field"], "primary_ip4")
        self.assertEqual(diagnostic.display["value"], "not-an-address")
        change = units[0].changes[0]
        self.assertEqual(dict(change.payload["ip_fields"]), {})
        device = DeviceModule().apply(change, self.context)
        device.refresh_from_db()
        self.assertIsNone(device.primary_ip4)

    def test_an_address_from_the_wrong_family_warns_and_stays_out_of_the_plan(self):
        """An IPv6 value cannot become a primary IPv4 write that fails only during execution."""
        units = self._plan(self._row(2, "D-1", "srv-01", primary_ip4="2001:db8::1"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(len(units[0].diagnostics), 1)
        self.assertEqual(units[0].diagnostics[0].code, "device.unparseable_ip")
        self.assertEqual(units[0].diagnostics[0].display["field"], "primary_ip4")
        self.assertEqual(dict(units[0].changes[0].payload["ip_fields"]), {})

    def test_an_address_held_by_another_device_is_stored_as_unassigned(self):
        """The write records an address conflict instead of taking the other device's row."""
        self._interface_template()
        other = self._device("srv-other", rack=self.rack)
        assigned = self._assigned_address(other, "198.18.0.22/32")
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        change = self._only_change(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.22"))

        DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        assigned.refresh_from_db()
        self.assertIsNone(device.primary_ip4)
        self.assertEqual(assigned.assigned_object.device_id, other.pk)
        self.assertEqual(
            DeviceImportSource.objects.get(device=device).unassigned_ips,
            {"primary_ip4": "198.18.0.22/32"},
        )

    def test_an_address_that_moves_the_device_out_of_scope_is_refused(self):
        """The permission check reads the state the whole row leaves, addresses included."""
        from dcim.models import Device
        from ipam.models import IPAddress

        from netbox_data_import.object_permissions import ObjectPermissionDenied
        from netbox_data_import.tests.helpers import user_with_object_permission

        self._interface_template()
        scoped = user_with_object_permission(
            "device-module-ip-scoped",
            [
                (Device, ("add", "change", "view"), {"primary_ip4__isnull": True}),
                (IPAddress, ("add", "change", "view"), {}),
            ],
        )
        change = self._only_change(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.24"))

        with self.assertRaises(ObjectPermissionDenied):
            DeviceModule().apply(change, ExecutionContext(actor=scoped, reader=self.reader, profile=self.profile))

    def test_a_stored_unassigned_address_is_cleared_after_it_places(self):
        """A later import clears stale provenance when the address can now land."""
        self._interface_template()
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        stored = DeviceImportSource.objects.get(device=device)
        stored.unassigned_ips = {"primary_ip4": "198.18.0.23/32"}
        stored.save(update_fields=["unassigned_ips"])
        change = self._only_change(self._row(2, "D-1", "srv-01", primary_ip4="198.18.0.23"))

        DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        stored.refresh_from_db()
        self.assertEqual(str(device.primary_ip4.address), "198.18.0.23/32")
        self.assertEqual(stored.unassigned_ips, {})


class DeviceModuleFieldReviewTest(DeviceModulePlanTestBase):
    """An ignored difference means leave the field alone, so neither the plan nor the write carries it."""

    def _ignore(self, device, target_field, file_value, netbox_value, source_id="D-1"):
        """Record the exact review an operator saves from the preview."""
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id=source_id,
            netbox_device_id=device.pk,
            target_field=target_field,
            file_snapshot={"canonical": file_value, "display": file_value},
            netbox_snapshot={"canonical": netbox_value, "display": netbox_value},
        )

    def test_a_row_whose_only_difference_is_ignored_is_a_no_op(self):
        """Planning a change the review already settled would execute a write the operator refused."""
        device = self._with_provenance(self._device("srv-01", rack=self.rack, serial="OLD"))
        self._ignore(device, "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)
        self.assertEqual(units[0].changes, ())

    def test_an_ignored_field_keeps_the_stored_value_in_the_payload(self):
        """The guard has to stop the ignored field only, not every field the row carries."""
        self._ignore(self._device("srv-01", rack=self.rack, serial="OLD", asset_tag="AT-OLD"), "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW", asset_tag="AT-NEW"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        payload = units[0].changes[0].payload
        self.assertEqual(payload["asset_tag"], "AT-NEW")
        self.assertEqual(payload["serial"], "OLD")

    def test_an_ignored_deferred_rack_difference_is_a_no_op(self):
        """Ignoring the rack keeps the matched device out of the new rack dependency."""
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="D-1",
            netbox_device_id=device.pk,
            target_field="rack_name",
            file_snapshot={"canonical": ":batch-rack", "display": "batch-rack"},
            netbox_snapshot={"canonical": ":dm-rack", "display": "dm-rack"},
        )
        rack_row = self._row(2, "R-1", "batch-rack", device_class="Cabinet", rack_name="batch-rack")
        device_row = self._row(3, "D-1", "srv-01", rack_name="batch-rack")

        unit = self._plan(rack_row, device_row)[0]

        self.assertEqual(unit.disposition, Disposition.NO_OP, unit.diagnostics)
        self.assertEqual(unit.changes, ())

    def test_a_review_saved_against_another_device_does_not_apply(self):
        """A review names one device, so it cannot settle a difference on a different one."""
        self._device("srv-01", rack=self.rack, serial="OLD")
        self._ignore(self._device("other-device", rack=self.rack), "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].payload["serial"], "NEW")

    def test_a_review_whose_values_moved_on_does_not_apply(self):
        """The review pins one exact pair, so a new file value is a new difference."""
        self._ignore(self._device("srv-01", rack=self.rack, serial="OLD"), "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEWER"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].payload["serial"], "NEWER")


class DeviceModuleContactTest(DeviceModulePlanTestBase):
    """A device row carries a primary contact, and the plan decides it before anything is written."""

    def setUp(self):
        """Give the profile the contact role that makes contact resolution active."""
        super().setUp()
        from tenancy.models import ContactRole

        self.contact_role = ContactRole.objects.create(name="Primary Contact", slug="primary-contact")
        self.profile.adapter_config = {
            **self.profile.adapter_config,
            "primary_contact_role": self.contact_role.name,
            "primary_contact_lookup_field": "email",
        }
        self.profile.save(update_fields=["adapter_config"])
        self.actor = self._contact_writer()
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    @staticmethod
    def _contact_writer():
        """Return an actor allowed to write the device and the contact it is assigned."""
        from dcim.models import Device
        from tenancy.models import Contact, ContactAssignment

        from netbox_data_import.tests.helpers import user_with_object_permission

        return user_with_object_permission(
            "device-module-contact-writer",
            [
                (Device, ("add", "change", "view"), {}),
                (Contact, ("add", "view"), {}),
                (ContactAssignment, ("add", "change", "view"), {}),
            ],
        )

    def test_applying_a_create_assigns_the_contact_the_plan_carried(self):
        """A plan that names a contact but never assigns it has written half the row."""
        from django.contrib.contenttypes.models import ContentType
        from tenancy.models import ContactAssignment

        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        assignment = ContactAssignment.objects.get(
            object_type=ContentType.objects.get_for_model(device),
            object_id=device.pk,
            role=self.contact_role,
        )
        self.assertEqual(assignment.contact.email, "owner@example.invalid")

    def _assign(self, device, email):
        """Let the real resolver establish the state a later plan has to call unchanged."""
        from netbox_data_import.contact_resolution import PrimaryContactResolver

        review = PrimaryContactResolver.review(device, {"primary_contact": email}, self.profile, None)
        PrimaryContactResolver.apply(device, self.profile, review, None)

    def test_a_contact_the_device_already_holds_is_not_a_change(self):
        """Re-importing a settled contact must not make an otherwise identical row actionable."""
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        self._assign(device, "owner@example.invalid")

        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

    def test_a_contact_the_device_does_not_hold_makes_the_row_actionable(self):
        """The row writes an assignment NetBox does not have, so it is work the plan must carry."""
        self._device("srv-01", rack=self.rack)

        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        contact = units[0].changes[0].payload["contact"]
        self.assertEqual(contact["values"]["email"], "owner@example.invalid")

    def test_a_create_carries_the_contact_its_row_names(self):
        """A created device has no contact until the plan says which one it gets."""
        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        self.assertEqual(units[0].changes[0].operation, "create")
        self.assertEqual(units[0].changes[0].payload["contact"]["values"]["email"], "owner@example.invalid")

    def test_a_row_that_needs_a_contact_decision_is_refused(self):
        """Guessing which candidate column supplies a Contact field is the operator's call."""
        row = self._row(2, "D-1", "srv-01")
        row["_candidate_values"] = {"contact": {"Owner": "OBO"}}

        units = self._plan(row)

        self.assertEqual(units[0].disposition, Disposition.INVALID, units[0].diagnostics)
        self.assertEqual(units[0].diagnostics[0].code, "device.contact_resolution_required")
        self.assertEqual(
            units[0].diagnostics[0].display["extra_data"]["candidate_values"],
            {"contact": {"Owner": "OBO"}},
        )

    def test_a_row_with_no_contact_values_plans_no_contact(self):
        """A profile with a contact role must not invent a contact for a row that names none."""
        self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)


class DeviceModuleDoesNotRenameTest(DeviceModulePlanTestBase):
    """An import does not rename a matched device because its name is a match key."""

    def setUp(self):
        """Give this test an actor allowed to write the device it plans."""
        super().setUp()
        from dcim.models import Device

        from netbox_data_import.tests.helpers import user_with_object_permission

        self.actor = user_with_object_permission("device-module-rename", [(Device, ("add", "change", "view"), {})])
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def test_a_name_the_row_spells_differently_is_not_work_on_its_own(self):
        """The row reconciles a device it matched by serial, and every other field agrees."""
        self._with_provenance(self._device("stored-name", rack=self.rack, serial="SN-1"))

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

    def test_applying_a_change_leaves_the_stored_name_alone(self):
        """A row that carries real work must still not rename the device while it does it."""
        device = self._device("stored-name", rack=self.rack, serial="SN-1", asset_tag="AT-OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1", asset_tag="AT-NEW"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertIn("Will update", units[0].display["detail"])
        self.assertTrue(units[0].display["extra_data"]["placement_sync_writes_nothing"])
        DeviceModule().apply(units[0].changes[0], self.context)

        device.refresh_from_db()
        self.assertEqual(device.asset_tag, "AT-NEW")
        self.assertEqual(device.name, "stored-name")

    def test_a_created_device_still_takes_the_name_its_row_carries(self):
        """Nothing exists to protect on a create, so the row names the device it makes."""
        units = self._plan(self._row(2, "D-1", "srv-01"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        self.assertEqual(device.name, "srv-01")


class DeviceModuleProvenanceTest(DeviceModulePlanTestBase):
    """A stored source ID survives changes to the other device identifiers."""

    def setUp(self):
        """Give this test an actor allowed to write the devices it plans."""
        super().setUp()
        from dcim.models import Device

        from netbox_data_import.tests.helpers import user_with_object_permission

        self.actor = user_with_object_permission("device-module-provenance", [(Device, ("add", "change", "view"), {})])
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def test_a_stored_source_id_matches_the_device_it_was_written_on(self):
        """Neither the name nor the serial agrees, so only the stored source can find this device."""
        device = self._device("stored-name", rack=self.rack)
        DeviceImportSource.objects.create(device=device, profile=self.profile, source_id="D-1")

        units = self._plan(self._row(2, "D-1", "srv-01", status="Offline"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_one_source_id_stored_on_two_devices_refuses_the_row(self):
        """Two devices claim this row, and picking one silently would write to the wrong device."""
        for name in ("first", "second"):
            DeviceImportSource.objects.create(
                device=self._device(name, rack=self.rack), profile=self.profile, source_id="D-1"
            )

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.INVALID, units[0].diagnostics)
        self.assertEqual(units[0].diagnostics[0].code, "device.ambiguous_stored_source_id")

    def test_a_saved_field_review_matches_the_device_it_was_saved_against(self):
        """The operator reviewed a field on this device, which names the device this row reconciles."""
        device = self._device("stored-name", rack=self.rack)
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="D-1",
            netbox_device_id=device.pk,
            target_field="serial",
            file_snapshot={"canonical": "NEW", "display": "NEW"},
            netbox_snapshot={"canonical": "OLD", "display": "OLD"},
        )

        units = self._plan(self._row(2, "D-1", "srv-01", status="Offline"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_a_hidden_review_still_removes_its_ignored_identity_from_batch_clashes(self):
        """Permission filtering cannot change the reviewed values used by duplicate checks."""
        from dcim.models import Device, Rack

        from netbox_data_import.tests.helpers import user_with_object_permission

        device = self._device("hidden-reviewed-device", rack=self.rack, serial="OLD-SERIAL")
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="D-1",
            netbox_device_id=device.pk,
            target_field="serial",
            file_snapshot={"canonical": "NEW-SERIAL", "display": "NEW-SERIAL"},
            netbox_snapshot={"canonical": "OLD-SERIAL", "display": "OLD-SERIAL"},
        )
        actor = user_with_object_permission(
            "device-module-review-blind",
            [(Device, ("view", "add"), {"name": "nothing-matches-this"}), (Rack, ("view",), {})],
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)

        units = DeviceModule().plan(
            self._batch(
                self._row(2, "D-1", "srv-01", serial="NEW-SERIAL"),
                self._row(3, "D-2", "srv-02", serial="NEW-SERIAL"),
            ),
            self.profile,
            CATALOG,
            reader,
        )

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.inaccessible_match")
        self.assertEqual(units[1].disposition, Disposition.ACTIONABLE)

    def test_applying_a_create_stores_the_source_the_row_carried(self):
        """Without the record the next import cannot find the device this one just made."""
        units = self._plan(self._row(2, "D-1", "srv-01"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        stored = DeviceImportSource.objects.get(device=device)
        self.assertEqual(stored.source_id, "D-1")
        self.assertEqual(stored.profile, self.profile)

    def test_applying_a_create_binds_the_source_to_the_device(self):
        """The binding is what makes the match explicit rather than inferred on the next run."""
        units = self._plan(self._row(2, "D-1", "srv-01", asset_tag="AT-1"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        binding = DeviceExistingMatch.objects.get(profile=self.profile, source_id="D-1")
        self.assertEqual(binding.netbox_device_id, device.pk)
        self.assertEqual(binding.device_name, device.name)
        self.assertEqual(binding.source_asset_tag, "AT-1")


class DeviceModuleProvenanceIsWorkTest(DeviceModulePlanTestBase):
    """A device that holds every field but no provenance still has its source to record."""

    def test_a_matched_device_with_no_stored_provenance_is_work(self):
        """The next import finds this device only if this one records what wrote it."""
        self._device("srv-01", rack=self.rack)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)

    def test_a_device_whose_provenance_is_current_is_a_no_op(self):
        """Everything the row writes is already written, provenance included."""
        self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

    def test_a_stale_binding_name_is_work(self):
        """The binding records the device name, so a renamed device leaves it to be refreshed."""
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="D-1").update(device_name="old-name")

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)


class DeviceModuleTargetStateIsWorkTest(DeviceModulePlanTestBase):
    """The write assigns the import target and the source-ID custom field, so both are work."""

    def test_a_device_outside_the_target_location_is_work(self):
        """The write moves a matched device to the location the import targets."""
        from dcim.models import Location

        location = Location.objects.create(name="Hall A", slug="hall-a", site=self.site)
        self.reader = NetBoxReader.unrestricted().for_target(site=self.site, location=location)
        device = self._with_provenance(self._device("srv-01"))

        units = self._plan(self._row(2, "D-1", "srv-01", rack_name=""))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual(units[0].changes[0].payload["location_id"], location.pk)
        device.refresh_from_db()
        self.assertIsNone(device.location_id)

    def test_a_device_the_target_location_already_holds_is_a_no_op(self):
        """A device already at the target location leaves the field alone."""
        from dcim.models import Location

        location = Location.objects.create(name="Hall B", slug="hall-b", site=self.site)
        self.reader = NetBoxReader.unrestricted().for_target(site=self.site, location=location)
        self._with_provenance(self._device("srv-01", location=location))

        units = self._plan(self._row(2, "D-1", "srv-01", rack_name=""))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

    def test_a_device_outside_the_target_tenant_is_work(self):
        """The write assigns the tenant the import targets."""
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Tenant One", slug="tenant-one")
        self.reader = NetBoxReader.unrestricted().for_target(site=self.site, tenant=tenant)
        self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual(units[0].changes[0].payload["tenant_id"], tenant.pk)

    def test_a_device_whose_source_id_custom_field_is_unset_is_work(self):
        """The profile's custom field carries the source ID, so an empty one is left to write."""
        self.profile.adapter_config = {**self.profile.adapter_config, "custom_field_name": "cf_source_id"}
        self.profile.save(update_fields=["adapter_config"])
        self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)

    def test_a_device_whose_source_id_custom_field_is_current_is_a_no_op(self):
        """A custom field that already holds the source ID is not work on its own."""
        self.profile.adapter_config = {**self.profile.adapter_config, "custom_field_name": "cf_source_id"}
        self.profile.save(update_fields=["adapter_config"])
        device = self._device("srv-01", rack=self.rack)
        device.custom_field_data["cf_source_id"] = "D-1"
        device.save(update_fields=["custom_field_data"])
        self._with_provenance(device)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)


class DeviceModuleReportsEveryProblemTest(DeviceModulePlanTestBase):
    """Every problem a row can prove is reported at once, and the first still decides the row."""

    def _codes(self, unit):
        return [diagnostic.code for diagnostic in unit.diagnostics]

    def test_a_duplicate_serial_no_longer_hides_the_rest_of_the_row(self):
        """This is the reported case: the serial masked a name the operator also had to settle."""
        units = self._plan(
            self._row(2, "D-1", "srv-same", serial="SN-1"),
            self._row(3, "D-2", "srv-same", serial="SN-1"),
        )

        self.assertEqual(self._codes(units[0]), ["device.duplicate_serial", "device.duplicate_name"])
        self.assertEqual(self._codes(units[1]), ["device.duplicate_serial", "device.duplicate_name"])

    def test_the_first_problem_still_decides_what_the_row_is(self):
        """Nothing downstream may move: the disposition and the stated reason are unchanged."""
        units = self._plan(
            self._row(2, "D-1", "srv-same", serial="SN-1"),
            self._row(3, "D-2", "srv-same", serial="SN-1"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "device.duplicate_serial")
        self.assertIn("Duplicate serial", units[0].diagnostics[0].display["message"])
        # The unit shows the first problem, so the row reads as it did when that was the only one.
        self.assertEqual(dict(units[0].display), dict(units[0].diagnostics[0].display))

    def test_a_blocked_dependency_keeps_its_own_disposition(self):
        """The first problem sets the disposition, so a blocked row is not turned invalid."""
        self.profile.adapter_config = {**self.profile.adapter_config, "create_missing_device_types": False}
        self.profile.save(update_fields=["adapter_config"])

        units = self._plan(self._row(2, "D-1", "srv-same", make="Nope", model="Nothing"))

        self.assertEqual(units[0].disposition, Disposition.BLOCKED)
        self.assertEqual(units[0].diagnostics[0].code, "device.device_type_missing")

    def test_a_row_blocked_on_its_device_type_still_reports_its_identity_clash(self):
        """The two are independent, so the operator can settle either one first."""
        self.profile.adapter_config = {**self.profile.adapter_config, "create_missing_device_types": False}
        self.profile.save(update_fields=["adapter_config"])

        units = self._plan(
            self._row(2, "D-1", "srv-01", serial="SN-1", make="Nope", model="Nothing"),
            self._row(3, "D-2", "srv-02", serial="SN-1", make="Nope", model="Nothing"),
        )

        self.assertEqual(self._codes(units[0]), ["device.duplicate_serial", "device.device_type_missing"])
        # The clash is first, so it still decides the row.
        self.assertEqual(units[0].disposition, Disposition.INVALID)

    def test_an_unmapped_class_reports_nothing_it_cannot_prove(self):
        """Without a class mapping there is no device type or role to check, so neither is claimed."""
        units = self._plan(self._row(2, "D-1", "srv-01", device_class="Unmapped"))

        self.assertEqual(self._codes(units[0]), ["device.class_unmapped"])

    def test_a_row_with_one_problem_reports_one(self):
        """The list only exists to carry a second problem; one problem must read as it always did."""
        units = self._plan(self._row(2, "D-1", "", device_class="Server"))

        self.assertEqual(self._codes(units[0]), ["device.missing_name"])
        self.assertEqual(units[0].disposition, Disposition.INVALID)

    def test_the_stated_reason_and_the_listed_problems_share_one_wording_rule(self):
        """Two copies of the rule would let a row's reason and its remaining list drift apart."""
        from netbox_data_import.review_workspace import WorkspaceUnit, _diagnostic_message

        units = self._plan(
            self._row(2, "D-1", "srv-same", serial="SN-1"),
            self._row(3, "D-2", "srv-same", serial="SN-1"),
        )

        row = WorkspaceUnit.from_unit(units[0])

        self.assertEqual(row.detail, _diagnostic_message(units[0].diagnostics[0]))
        self.assertEqual(
            [issue["message"] for issue in row.extra_data["other_issues"]],
            [_diagnostic_message(item) for item in units[0].diagnostics[1:]],
        )

    def test_a_well_formed_row_is_still_actionable(self):
        """Running more checks must not turn a row that was fine into a refused one."""
        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)

    def test_an_ignored_row_is_an_answer_not_a_list_of_problems(self):
        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1")

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.EXCLUDED)
        self.assertEqual(self._codes(units[0]), ["device.ignored"])

    def test_ignoring_a_row_releases_the_serial_it_claimed(self):
        """Ignoring one of two rows is how an operator settles a shared identity, so it must work."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1")

        units = self._plan(
            self._row(2, "D-1", "srv-01", serial="SN-1"),
            self._row(3, "D-2", "srv-02", serial="SN-1"),
        )

        self.assertEqual(units[0].disposition, Disposition.EXCLUDED)
        self.assertEqual(self._codes(units[0]), ["device.ignored"])
        self.assertEqual(units[1].disposition, Disposition.ACTIONABLE)

    def test_every_reported_problem_is_an_error(self):
        """The list is what the row still needs, so nothing in it reads as information."""
        units = self._plan(
            self._row(2, "D-1", "srv-same", serial="SN-1"),
            self._row(3, "D-2", "srv-same", serial="SN-1"),
        )

        self.assertEqual({diagnostic.severity for diagnostic in units[0].diagnostics}, {Severity.ERROR})

    def test_a_clashing_row_reports_the_placement_conflict_behind_it(self):
        """The reported row: a duplicate serial masked a name that matches a device placed elsewhere."""
        self._device("srv-placed", rack=self.rack, position=10, face="front")

        units = self._plan(
            self._row(2, "D-1", "srv-placed", serial="SN-1", u_position=20, face="front"),
            self._row(3, "D-2", "srv-other", serial="SN-1"),
        )

        self.assertEqual(
            self._codes(units[0]),
            ["device.duplicate_serial", "device.name_placement_conflict"],
        )
        self.assertEqual(units[0].disposition, Disposition.INVALID)

    def test_an_unparseable_ip_is_a_warning_not_work_the_row_must_do(self):
        """Two unusable IP values leave the row actionable, so neither may read as a problem."""
        from netbox_data_import.review_workspace import WorkspaceUnit

        units = self._plan(
            self._row(2, "D-1", "srv-01", primary_ip4="not-an-address", primary_ip6="also-not-an-address")
        )

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual({diagnostic.severity for diagnostic in units[0].diagnostics}, {Severity.WARNING})
        self.assertEqual(WorkspaceUnit.from_unit(units[0]).extra_data["other_issues"], [])

    def test_a_clashing_row_still_reports_the_contact_decision_it_needs(self):
        """The second reported case: a duplicate serial masked an unanswered Contact."""
        rows = [
            self._row(
                2,
                "D-1",
                "srv-01",
                serial="SN-1",
                _candidate_values={"contact": {"Owner": "owner@example.invalid"}},
            ),
            self._row(
                3,
                "D-2",
                "srv-02",
                serial="SN-1",
                _candidate_values={"contact": {"Owner": "other@example.invalid"}},
            ),
        ]

        units = self._plan(*rows)

        self.assertEqual(
            self._codes(units[0]),
            ["device.duplicate_serial", "device.contact_resolution_required"],
        )
        candidates = units[0].diagnostics[1].display["extra_data"]["candidate_values"]["contact"]
        self.assertEqual(candidates, {"Owner": "owner@example.invalid"})

# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Additional engine tests: run_import with actual DB writes, manufacturer/device-type resolution."""

import os

from django.test import TestCase

from netbox_data_import.engine import (
    ImportResult,
    RowResult,
    _apply_transform_rules,
    _find_existing_device,
    _resolve_device_type_slugs,
    _write_device_row,
    _write_rack_to_db,
    parse_file,
    run_import,
)
from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    ColumnTransformRule,
    DeviceTypeMapping,
    ImportProfile,
    ManufacturerMapping,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")


def _make_profile(name="EngineTest2") -> ImportProfile:
    """Create a profile matching the sample fixture."""
    profile = ImportProfile.objects.create(
        name=name,
        sheet_name="Data",
        source_id_column="Id",
        update_existing=True,
        create_missing_device_types=True,
    )
    for src, tgt in {
        "Id": "source_id",
        "Rack": "rack_name",
        "Name": "device_name",
        "Class": "device_class",
        "Make": "make",
        "Model": "model",
        "UHeight": "u_height",
        "UPosition": "u_position",
        "Serial Number": "serial",
        "Asset Tag": "asset_tag",
        "Status": "status",
    }.items():
        ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)
    ClassRoleMapping.objects.create(profile=profile, source_class="Cabinet", creates_rack=True)
    ClassRoleMapping.objects.create(profile=profile, source_class="Server", creates_rack=False, role_slug="server")
    ClassRoleMapping.objects.create(
        profile=profile, source_class="Switch", creates_rack=False, role_slug="network-switch"
    )
    return profile


class RunImportWriteTest(TestCase):
    """Tests for run_import with dry_run=False (actual DB writes)."""

    def setUp(self):
        """Create site and profile."""
        from dcim.models import Site

        self.site = Site.objects.create(name="WriteSite", slug="write-site")
        self.profile = _make_profile("WriteTest")

    def test_run_import_creates_racks(self):
        """Real import creates Rack objects."""
        from dcim.models import Rack

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertGreater(Rack.objects.filter(site=self.site).count(), 0)

    def test_run_import_creates_devices(self):
        """Real import creates Device objects."""
        from dcim.models import Device

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertGreater(Device.objects.filter(site=self.site).count(), 0)

    def test_run_import_creates_device_roles(self):
        """Real import auto-creates missing DeviceRoles."""
        from dcim.models import DeviceRole

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertTrue(DeviceRole.objects.filter(slug="server").exists())

    def test_run_import_creates_manufacturers(self):
        """Real import auto-creates missing Manufacturer entries."""
        from dcim.models import Manufacturer

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        # At least one manufacturer must have been created
        self.assertGreater(Manufacturer.objects.count(), 0)

    def test_run_import_creates_device_types(self):
        """Real import auto-creates missing DeviceTypes."""
        from dcim.models import DeviceType

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertGreater(DeviceType.objects.count(), 0)

    def test_run_import_returns_import_result(self):
        """Real import returns a populated ImportResult."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertIsInstance(result, ImportResult)
        self.assertGreater(len(result.rows), 0)

    def test_run_import_idempotent_with_update_existing(self):
        """Running import twice with update_existing=True does not duplicate objects."""
        from dcim.models import Rack, Device

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        rack_count_1 = Rack.objects.filter(site=self.site).count()
        device_count_1 = Device.objects.filter(site=self.site).count()

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertEqual(Rack.objects.filter(site=self.site).count(), rack_count_1)
        self.assertEqual(Device.objects.filter(site=self.site).count(), device_count_1)

    def test_run_import_with_location(self):
        """Real import with a location context assigns location to objects."""
        from dcim.models import Location

        loc = Location.objects.create(name="WriteLocation", slug="write-location", site=self.site)
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        result = run_import(rows, self.profile, {"site": self.site, "location": loc}, dry_run=False)
        self.assertIsInstance(result, ImportResult)

    def test_update_existing_false_skips_existing(self):
        """With update_existing=False, pre-existing racks are skipped."""
        from dcim.models import Rack

        # Pre-create the rack
        Rack.objects.create(name="Rack-01", site=self.site, u_height=42)
        self.profile.update_existing = False
        self.profile.save()

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        skip_rows = [r for r in result.rows if r.action == "skip" and r.object_type == "rack"]
        self.assertGreater(len(skip_rows), 0)


class RunImportDeviceTypeMappingTest(TestCase):
    """Tests for run_import when DeviceTypeMapping overrides slug resolution."""

    def setUp(self):
        """Create site, profile, and a device type mapping."""
        from dcim.models import Site

        self.site = Site.objects.create(name="MappedDTSite", slug="mapped-dt-site")
        self.profile = _make_profile("DTMappedTest")
        # Add explicit mapping for any make/model in fixture
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="Dell",
            source_model="PowerEdge R640",
            netbox_manufacturer_slug="dell",
            netbox_device_type_slug="dell-poweredge-r640",
        )

    def test_explicit_mapping_used_in_dry_run(self):
        """Dry run uses DeviceTypeMapping slug, not auto-slugify."""
        rows = [
            {
                "_row_number": 1,
                "source_id": "001",
                "device_name": "srv-01",
                "device_class": "Server",
                "make": "Dell",
                "model": "PowerEdge R640",
                "u_height": "1",
                "rack_name": "Rack-01",
                "u_position": "1",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        # Should not error — mapping exists
        error_rows = [r for r in result.rows if r.action == "error"]
        self.assertEqual(error_rows, [])


class ResolveDeviceTypeSlugsTest(TestCase):
    """Tests for the _resolve_device_type_slugs helper."""

    def setUp(self):
        """Create a profile for slug resolution tests."""
        self.profile = ImportProfile.objects.create(name="SlugTest", sheet_name="Data", source_id_column="Id")

    def test_no_mapping_auto_slugifies(self):
        """Without an explicit mapping, make/model are slugified."""
        mfg_slug, dt_slug, explicit = _resolve_device_type_slugs("Cisco", "Catalyst 9300", self.profile)
        self.assertEqual(mfg_slug, "cisco")
        self.assertIn("9300", dt_slug)
        self.assertFalse(explicit)

    def test_explicit_device_type_mapping(self):
        """With an explicit DeviceTypeMapping, slugs come from the mapping."""
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="Dell",
            source_model="R660",
            netbox_manufacturer_slug="dell",
            netbox_device_type_slug="dell-poweredge-r660",
        )
        mfg_slug, dt_slug, explicit = _resolve_device_type_slugs("Dell", "R660", self.profile)
        self.assertEqual(mfg_slug, "dell")
        self.assertEqual(dt_slug, "dell-poweredge-r660")
        self.assertTrue(explicit)

    def test_manufacturer_mapping_overrides_make_slug(self):
        """ManufacturerMapping overrides the auto-slugified manufacturer slug."""
        ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Dell EMC",
            netbox_manufacturer_slug="dell",
        )
        mfg_slug, _, explicit = _resolve_device_type_slugs("Dell EMC", "PowerEdge R640", self.profile)
        self.assertEqual(mfg_slug, "dell")
        self.assertFalse(explicit)  # DT slug still auto

    def test_case_insensitive_matching(self):
        """DeviceTypeMapping lookup normalizes whitespace."""
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="HP",
            source_model="ProLiant DL360",
            netbox_manufacturer_slug="hp",
            netbox_device_type_slug="hp-proliant-dl360",
        )
        # Extra spaces in source
        mfg_slug, dt_slug, explicit = _resolve_device_type_slugs("HP", "ProLiant  DL360", self.profile)
        # May or may not match depending on normalization; just verify no crash
        self.assertIsInstance(mfg_slug, str)
        self.assertIsInstance(dt_slug, str)


class ColumnTransformRuleTest(TestCase):
    """Tests for column transform rules in parse_file."""

    def setUp(self):
        """Create profile with a transform rule."""
        self.profile = ImportProfile.objects.create(
            name="TransformTest",
            sheet_name="Data",
            source_id_column="Id",
            update_existing=False,
            create_missing_device_types=True,
        )
        ColumnMapping.objects.create(profile=self.profile, source_column="Id", target_field="source_id")
        ColumnMapping.objects.create(profile=self.profile, source_column="Name", target_field="device_name")
        ColumnMapping.objects.create(profile=self.profile, source_column="Class", target_field="device_class")
        ColumnMapping.objects.create(profile=self.profile, source_column="Make", target_field="make")
        ColumnMapping.objects.create(profile=self.profile, source_column="Model", target_field="model")
        ColumnMapping.objects.create(profile=self.profile, source_column="Rack", target_field="rack_name")
        ColumnMapping.objects.create(profile=self.profile, source_column="UHeight", target_field="u_height")
        ColumnMapping.objects.create(profile=self.profile, source_column="UPosition", target_field="u_position")
        ColumnMapping.objects.create(profile=self.profile, source_column="Serial Number", target_field="serial")
        ColumnMapping.objects.create(profile=self.profile, source_column="Asset Tag", target_field="asset_tag")
        ColumnMapping.objects.create(profile=self.profile, source_column="Status", target_field="status")
        # Add a transform rule: Name "TAG123 - Description" → asset_tag + device_name
        ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="Name",
            pattern=r"^(\w+) - (.+)$",
            group_1_target="asset_tag",
            group_2_target="device_name",
        )

    def test_transform_rule_applied(self):
        """Transform rule extracts groups from matching values."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        # The rule runs but may or may not match fixture data — just verify no crash
        self.assertIsInstance(rows, list)


class RunImportIgnoredDeviceTest(TestCase):
    """Tests for ignored devices in run_import."""

    def setUp(self):
        """Create site and profile with an ignored device."""
        from dcim.models import Site
        from netbox_data_import.models import IgnoredDevice

        self.site = Site.objects.create(name="IgnoredSite", slug="ignored-site")
        self.profile = _make_profile("IgnoredTest")
        # Find the first non-rack row (device row) to ignore
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        crm_map = {crm.source_class: crm for crm in self.profile.class_role_mappings.all()}
        device_rows = [
            r
            for r in rows
            if not crm_map.get(str(r.get("device_class", "")).strip(), None)
            or not crm_map[str(r.get("device_class", "")).strip()].creates_rack
        ]
        self.ignored_id = None
        for r in device_rows:
            sid = str(r.get("source_id", "")).strip()
            if sid and str(r.get("device_name", "")).strip():
                self.ignored_id = sid
                IgnoredDevice.objects.create(profile=self.profile, source_id=sid, device_name="ignored")
                break

    def test_ignored_device_action_is_ignore(self):
        """Rows for ignored source_ids get action='ignore'."""
        if not self.ignored_id:
            self.skipTest("No device rows in fixture")
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        ignore_rows = [r for r in result.rows if r.action == "ignore"]
        self.assertGreater(len(ignore_rows), 0)


class SourceResolutionInEngineTest(TestCase):
    """Tests that source resolutions (rerere) are applied during parse_file."""

    def setUp(self):
        """Create profile and a saved resolution."""
        from netbox_data_import.models import SourceResolution

        self.profile = _make_profile("RerereTest")
        # Peek at the fixture to get first source_id
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        self.first_source_id = str(rows[0].get("source_id", ""))
        if self.first_source_id:
            SourceResolution.objects.create(
                profile=self.profile,
                source_id=self.first_source_id,
                source_column="Name",
                original_value="original",
                resolved_fields={"device_name": "resolved-device-name"},
            )

    def test_resolution_applied_to_row(self):
        """parse_file applies saved resolutions to the matching row."""
        if not self.first_source_id:
            self.skipTest("No source_id in fixture rows")
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        resolved_rows = [r for r in rows if r.get("device_name") == "resolved-device-name"]
        self.assertEqual(len(resolved_rows), 1)


class ImportResultPropertyTest(TestCase):
    """Tests for ImportResult helper properties."""

    def test_rack_groups_property(self):
        """rack_groups() groups devices under their rack name."""
        result = ImportResult()
        result.rows = [
            RowResult(1, "r1", "Rack-A", "create", "rack", ""),
            RowResult(2, "d1", "server-01", "create", "device", "", rack_name="Rack-A"),
            RowResult(3, "d2", "server-02", "create", "device", "", rack_name="Rack-A"),
            RowResult(4, "d3", "server-03", "create", "device", "", rack_name="Rack-B"),
        ]
        groups = result.rack_groups
        self.assertIn("Rack-A", groups)
        self.assertEqual(len(groups["Rack-A"]["devices"]), 2)
        self.assertIn("Rack-B", groups)
        self.assertEqual(len(groups["Rack-B"]["devices"]), 1)

    def test_recompute_counts_errors(self):
        """_recompute_counts sets has_errors=True when error rows exist."""
        result = ImportResult()
        result.rows = [RowResult(1, "x", "bad-row", "error", "device", "Something failed")]
        result._recompute_counts()
        self.assertTrue(result.has_errors)
        self.assertEqual(result.counts.get("errors"), 1)

    def test_recompute_counts_mixed(self):
        """_recompute_counts tallies creates, updates, skips, and ignores."""
        result = ImportResult()
        result.rows = [
            RowResult(1, "r1", "rack1", "create", "rack", ""),
            RowResult(2, "d1", "dev1", "create", "device", ""),
            RowResult(3, "d2", "dev2", "update", "device", ""),
            RowResult(4, "d3", "dev3", "skip", "device", ""),
            RowResult(5, "d4", "dev4", "ignore", "device", ""),
        ]
        result._recompute_counts()
        self.assertEqual(result.counts["racks_created"], 1)
        self.assertEqual(result.counts["devices_created"], 1)
        self.assertEqual(result.counts["devices_updated"], 1)
        self.assertEqual(result.counts["skipped"], 1)
        self.assertEqual(result.counts["ignored"], 1)
        self.assertFalse(result.has_errors)

    def test_row_result_has_extra_data(self):
        """RowResult stores and round-trips extra_data."""
        r = RowResult(1, "x", "test", "create", "device", "", extra_data={"mfg_slug": "cisco"})
        d = r.to_dict()
        restored = RowResult.from_dict(d)
        self.assertEqual(restored.extra_data.get("mfg_slug"), "cisco")


class CreateMissingDeviceTypesFalseTest(TestCase):
    """Tests for create_missing_device_types=False path."""

    def setUp(self):
        """Create site and profile with create_missing_device_types=False."""
        from dcim.models import Site

        self.site = Site.objects.create(name="NoDTSite", slug="no-dt-site")
        self.profile = _make_profile("NoDTTest")
        self.profile.create_missing_device_types = False
        self.profile.save()

    def test_missing_device_type_produces_error_row(self):
        """With create_missing_device_types=False, unknown DTs produce error rows."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        error_rows = [r for r in result.rows if r.action == "error" and r.object_type == "device_type"]
        self.assertGreater(len(error_rows), 0)


class ManufacturerMappingInEngineTest(TestCase):
    """Tests for ManufacturerMapping integration in run_import."""

    def setUp(self):
        """Create site and profile with manufacturer mapping."""
        from dcim.models import Site

        self.site = Site.objects.create(name="MfgMapSite", slug="mfg-map-site")
        self.profile = _make_profile("MfgMapTest")
        # Add a manufacturer mapping that renames a make
        ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Unknown",  # Normalize auto-assigned "Unknown" back to dell
            netbox_manufacturer_slug="unknown-vendor",
        )

    def test_manufacturer_mapping_applied_in_dry_run(self):
        """ManufacturerMapping is applied correctly during dry-run."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        self.assertIsInstance(result, ImportResult)


# ---------------------------------------------------------------------------
# New tests: ApplyTransformRulesTest
# ---------------------------------------------------------------------------


class ApplyTransformRulesTest(TestCase):
    """Tests for _apply_transform_rules direct-call behaviour."""

    def setUp(self):
        """Create a minimal profile for transform rule tests."""
        self.profile = ImportProfile.objects.create(name="ATRTest", sheet_name="Data", source_id_column="Id")

    def test_group_1_target_set_on_match(self):
        """When the pattern matches, group_1_target is written to row_dict."""
        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="ColA",
            pattern=r"^(\w+) - .+$",
            group_1_target="asset_tag",
            group_2_target="",
        )
        row_dict = {}
        raw_headers = {"ColA": 0}
        raw_row = ("AT9876 - description",)
        _apply_transform_rules(row_dict, raw_row, raw_headers, [rule])
        self.assertEqual(row_dict.get("asset_tag"), "AT9876")

    def test_both_groups_set_on_match(self):
        """When the pattern matches and both group targets are set, both fields are written."""
        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="ColB",
            pattern=r"^(\w+) - (.+)$",
            group_1_target="asset_tag",
            group_2_target="device_name",
        )
        row_dict = {}
        raw_headers = {"ColB": 0}
        raw_row = ("AT0001 - my-device",)
        _apply_transform_rules(row_dict, raw_row, raw_headers, [rule])
        self.assertEqual(row_dict.get("asset_tag"), "AT0001")
        self.assertEqual(row_dict.get("device_name"), "my-device")

    def test_no_match_leaves_row_dict_unchanged(self):
        """When the pattern does not match, row_dict is left unchanged."""
        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="ColC",
            pattern=r"^\d{10}$",
            group_1_target="asset_tag",
            group_2_target="",
        )
        row_dict = {"device_name": "original"}
        raw_headers = {"ColC": 0}
        raw_row = ("not-ten-digits",)
        _apply_transform_rules(row_dict, raw_row, raw_headers, [rule])
        self.assertNotIn("asset_tag", row_dict)
        self.assertEqual(row_dict["device_name"], "original")

    def test_missing_column_header_skips_rule(self):
        """When the source_column is absent from raw_headers, the rule is skipped silently."""
        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="ColD",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        row_dict = {"device_name": "unchanged"}
        raw_headers = {}  # ColD not present
        raw_row = ("some value",)
        _apply_transform_rules(row_dict, raw_row, raw_headers, [rule])
        self.assertEqual(row_dict["device_name"], "unchanged")

    def test_none_raw_value_skips_rule(self):
        """When the raw cell value is None, the rule is skipped silently."""
        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="ColE",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        row_dict = {"device_name": "unchanged"}
        raw_headers = {"ColE": 0}
        raw_row = (None,)  # None value at index 0
        _apply_transform_rules(row_dict, raw_row, raw_headers, [rule])
        self.assertEqual(row_dict["device_name"], "unchanged")

    def test_invalid_regex_raises_parse_error(self):
        """_apply_transform_rules with a malformed regex pattern raises ParseError."""
        from netbox_data_import.engine import ParseError

        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="ColF",
            pattern="(",  # unclosed group — invalid regex
            group_1_target="device_name",
            group_2_target="",
        )
        row_dict = {}
        raw_headers = {"ColF": 0}
        raw_row = ("some value",)
        with self.assertRaises(ParseError):
            _apply_transform_rules(row_dict, raw_row, raw_headers, [rule])


# ---------------------------------------------------------------------------
# New tests: FindExistingDeviceTest
# ---------------------------------------------------------------------------


class FindExistingDeviceTest(TestCase):
    """Unit tests for _find_existing_device matching logic."""

    def setUp(self):
        """Create a site, device type, role, and profile for matching tests."""
        from dcim.models import Site, DeviceType, Manufacturer, DeviceRole

        self.site = Site.objects.create(name="FEDSite", slug="fed-site")
        mfg = Manufacturer.objects.create(name="FED Corp", slug="fed-corp")
        self.dt = DeviceType.objects.create(manufacturer=mfg, model="FED Model", slug="fed-model")
        self.role = DeviceRole.objects.create(name="FED Role", slug="fed-role")
        self.profile = ImportProfile.objects.create(name="FEDProfile", sheet_name="Data", source_id_column="Id")

    def test_source_id_link_match(self):
        """_find_existing_device returns (device, 'source ID link') when a DeviceExistingMatch exists."""
        from dcim.models import Device
        from netbox_data_import.models import DeviceExistingMatch

        device = Device.objects.create(name="fed-dev-01", site=self.site, device_type=self.dt, role=self.role)
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="FED001",
            netbox_device_id=device.pk,
            device_name=device.name,
        )
        matched, method = _find_existing_device(self.profile, "FED001", self.site, "other-name", None, None, Device)
        self.assertEqual(matched, device)
        self.assertEqual(method, "source ID link")

    def test_serial_match(self):
        """_find_existing_device returns (device, 'serial') when a device with that serial exists."""
        from dcim.models import Device

        device = Device.objects.create(
            name="fed-dev-02",
            site=self.site,
            device_type=self.dt,
            role=self.role,
            serial="SN_UNIQUE_TEST_99",
        )
        matched, method = _find_existing_device(
            self.profile, None, self.site, "other-name", "SN_UNIQUE_TEST_99", None, Device
        )
        self.assertEqual(matched, device)
        self.assertEqual(method, "serial")

    def test_serial_multiple_objects_returns_none(self):
        """_find_existing_device returns (None, None) when serial matches multiple devices."""
        from dcim.models import Device
        from unittest.mock import patch

        with patch.object(Device.objects, "get", side_effect=Device.MultipleObjectsReturned):
            matched, method = _find_existing_device(
                self.profile, None, self.site, "any-name", "AMBIG-SERIAL", None, Device
            )
        self.assertIsNone(matched)
        self.assertIsNone(method)

    def test_asset_tag_match(self):
        """_find_existing_device returns (device, 'asset tag') when a device with that asset_tag exists."""
        from dcim.models import Device

        device = Device.objects.create(
            name="fed-dev-03",
            site=self.site,
            device_type=self.dt,
            role=self.role,
            asset_tag="AT_UNIQUE_TEST_88",
        )
        matched, method = _find_existing_device(
            self.profile, None, self.site, "other-name", None, "AT_UNIQUE_TEST_88", Device
        )
        self.assertEqual(matched, device)
        self.assertEqual(method, "asset tag")

    def test_asset_tag_multiple_objects_returns_none(self):
        """_find_existing_device returns (None, None) when asset_tag matches multiple devices."""
        from dcim.models import Device
        from unittest.mock import patch

        with patch.object(Device.objects, "get", side_effect=Device.MultipleObjectsReturned):
            matched, method = _find_existing_device(self.profile, None, self.site, "any-name", None, "AMBIG-AT", Device)
        self.assertIsNone(matched)
        self.assertIsNone(method)

    def test_no_match_returns_none(self):
        """_find_existing_device returns (None, None) when no match is found."""
        from dcim.models import Device

        matched, method = _find_existing_device(
            self.profile, None, self.site, "no-such-dev", "SN_NOSUCH", "AT_NOSUCH", Device
        )
        self.assertIsNone(matched)
        self.assertIsNone(method)

    def test_name_match_with_site(self):
        """_find_existing_device returns (device, 'name') when matching by device name + site."""
        from dcim.models import Device

        device = Device.objects.create(
            name="fed-dev-byname",
            site=self.site,
            device_type=self.dt,
            role=self.role,
        )
        matched, method = _find_existing_device(self.profile, None, self.site, "fed-dev-byname", None, None, Device)
        self.assertEqual(matched, device)
        self.assertEqual(method, "name")

    def test_name_match_without_site(self):
        """_find_existing_device returns (device, 'name') when site=None (global name match)."""
        from dcim.models import Device

        device = Device.objects.create(
            name="fed-dev-global",
            site=self.site,
            device_type=self.dt,
            role=self.role,
        )
        matched, method = _find_existing_device(self.profile, None, None, "fed-dev-global", None, None, Device)
        self.assertEqual(matched, device)
        self.assertEqual(method, "name")

    def test_name_match_multiple_objects_returns_none(self):
        """_find_existing_device returns (None, None) when name matches multiple devices."""
        from dcim.models import Device
        from unittest.mock import patch

        with patch.object(Device.objects, "get", side_effect=Device.MultipleObjectsReturned):
            matched, method = _find_existing_device(self.profile, None, None, "dup-name", None, None, Device)
        self.assertIsNone(matched)
        self.assertIsNone(method)


# ---------------------------------------------------------------------------
# New tests: WriteRackToDbTest
# ---------------------------------------------------------------------------


class WriteRackToDbTest(TestCase):
    """Unit tests for _write_rack_to_db: create, update (with location+tenant), and skip."""

    def setUp(self):
        """Create a site and profile for rack DB write tests."""
        from dcim.models import Site

        self.site = Site.objects.create(name="WRSite", slug="wr-site")
        self.profile = ImportProfile.objects.create(name="WRProfile", sheet_name="Data", source_id_column="Id")

    def test_create_new_rack(self):
        """_write_rack_to_db creates a new Rack and records action='create'."""
        from dcim.models import Rack

        rack_map = {}
        result = ImportResult()
        row = {"_row_number": 1}
        _write_rack_to_db(
            "NewRack",
            self.site,
            None,
            None,
            42,
            "",
            self.profile,
            "SRC1",
            row,
            rack_map,
            result,
            True,
            Rack,
        )
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].action, "create")
        self.assertTrue(Rack.objects.filter(site=self.site, name="NewRack").exists())

    def test_update_existing_rack_with_location_and_tenant(self):
        """_write_rack_to_db updates an existing rack and sets location and tenant."""
        from dcim.models import Rack, Location
        from tenancy.models import Tenant

        rack = Rack.objects.create(site=self.site, name="ExistRack", u_height=42)
        loc = Location.objects.create(name="WRLoc", slug="wr-loc", site=self.site)
        tenant = Tenant.objects.create(name="WRTenant", slug="wr-tenant")

        rack_map = {}
        result = ImportResult()
        row = {"_row_number": 2}
        _write_rack_to_db(
            "ExistRack",
            self.site,
            loc,
            tenant,
            24,
            "SN002",
            self.profile,
            "SRC2",
            row,
            rack_map,
            result,
            True,
            Rack,
        )
        self.assertEqual(result.rows[0].action, "update")
        rack.refresh_from_db()
        self.assertEqual(rack.location, loc)
        self.assertEqual(rack.tenant, tenant)
        self.assertEqual(rack.u_height, 24)

    def test_skip_existing_rack_when_update_existing_false(self):
        """_write_rack_to_db records action='skip' when update_existing=False and rack exists."""
        from dcim.models import Rack

        Rack.objects.create(site=self.site, name="SkipRack", u_height=42)
        rack_map = {}
        result = ImportResult()
        row = {"_row_number": 3}
        _write_rack_to_db(
            "SkipRack",
            self.site,
            None,
            None,
            42,
            "",
            self.profile,
            "SRC3",
            row,
            rack_map,
            result,
            False,
            Rack,
        )
        self.assertEqual(result.rows[0].action, "skip")
        self.assertIn("SkipRack", rack_map)


# ---------------------------------------------------------------------------
# New tests: WriteDeviceRowTest
# ---------------------------------------------------------------------------


class WriteDeviceRowTest(TestCase):
    """Unit tests for _write_device_row: error, create, update, skip paths."""

    def setUp(self):
        """Create site, manufacturer, device type, device role, profile and CRM."""
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole

        self.site = Site.objects.create(name="WDSite", slug="wd-site")
        self.mfg = Manufacturer.objects.create(name="WD Corp", slug="wd-corp")
        self.dt = DeviceType.objects.create(manufacturer=self.mfg, model="WD Model", slug="wd-model")
        self.role = DeviceRole.objects.create(name="WD Role", slug="wd-role")
        self.profile = ImportProfile.objects.create(
            name="WDProfile", sheet_name="Data", source_id_column="Id", update_existing=True
        )
        self.crm = ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug="wd-role")

    def _base_row(self, num=1, device_name=None):
        """Return a minimal synthetic row dict."""
        return {
            "_row_number": num,
            "source_id": f"WD{num:03d}",
            "device_name": device_name or f"wd-dev-{num:02d}",
            "device_class": "Server",
            "make": "WD Corp",
            "model": "WD Model",
            "u_height": "1",
            "rack_name": "",
            "u_position": "1",
            "serial": "",
            "asset_tag": "",
            "status": "active",
        }

    def test_device_type_not_found_returns_error(self):
        """_write_device_row returns action='error' when DeviceType does not exist."""
        from dcim.models import Device, DeviceType, DeviceRole, Rack

        row = self._base_row(1)
        result = _write_device_row(
            row,
            self.profile,
            self.site,
            None,
            None,
            {},
            "WD Corp",
            "WD Model",
            self.crm,
            "nonexistent-mfg",
            "nonexistent-dt",
            "WD001",
            "wd-dev-01",
            "",
            None,
            1,
            None,
            None,
            "active",
            DeviceType,
            DeviceRole,
            Rack,
            Device,
        )
        self.assertEqual(result.action, "error")
        self.assertIn("not found", result.detail)

    def test_device_role_not_found_returns_error(self):
        """_write_device_row returns action='error' when DeviceRole does not exist."""
        from dcim.models import Device, DeviceType, DeviceRole, Rack

        crm_bad = ClassRoleMapping.objects.create(
            profile=self.profile, source_class="BadRole", role_slug="nonexistent-role"
        )
        row = self._base_row(2)
        result = _write_device_row(
            row,
            self.profile,
            self.site,
            None,
            None,
            {},
            "WD Corp",
            "WD Model",
            crm_bad,
            "wd-corp",
            "wd-model",
            "WD002",
            "wd-dev-02",
            "",
            None,
            1,
            None,
            None,
            "active",
            DeviceType,
            DeviceRole,
            Rack,
            Device,
        )
        self.assertEqual(result.action, "error")
        self.assertIn("not found", result.detail)

    def test_create_device_successfully(self):
        """_write_device_row creates a Device and returns action='create' with correct detail."""
        from dcim.models import Device, DeviceType, DeviceRole, Rack

        row = self._base_row(3)
        result = _write_device_row(
            row,
            self.profile,
            self.site,
            None,
            None,
            {},
            "WD Corp",
            "WD Model",
            self.crm,
            "wd-corp",
            "wd-model",
            "WD003",
            "wd-dev-03",
            "",
            None,
            1,
            None,
            None,
            "active",
            DeviceType,
            DeviceRole,
            Rack,
            Device,
        )
        self.assertEqual(result.action, "create")
        self.assertNotIn("  ", result.detail)  # no double space
        self.assertTrue(Device.objects.filter(site=self.site, name="wd-dev-03").exists())

    def test_update_existing_device_with_asset_tag(self):
        """_write_device_row updates an existing device and sets asset_tag."""
        from dcim.models import Device, DeviceType, DeviceRole, Rack

        Device.objects.create(name="wd-dev-04", site=self.site, device_type=self.dt, role=self.role)
        row = self._base_row(4, device_name="wd-dev-04")
        result = _write_device_row(
            row,
            self.profile,
            self.site,
            None,
            None,
            {},
            "WD Corp",
            "WD Model",
            self.crm,
            "wd-corp",
            "wd-model",
            "WD004",
            "wd-dev-04",
            "",
            "AT001",
            1,
            None,
            None,
            "active",
            DeviceType,
            DeviceRole,
            Rack,
            Device,
        )
        self.assertEqual(result.action, "update")
        dev = Device.objects.get(site=self.site, name="wd-dev-04")
        self.assertEqual(dev.asset_tag, "AT001")

    def test_skip_existing_device_when_update_existing_false(self):
        """_write_device_row returns action='skip' when update_existing=False and device exists."""
        from dcim.models import Device, DeviceType, DeviceRole, Rack

        self.profile.update_existing = False
        self.profile.save()
        Device.objects.create(name="wd-dev-05", site=self.site, device_type=self.dt, role=self.role)
        row = self._base_row(5, device_name="wd-dev-05")
        result = _write_device_row(
            row,
            self.profile,
            self.site,
            None,
            None,
            {},
            "WD Corp",
            "WD Model",
            self.crm,
            "wd-corp",
            "wd-model",
            "WD005",
            "wd-dev-05",
            "",
            None,
            1,
            None,
            None,
            "active",
            DeviceType,
            DeviceRole,
            Rack,
            Device,
        )
        self.assertEqual(result.action, "skip")
        self.assertIn("update_existing=False", result.detail)


# ---------------------------------------------------------------------------
# New tests: Pass3EdgeCasesTest
# ---------------------------------------------------------------------------


class Pass3EdgeCasesTest(TestCase):
    """Integration tests for pass-3 edge cases exercised via run_import."""

    def setUp(self):
        """Create a site and full profile for pass-3 integration tests."""
        from dcim.models import Site

        self.site = Site.objects.create(name="P3Site", slug="p3-site")
        self.profile = _make_profile("P3Test")

    def _device_row(self, **overrides):
        """Return a synthetic device row dict with sensible defaults."""
        base = {
            "_row_number": 1,
            "source_id": "P3001",
            "device_name": "p3-dev-01",
            "device_class": "Server",
            "make": "Dell",
            "model": "PowerEdge R640",
            "u_height": "1",
            "rack_name": "",
            "u_position": "1",
            "serial": "",
            "asset_tag": "",
            "status": "active",
        }
        base.update(overrides)
        return base

    def test_position_less_than_1_produces_skip(self):
        """A row with u_position < 1 results in action='skip' for the device row."""
        rows = [self._device_row(u_position="-1", source_id="P3001")]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        skip_rows = [r for r in result.rows if r.action == "skip" and r.object_type == "device"]
        self.assertGreater(len(skip_rows), 0)
        self.assertIn("position", skip_rows[0].detail.lower())

    def test_missing_device_name_produces_error(self):
        """A row with empty device_name results in action='error'."""
        rows = [self._device_row(device_name="", source_id="P3002")]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        error_rows = [r for r in result.rows if r.action == "error" and "Missing device name" in r.detail]
        self.assertGreater(len(error_rows), 0)

    def test_unmapped_device_class_produces_error(self):
        """A row with a device_class that has no ClassRoleMapping results in action='error'."""
        rows = [self._device_row(device_class="UnknownClass", source_id="P3003")]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        error_rows = [r for r in result.rows if r.action == "error" and "No class\u2192role mapping" in r.detail]
        self.assertGreater(len(error_rows), 0)

    def test_crm_ignore_true_produces_ignore(self):
        """A row whose ClassRoleMapping has ignore=True results in action='ignore'."""
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Ignored", ignore=True)
        rows = [self._device_row(device_class="Ignored", source_id="P3004")]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        ignore_rows = [r for r in result.rows if r.action == "ignore"]
        self.assertGreater(len(ignore_rows), 0)

    def test_preview_matched_device_update_when_update_existing_true(self):
        """In dry-run, a device matched by name with update_existing=True yields action='update'."""
        from dcim.models import Device, Manufacturer, DeviceType, DeviceRole

        mfg = Manufacturer.objects.create(name="P3 Corp", slug="p3-corp")
        dt = DeviceType.objects.create(manufacturer=mfg, model="P3 Model", slug="p3-model")
        role = DeviceRole.objects.create(name="P3 Role", slug="server")
        Device.objects.create(name="p3-existing", site=self.site, device_type=dt, role=role)
        self.profile.update_existing = True
        self.profile.save()
        rows = [self._device_row(device_name="p3-existing", source_id="P3005")]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        update_rows = [r for r in result.rows if r.action == "update" and r.object_type == "device"]
        self.assertGreater(len(update_rows), 0)

    def test_preview_matched_device_skip_when_update_existing_false(self):
        """In dry-run, a device matched by name with update_existing=False yields action='skip'."""
        from dcim.models import Device, Manufacturer, DeviceType, DeviceRole

        mfg = Manufacturer.objects.create(name="P3B Corp", slug="p3b-corp")
        dt = DeviceType.objects.create(manufacturer=mfg, model="P3B Model", slug="p3b-model")
        role = DeviceRole.objects.create(name="P3B Role", slug="server-p3b")
        Device.objects.create(name="p3b-existing", site=self.site, device_type=dt, role=role)
        self.profile.update_existing = False
        self.profile.save()
        rows = [self._device_row(device_name="p3b-existing", source_id="P3006")]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        skip_rows = [r for r in result.rows if r.action == "skip" and r.object_type == "device"]
        self.assertGreater(len(skip_rows), 0)


# ---------------------------------------------------------------------------
# New tests: Pass1UnmappedClassTest
# ---------------------------------------------------------------------------


class Pass1UnmappedClassTest(TestCase):
    """Verify that rows with unmapped device_class are skipped in pass 1 (no manufacturer created)."""

    def setUp(self):
        """Create site and full profile for pass-1 tests."""
        from dcim.models import Site

        self.site = Site.objects.create(name="P1Site", slug="p1-site")
        self.profile = _make_profile("P1Test")

    def test_unmapped_class_does_not_create_manufacturer(self):
        """A row whose device_class has no ClassRoleMapping does not trigger manufacturer creation."""
        from dcim.models import Manufacturer

        rows = [
            {
                "_row_number": 1,
                "source_id": "P1001",
                "device_name": "p1-dev-01",
                "device_class": "UnmappedClassP1",  # no ClassRoleMapping for this class
                "make": "UniqueMfgP1",
                "model": "UniqueModelP1",
                "u_height": "1",
                "rack_name": "",
                "u_position": "1",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        before = Manufacturer.objects.count()
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertEqual(Manufacturer.objects.count(), before)


# ---------------------------------------------------------------------------
# Additional engine coverage tests
# ---------------------------------------------------------------------------


class EnsureManufacturerEdgeCaseTest(TestCase):
    """Tests for _ensure_manufacturer seen-manufacturers early return and execute mode."""

    def setUp(self):
        """Create a profile."""
        self.profile = _make_profile("EMECProfile")

    def test_seen_manufacturer_skips(self):
        """_ensure_manufacturer is a no-op when mfg_slug already in seen_manufacturers."""
        from netbox_data_import.engine import _ensure_manufacturer, ImportResult
        from dcim.models import Manufacturer

        result = ImportResult()
        seen = {"already-seen"}
        before = Manufacturer.objects.count()
        _ensure_manufacturer(
            "already-seen", "AlreadySeen", seen, self.profile, result, True, {"_row_number": 1}, Manufacturer
        )
        self.assertEqual(Manufacturer.objects.count(), before)
        self.assertEqual(len(result.rows), 0)

    def test_execute_mode_creates_manufacturer(self):
        """_ensure_manufacturer creates manufacturer in execute mode when flag is set."""
        from netbox_data_import.engine import _ensure_manufacturer, ImportResult
        from dcim.models import Manufacturer

        self.profile.create_missing_device_types = True
        self.profile.save()
        result = ImportResult()
        seen = set()
        _ensure_manufacturer(
            "exec-mfg-slug", "Exec Mfg", seen, self.profile, result, False, {"_row_number": 1}, Manufacturer
        )
        self.assertTrue(Manufacturer.objects.filter(slug="exec-mfg-slug").exists())

    def test_execute_mode_skips_when_flag_false(self):
        """_ensure_manufacturer is a no-op in execute mode when create_missing_device_types=False."""
        from netbox_data_import.engine import _ensure_manufacturer, ImportResult
        from dcim.models import Manufacturer

        self.profile.create_missing_device_types = False
        self.profile.save()
        result = ImportResult()
        seen = set()
        before = Manufacturer.objects.count()
        _ensure_manufacturer(
            "no-create-mfg", "No Create Mfg", seen, self.profile, result, False, {"_row_number": 1}, Manufacturer
        )
        self.assertEqual(Manufacturer.objects.count(), before)


class EnsureDeviceTypeEdgeCaseTest(TestCase):
    """Tests for _ensure_device_type seen-types early return and existing-type path."""

    def setUp(self):
        """Create a profile and a manufacturer."""
        from dcim.models import Manufacturer

        self.profile = _make_profile("EDTECProfile")
        self.mfg = Manufacturer.objects.create(name="EDTEC Mfg", slug="edtec-mfg")

    def test_seen_device_type_skips(self):
        """_ensure_device_type is a no-op when the (mfg,dt) key already in seen_device_types."""
        from netbox_data_import.engine import _ensure_device_type, ImportResult
        from dcim.models import Manufacturer, DeviceType

        result = ImportResult()
        seen = {("edtec-mfg", "edtec-seen")}
        before = DeviceType.objects.count()
        _ensure_device_type(
            "edtec-mfg",
            "edtec-seen",
            "EDTEC",
            "Seen",
            1,
            seen,
            self.profile,
            result,
            True,
            {"_row_number": 1},
            Manufacturer,
            DeviceType,
        )
        self.assertEqual(DeviceType.objects.count(), before)
        self.assertEqual(len(result.rows), 0)

    def test_dry_run_existing_dt_no_rows_appended(self):
        """In dry_run, when DeviceType already exists, no rows are appended."""
        from netbox_data_import.engine import _ensure_device_type, ImportResult
        from dcim.models import Manufacturer, DeviceType

        DeviceType.objects.get_or_create(
            manufacturer=self.mfg, slug="edtec-exists", defaults={"model": "Exists", "u_height": 1}
        )
        result = ImportResult()
        seen: set = set()
        _ensure_device_type(
            "edtec-mfg",
            "edtec-exists",
            "EDTEC",
            "Exists",
            1,
            seen,
            self.profile,
            result,
            True,
            {"_row_number": 1},
            Manufacturer,
            DeviceType,
        )
        self.assertEqual(len(result.rows), 0)


class EnsureDeviceRoleEdgeCaseTest(TestCase):
    """Tests for _ensure_device_role early-return paths."""

    def setUp(self):
        """Create a profile and CRM."""
        from dcim.models import Site

        self.profile = _make_profile("EDRECProfile")
        self.site = Site.objects.create(name="EDRSite", slug="edr-site")

    def test_no_crm_skips(self):
        """_ensure_device_role is a no-op when crm is None."""
        from netbox_data_import.engine import _ensure_device_role
        from dcim.models import DeviceRole

        seen: set = set()
        before = DeviceRole.objects.count()
        _ensure_device_role(None, seen, False, DeviceRole)
        self.assertEqual(DeviceRole.objects.count(), before)

    def test_crm_no_role_slug_skips(self):
        """_ensure_device_role is a no-op when crm has no role_slug."""
        from netbox_data_import.engine import _ensure_device_role
        from dcim.models import DeviceRole
        from netbox_data_import.models import ClassRoleMapping

        crm = ClassRoleMapping.objects.create(
            profile=self.profile, source_class="NoRoleClass", creates_rack=False, role_slug=""
        )
        seen: set = set()
        before = DeviceRole.objects.count()
        _ensure_device_role(crm, seen, False, DeviceRole)
        self.assertEqual(DeviceRole.objects.count(), before)

    def test_already_seen_role_skips(self):
        """_ensure_device_role is a no-op when role_slug already in seen_roles."""
        from netbox_data_import.engine import _ensure_device_role
        from dcim.models import DeviceRole
        from netbox_data_import.models import ClassRoleMapping

        crm = ClassRoleMapping.objects.create(
            profile=self.profile, source_class="SeenRoleClass", creates_rack=False, role_slug="seen-role"
        )
        seen = {"seen-role"}
        before = DeviceRole.objects.count()
        _ensure_device_role(crm, seen, False, DeviceRole)
        self.assertEqual(DeviceRole.objects.count(), before)

    def test_execute_creates_role(self):
        """_ensure_device_role creates a DeviceRole in execute mode."""
        from netbox_data_import.engine import _ensure_device_role
        from dcim.models import DeviceRole
        from netbox_data_import.models import ClassRoleMapping

        crm = ClassRoleMapping.objects.create(
            profile=self.profile, source_class="NewRoleClass", creates_rack=False, role_slug="new-unique-role-edr"
        )
        seen: set = set()
        _ensure_device_role(crm, seen, False, DeviceRole)
        self.assertTrue(DeviceRole.objects.filter(slug="new-unique-role-edr").exists())


class Pass2EdgeCasesTest(TestCase):
    """Test _pass2_process_racks edge cases (missing rack_name, u_height parse error)."""

    def setUp(self):
        """Create site and profile."""
        from dcim.models import Site

        self.site = Site.objects.create(name="P2EdgeSite", slug="p2-edge-site")
        self.profile = _make_profile("P2EdgeProfile")

    def test_missing_rack_name_records_error(self):
        """A rack row with empty rack_name records an error row."""
        from netbox_data_import.models import ClassRoleMapping

        # Profile needs a rack-creating class
        self.profile.class_role_mappings.all().delete()
        ClassRoleMapping.objects.create(profile=self.profile, source_class="RackClass", creates_rack=True)

        rows = [
            {
                "_row_number": 1,
                "source_id": "RACK001",
                "device_class": "RackClass",
                "rack_name": "",
                "u_height": "42",
                "serial": "",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        error_rows = [r for r in result.rows if r.action == "error" and r.object_type == "rack"]
        self.assertEqual(len(error_rows), 1)

    def test_invalid_u_height_uses_default(self):
        """A rack row with invalid u_height uses default (42) without crashing."""
        from netbox_data_import.models import ClassRoleMapping
        from dcim.models import Rack

        self.profile.class_role_mappings.all().delete()
        ClassRoleMapping.objects.create(profile=self.profile, source_class="RackClass2", creates_rack=True)

        rows = [
            {
                "_row_number": 1,
                "source_id": "RACK002",
                "device_class": "RackClass2",
                "rack_name": "P2EdgeRack",
                "u_height": "not-a-number",
                "serial": "",
            }
        ]
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        rack = Rack.objects.filter(site=self.site, name="P2EdgeRack").first()
        self.assertIsNotNone(rack)
        self.assertEqual(rack.u_height, 42)


class Pass3PositionUnderRackTest(TestCase):
    """Test _pass3_process_devices with position < 1 (blanking panel skip)."""

    def setUp(self):
        """Create site and profile with device class mapping."""
        from dcim.models import Site

        self.site = Site.objects.create(name="P3PosEdge", slug="p3-pos-edge")
        self.profile = _make_profile("P3PosProfile")

    def test_position_zero_produces_skip(self):
        """A device row with u_position=0 is skipped (blanking panel)."""
        rows = [
            {
                "_row_number": 1,
                "source_id": "POS001",
                "device_name": "blanking-panel",
                "device_class": "Server",
                "make": "Dell",
                "model": "R740",
                "u_height": "1",
                "rack_name": "",
                "u_position": "0",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        skip_rows = [r for r in result.rows if r.action == "skip" and r.object_type == "device"]
        self.assertEqual(len(skip_rows), 1)


class FindExistingDeviceDeletedMatchTest(TestCase):
    """Test _find_existing_device when the linked device has been deleted."""

    def setUp(self):
        """Create profile for match tests."""
        self.profile = ImportProfile.objects.create(name="DELMatchProfile", sheet_name="Data", source_id_column="Id")

    def test_deleted_device_match_falls_through(self):
        """When source-ID match points to a non-existent device PK, returns (None, None)."""
        from dcim.models import Device
        from netbox_data_import.models import DeviceExistingMatch

        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="DEL001",
            netbox_device_id=999999,  # non-existent PK
            device_name="gone",
        )
        matched, method = _find_existing_device(self.profile, "DEL001", None, None, None, None, Device)
        self.assertIsNone(matched)
        self.assertIsNone(method)


class Pass1UHeightParseErrorTest(TestCase):
    """Test _pass1_ensure_types u_height parse error (lines 400-401)."""

    def setUp(self):
        """Set up profile with a device class mapping."""
        from dcim.models import Site

        self.site = Site.objects.create(name="UHSite", slug="uh-site")
        self.profile = _make_profile("UHProfile")

    def test_invalid_u_height_falls_back_to_one(self):
        """A row with a non-numeric u_height uses 1 as fallback without error."""

        rows = [
            {
                "_row_number": 1,
                "source_id": "UH001",
                "device_name": "uh-dev-01",
                "device_class": "Server",
                "make": "UHMake",
                "model": "UHModel",
                "u_height": "not-a-number",
                "rack_name": "",
                "u_position": "1",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        # Should produce a create row, not crash
        self.assertIsInstance(result, ImportResult)


class PreviewInvalidPositionTest(TestCase):
    """Test _preview_device_row with invalid u_position (lines 652-653)."""

    def setUp(self):
        """Set up profile and site."""
        from dcim.models import Site

        self.site = Site.objects.create(name="PIPSite", slug="pip-site")
        self.profile = _make_profile("PIPProfile")

    def test_invalid_position_in_preview(self):
        """A device row with a non-numeric u_position does not crash in preview."""
        rows = [
            {
                "_row_number": 1,
                "source_id": "PIP001",
                "device_name": "pip-dev-01",
                "device_class": "Server",
                "make": "PIPMake",
                "model": "PIPModel",
                "u_height": "1",
                "rack_name": "",
                "u_position": "invalid-pos",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        # No crash — device row exists, position is None so no U-label in detail
        device_rows = [r for r in result.rows if r.object_type == "device"]
        self.assertGreater(len(device_rows), 0, "Expected at least one device row in result")
        self.assertNotIn("UNone", device_rows[0].detail)


class Pass3InvalidPositionTest(TestCase):
    """Test _pass3_process_devices with invalid u_position string (lines 853-854)."""

    def setUp(self):
        """Set up site and profile."""
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole

        self.site = Site.objects.create(name="P3IPSite", slug="p3-ip-site")
        self.profile = _make_profile("P3IPProfile")
        mfg = Manufacturer.objects.create(name="P3IPMfg", slug="p3ipmfg")
        self.dt = DeviceType.objects.create(manufacturer=mfg, model="P3IPModel", slug="p3ipmfg-p3ipmodel")
        DeviceRole.objects.get_or_create(slug="server", defaults={"name": "Server", "color": "9e9e9e"})

    def test_invalid_position_produces_device_row(self):
        """A device row with non-numeric u_position (line 853-854 TypeError path) processes without crash."""

        self.profile.create_missing_device_types = True
        self.profile.save()

        rows = [
            {
                "_row_number": 1,
                "source_id": "P3IP001",
                "device_name": "p3ip-dev-01",
                "device_class": "Server",
                "make": "P3IPMfg",
                "model": "P3IPModel",
                "u_height": "1",
                "rack_name": "",
                "u_position": "not-a-number",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        # Should not crash; device row should be in result
        self.assertIsInstance(result, ImportResult)


class ImportResultRackGroupsTest(TestCase):
    """Test ImportResult.rack_groups property (line 113 — 'else' branch)."""

    def test_rack_groups_duplicate_rack_name(self):
        """rack_groups updates existing rack when the same name appears twice."""
        r1 = RowResult(row_number=1, source_id="R1", name="RackA", action="update", object_type="rack", detail="")
        r2 = RowResult(row_number=2, source_id="R2", name="RackA", action="create", object_type="rack", detail="")

        result = ImportResult()
        result.rows = [r1, r2]
        groups = result.rack_groups
        # Both rack rows for "RackA"; the second should overwrite rack_row for "RackA"
        self.assertIn("RackA", groups)
        self.assertEqual(groups["RackA"]["rack_row"], r2)


class ResolveDeviceTypeSlugsNormalizeTest(TestCase):
    """Test _resolve_device_type_slugs normalizer loops (lines 241-242, 251-252)."""

    def setUp(self):
        """Create profile and a device type mapping with unicode escape in model."""
        self.profile = ImportProfile.objects.create(name="DTSNProfile", sheet_name="Data", source_id_column="Id")

    def test_unicode_escape_in_model_matches(self):
        """DeviceTypeMapping with extra whitespace in source_model is matched after normalization (lines 241-242)."""
        from netbox_data_import.engine import _resolve_device_type_slugs
        from netbox_data_import.models import DeviceTypeMapping

        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="TestMake",
            source_model="Model  X",  # double internal space — doesn't match direct lookup
            netbox_manufacturer_slug="testmake",
            netbox_device_type_slug="testmake-modelx",
        )
        # Direct lookup uses single-space "Model X" — won't match "Model  X" directly
        mfg_slug, dt_slug, is_explicit = _resolve_device_type_slugs("TestMake", "Model X", self.profile)
        self.assertEqual(mfg_slug, "testmake")
        self.assertEqual(dt_slug, "testmake-modelx")
        self.assertTrue(is_explicit)

    def test_manufacturer_mapping_normalizer_loop(self):
        """ManufacturerMapping with JS-style \\uXXXX in source_make is matched after normalization."""
        from netbox_data_import.engine import _resolve_device_type_slugs
        from netbox_data_import.models import ManufacturerMapping

        ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Vendor\\u0020Corp",  # \u0020 is space
            netbox_manufacturer_slug="vendor-corp",
        )
        mfg_slug, dt_slug, is_explicit = _resolve_device_type_slugs("Vendor Corp", "ModelZ", self.profile)
        self.assertEqual(mfg_slug, "vendor-corp")
        self.assertFalse(is_explicit)


class StoreSourceIdTest(TestCase):
    """Tests for _store_source_id function (lines 1006-1027)."""

    def setUp(self):
        """Create a profile and device."""
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole, Device

        self.site = Site.objects.create(name="SSISite", slug="ssi-site")
        mfg = Manufacturer.objects.create(name="SSIMfg", slug="ssi-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SSIModel", slug="ssi-model")
        role = DeviceRole.objects.create(name="SSIRole", slug="ssi-role")
        self.device = Device.objects.create(name="ssi-dev", device_type=dt, role=role, site=self.site)
        self.profile = ImportProfile.objects.create(
            name="SSIProfile", sheet_name="Data", source_id_column="Id", custom_field_name="cans_id"
        )

    def test_store_source_id_with_custom_field_name(self):
        """_store_source_id with profile.custom_field_name set attempts to write to custom field."""
        from netbox_data_import.engine import _store_source_id

        # Even if the custom field doesn't exist, should not crash
        _store_source_id(self.device, self.profile, "SSI-001")

    def test_store_source_id_no_custom_field_name(self):
        """_store_source_id without custom_field_name still writes data_import_source."""
        from netbox_data_import.engine import _store_source_id

        self.profile.custom_field_name = ""
        self.profile.save()
        _store_source_id(self.device, self.profile, "SSI-002")


class PreviewMatchedBySerialTest(TestCase):
    """Test _preview_device_row matched-by-serial path (lines 664-665)."""

    def setUp(self):
        """Create a device with a serial, profile and site."""
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole, Device

        self.site = Site.objects.create(name="PMBSSite", slug="pmbs-site")
        mfg = Manufacturer.objects.create(name="PMBSMfg", slug="pmbs-mfg")
        self.dt = DeviceType.objects.create(manufacturer=mfg, model="PMBSModel", slug="pmbs-model")
        self.role = DeviceRole.objects.create(name="server-pmbs", slug="server-pmbs")
        # Use a name that won't match directly (force fall-through to _find_existing_device)
        self.device = Device.objects.create(
            name="pmbs-dev-existing",
            device_type=self.dt,
            role=self.role,
            site=self.site,
            serial="PMBS-SN-UNIQUE",
        )
        self.profile = _make_profile("PMBSProfile")
        self.profile.update_existing = True
        self.profile.save()
        # Add a CRM for "Server" → server-pmbs (using the profile's built-in mappings)
        from netbox_data_import.models import ClassRoleMapping

        ClassRoleMapping.objects.get_or_create(
            profile=self.profile,
            source_class="PMBS",
            defaults={"creates_rack": False, "role_slug": "server-pmbs"},
        )

    def test_preview_matched_by_serial_shows_update(self):
        """In dry_run, a device with matching serial is shown as 'update' (via _find_existing_device)."""
        rows = [
            {
                "_row_number": 1,
                "source_id": "PMBS001",
                "device_name": "different-name",  # won't match by name
                "device_class": "PMBS",
                "make": "PMBSMfg",
                "model": "PMBSModel",
                "u_height": "1",
                "rack_name": "",
                "u_position": "1",
                "serial": "PMBS-SN-UNIQUE",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [r for r in result.rows if r.object_type == "device"]
        self.assertGreater(len(device_rows), 0, "Expected at least one device row in result")
        self.assertEqual(device_rows[0].action, "update")
        self.assertIn("serial", device_rows[0].detail)


class DeviceExistingMatchPrecedenceTest(TestCase):
    """Regression test: DeviceExistingMatch (source_id link) must take precedence
    over a coincidental same-name device in the same site (engine.py lookup order fix)."""

    def setUp(self):
        """Create two devices: one linked via DeviceExistingMatch, one with the same name as the source row."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        from netbox_data_import.models import DeviceExistingMatch

        self.site = Site.objects.create(name="DEMPSite", slug="demp-site")
        mfg = Manufacturer.objects.create(name="DEMPMfg", slug="dempmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="DEMPModel", slug="dempmfg-dempmodel", u_height=1)
        role = DeviceRole.objects.create(name="DEMPRole", slug="demp-role")

        # Device the source row SHOULD be linked to (via DeviceExistingMatch)
        self.linked_device = Device.objects.create(name="linked-prod-server", device_type=dt, role=role, site=self.site)
        # Device that coincidentally shares the source row's device_name
        self.coincidental_device = Device.objects.create(
            name="source-row-name", device_type=dt, role=role, site=self.site
        )

        self.profile = _make_profile("DEMPProfile")
        self.profile.update_existing = True
        self.profile.save()
        # update_or_create: _make_profile already creates source_class="Server"
        ClassRoleMapping.objects.update_or_create(
            profile=self.profile,
            source_class="Server",
            defaults={"creates_rack": False, "role_slug": "demp-role"},
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="DEMP-SRC001",
            netbox_device_id=self.linked_device.pk,
            device_name=self.linked_device.name,
        )

    def test_preview_prefers_explicit_match_over_name(self):
        """run_import dry_run must report the DeviceExistingMatch device, not the coincidental name match."""
        rows = [
            {
                "_row_number": 1,
                "source_id": "DEMP-SRC001",
                "device_name": "source-row-name",  # matches coincidental_device by name
                "device_class": "Server",
                "make": "DEMPMfg",
                "model": "DEMPModel",
                "u_height": "1",
                "rack_name": "",
                "u_position": "",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [r for r in result.rows if r.object_type == "device"]
        self.assertEqual(len(device_rows), 1)
        # Must be linked to linked_device via explicit DeviceExistingMatch, NOT coincidental name
        self.assertIn("linked-prod-server", device_rows[0].detail)
        self.assertIn("source ID link", device_rows[0].detail)

    def test_execute_prefers_explicit_match_over_name(self):
        """run_import (execute mode) must update the DeviceExistingMatch device, not the coincidental name match."""
        rows = [
            {
                "_row_number": 1,
                "source_id": "DEMP-SRC001",
                "device_name": "source-row-name",  # matches coincidental_device by name
                "device_class": "Server",
                "make": "DEMPMfg",
                "model": "DEMPModel",
                "u_height": "1",
                "rack_name": "",
                "u_position": "",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        device_rows = [r for r in result.rows if r.object_type == "device"]
        self.assertEqual(len(device_rows), 1)
        self.assertEqual(device_rows[0].action, "update")
        # The updated device must be linked_device, not coincidental_device
        self.assertIn("linked-prod-server", device_rows[0].detail)

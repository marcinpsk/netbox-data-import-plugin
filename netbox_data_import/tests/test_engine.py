# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for the import engine: parse_file and run_import (dry_run mode)."""

import os
from io import BytesIO

from django.test import TestCase

from netbox_data_import.engine import (
    ImportContext,
    ImportResult,
    ParseError,
    RowResult,
    _ensure_device_type,
    _preview_device_row,
    parse_file,
    run_import,
)
from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile


FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")


def _make_profile(name="Test") -> ImportProfile:
    """Create a fully configured ImportProfile matching the sample fixture."""
    profile = ImportProfile.objects.create(
        name=name,
        sheet_name="Data",
        source_id_column="Id",
        custom_field_name="",
        update_existing=True,
        create_missing_device_types=True,
    )
    # Standard CANS column mappings
    field_map = {
        "Id": "source_id",
        "Rack": "rack_name",
        "Name": "device_name",
        "Class": "device_class",
        "Side": "face",
        "Airflow": "airflow",
        "UPosition": "u_position",
        "Status": "status",
        "Make": "make",
        "Model": "model",
        "UHeight": "u_height",
        "Serial Number": "serial",
        "Asset Tag": "asset_tag",
    }
    for src, tgt in field_map.items():
        ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)

    # Cabinet class → rack
    ClassRoleMapping.objects.create(
        profile=profile,
        source_class="Cabinet",
        creates_rack=True,
    )
    # Server class → device role
    ClassRoleMapping.objects.create(
        profile=profile,
        source_class="Server",
        creates_rack=False,
        role_slug="server",
    )
    # Switch class → device role
    ClassRoleMapping.objects.create(
        profile=profile,
        source_class="Switch",
        creates_rack=False,
        role_slug="network-switch",
    )
    return profile


class ParseFileTest(TestCase):
    """Tests for engine.parse_file."""

    def test_parse_sample_fixture(self):
        """parse_file returns one row-dict per non-empty data row."""
        profile = _make_profile("ParseTest")
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, profile)

        # The fixture has 3 data rows (1 rack + 2 devices)
        self.assertEqual(len(rows), 3)

    def test_row_keys_match_target_fields(self):
        """Each row-dict is keyed by target_field names, not source column names."""
        profile = _make_profile("KeyTest")
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, profile)

        for row in rows:
            # No raw source column names should appear (only target fields + _row_number)
            self.assertIn("_row_number", row)
            self.assertNotIn("Serial Number", row)  # source name must be replaced
            self.assertNotIn("UPosition", row)

    def test_rack_row_has_rack_class(self):
        """The Cabinet row maps device_class to 'Cabinet'."""
        profile = _make_profile("RackRow")
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, profile)

        rack_rows = [r for r in rows if r.get("device_class") == "Cabinet"]
        self.assertEqual(len(rack_rows), 1)
        self.assertEqual(rack_rows[0]["rack_name"], "Rack-01")

    def test_missing_sheet_raises_parse_error(self):
        """ParseError is raised when the sheet name doesn't exist."""
        profile = _make_profile("BadSheet")
        profile.sheet_name = "NonExistent"
        with open(FIXTURE_PATH, "rb") as f:
            with self.assertRaises(ParseError):
                parse_file(f, profile)

    def test_invalid_file_raises_parse_error(self):
        """ParseError is raised for non-Excel binary data."""
        profile = _make_profile("BadFile")
        garbage = BytesIO(b"this is not an excel file")
        with self.assertRaises(ParseError):
            parse_file(garbage, profile)


class RunImportDryRunTest(TestCase):
    """Tests for engine.run_import with dry_run=True (no DB writes)."""

    def setUp(self):
        from dcim.models import Site

        self.site = Site.objects.create(name="Test Site", slug="test-site")
        self.profile = _make_profile("DryRun")

    def test_dry_run_returns_import_result(self):
        """run_import returns an ImportResult instance."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        self.assertIsInstance(result, ImportResult)

    def test_dry_run_has_no_errors(self):
        """The sample fixture produces no error rows in dry-run mode."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        error_rows = [r for r in result.rows if r.action == "error"]
        self.assertEqual(error_rows, [], msg=f"Unexpected errors: {error_rows}")

    def test_dry_run_identifies_rack_and_devices(self):
        """Dry-run result contains both rack and device rows."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        types = {r.object_type for r in result.rows}
        self.assertIn("rack", types)
        self.assertIn("device", types)

    def test_dry_run_does_not_write_to_db(self):
        """No Rack or Device rows are created in dry-run mode."""
        from dcim.models import Device, Rack

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)

        run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        self.assertEqual(Rack.objects.filter(site=self.site).count(), 0)
        self.assertEqual(Device.objects.filter(site=self.site).count(), 0)

    def test_dry_run_counts(self):
        """Result counts reflect what would be created."""
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, self.profile)

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        # There should be at least 1 rack to create
        self.assertGreater(result.counts.get("racks_created", 0), 0)


class RowResultSerializationTest(TestCase):
    """Tests for RowResult and ImportResult serialization helpers."""

    def test_row_result_roundtrip(self):
        """RowResult.to_dict() and RowResult.from_dict() are inverse operations."""
        r = RowResult(
            row_number=5,
            source_id="42",
            name="switch-01",
            action="create",
            object_type="device",
            detail="Would create device",
            netbox_url="",
        )
        d = r.to_dict()
        restored = RowResult.from_dict(d)
        self.assertEqual(restored.name, r.name)
        self.assertEqual(restored.action, r.action)

    def test_import_result_session_roundtrip(self):
        """ImportResult can be serialised to a dict and restored correctly."""
        result = ImportResult()
        result.rows = [
            RowResult(1, "1", "rack-01", "create", "rack", "Would create rack"),
            RowResult(2, "2", "server-01", "create", "device", "Would create device"),
        ]
        result._recompute_counts()

        session_dict = result.to_session_dict()
        restored = ImportResult.from_session_dict(session_dict)

        self.assertEqual(len(restored.rows), 2)
        self.assertEqual(restored.counts.get("racks_created"), 1)
        self.assertEqual(restored.counts.get("devices_created"), 1)


class PreviewDeviceRowTest(TestCase):
    """Unit tests for _preview_device_row internals."""

    def setUp(self):
        from dcim.models import Site

        self.site = Site.objects.create(name="Preview Site", slug="preview-site")
        self.profile = _make_profile("Preview")

    def test_rack_label_empty_rack_name(self):
        """An empty rack_name produces '(no rack)' — no leading space in detail."""
        from dcim.models import Device, DeviceType, Rack

        row = {
            "_row_number": 1,
            "rack_name": "",
            "u_position": 3,
        }
        ctx = ImportContext(
            profile=self.profile, site=self.site, location=None, tenant=None, dry_run=True, result=ImportResult()
        )
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="TestMake",
            model="TestModel",
            mfg_slug="test-mfg",
            dt_slug="test-dt",
            source_id="1",
            device_name="test-device-01",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        # Should use '(no rack)' placeholder, not '  (not found)' with leading space
        self.assertNotIn("  ", result_row.detail, "Detail should not contain double space")
        self.assertIn("(no rack)", result_row.detail)

    def test_rack_label_unknown_rack(self):
        """A non-empty rack_name not in rack_map produces 'rack-X (not found)'."""
        from dcim.models import Device, DeviceType, Rack

        row = {
            "_row_number": 2,
            "rack_name": "RACK-99",
            "u_position": 5,
        }
        ctx = ImportContext(
            profile=self.profile, site=self.site, location=None, tenant=None, dry_run=True, result=ImportResult()
        )
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="TestMake",
            model="TestModel",
            mfg_slug="test-mfg",
            dt_slug="test-dt",
            source_id="2",
            device_name="test-device-02",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        self.assertIn("RACK-99 (not found)", result_row.detail)

    def test_extra_data_includes_slugs(self):
        """extra_data includes mfg_slug and dt_slug for device rows."""
        from dcim.models import Device, DeviceType, Rack

        row = {
            "_row_number": 3,
            "rack_name": "",
            "u_position": None,
        }
        ctx = ImportContext(
            profile=self.profile, site=self.site, location=None, tenant=None, dry_run=True, result=ImportResult()
        )
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="Dell",
            model="PowerEdge R640",
            mfg_slug="dell",
            dt_slug="poweredge-r640",
            source_id="3",
            device_name="server-03",
            serial="SN12345",
            asset_tag="AT789",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        # Verify we're on the success path (not error)
        self.assertEqual(result_row.action, "create")
        # Verify extra_data contains all required fields
        self.assertIn("source_make", result_row.extra_data)
        self.assertIn("source_model", result_row.extra_data)
        self.assertIn("mfg_slug", result_row.extra_data)
        self.assertIn("dt_slug", result_row.extra_data)
        self.assertEqual(result_row.extra_data["source_make"], "Dell")
        self.assertEqual(result_row.extra_data["source_model"], "PowerEdge R640")
        self.assertEqual(result_row.extra_data["mfg_slug"], "dell")
        self.assertEqual(result_row.extra_data["dt_slug"], "poweredge-r640")
        self.assertIn("u_height", result_row.extra_data)
        self.assertIn("asset_tag", result_row.extra_data)
        self.assertEqual(result_row.extra_data["asset_tag"], "AT789")

    def test_extra_data_includes_slugs_on_error(self):
        """extra_data includes slugs even when device type is not found."""
        from dcim.models import Device, DeviceType, Rack

        # Disable create_missing_device_types to force error path
        self.profile.create_missing_device_types = False
        self.profile.save()

        row = {
            "_row_number": 4,
            "rack_name": "",
            "u_position": None,
        }
        ctx = ImportContext(
            profile=self.profile, site=self.site, location=None, tenant=None, dry_run=True, result=ImportResult()
        )
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="UnknownMfg",
            model="UnknownModel",
            mfg_slug="unknownmfg",
            dt_slug="unknownmodel",
            source_id="4",
            device_name="unknown-device",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        # Should be an error action
        self.assertEqual(result_row.action, "error")
        # extra_data should still contain slugs
        self.assertIn("source_make", result_row.extra_data)
        self.assertIn("source_model", result_row.extra_data)
        self.assertIn("mfg_slug", result_row.extra_data)
        self.assertIn("dt_slug", result_row.extra_data)
        self.assertEqual(result_row.extra_data["mfg_slug"], "unknownmfg")
        self.assertEqual(result_row.extra_data["dt_slug"], "unknownmodel")
        self.assertIn("u_height", result_row.extra_data)
        self.assertIn("asset_tag", result_row.extra_data)
        self.assertEqual(result_row.extra_data["asset_tag"], "")

    def test_ip_fields_in_preview_extra_data(self):
        """extra_data includes _ip dict when ip_fields is passed."""
        from dcim.models import Device, DeviceType, Rack

        row = {
            "_row_number": 5,
            "rack_name": "",
            "u_position": None,
        }
        ctx = ImportContext(
            profile=self.profile, site=self.site, location=None, tenant=None, dry_run=True, result=ImportResult()
        )
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="TestMake",
            model="TestModel",
            mfg_slug="test-mfg",
            dt_slug="test-dt",
            source_id="5",
            device_name="test-device-05",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
            ip_fields={"primary_ip4": "192.168.1.1/32"},
        )
        # Verify we're on the success path
        self.assertEqual(result_row.action, "create")
        # Verify extra_data contains _ip
        self.assertIn("_ip", result_row.extra_data)
        self.assertEqual(result_row.extra_data["_ip"], {"primary_ip4": "192.168.1.1/32"})

    def test_ip_fields_absent_when_not_provided(self):
        """extra_data does NOT contain _ip key when ip_fields is empty/None."""
        from dcim.models import Device, DeviceType, Rack

        row = {
            "_row_number": 6,
            "rack_name": "",
            "u_position": None,
        }
        ctx = ImportContext(
            profile=self.profile, site=self.site, location=None, tenant=None, dry_run=True, result=ImportResult()
        )
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="TestMake",
            model="TestModel",
            mfg_slug="test-mfg",
            dt_slug="test-dt",
            source_id="6",
            device_name="test-device-06",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        # Verify we're on the success path
        self.assertEqual(result_row.action, "create")
        # Verify extra_data does NOT contain _ip
        self.assertNotIn("_ip", result_row.extra_data)


class ParseIPWithPrefixTest(TestCase):
    """Tests for _parse_ip_with_prefix helper function."""

    def test_parse_ip_with_prefix_adds_cidr(self):
        """_parse_ip_with_prefix adds /32 or /128 prefix if absent."""
        from netbox_data_import.engine import _parse_ip_with_prefix

        # IPv4 without prefix
        self.assertEqual(_parse_ip_with_prefix("192.168.1.1"), "192.168.1.1/32")

        # IPv4 with prefix
        self.assertEqual(_parse_ip_with_prefix("192.168.1.1/24"), "192.168.1.1/24")

        # IPv6 without prefix
        self.assertEqual(_parse_ip_with_prefix("::1"), "::1/128")

        # Invalid IP
        self.assertIsNone(_parse_ip_with_prefix("not-an-ip"))

        # Empty string
        self.assertIsNone(_parse_ip_with_prefix(""))

        # IPv4 with prefix preserved
        self.assertEqual(_parse_ip_with_prefix("192.168.1.1/24"), "192.168.1.1/24")


class EnsureDeviceTypeExecuteModeTest(TestCase):
    """Tests that _ensure_device_type never appends RowResult rows in execute mode."""

    def setUp(self):
        self.profile = _make_profile("EnsureDT")

    def test_execute_mode_no_row_results_create_missing_false(self):
        """Execute mode with create_missing_device_types=False appends no RowResult rows."""
        from dcim.models import DeviceType, Manufacturer

        self.profile.create_missing_device_types = False
        result = ImportResult()
        row = {"_row_number": 1, "source_id": "1"}
        ctx = ImportContext(profile=self.profile, site=None, location=None, tenant=None, dry_run=False, result=result)
        _ensure_device_type(
            "unknown-mfg",
            "unknown-dt",
            "Unknown Make",
            "Unknown Model",
            1,
            set(),
            ctx,
            row,
            Manufacturer,
            DeviceType,
        )
        device_type_rows = [r for r in result.rows if r.object_type == "device_type"]
        self.assertEqual(device_type_rows, [], "Execute mode must not append device_type RowResult rows")

    def test_execute_mode_no_row_results_create_missing_true(self):
        """Execute mode with create_missing_device_types=True appends no RowResult rows (creates silently)."""
        from dcim.models import DeviceType, Manufacturer

        self.profile.create_missing_device_types = True
        result = ImportResult()
        row = {"_row_number": 1, "source_id": "1"}
        ctx = ImportContext(profile=self.profile, site=None, location=None, tenant=None, dry_run=False, result=result)
        _ensure_device_type(
            "silent-mfg",
            "silent-dt",
            "Silent Make",
            "Silent Model",
            1,
            set(),
            ctx,
            row,
            Manufacturer,
            DeviceType,
        )
        device_type_rows = [r for r in result.rows if r.object_type == "device_type"]
        self.assertEqual(device_type_rows, [], "Execute mode must not append device_type RowResult rows")
        # Verify the device type was actually created in DB
        self.assertTrue(DeviceType.objects.filter(manufacturer__slug="silent-mfg", slug="silent-dt").exists())

    def test_dry_run_appends_error_row_when_create_missing_false(self):
        """Dry-run with create_missing_device_types=False does append an error RowResult."""
        from dcim.models import DeviceType, Manufacturer

        self.profile.create_missing_device_types = False
        result = ImportResult()
        row = {"_row_number": 1, "source_id": "1"}
        ctx = ImportContext(profile=self.profile, site=None, location=None, tenant=None, dry_run=True, result=result)
        _ensure_device_type(
            "dry-mfg",
            "dry-dt",
            "Dry Make",
            "Dry Model",
            1,
            set(),
            ctx,
            row,
            Manufacturer,
            DeviceType,
        )
        device_type_rows = [r for r in result.rows if r.object_type == "device_type"]
        self.assertEqual(len(device_type_rows), 1)
        self.assertEqual(device_type_rows[0].action, "error")


class ParseFileEdgeCasesTest(TestCase):
    """Tests for parse_file edge cases: empty rows and missing column headers."""

    def test_empty_rows_are_skipped(self):
        """parse_file skips fully-empty data rows (line 192 coverage)."""
        import openpyxl
        from io import BytesIO

        profile = _make_profile("EmptyRowTest")

        # Build an xlsx with one data row and one empty row
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(
            [
                "Id",
                "Rack",
                "Name",
                "Class",
                "Make",
                "Model",
                "UHeight",
                "UPosition",
                "Serial Number",
                "Asset Tag",
                "Status",
            ]
        )
        ws.append(["SRC001", "Rack-01", "dev-01", "Server", "Dell", "R740", "1", "1", "", "", "active"])
        ws.append([None, None, None, None, None, None, None, None, None, None, None])  # empty row
        ws.append(["SRC002", "Rack-01", "dev-02", "Server", "Dell", "R740", "1", "2", "", "", "active"])

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        rows = parse_file(buf, profile)
        # Only 2 non-empty rows
        self.assertEqual(len(rows), 2)

    def test_mapping_with_missing_source_column_skips(self):
        """parse_file silently skips column mappings whose header doesn't exist in the file (line 198)."""
        import openpyxl
        from io import BytesIO

        profile = _make_profile("MissingColTest")
        # Add a mapping for a column that doesn't exist in the file
        ColumnMapping.objects.create(profile=profile, source_column="NonExistentCol", target_field="tenant")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(["Id", "Name", "Class"])
        ws.append(["SRC001", "dev-01", "Server"])

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        rows = parse_file(buf, profile)
        self.assertEqual(len(rows), 1)
        # tenant should be absent (no mapping target applied since column didn't exist)
        self.assertNotIn("tenant", rows[0])


class FieldDiffComputationTest(TestCase):
    """Tests for field_diff computation in _preview_device_row."""

    def setUp(self):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        self.site = Site.objects.create(name="Diff Site", slug="diff-site")
        self.profile = _make_profile("FieldDiff")

        mfg = Manufacturer.objects.create(name="TestMfg", slug="testmfg")
        self.device_type = DeviceType.objects.create(
            manufacturer=mfg,
            model="TestModel",
            slug="testmodel",
            u_height=1,
        )
        self.role = DeviceRole.objects.create(name="Server", slug="server", color="000000")

    def _make_existing_device(self, name="existing-server", serial="OLD123", asset_tag="OLD-TAG"):
        from dcim.models import Device

        return Device.objects.create(
            name=name,
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial=serial,
            asset_tag=asset_tag,
            status="active",
        )

    def _call_preview(self, device_name, serial, asset_tag, device_status="active", ip_fields=None):
        from dcim.models import Device, DeviceType, Rack

        row = {"_row_number": 1, "rack_name": "", "u_position": None}
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=True,
            result=ImportResult(),
        )
        return _preview_device_row(
            row=row,
            ctx=ctx,
            make="TestMfg",
            model="TestModel",
            mfg_slug="testmfg",
            dt_slug="testmodel",
            source_id="99",
            device_name=device_name,
            serial=serial,
            asset_tag=asset_tag,
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
            ip_fields=ip_fields,
            device_face=None,
            device_airflow=None,
            device_status=device_status,
            u_position=None,
        )

    def test_field_diff_on_update_row(self):
        """Update rows include field_diff with changed serial and asset_tag."""
        self._make_existing_device(serial="OLD123", asset_tag="OLD-TAG")
        result = self._call_preview("existing-server", serial="NEW456", asset_tag="A-001")
        self.assertEqual(result.action, "update")
        self.assertIn("field_diff", result.extra_data)
        diff = result.extra_data["field_diff"]
        self.assertIn("serial", diff)
        self.assertEqual(diff["serial"]["netbox"], "OLD123")
        self.assertEqual(diff["serial"]["file"], "NEW456")
        self.assertIn("asset_tag", diff)
        self.assertEqual(diff["asset_tag"]["netbox"], "OLD-TAG")
        self.assertEqual(diff["asset_tag"]["file"], "A-001")

    def test_field_diff_absent_on_create_row(self):
        """Create rows must not have field_diff in extra_data."""
        result = self._call_preview("brand-new-device", serial="SN001", asset_tag="AT001")
        self.assertEqual(result.action, "create")
        self.assertNotIn("field_diff", result.extra_data)

    def test_field_diff_absent_on_skip_row(self):
        """Skip rows (update_existing=False) must not have field_diff in extra_data."""
        self.profile.update_existing = False
        self.profile.save()
        self._make_existing_device(serial="OLD123", asset_tag="OLD-TAG")
        result = self._call_preview("existing-server", serial="NEW456", asset_tag="A-001")
        self.assertEqual(result.action, "skip")
        self.assertNotIn("field_diff", result.extra_data)

    def test_field_diff_excludes_ip_fields(self):
        """field_diff must never include IP field keys even when ip_fields are passed."""
        self._make_existing_device(serial="OLD123", asset_tag="OLD-TAG")
        result = self._call_preview(
            "existing-server",
            serial="NEW456",
            asset_tag="A-001",
            ip_fields={"primary_ip4": "10.0.0.1/32"},
        )
        self.assertEqual(result.action, "update")
        self.assertIn("field_diff", result.extra_data)
        diff = result.extra_data["field_diff"]
        self.assertNotIn("primary_ip4", diff)
        self.assertNotIn("primary_ip6", diff)
        self.assertNotIn("oob_ip", diff)

    def test_field_diff_excludes_matching_fields(self):
        """field_diff must not include a field when xls value matches the existing device value."""
        self._make_existing_device(serial="SAME-SERIAL", asset_tag="DIFF-TAG")
        result = self._call_preview("existing-server", serial="SAME-SERIAL", asset_tag="NEW-TAG")
        self.assertEqual(result.action, "update")
        diff = result.extra_data.get("field_diff", {})
        self.assertNotIn("serial", diff)
        self.assertIn("asset_tag", diff)

    def test_field_diff_no_u_height_when_matches(self):
        """u_height must not appear in diff when XLS value equals device type u_height."""
        self._make_existing_device(serial="S1", asset_tag="A1")
        # row has no u_height key → defaults to 1; device_type was created with u_height=1
        result = self._call_preview("existing-server", serial="S1", asset_tag="A1")
        diff = result.extra_data.get("field_diff", {})
        self.assertNotIn("u_height", diff, "u_height must not appear in diff when values match")

    def test_extra_data_includes_netbox_device_id_on_update(self):
        """Update rows must include netbox_device_id in extra_data equal to matched device PK."""
        device = self._make_existing_device(serial="SN-ID", asset_tag="AT-ID")
        result = self._call_preview("existing-server", serial="SN-ID", asset_tag="NEW-TAG")
        self.assertEqual(result.action, "update")
        self.assertIn("netbox_device_id", result.extra_data)
        self.assertEqual(result.extra_data["netbox_device_id"], device.pk)

    def test_netbox_device_id_absent_on_skip_row(self):
        """netbox_device_id must NOT be present on skip rows (update_existing=False)."""
        self.profile.update_existing = False
        self.profile.save()
        self._make_existing_device(serial="SN-SKIP", asset_tag=None)
        result = self._call_preview("existing-server", serial="SN-SKIP", asset_tag=None)
        self.assertEqual(result.action, "skip")
        self.assertNotIn("netbox_device_id", result.extra_data)

    def test_netbox_device_id_absent_on_create_row(self):
        """netbox_device_id must NOT be present on create rows (no matching device)."""
        result = self._call_preview("brand-new-device-xyz", serial="SN-NEW-XYZ", asset_tag=None)
        self.assertEqual(result.action, "create")
        self.assertNotIn("netbox_device_id", result.extra_data)

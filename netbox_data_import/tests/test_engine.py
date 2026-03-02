# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for the import engine: parse_file and run_import (dry_run mode)."""

import os
from io import BytesIO

from django.test import TestCase

from netbox_data_import.engine import (
    ImportResult,
    ParseError,
    RowResult,
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

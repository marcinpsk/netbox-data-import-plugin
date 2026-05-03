# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Coverage tests for template_content, forms, and tables modules."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from netbox_data_import.engine import _str_val
from netbox_data_import.models import ImportJob, ImportProfile
from netbox_data_import.tables import ImportJobTable
from netbox_data_import.template_content import DeviceImportDataExtension


def _make_profile(name="MiscTest") -> ImportProfile:
    return ImportProfile.objects.create(
        name=name,
        sheet_name="Data",
        source_id_column="Id",
    )


class DeviceImportDataExtensionTest(TestCase):
    """Tests for DeviceImportDataExtension.right_page() covering all branches."""

    def _make_ext(self, context):
        ext = DeviceImportDataExtension.__new__(DeviceImportDataExtension)
        ext.context = context
        return ext

    def test_returns_empty_string_when_no_object_in_context(self):
        """right_page() returns '' when context has no 'object' key."""
        ext = self._make_ext({})
        self.assertEqual(ext.right_page(), "")

    def test_returns_empty_string_when_cf_is_none(self):
        """right_page() returns '' when device.cf is None (no custom fields)."""
        obj = MagicMock()
        obj.cf = None
        ext = self._make_ext({"object": obj})
        self.assertEqual(ext.right_page(), "")

    def test_returns_empty_string_when_no_import_data_in_cf(self):
        """right_page() returns '' when data_import_source is not in cf."""
        obj = MagicMock()
        obj.cf = {}
        ext = self._make_ext({"object": obj})
        self.assertEqual(ext.right_page(), "")

    def test_renders_when_import_data_present(self):
        """right_page() calls self.render() when data_import_source is in cf."""
        obj = MagicMock()
        obj.cf = {"data_import_source": {"source_id": "SRC-1", "extra": {"jira_id": "J-42"}}}
        ext = self._make_ext({"object": obj})
        with patch.object(ext, "render", return_value="<rendered>") as mock_render:
            result = ext.right_page()
        self.assertEqual(result, "<rendered>")
        mock_render.assert_called_once()
        _, kwargs = mock_render.call_args
        self.assertIn("import_data", kwargs["extra_context"])
        self.assertIn("extra_columns", kwargs["extra_context"])
        self.assertEqual(kwargs["extra_context"]["extra_columns"], {"jira_id": "J-42"})

    def test_renders_when_import_data_has_no_extra_key(self):
        """right_page() works when data_import_source exists but has no 'extra' key."""
        obj = MagicMock()
        obj.cf = {"data_import_source": {"source_id": "SRC-2"}}
        ext = self._make_ext({"object": obj})
        with patch.object(ext, "render", return_value="<ok>"):
            result = ext.right_page()
        self.assertEqual(result, "<ok>")

    def test_renders_with_ip_data_populates_ip_status(self):
        """right_page() populates ip_status when data_import_source has _ip key — lines 29-37."""
        obj = MagicMock()
        obj.primary_ip4 = None
        obj.cf = {"data_import_source": {"source_id": "SRC-3", "_ip": {"primary_ip4": "10.0.0.1/32"}}}
        ext = self._make_ext({"object": obj})
        with patch.object(ext, "render", return_value="<rendered-ip>") as mock_render:
            result = ext.right_page()
        self.assertEqual(result, "<rendered-ip>")
        _, kwargs = mock_render.call_args
        ip_status = kwargs["extra_context"]["ip_status"]
        self.assertIn("primary_ip4", ip_status)
        self.assertFalse(ip_status["primary_ip4"]["in_netbox"])
        self.assertEqual(ip_status["primary_ip4"]["value"], "10.0.0.1/32")


class ImportSetupFormValidationTest(TestCase):
    """Tests for ImportSetupForm.clean_excel_file() file-size validation."""

    def test_file_too_large_raises_validation_error(self):
        """Files exceeding MAX_UPLOAD_SIZE fail clean_excel_file validation."""
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile

        from netbox_data_import.forms import ImportSetupForm

        big_file = SimpleUploadedFile("big.xlsx", b"x")
        big_file.size = ImportSetupForm.MAX_UPLOAD_SIZE + 1

        form = ImportSetupForm.__new__(ImportSetupForm)
        form.cleaned_data = {"excel_file": big_file}
        with self.assertRaises(ValidationError):
            form.clean_excel_file()

    def test_file_within_limit_passes(self):
        """Files within MAX_UPLOAD_SIZE pass clean_excel_file without error."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from netbox_data_import.forms import ImportSetupForm

        small_file = SimpleUploadedFile("small.xlsx", b"x" * 100)
        small_file.size = 100

        form = ImportSetupForm.__new__(ImportSetupForm)
        form.cleaned_data = {"excel_file": small_file}
        result = form.clean_excel_file()
        self.assertEqual(result, small_file)


class ImportJobTableRenderTest(TestCase):
    """Tests for ImportJobTable render methods."""

    def test_render_profile_with_valid_profile(self):
        """render_profile() returns the profile name when profile is set."""
        profile = _make_profile("TableProfile")
        job = ImportJob.objects.create(profile=profile, input_filename="test.xlsx")
        table = ImportJobTable([job])
        self.assertEqual(table.render_profile(job), "TableProfile")

    def test_render_profile_with_null_profile(self):
        """render_profile() returns '(deleted)' when profile is None."""
        job = ImportJob.objects.create(profile=None, input_filename="test.xlsx")
        table = ImportJobTable([job])
        self.assertEqual(table.render_profile(job), "(deleted)")

    def test_render_racks_created_with_counts(self):
        """render_racks_created() extracts racks_created from result_counts dict."""
        table = ImportJobTable([])
        self.assertEqual(table.render_racks_created({"racks_created": 3, "devices_created": 7}), 3)

    def test_render_racks_created_with_none(self):
        """render_racks_created() returns 0 when value is None."""
        table = ImportJobTable([])
        self.assertEqual(table.render_racks_created(None), 0)

    def test_render_devices_created_with_counts(self):
        """render_devices_created() extracts devices_created from result_counts dict."""
        table = ImportJobTable([])
        self.assertEqual(table.render_devices_created({"racks_created": 2, "devices_created": 12}), 12)

    def test_render_devices_created_with_none(self):
        """render_devices_created() returns 0 when value is None."""
        table = ImportJobTable([])
        self.assertEqual(table.render_devices_created(None), 0)


class StrValHelperTests(TestCase):
    """Tests for engine._str_val — guards against None/NaN cells producing literal 'None'."""

    def test_none_returns_empty(self):
        self.assertEqual(_str_val(None), "")

    def test_string_none_returns_empty(self):
        """str(None) == 'None'; _str_val must not return that literal."""
        self.assertEqual(_str_val("None"), "")

    def test_string_nan_returns_empty(self):
        self.assertEqual(_str_val("nan"), "")
        self.assertEqual(_str_val("NaN"), "")

    def test_string_null_returns_empty(self):
        self.assertEqual(_str_val("null"), "")
        self.assertEqual(_str_val("NULL"), "")

    def test_normal_string_passes_through(self):
        self.assertEqual(_str_val("RACK-01"), "RACK-01")

    def test_strips_whitespace(self):
        self.assertEqual(_str_val("  rack-01  "), "rack-01")

    def test_integer_converts(self):
        self.assertEqual(_str_val(42), "42")

    def test_empty_string_returns_empty(self):
        self.assertEqual(_str_val(""), "")

    def test_rack_name_falls_back_to_device_name_when_empty(self):
        """Cabinet rows have rack_name=None (Rack column empty); device_name holds the cabinet name."""
        row = {"device_name": "ITC-RACK-01", "rack_name": None}
        resolved = _str_val(row.get("rack_name")) or _str_val(row.get("device_name"))
        self.assertEqual(resolved, "ITC-RACK-01")

    def test_rack_name_wins_over_device_name_when_set(self):
        """When rack_name is explicitly set it takes precedence over device_name."""
        row = {"device_name": "CABINET-X", "rack_name": "RACK-01"}
        resolved = _str_val(row.get("rack_name")) or _str_val(row.get("device_name"))
        self.assertEqual(resolved, "RACK-01")

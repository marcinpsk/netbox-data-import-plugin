# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Coverage tests for template_content, forms, and tables modules."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

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

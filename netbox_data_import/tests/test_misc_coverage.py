# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Coverage tests for template_content, forms, and tables modules."""

from django.test import TestCase

from netbox_data_import.engine import _str_val
from netbox_data_import.models import ImportJob, ImportProfile
from netbox_data_import.tables import ColumnMappingTable, ImportJobTable
from netbox_data_import.template_content import DeviceImportDataExtension
from netbox_data_import.tests.helpers import set_import_source


def _make_profile(name="MiscTest") -> ImportProfile:
    """Create an import profile configured for the test data sheet and source ID column."""
    return ImportProfile.objects.create(name=name, adapter_config={"sheet_name": "Data", "source_id_column": "Id"})


class DeviceImportDataExtensionTest(TestCase):
    """Render the Device detail card against a real Device and its real import record."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        site = Site.objects.create(name="Card Site", slug="card-site")
        manufacturer = Manufacturer.objects.create(name="Card Mfg", slug="card-mfg")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Card Model", slug="card-model", u_height=1
        )
        role = DeviceRole.objects.create(name="Card Role", slug="card-role")
        cls.device = Device.objects.create(name="card-device", site=site, device_type=device_type, role=role)
        cls.profile = _make_profile("Card Profile")

    def _render(self, source_id="", extra_columns=None, unassigned_ips=None, **device_attrs):
        """Return the card HTML the Device page would show for one stored import record."""
        for name, value in device_attrs.items():
            setattr(self.device, name, value)
        set_import_source(
            self.device,
            self.profile,
            source_id,
            extra_columns=extra_columns,
            unassigned_ips=unassigned_ips,
        )
        return DeviceImportDataExtension({"object": self.device}).left_page()

    def test_card_renders_in_the_left_column(self):
        """NetBox appends plugin content to a column end; this card belongs to the left one."""
        self.assertIn("Import Data", self._render("SRC-COLUMN"))
        self.assertNotIn("right_page", DeviceImportDataExtension.__dict__)

    def test_returns_empty_string_when_no_object_in_context(self):
        """A list view has no object, so the card renders nothing."""
        self.assertEqual(DeviceImportDataExtension({}).left_page(), "")

    def test_returns_empty_string_without_stored_import_data(self):
        """A device the plugin never imported shows no card."""
        self.assertEqual(DeviceImportDataExtension({"object": self.device}).left_page(), "")

    def test_renders_source_columns_and_metadata(self):
        """Extra source columns and the import metadata reach the rendered card."""
        html = self._render("SRC-1", extra_columns={"jira_id": "J-42"})

        self.assertIn("Import Data", html)
        self.assertIn("jira_id", html)
        self.assertIn("J-42", html)
        self.assertIn("SRC-1", html)
        self.assertIn(self.profile.name, html)

    def test_reports_a_stored_ip_that_netbox_does_not_hold(self):
        """An IP the import could not assign natively is listed as not assigned."""
        html = self._render("SRC-3", unassigned_ips={"primary_ip4": "10.0.0.1/32"})

        self.assertIn("Primary IPv4", html)
        self.assertIn("10.0.0.1/32", html)
        self.assertIn("Not assigned", html)

    def test_reports_a_stored_ip_that_netbox_already_holds(self):
        """An IP that reached NetBox natively is listed as present."""
        from ipam.models import IPAddress

        address = IPAddress.objects.create(address="10.0.0.9/32")

        html = self._render("SRC-4", unassigned_ips={"primary_ip4": "10.0.0.9/32"}, primary_ip4=address)

        self.assertIn("In NetBox", html)


class ImportSetupFormValidationTest(TestCase):
    """Tests for ImportSetupForm.clean_excel_file() file-size validation."""

    def test_form_without_user_keeps_its_default_querysets(self):
        """Background callers can construct the setup form without permission scoping."""
        from netbox_data_import.forms import ImportSetupForm

        form = ImportSetupForm()

        self.assertIsNotNone(form.fields["profile"].queryset)
        self.assertIsNotNone(form.fields["site"].queryset)

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


class ColumnMappingTableTest(TestCase):
    """Test ColumnMapping table configuration."""

    def test_target_display_orders_by_the_model_field(self):
        """Sort the display label through the underlying target field."""
        table = ColumnMappingTable([])

        self.assertEqual(tuple(table.columns["target_field"].order_by), ("target_field",))


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

# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for dmSearch() serial number display in device matching modal."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

User = get_user_model()


class SearchDeviceSerialDisplayTest(TestCase):
    """Test dmSearch() function serial number display with mismatch warnings."""

    @classmethod
    def setUpTestData(cls):
        """Set up test data for device search with serials."""
        cls.user = User.objects.create_user(username="testuser", password="testpass")
        perms = [
            Permission.objects.get(content_type__app_label="dcim", codename="view_device"),
            Permission.objects.get(content_type__app_label="dcim", codename="view_devicetype"),
            Permission.objects.get(content_type__app_label="dcim", codename="view_manufacturer"),
            Permission.objects.get(content_type__app_label="netbox_data_import", codename="view_importprofile"),
        ]
        cls.user.user_permissions.add(*perms)

        # Create site
        cls.site = Site.objects.create(name="TestSite", slug="testsite")

        # Create manufacturer and device types
        cls.mfg = Manufacturer.objects.create(name="Dell", slug="dell")
        cls.dt = DeviceType.objects.create(manufacturer=cls.mfg, model="R640", slug="r640", u_height=2)

        # Create device role
        cls.role = DeviceRole.objects.create(name="Server", slug="server")

        # Create test devices with different serials
        cls.device_with_serial = Device.objects.create(
            name="server-001",
            device_type=cls.dt,
            role=cls.role,
            site=cls.site,
            serial="ABC123",
        )
        cls.device_with_matching_serial = Device.objects.create(
            name="server-002",
            device_type=cls.dt,
            role=cls.role,
            site=cls.site,
            serial="XYZ789",
        )
        cls.device_without_serial = Device.objects.create(
            name="server-003", device_type=cls.dt, role=cls.role, site=cls.site
        )

    def setUp(self):
        """Set up test fixtures for each test."""
        self.client = Client()
        self.client.force_login(self.user)

    def test_search_objects_includes_serial_in_response(self):
        """search_objects API returns device serial in results."""
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url, {"type": "device", "q": "server"})
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertEqual(len(data["results"]), 3)

        # Find device with serial
        results = {r["name"]: r for r in data["results"]}
        self.assertIn("server-001", results)
        self.assertEqual(results["server-001"]["serial"], "ABC123")
        self.assertEqual(results["server-002"]["serial"], "XYZ789")
        self.assertIsNone(results["server-003"]["serial"])

    def test_search_objects_includes_site_in_response(self):
        """search_objects API returns device site in results."""
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url, {"type": "device", "q": "server"})
        data = resp.json()

        results = {r["name"]: r for r in data["results"]}
        self.assertEqual(results["server-001"]["site"], "TestSite")
        self.assertEqual(results["server-002"]["site"], "TestSite")
        self.assertEqual(results["server-003"]["site"], "TestSite")

    def test_search_objects_filters_by_name_substring(self):
        """search_objects API filters devices by name substring."""
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url, {"type": "device", "q": "server-001"})
        data = resp.json()

        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["name"], "server-001")
        self.assertEqual(data["results"][0]["serial"], "ABC123")

    def test_search_objects_empty_query_returns_empty(self):
        """search_objects API with empty query returns empty results."""
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url, {"type": "device", "q": ""})
        data = resp.json()

        self.assertEqual(len(data["results"]), 0)

    def test_search_objects_requires_view_device_permission(self):
        """search_objects requires view_device permission."""
        user = User.objects.create_user(username="noview", password="pass")
        self.client.force_login(user)

        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url, {"type": "device", "q": "server"})
        self.assertEqual(resp.status_code, 403)

    def test_search_objects_returns_device_id_name_serial_site(self):
        """search_objects returns all necessary fields for dmSearch display."""
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url, {"type": "device", "q": "server-001"})
        data = resp.json()

        result = data["results"][0]
        # Verify all required fields for table display
        self.assertIn("id", result)
        self.assertIn("name", result)
        self.assertIn("serial", result)
        self.assertIn("site", result)
        self.assertIn("url", result)

    def test_search_objects_no_match(self):
        """search_objects returns empty results when no devices match."""
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url, {"type": "device", "q": "nonexistent-device-xyz"})
        data = resp.json()

        self.assertEqual(len(data["results"]), 0)

    def test_dmSearch_results_table_structure(self):
        """Import preview template includes dmSearch() function and table ID."""
        from netbox_data_import.models import ColumnMapping, ImportProfile

        profile = ImportProfile.objects.create(
            name="TestProfile",
            sheet_name="Data",
            source_id_column="Id",
            update_existing=False,
        )
        for src, tgt in {
            "Id": "source_id",
            "Name": "device_name",
            "Serial": "serial",
        }.items():
            ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)

        url = reverse("plugins:netbox_data_import:import_preview", kwargs={"pk": profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Check that dmSearch function exists
        self.assertIn("function dmSearch()", resp.content.decode())
        # Check that table ID exists
        self.assertIn('id="mm_search_results_table"', resp.content.decode())
        # Check for serial column header
        self.assertIn("Serial", resp.content.decode())
        # Check for mismatch warning div
        self.assertIn('id="mm_serial_mismatch_warning"', resp.content.decode())

    def test_dmSearch_source_serial_in_modal(self):
        """Device Match modal captures source serial from button data."""
        from netbox_data_import.models import ColumnMapping, ImportProfile

        profile = ImportProfile.objects.create(
            name="TestProfile",
            sheet_name="Data",
            source_id_column="Id",
            update_existing=False,
        )
        for src, tgt in {
            "Id": "source_id",
            "Name": "device_name",
            "Serial": "serial",
        }.items():
            ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)

        url = reverse("plugins:netbox_data_import:import_preview", kwargs={"pk": profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Check that dm_source_serial input exists
        self.assertIn('id="dm_source_serial"', resp.content.decode())
        # Check for data-source-serial attribute in modal trigger buttons
        self.assertIn("data-source-serial=", resp.content.decode())

    def test_dmSearch_mismatch_warning_banner(self):
        """Device Match modal includes mismatch warning banner."""
        from netbox_data_import.models import ColumnMapping, ImportProfile

        profile = ImportProfile.objects.create(
            name="TestProfile",
            sheet_name="Data",
            source_id_column="Id",
            update_existing=False,
        )
        for src, tgt in {
            "Id": "source_id",
            "Name": "device_name",
            "Serial": "serial",
        }.items():
            ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)

        url = reverse("plugins:netbox_data_import:import_preview", kwargs={"pk": profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Check for mismatch warning with correct classes and styling
        html = resp.content.decode()
        self.assertIn('id="mm_serial_mismatch_warning"', html)
        self.assertIn('class="alert alert-warning"', html)
        self.assertIn('style="display:none;"', html)

    def test_dmSearch_escapes_html_in_warning(self):
        """dmSearch() includes HTML escape function for security."""
        from netbox_data_import.models import ColumnMapping, ImportProfile

        profile = ImportProfile.objects.create(
            name="TestProfile",
            sheet_name="Data",
            source_id_column="Id",
            update_existing=False,
        )
        for src, tgt in {
            "Id": "source_id",
            "Name": "device_name",
            "Serial": "serial",
        }.items():
            ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)

        url = reverse("plugins:netbox_data_import:import_preview", kwargs={"pk": profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Check for escapeHtml function
        self.assertIn("function escapeHtml(text)", resp.content.decode())

    def test_serial_color_coding_logic(self):
        """dmSearch() function includes color coding logic for serial display."""
        from netbox_data_import.models import ColumnMapping, ImportProfile

        profile = ImportProfile.objects.create(
            name="TestProfile",
            sheet_name="Data",
            source_id_column="Id",
            update_existing=False,
        )
        for src, tgt in {
            "Id": "source_id",
            "Name": "device_name",
            "Serial": "serial",
        }.items():
            ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)

        url = reverse("plugins:netbox_data_import:import_preview", kwargs={"pk": profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        html = resp.content.decode()
        # Check for green checkmark in serial display logic
        self.assertIn("color:green;", html)
        # Check for orange/alert icon for mismatches
        self.assertIn("color:orange;", html)
        # Check for gray dash for no serial
        self.assertIn("color:gray;", html)

    def test_modal_trigger_button_has_source_serial(self):
        """Device Match modal trigger buttons include data-source-serial attribute."""
        from netbox_data_import.models import ColumnMapping, ImportProfile

        profile = ImportProfile.objects.create(
            name="TestProfile",
            sheet_name="Data",
            source_id_column="Id",
            update_existing=False,
        )
        for src, tgt in {
            "Id": "source_id",
            "Name": "device_name",
            "Serial": "serial",
        }.items():
            ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)

        url = reverse("plugins:netbox_data_import:import_preview", kwargs={"pk": profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        html = resp.content.decode()
        # Check that buttons have all required data attributes
        self.assertIn("data-source-id=", html)
        self.assertIn("data-source-name=", html)
        self.assertIn("data-source-serial=", html)
        self.assertIn("data-source-asset-tag=", html)
        self.assertIn("data-profile-id=", html)

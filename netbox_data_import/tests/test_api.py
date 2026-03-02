# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the REST API views."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceTypeMapping,
    ImportProfile,
    ManufacturerMapping,
)

User = get_user_model()


def _make_profile(name="APITest") -> ImportProfile:
    """Create a minimal ImportProfile."""
    return ImportProfile.objects.create(name=name, sheet_name="Data", source_id_column="Id")


class BaseAPITestCase(TestCase):
    """Base class with an authenticated superuser client."""

    def setUp(self):
        """Create superuser and authenticate."""
        self.user = User.objects.create_superuser("apiuser", "api@example.com", "apipass")
        self.client = Client()
        self.client.login(username="apiuser", password="apipass")


class ImportProfileAPITest(BaseAPITestCase):
    """Tests for the ImportProfile REST API endpoint."""

    def test_list_profiles(self):
        """GET /api/plugins/data-import/profiles/ returns 200."""
        _make_profile("APIListProfile")
        resp = self.client.get("/api/plugins/data-import/profiles/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)
        import json

        data = json.loads(resp.content)
        self.assertIn("results", data)

    def test_create_profile_via_api(self):
        """POST to API creates a profile."""
        import json

        resp = self.client.post(
            "/api/plugins/data-import/profiles/",
            data=json.dumps({"name": "APICreatedProfile", "sheet_name": "Data", "source_id_column": "Id"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertIn(resp.status_code, [200, 201])
        if resp.status_code == 201:
            self.assertTrue(ImportProfile.objects.filter(name="APICreatedProfile").exists())


class ColumnMappingAPITest(BaseAPITestCase):
    """Tests for the ColumnMapping REST API with profile_id filter."""

    def setUp(self):
        """Set up profiles and column mappings."""
        super().setUp()
        self.p1 = _make_profile("APIColMapProfile1")
        self.p2 = _make_profile("APIColMapProfile2")
        ColumnMapping.objects.create(profile=self.p1, source_column="Name", target_field="device_name")
        ColumnMapping.objects.create(profile=self.p2, source_column="Name", target_field="device_name")

    def test_list_all_column_mappings(self):
        """GET /api/plugins/data-import/column-mappings/ returns all mappings."""
        resp = self.client.get("/api/plugins/data-import/column-mappings/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)
        import json

        data = json.loads(resp.content)
        self.assertGreaterEqual(data["count"], 2)

    def test_filter_by_profile_id(self):
        """GET with ?profile_id=<pk> returns only that profile's mappings."""
        import json

        resp = self.client.get(
            f"/api/plugins/data-import/column-mappings/?profile_id={self.p1.pk}",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        for item in data["results"]:
            self.assertEqual(item["profile"], self.p1.pk)


class ClassRoleMappingAPITest(BaseAPITestCase):
    """Tests for the ClassRoleMapping REST API with profile_id filter."""

    def setUp(self):
        """Set up profiles and class-role mappings."""
        super().setUp()
        self.p1 = _make_profile("APICRMProfile1")
        self.p2 = _make_profile("APICRMProfile2")
        ClassRoleMapping.objects.create(profile=self.p1, source_class="Server", role_slug="server")
        ClassRoleMapping.objects.create(profile=self.p2, source_class="Server", role_slug="server")

    def test_list_all_class_role_mappings(self):
        """GET /api/plugins/data-import/class-role-mappings/ returns 200."""
        resp = self.client.get("/api/plugins/data-import/class-role-mappings/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)

    def test_filter_by_profile_id(self):
        """GET with ?profile_id filters to a single profile's mappings."""
        import json

        resp = self.client.get(
            f"/api/plugins/data-import/class-role-mappings/?profile_id={self.p1.pk}",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        for item in data["results"]:
            self.assertEqual(item["profile"], self.p1.pk)


class DeviceTypeMappingAPITest(BaseAPITestCase):
    """Tests for the DeviceTypeMapping REST API."""

    def setUp(self):
        """Set up profiles and device type mappings."""
        super().setUp()
        self.p1 = _make_profile("APIDTMProfile1")
        self.p2 = _make_profile("APIDTMProfile2")
        DeviceTypeMapping.objects.create(
            profile=self.p1,
            source_make="Cisco",
            source_model="C9300",
            netbox_manufacturer_slug="cisco",
            netbox_device_type_slug="cisco-c9300",
        )
        DeviceTypeMapping.objects.create(
            profile=self.p2,
            source_make="Dell",
            source_model="R660",
            netbox_manufacturer_slug="dell",
            netbox_device_type_slug="dell-r660",
        )

    def test_list_dtm(self):
        """GET device-type-mappings returns 200."""
        resp = self.client.get("/api/plugins/data-import/device-type-mappings/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)

    def test_filter_dtm_by_profile_id(self):
        """Filtering DTMs by profile_id returns only that profile's entries."""
        import json

        resp = self.client.get(
            f"/api/plugins/data-import/device-type-mappings/?profile_id={self.p1.pk}",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["source_make"], "Cisco")


class ManufacturerMappingAPITest(BaseAPITestCase):
    """Tests for the ManufacturerMapping (if exposed via API)."""

    def setUp(self):
        """Set up profile and manufacturer mapping."""
        super().setUp()
        self.profile = _make_profile("APIMfgProfile")
        ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Dell EMC",
            netbox_manufacturer_slug="dell",
        )

    def test_manufacturer_mapping_exists(self):
        """ManufacturerMapping is saved and retrievable."""
        mm = ManufacturerMapping.objects.get(profile=self.profile, source_make="Dell EMC")
        self.assertEqual(mm.netbox_manufacturer_slug, "dell")

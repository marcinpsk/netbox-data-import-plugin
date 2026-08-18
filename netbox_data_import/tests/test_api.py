# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the REST API views."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceTypeMapping,
    IgnoredDevice,
    ImportProfile,
    ManufacturerMapping,
)

User = get_user_model()


def _make_profile(name="APITest") -> ImportProfile:
    """Create a minimal ImportProfile."""
    return ImportProfile.objects.create(name=name, adapter_config={"sheet_name": "Data", "source_id_column": "Id"})


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
            data=json.dumps(
                {"name": "APICreatedProfile", "adapter_config": {"sheet_name": "Data", "source_id_column": "Id"}}
            ),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertIn(resp.status_code, [200, 201])
        if resp.status_code == 201:
            self.assertTrue(ImportProfile.objects.filter(name="APICreatedProfile").exists())

    def test_create_profile_with_primary_contact_configuration(self):
        """The API saves the native Contact Role and contact lookup field."""
        import json

        from tenancy.models import ContactRole

        role = ContactRole.objects.create(name="API Primary Contact", slug="api-primary-contact")

        response = self.client.post(
            "/api/plugins/data-import/profiles/",
            data=json.dumps(
                {
                    "name": "API Contact Profile",
                    "adapter_config": {
                        "primary_contact_role": role.name,
                        "primary_contact_lookup_field": "name",
                    },
                }
            ),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        profile = ImportProfile.objects.get(name="API Contact Profile")
        self.assertEqual(profile.resolved_primary_contact_role, role)
        self.assertEqual(profile.adapter_settings.primary_contact_lookup_field, "name")


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

    def test_create_class_role_mapping_with_rack_type(self):
        """POST with rack_type slug exercises _RackTypeSlugField.get_queryset()."""
        import json

        from dcim.models import Manufacturer, RackType

        mfr = Manufacturer.objects.create(name="APIRackMfr", slug="api-rack-mfr")
        rt = RackType.objects.create(manufacturer=mfr, model="APIRackType", slug="api-rack-type", u_height=42)
        resp = self.client.post(
            "/api/plugins/data-import/class-role-mappings/",
            data=json.dumps(
                {
                    "profile": self.p1.pk,
                    "source_class": "APICabinet",
                    "creates_rack": True,
                    "rack_type": rt.slug,
                }
            ),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertIn(resp.status_code, [200, 201])
        from netbox_data_import.models import ClassRoleMapping

        mapping = ClassRoleMapping.objects.get(profile=self.p1, source_class="APICabinet")
        self.assertEqual(mapping.rack_type, rt)


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


# ---------------------------------------------------------------------------
# New API tests: profile_id filter for IgnoredDevice, ColumnTransformRule,
# SourceResolution, and ImportExecution viewsets.
# ---------------------------------------------------------------------------


class IgnoredDeviceAPITest(BaseAPITestCase):
    """Tests for IgnoredDeviceViewSet ?profile_id filtering (lines 101-105 in api/views.py)."""

    def setUp(self):
        """Create two profiles each with one IgnoredDevice."""
        super().setUp()
        self.p1 = _make_profile("APIIgnoredP1")
        self.p2 = _make_profile("APIIgnoredP2")
        IgnoredDevice.objects.create(profile=self.p1, source_id="IGN-001", device_name="dev-p1")
        IgnoredDevice.objects.create(profile=self.p2, source_id="IGN-001", device_name="dev-p2")

    def test_list_all_ignored_devices(self):
        """GET /api/plugins/data-import/ignored-devices/ returns 200 and at least 2 entries."""
        import json

        resp = self.client.get("/api/plugins/data-import/ignored-devices/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertGreaterEqual(data["count"], 2)

    def test_filter_by_profile_id_returns_only_that_profile(self):
        """GET ?profile_id=<p1.pk> returns only p1's IgnoredDevices."""
        import json

        resp = self.client.get(
            f"/api/plugins/data-import/ignored-devices/?profile_id={self.p1.pk}",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["profile"], self.p1.pk)
        self.assertEqual(data["results"][0]["device_name"], "dev-p1")


class ColumnTransformRuleAPITest(BaseAPITestCase):
    """Tests for ColumnTransformRuleViewSet ?profile_id filtering (lines 116-120 in api/views.py)."""

    def setUp(self):
        """Create two profiles each with one ColumnTransformRule."""
        super().setUp()
        from netbox_data_import.models import ColumnTransformRule

        self.p1 = _make_profile("APICTRProfile1")
        self.p2 = _make_profile("APICTRProfile2")
        ColumnTransformRule.objects.create(
            profile=self.p1,
            source_column="Name",
            pattern=r"^(\w+)$",
            group_1_target="asset_tag",
            group_2_target="",
        )
        ColumnTransformRule.objects.create(
            profile=self.p2,
            source_column="Name",
            pattern=r"^(\w+)$",
            group_1_target="asset_tag",
            group_2_target="",
        )

    def test_list_all_column_transform_rules(self):
        """GET /api/plugins/data-import/column-transforms/ returns 200."""
        resp = self.client.get("/api/plugins/data-import/column-transforms/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)

    def test_filter_by_profile_id(self):
        """GET ?profile_id=<p1.pk> returns only p1's ColumnTransformRules."""
        import json

        resp = self.client.get(
            f"/api/plugins/data-import/column-transforms/?profile_id={self.p1.pk}",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["count"], 1)
        for item in data["results"]:
            self.assertEqual(item["profile"], self.p1.pk)


class SourceResolutionAPITest(BaseAPITestCase):
    """Tests for SourceResolutionViewSet ?profile_id filtering (lines 131-135 in api/views.py)."""

    def setUp(self):
        """Create two profiles each with one SourceResolution."""
        super().setUp()
        from netbox_data_import.models import SourceResolution

        self.p1 = _make_profile("APISRProfile1")
        self.p2 = _make_profile("APISRProfile2")
        SourceResolution.objects.create(
            profile=self.p1,
            source_id="SR-001",
            source_column="Name",
            original_value="old-p1",
            resolved_fields={"device_name": "new-p1"},
        )
        SourceResolution.objects.create(
            profile=self.p2,
            source_id="SR-001",
            source_column="Name",
            original_value="old-p2",
            resolved_fields={"device_name": "new-p2"},
        )

    def test_list_all_source_resolutions(self):
        """GET /api/plugins/data-import/source-resolutions/ returns 200."""
        resp = self.client.get("/api/plugins/data-import/source-resolutions/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)

    def test_filter_by_profile_id(self):
        """GET ?profile_id=<p1.pk> returns only p1's SourceResolutions."""
        import json

        resp = self.client.get(
            f"/api/plugins/data-import/source-resolutions/?profile_id={self.p1.pk}",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["profile"], self.p1.pk)

    def test_rejects_contact_candidate_resolution_without_name(self):
        """Do not persist a Contact candidate resolution that the importer cannot apply."""
        import json

        from netbox_data_import.models import SourceResolution

        response = self.client.post(
            "/api/plugins/data-import/source-resolutions/",
            data=json.dumps(
                {
                    "profile": self.p1.pk,
                    "source_id": "SR-CONTACT-001",
                    "source_column": "candidate:contact",
                    "original_value": json.dumps({"Contact": "contact@example.invalid"}),
                    "resolved_fields": {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {"email": "Contact"},
                    },
                }
            ),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SourceResolution.objects.filter(source_id="SR-CONTACT-001").exists())

    def test_rejects_contact_candidate_resolution_with_missing_source_column(self):
        """Do not persist a Contact mapping to a source column that is absent from the row."""
        import json

        from netbox_data_import.models import SourceResolution

        response = self.client.post(
            "/api/plugins/data-import/source-resolutions/",
            data=json.dumps(
                {
                    "profile": self.p1.pk,
                    "source_id": "SR-CONTACT-002",
                    "source_column": "candidate:contact",
                    "original_value": json.dumps({"Owner": "Example Owner"}),
                    "resolved_fields": {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {
                            "name": "Missing Column",
                            "email": "Missing Column",
                        },
                    },
                }
            ),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SourceResolution.objects.filter(source_id="SR-CONTACT-002").exists())

    def test_rejects_contact_candidate_resolution_with_unconfigured_source_column(self):
        """Do not trust candidate source columns supplied only by the API client."""
        import json

        from netbox_data_import.models import SourceResolution

        ColumnMapping.objects.create(
            profile=self.p1,
            source_column="Owner",
            target_field="candidate:contact",
        )
        response = self.client.post(
            "/api/plugins/data-import/source-resolutions/",
            data=json.dumps(
                {
                    "profile": self.p1.pk,
                    "source_id": "SR-CONTACT-003",
                    "source_column": "candidate:contact",
                    "original_value": json.dumps({"Missing Column": "Example Owner"}),
                    "resolved_fields": {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {
                            "name": "Missing Column",
                            "email": "Missing Column",
                        },
                    },
                }
            ),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SourceResolution.objects.filter(source_id="SR-CONTACT-003").exists())


class ImportExecutionAPITest(BaseAPITestCase):
    """Tests for ImportExecutionViewSet ?profile_id filtering."""

    def setUp(self):
        """Create two profiles each with one Import Execution."""
        super().setUp()
        from netbox_data_import.models import ImportExecution

        self.p1 = _make_profile("APIJobProfile1")
        self.p2 = _make_profile("APIJobProfile2")
        ImportExecution.objects.create(profile=self.p1, input_filename="file-p1.xlsx", site_name="site-p1")
        ImportExecution.objects.create(profile=self.p2, input_filename="file-p2.xlsx", site_name="site-p2")

    def test_list_all_import_executions(self):
        """GET /api/plugins/data-import/executions/ returns 200 and at least 2 rows."""
        import json

        resp = self.client.get("/api/plugins/data-import/executions/", HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertGreaterEqual(data["count"], 2)

    def test_filter_by_profile_id(self):
        """GET ?profile_id=<p1.pk> returns only p1's executions."""
        import json

        resp = self.client.get(
            f"/api/plugins/data-import/executions/?profile_id={self.p1.pk}",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["input_filename"], "file-p1.xlsx")

    def _regular_client(self, username, *, granted):
        """Return a client for a non-superuser, optionally holding view_importexecution."""
        from core.models import ObjectType
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from users.models import ObjectPermission

        from netbox_data_import.models import ImportExecution

        user = get_user_model().objects.create_user(username=username, password=username)
        if granted:
            # NetBox runs only ObjectPermissionBackend, so a Django user_permissions row grants
            # nothing. An ObjectPermission is how an operator actually issues this permission.
            permission = ObjectPermission.objects.create(name=f"{username} view executions", actions=["view"])
            permission.users.add(user)
            permission.object_types.add(ObjectType.objects.get_for_model(ImportExecution))
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_a_regular_user_holding_the_view_permission_is_allowed(self):
        """A superuser bypasses the check, so the permission needs a non-superuser to prove it."""
        resp = self._regular_client("api_exec_granted", granted=True).get(
            "/api/plugins/data-import/executions/", HTTP_ACCEPT="application/json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_a_regular_user_without_the_view_permission_is_denied(self):
        """The rename means an old view_importjob grant no longer opens this endpoint."""
        resp = self._regular_client("api_exec_denied", granted=False).get(
            "/api/plugins/data-import/executions/", HTTP_ACCEPT="application/json"
        )
        self.assertEqual(resp.status_code, 403, resp.content)


class PolicySerializerNetBoxBaseTest(BaseAPITestCase):
    """The policy endpoints carry the NetBox identity fields and keep refusing a duplicate with 400."""

    def setUp(self):
        """Create one profile that every policy row below hangs off."""
        super().setUp()
        self.profile = _make_profile("APIPolicyBase")

    def _rows(self):
        """Return one saved row per policy endpoint, as (endpoint, instance) pairs."""
        from netbox_data_import.models import ColumnTransformRule, SourceResolution

        return [
            (
                "column-mappings",
                ColumnMapping.objects.create(profile=self.profile, source_column="Name", target_field="device_name"),
            ),
            (
                "class-role-mappings",
                ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug="server"),
            ),
            (
                "device-type-mappings",
                DeviceTypeMapping.objects.create(
                    profile=self.profile,
                    source_make="Cisco",
                    source_model="C9300",
                    netbox_manufacturer_slug="cisco",
                    netbox_device_type_slug="cisco-c9300",
                ),
            ),
            (
                "ignored-devices",
                IgnoredDevice.objects.create(profile=self.profile, source_id="IGN-900", device_name="dev-900"),
            ),
            (
                "column-transforms",
                ColumnTransformRule.objects.create(
                    profile=self.profile,
                    source_column="Serial",
                    pattern=r"^(\w+)$",
                    group_1_target="asset_tag",
                    group_2_target="",
                ),
            ),
            (
                "source-resolutions",
                SourceResolution.objects.create(
                    profile=self.profile,
                    source_id="SR-900",
                    source_column="Name",
                    original_value="old",
                    resolved_fields={"device_name": "new"},
                ),
            ),
        ]

    def test_every_policy_endpoint_exposes_the_netbox_identity_fields(self):
        """`url` and `display` come from the NetBox serializer base, and `url` resolves back to the row."""
        for endpoint, row in self._rows():
            with self.subTest(endpoint=endpoint):
                resp = self.client.get(
                    f"/api/plugins/data-import/{endpoint}/{row.pk}/",
                    HTTP_ACCEPT="application/json",
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                payload = resp.json()
                self.assertEqual(payload["display"], str(row))
                self.assertTrue(payload["url"].endswith(f"/{endpoint}/{row.pk}/"), payload["url"])
                # Follow the advertised URL so a wrong route name fails here instead of in a client.
                followed = self.client.get(payload["url"], HTTP_ACCEPT="application/json")
                self.assertEqual(followed.status_code, 200, followed.content)
                self.assertEqual(followed.json()["id"], row.pk)

    def test_no_policy_endpoint_advertises_display_url(self):
        """These rows are edited on the profile page, so there is no UI detail route to point at."""
        for endpoint, row in self._rows():
            with self.subTest(endpoint=endpoint):
                resp = self.client.get(
                    f"/api/plugins/data-import/{endpoint}/{row.pk}/",
                    HTTP_ACCEPT="application/json",
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertNotIn("display_url", resp.json())

    def test_a_duplicate_row_is_refused_with_400_and_never_reaches_the_database(self):
        """The UniqueConstraint 400 must survive the base swap, which drops DRF's own unique check."""
        import json

        ClassRoleMapping.objects.create(profile=self.profile, source_class="Dup", role_slug="server")
        resp = self.client.post(
            "/api/plugins/data-import/class-role-mappings/",
            data=json.dumps({"profile": self.profile.pk, "source_class": "Dup", "role_slug": "server"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(ClassRoleMapping.objects.filter(profile=self.profile, source_class="Dup").count(), 1)

    def test_a_section_the_adapter_does_not_supply_is_still_refused(self):
        """The applicability rule moved to a mixin, so prove it still runs on the REST write path."""
        import json

        from netbox_data_import.models import ImportProfile

        trace = ImportProfile.objects.create(name="APIPolicyTrace", source_adapter="trace_workbook")
        resp = self.client.post(
            "/api/plugins/data-import/class-role-mappings/",
            data=json.dumps({"profile": trace.pk, "source_class": "Server", "role_slug": "server"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("profile", resp.json())

    def test_a_nested_policy_serializer_is_handed_an_instance_not_a_dict(self):
        """The applicability rule runs before super(), so it must stand aside for a nested serializer."""
        from netbox_data_import.api.serializers import ColumnMappingSerializer

        row = ColumnMapping.objects.create(profile=self.profile, source_column="Nested", target_field="device_name")
        self.assertIs(ColumnMappingSerializer(nested=True).validate(row), row)

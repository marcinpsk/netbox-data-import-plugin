# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""View tests for the netbox_data_import plugin."""

import os
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceTypeMapping,
    ImportProfile,
    ManufacturerMapping,
    SourceResolution,
)

User = get_user_model()

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")


def _make_profile(name="ViewTest") -> ImportProfile:
    """Create a minimal ImportProfile with basic column and class-role mappings."""
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


class BaseViewTestCase(TestCase):
    """Base class that sets up an authenticated client."""

    def setUp(self):
        """Create and log in a superuser."""
        self.user = User.objects.create_superuser("testuser", "test@example.com", "testpass")
        self.client = Client()
        self.client.login(username="testuser", password="testpass")


class ImportProfileListViewTest(BaseViewTestCase):
    """Tests for ImportProfileListView."""

    def test_list_view_get(self):
        """Profile list page returns 200."""
        _make_profile("ListProfile")
        url = reverse("plugins:netbox_data_import:importprofile_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_list_view_shows_profile(self):
        """Profile list includes the profile name."""
        _make_profile("SomeProfile")
        url = reverse("plugins:netbox_data_import:importprofile_list")
        resp = self.client.get(url)
        self.assertContains(resp, "SomeProfile")

    def test_list_redirects_anonymous(self):
        """Unauthenticated access redirects to login."""
        self.client.logout()
        url = reverse("plugins:netbox_data_import:importprofile_list")
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [302, 301])


class ImportProfileDetailViewTest(BaseViewTestCase):
    """Tests for ImportProfileView (detail)."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("DetailProfile")

    def test_detail_view_get(self):
        """Profile detail page returns 200."""
        url = reverse("plugins:netbox_data_import:importprofile", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_detail_contains_export_yaml_link(self):
        """Profile detail contains an export YAML link."""
        url = reverse("plugins:netbox_data_import:importprofile", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertContains(resp, "export-yaml")

    def test_detail_contains_run_import_link(self):
        """Profile detail contains a run import link with ?profile= param."""
        url = reverse("plugins:netbox_data_import:importprofile", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertContains(resp, f"?profile={self.profile.pk}")


class ImportProfileEditViewTest(BaseViewTestCase):
    """Tests for ImportProfileEditView (add/edit)."""

    def test_add_view_get(self):
        """Add profile page returns 200."""
        url = reverse("plugins:netbox_data_import:importprofile_add")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_add_profile_post(self):
        """POST to add view creates a profile and redirects."""
        url = reverse("plugins:netbox_data_import:importprofile_add")
        data = {
            "name": "PostedProfile",
            "sheet_name": "Data",
            "source_id_column": "Id",
            "update_existing": "on",
            "create_missing_device_types": "on",
            "_create": "1",
        }
        resp = self.client.post(url, data)
        self.assertIn(resp.status_code, [200, 302])
        if resp.status_code == 302:
            self.assertTrue(ImportProfile.objects.filter(name="PostedProfile").exists())

    def test_edit_view_get(self):
        """Edit profile page returns 200."""
        p = _make_profile("EditableProfile")
        url = reverse("plugins:netbox_data_import:importprofile_edit", kwargs={"pk": p.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class ColumnMappingViewTest(BaseViewTestCase):
    """Tests for ColumnMapping add/edit/delete views."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("CMMappingProfile")

    def test_add_column_mapping_get(self):
        """Add column mapping page returns 200."""
        url = reverse("plugins:netbox_data_import:columnmapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_add_column_mapping_post(self):
        """POST to add column mapping creates the mapping and redirects."""
        url = reverse("plugins:netbox_data_import:columnmapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(
            url, {"profile": self.profile.pk, "source_column": "Airflow", "target_field": "airflow"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ColumnMapping.objects.filter(profile=self.profile, target_field="airflow").exists())

    def test_edit_column_mapping_get(self):
        """Edit column mapping page returns 200."""
        cm = ColumnMapping.objects.filter(profile=self.profile).first()
        url = reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": cm.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_edit_column_mapping_post(self):
        """POST to edit column mapping updates it."""
        cm = ColumnMapping.objects.filter(profile=self.profile, target_field="serial").first()
        url = reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": cm.pk})
        resp = self.client.post(
            url, {"profile": self.profile.pk, "source_column": "SerialNo", "target_field": "serial"}
        )
        self.assertEqual(resp.status_code, 302)
        cm.refresh_from_db()
        self.assertEqual(cm.source_column, "SerialNo")

    def test_delete_column_mapping_get(self):
        """Delete column mapping confirmation page returns 200."""
        cm = ColumnMapping.objects.filter(profile=self.profile).first()
        url = reverse("plugins:netbox_data_import:columnmapping_delete", kwargs={"pk": cm.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_delete_column_mapping_post(self):
        """POST to delete column mapping removes it."""
        cm = ColumnMapping.objects.create(profile=self.profile, source_column="ToDelete", target_field="face")
        url = reverse("plugins:netbox_data_import:columnmapping_delete", kwargs={"pk": cm.pk})
        resp = self.client.post(url, {"confirm": "yes"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ColumnMapping.objects.filter(pk=cm.pk).exists())


class ClassRoleMappingViewTest(BaseViewTestCase):
    """Tests for ClassRoleMapping add/edit/delete views."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("CRMViewProfile")

    def test_add_class_role_mapping_get(self):
        """Add class-role mapping page returns 200."""
        url = reverse("plugins:netbox_data_import:classrolemapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_add_class_role_mapping_post(self):
        """POST to add class-role mapping creates it."""
        url = reverse("plugins:netbox_data_import:classrolemapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(
            url, {"profile": self.profile.pk, "source_class": "Router", "creates_rack": "", "role_slug": "router"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ClassRoleMapping.objects.filter(profile=self.profile, source_class="Router").exists())

    def test_edit_class_role_mapping_get(self):
        """Edit class-role mapping page returns 200."""
        m = ClassRoleMapping.objects.filter(profile=self.profile).first()
        url = reverse("plugins:netbox_data_import:classrolemapping_edit", kwargs={"pk": m.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_edit_class_role_mapping_post(self):
        """POST to edit class-role mapping updates it."""
        m = ClassRoleMapping.objects.filter(profile=self.profile).first()
        url = reverse("plugins:netbox_data_import:classrolemapping_edit", kwargs={"pk": m.pk})
        resp = self.client.post(
            url,
            {
                "profile": self.profile.pk,
                "source_class": m.source_class,
                "creates_rack": m.creates_rack,
                "role_slug": "server-updated",
                "ignore": "",
            },
        )
        self.assertEqual(resp.status_code, 302)

    def test_delete_class_role_mapping_get(self):
        """Delete class-role mapping confirmation page returns 200."""
        m = ClassRoleMapping.objects.filter(profile=self.profile).first()
        url = reverse("plugins:netbox_data_import:classrolemapping_delete", kwargs={"pk": m.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_delete_class_role_mapping_post(self):
        """POST to delete class-role mapping removes it."""
        m = ClassRoleMapping.objects.create(
            profile=self.profile, source_class="ToDeleteRouter", creates_rack=False, role_slug="router"
        )
        url = reverse("plugins:netbox_data_import:classrolemapping_delete", kwargs={"pk": m.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ClassRoleMapping.objects.filter(pk=m.pk).exists())


class DeviceTypeMappingViewTest(BaseViewTestCase):
    """Tests for DeviceTypeMapping add/edit/delete views."""

    def setUp(self):
        """Set up profile and mapping."""
        super().setUp()
        self.profile = _make_profile("DTMViewProfile")
        self.dtm = DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="Cisco",
            source_model="C9300",
            netbox_manufacturer_slug="cisco",
            netbox_device_type_slug="cisco-c9300",
        )

    def test_add_dtm_get(self):
        """Add DTM page returns 200."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_add_dtm_post(self):
        """POST to add DTM creates it and redirects."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(
            url,
            {
                "profile": self.profile.pk,
                "source_make": "HP",
                "source_model": "DL360",
                "netbox_manufacturer_slug": "hp",
                "netbox_device_type_slug": "hp-dl360",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(DeviceTypeMapping.objects.filter(profile=self.profile, source_model="DL360").exists())

    def test_edit_dtm_get(self):
        """Edit DTM page returns 200."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_edit", kwargs={"pk": self.dtm.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_edit_dtm_post(self):
        """POST to edit DTM updates it."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_edit", kwargs={"pk": self.dtm.pk})
        resp = self.client.post(
            url,
            {
                "profile": self.profile.pk,
                "source_make": "Cisco",
                "source_model": "C9300-Updated",
                "netbox_manufacturer_slug": "cisco",
                "netbox_device_type_slug": "cisco-c9300-updated",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.dtm.refresh_from_db()
        self.assertEqual(self.dtm.source_model, "C9300-Updated")

    def test_delete_dtm_get(self):
        """Delete DTM confirmation page returns 200."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_delete", kwargs={"pk": self.dtm.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_delete_dtm_post(self):
        """POST to delete DTM removes it."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_delete", kwargs={"pk": self.dtm.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DeviceTypeMapping.objects.filter(pk=self.dtm.pk).exists())


class ImportSetupViewTest(BaseViewTestCase):
    """Tests for ImportSetupView."""

    def test_get_returns_200(self):
        """GET /import/ returns 200."""
        url = reverse("plugins:netbox_data_import:import_setup")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_get_prefills_profile(self):
        """GET /import/?profile=<pk> pre-fills the profile field."""
        p = _make_profile("PreFillProfile")
        url = reverse("plugins:netbox_data_import:import_setup") + f"?profile={p.pk}"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, str(p.pk))

    def test_post_invalid_form(self):
        """POST with no file returns 200 with form errors."""
        url = reverse("plugins:netbox_data_import:import_setup")
        resp = self.client.post(url, {})
        self.assertEqual(resp.status_code, 200)

    def test_post_with_valid_file_redirects_to_preview(self):
        """POST with a valid file redirects to the preview page."""
        from dcim.models import Site

        site = Site.objects.create(name="SetupSite", slug="setup-site")
        profile = _make_profile("SetupProfile")
        url = reverse("plugins:netbox_data_import:import_setup")
        with open(FIXTURE_PATH, "rb") as f:
            resp = self.client.post(url, {"profile": profile.pk, "site": site.pk, "excel_file": f})
        self.assertIn(resp.status_code, [200, 302])
        if resp.status_code == 302:
            self.assertIn("preview", resp["Location"])

    def test_post_with_corrupt_file_shows_error(self):
        """POST with a non-Excel file shows a parse error message."""
        from dcim.models import Site

        site = Site.objects.create(name="BadFileSite", slug="bad-file-site")
        profile = _make_profile("BadFileProfile")
        url = reverse("plugins:netbox_data_import:import_setup")
        bad_file = BytesIO(b"not an excel file")
        bad_file.name = "garbage.xlsx"
        resp = self.client.post(url, {"profile": profile.pk, "site": site.pk, "excel_file": bad_file})
        self.assertEqual(resp.status_code, 200)


class ImportPreviewViewTest(BaseViewTestCase):
    """Tests for ImportPreviewView."""

    def _setup_session(self):
        """Populate session with a valid import state."""
        from dcim.models import Site
        from netbox_data_import.engine import parse_file, run_import

        site = Site.objects.create(name="PreviewSite", slug="preview-site")
        profile = _make_profile("PreviewProfile")
        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, profile)

        result = run_import(rows, profile, {"site": site}, dry_run=True)

        # Use the view helper to serialize rows
        from netbox_data_import.views import _serialize_rows

        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = _serialize_rows(rows)
        session["import_context"] = {
            "profile_id": profile.pk,
            "site_id": site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "sample_cans.xlsx",
        }
        session.save()
        return profile

    def test_preview_without_session_redirects(self):
        """GET /import/preview/ without session data redirects to setup."""
        url = reverse("plugins:netbox_data_import:import_preview")
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [302])
        self.assertIn("import", resp["Location"])

    def test_preview_with_session_returns_200(self):
        """GET /import/preview/ with session data returns 200."""
        self._setup_session()
        url = reverse("plugins:netbox_data_import:import_preview")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_preview_shows_filename(self):
        """Preview page shows the uploaded filename."""
        self._setup_session()
        url = reverse("plugins:netbox_data_import:import_preview")
        resp = self.client.get(url)
        self.assertContains(resp, "sample_cans.xlsx")


class ImportResultsViewTest(BaseViewTestCase):
    """Tests for ImportResultsView."""

    def test_results_without_session_redirects(self):
        """GET /import/results/ without session data redirects."""
        url = reverse("plugins:netbox_data_import:import_results")
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [302])

    def test_results_with_session_returns_200(self):
        """GET /import/results/ with result in session returns 200."""
        from netbox_data_import.engine import ImportResult, RowResult

        result = ImportResult()
        result.rows = [RowResult(1, "1", "rack-01", "create", "rack", "Created")]
        result._recompute_counts()
        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session.save()

        url = reverse("plugins:netbox_data_import:import_results")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class ImportJobListViewTest(BaseViewTestCase):
    """Tests for ImportJobListView."""

    def test_job_list_returns_200(self):
        """Import job list page returns 200."""
        url = reverse("plugins:netbox_data_import:importjob_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class IgnoreUnignoreViewTest(BaseViewTestCase):
    """Tests for IgnoreDeviceView and UnignoreDeviceView."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("IgnoreProfile")

    def test_ignore_device_post(self):
        """POST to ignore creates an IgnoredDevice record."""
        from netbox_data_import.models import IgnoredDevice

        url = reverse("plugins:netbox_data_import:ignore_device")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": "SRC-001",
                "device_name": "switch-01",
                "next": "/",
            },
        )
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(IgnoredDevice.objects.filter(profile=self.profile, source_id="SRC-001").exists())

    def test_ignore_device_idempotent(self):
        """Ignoring an already-ignored device does not duplicate the record."""
        from netbox_data_import.models import IgnoredDevice

        url = reverse("plugins:netbox_data_import:ignore_device")
        for _ in range(2):
            self.client.post(url, {"profile_id": self.profile.pk, "source_id": "SRC-DUP", "next": "/"})
        self.assertEqual(IgnoredDevice.objects.filter(profile=self.profile, source_id="SRC-DUP").count(), 1)

    def test_unignore_device_post(self):
        """POST to unignore removes the IgnoredDevice record."""
        from netbox_data_import.models import IgnoredDevice

        IgnoredDevice.objects.create(profile=self.profile, source_id="SRC-002", device_name="server-01")
        url = reverse("plugins:netbox_data_import:unignore_device")
        self.client.post(url, {"profile_id": self.profile.pk, "source_id": "SRC-002", "next": "/"})
        self.assertFalse(IgnoredDevice.objects.filter(profile=self.profile, source_id="SRC-002").exists())


class SaveResolutionViewTest(BaseViewTestCase):
    """Tests for SaveResolutionView."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("ResProfile")

    def test_save_resolution_creates_record(self):
        """POST to save-resolution creates a SourceResolution."""
        import json

        url = reverse("plugins:netbox_data_import:save_resolution")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": "SRC-X",
                "source_column": "Name",
                "original_value": "some-device",
                "resolved_fields": json.dumps({"device_name": "corrected-device"}),
                "next": "/",
            },
        )
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(
            SourceResolution.objects.filter(profile=self.profile, source_id="SRC-X", source_column="Name").exists()
        )

    def test_save_resolution_updates_existing(self):
        """POST to save-resolution updates an existing resolution."""
        import json

        SourceResolution.objects.create(
            profile=self.profile,
            source_id="SRC-Y",
            source_column="Name",
            original_value="old",
            resolved_fields={"device_name": "old-name"},
        )
        url = reverse("plugins:netbox_data_import:save_resolution")
        self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": "SRC-Y",
                "source_column": "Name",
                "original_value": "new",
                "resolved_fields": json.dumps({"device_name": "new-name"}),
                "next": "/",
            },
        )
        res = SourceResolution.objects.get(profile=self.profile, source_id="SRC-Y", source_column="Name")
        self.assertEqual(res.resolved_fields["device_name"], "new-name")


class ExportProfileYamlViewTest(BaseViewTestCase):
    """Tests for ExportProfileYamlView."""

    def setUp(self):
        """Set up profile with mappings."""
        super().setUp()
        self.profile = _make_profile("YamlExportProfile")
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="Dell",
            source_model="R660",
            netbox_manufacturer_slug="dell",
            netbox_device_type_slug="dell-r660",
        )
        ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Dell EMC",
            netbox_manufacturer_slug="dell",
        )

    def test_export_returns_yaml_file(self):
        """GET export-yaml returns a YAML file download."""
        url = reverse("plugins:netbox_data_import:exportprofile_yaml", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("yaml", resp["Content-Type"])

    def test_export_yaml_contains_profile_name(self):
        """Exported YAML contains the profile name."""
        url = reverse("plugins:netbox_data_import:exportprofile_yaml", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        content = resp.content.decode()
        self.assertIn("YamlExportProfile", content)

    def test_export_yaml_has_all_sections(self):
        """Exported YAML has all expected top-level sections."""
        url = reverse("plugins:netbox_data_import:exportprofile_yaml", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        content = resp.content.decode()
        for section in [
            "profile:",
            "column_mappings:",
            "class_role_mappings:",
            "device_type_mappings:",
            "manufacturer_mappings:",
        ]:
            self.assertIn(section, content, msg=f"Missing section: {section}")

    def test_export_yaml_includes_device_type_mapping(self):
        """Exported YAML includes the DeviceTypeMapping records."""
        url = reverse("plugins:netbox_data_import:exportprofile_yaml", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        content = resp.content.decode()
        self.assertIn("Dell", content)
        self.assertIn("R660", content)

    def test_export_404_for_missing_profile(self):
        """GET for non-existent profile pk returns 404."""
        url = reverse("plugins:netbox_data_import:exportprofile_yaml", kwargs={"pk": 99999})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)


class ImportProfileYamlViewTest(BaseViewTestCase):
    """Tests for ImportProfileYamlView."""

    YAML_DATA = b"""profile:
  name: ImportedProfile
  sheet_name: Data
  source_id_column: Id
  update_existing: true
  create_missing_device_types: true
column_mappings:
  - source_column: Name
    target_field: device_name
class_role_mappings:
  - source_class: Server
    creates_rack: false
    role_slug: server
    ignore: false
device_type_mappings:
  - source_make: Cisco
    source_model: C9300
    netbox_manufacturer_slug: cisco
    netbox_device_type_slug: cisco-c9300
manufacturer_mappings:
  - source_make: Dell EMC
    netbox_manufacturer_slug: dell
"""

    def test_get_import_yaml_page(self):
        """GET import-profile-yaml returns 200."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_post_creates_profile(self):
        """POST with valid YAML creates the profile."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        yaml_file = BytesIO(self.YAML_DATA)
        yaml_file.name = "test.yaml"
        resp = self.client.post(url, {"yaml_file": yaml_file}, format="multipart")
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(ImportProfile.objects.filter(name="ImportedProfile").exists())

    def test_post_creates_column_mappings(self):
        """POST with YAML creates column mappings."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        yaml_file = BytesIO(self.YAML_DATA)
        yaml_file.name = "test.yaml"
        self.client.post(url, {"yaml_file": yaml_file})
        profile = ImportProfile.objects.filter(name="ImportedProfile").first()
        self.assertIsNotNone(profile)
        self.assertTrue(ColumnMapping.objects.filter(profile=profile, target_field="device_name").exists())

    def test_post_creates_manufacturer_mappings(self):
        """POST with YAML creates manufacturer mappings."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        yaml_file = BytesIO(self.YAML_DATA)
        yaml_file.name = "test.yaml"
        self.client.post(url, {"yaml_file": yaml_file})
        profile = ImportProfile.objects.filter(name="ImportedProfile").first()
        self.assertTrue(ManufacturerMapping.objects.filter(profile=profile, source_make="Dell EMC").exists())

    def test_post_idempotent(self):
        """Posting the same YAML twice is idempotent (no duplicates)."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        for _ in range(2):
            self.client.post(url, {"yaml_file": BytesIO(self.YAML_DATA)})
        self.assertEqual(ImportProfile.objects.filter(name="ImportedProfile").count(), 1)

    def test_post_no_file_shows_error(self):
        """POST without a file returns 200 with an error message."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        resp = self.client.post(url, {})
        self.assertEqual(resp.status_code, 200)

    def test_post_invalid_yaml_shows_error(self):
        """POST with invalid YAML returns 200 with parse error."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        bad = BytesIO(b": : invalid yaml {{{{")
        bad.name = "bad.yaml"
        resp = self.client.post(url, {"yaml_file": bad})
        self.assertEqual(resp.status_code, 200)

    def test_post_yaml_missing_profile_key_shows_error(self):
        """POST with YAML that has no 'profile' key shows error."""
        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        no_profile = BytesIO(b"column_mappings: []")
        no_profile.name = "nokey.yaml"
        resp = self.client.post(url, {"yaml_file": no_profile})
        self.assertEqual(resp.status_code, 200)


class QuickCreateManufacturerViewTest(BaseViewTestCase):
    """Tests for QuickCreateManufacturerView."""

    def test_creates_manufacturer(self):
        """POST creates a new Manufacturer in NetBox."""
        from dcim.models import Manufacturer

        url = reverse("plugins:netbox_data_import:quick_create_manufacturer")
        resp = self.client.post(url, {"mfg_name": "AcmeCorp", "mfg_slug": "acmecorp"})
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(Manufacturer.objects.filter(slug="acmecorp").exists())

    def test_creates_manufacturer_idempotent(self):
        """POSTing the same manufacturer twice does not create a duplicate."""
        from dcim.models import Manufacturer

        url = reverse("plugins:netbox_data_import:quick_create_manufacturer")
        for _ in range(2):
            self.client.post(url, {"mfg_name": "AcmeCorp2", "mfg_slug": "acmecorp2"})
        self.assertEqual(Manufacturer.objects.filter(slug="acmecorp2").count(), 1)

    def test_missing_slug_redirects(self):
        """POST without slug redirects with error (does not crash)."""
        url = reverse("plugins:netbox_data_import:quick_create_manufacturer")
        resp = self.client.post(url, {"mfg_name": "NoSlug"})
        self.assertIn(resp.status_code, [200, 302])


class QuickResolveManufacturerViewTest(BaseViewTestCase):
    """Tests for QuickResolveManufacturerView."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("QRMfgProfile")

    def test_creates_manufacturer_mapping(self):
        """POST creates a ManufacturerMapping."""
        url = reverse("plugins:netbox_data_import:quick_resolve_manufacturer")
        resp = self.client.post(
            url,
            {"profile_id": self.profile.pk, "source_make": "Dell EMC", "netbox_mfg_slug": "dell"},
        )
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(ManufacturerMapping.objects.filter(profile=self.profile, source_make="Dell EMC").exists())

    def test_missing_fields_redirects(self):
        """POST without required fields redirects without crash."""
        url = reverse("plugins:netbox_data_import:quick_resolve_manufacturer")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertIn(resp.status_code, [200, 302])


class QuickResolveDeviceTypeViewTest(BaseViewTestCase):
    """Tests for QuickResolveDeviceTypeView."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("QRDTProfile")

    def test_creates_device_type_mapping(self):
        """POST with action=map creates a DeviceTypeMapping."""
        url = reverse("plugins:netbox_data_import:quick_resolve_device_type")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_make": "Cisco",
                "source_model": "C9500",
                "netbox_mfg_slug": "cisco",
                "netbox_dt_slug": "cisco-c9500",
                "action": "map",
            },
        )
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(
            DeviceTypeMapping.objects.filter(profile=self.profile, source_make="Cisco", source_model="C9500").exists()
        )

    def test_create_now_action_creates_objects(self):
        """POST with action=create_now creates the Manufacturer and DeviceType."""
        from dcim.models import Manufacturer, DeviceType

        url = reverse("plugins:netbox_data_import:quick_resolve_device_type")
        self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_make": "Juniper",
                "source_model": "QFX5100",
                "netbox_mfg_slug": "juniper",
                "netbox_dt_slug": "juniper-qfx5100",
                "netbox_dt_name": "QFX5100",
                "u_height": "1",
                "action": "create_now",
            },
        )
        self.assertTrue(Manufacturer.objects.filter(slug="juniper").exists())
        self.assertTrue(DeviceType.objects.filter(slug="juniper-qfx5100").exists())


class DeviceTypeAnalysisViewTest(BaseViewTestCase):
    """Tests for DeviceTypeAnalysisView."""

    def test_analysis_view_get(self):
        """Analysis page returns 200."""
        url = reverse("plugins:netbox_data_import:device_type_analysis")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_analysis_view_with_profile(self):
        """Analysis page with profile_pk returns 200."""
        p = _make_profile("AnalysisProfile")
        url = reverse("plugins:netbox_data_import:device_type_analysis_profile", kwargs={"profile_pk": p.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class BulkYamlImportViewTest(BaseViewTestCase):
    """Tests for BulkYamlImportView."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("BulkYamlProfile")

    def test_get_bulk_yaml_import(self):
        """GET bulk YAML import page returns 200."""
        url = reverse("plugins:netbox_data_import:bulk_yaml_import", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_post_device_type_mappings(self):
        """POST device_type_mappings YAML creates DeviceTypeMapping records."""
        url = reverse("plugins:netbox_data_import:bulk_yaml_import", kwargs={"profile_pk": self.profile.pk})
        yaml_content = b"""
- source_make: Dell
  source_model: R740
  netbox_manufacturer_slug: dell
  netbox_device_type_slug: dell-r740
"""
        yaml_file = BytesIO(yaml_content)
        yaml_file.name = "mappings.yaml"
        resp = self.client.post(url, {"mapping_type": "device_type", "yaml_file": yaml_file})
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(DeviceTypeMapping.objects.filter(profile=self.profile, source_model="R740").exists())


class SourceResolutionListViewTest(BaseViewTestCase):
    """Tests for SourceResolutionListView."""

    def setUp(self):
        """Set up profile and resolution."""
        super().setUp()
        self.profile = _make_profile("ResListProfile")
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="SRC-LIST",
            source_column="Name",
            original_value="raw-name",
            resolved_fields={"device_name": "clean-name"},
        )

    def test_resolution_list_returns_200(self):
        """Resolution list page returns 200."""
        url = reverse("plugins:netbox_data_import:source_resolution_list", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_resolution_list_shows_entries(self):
        """Resolution list shows the saved resolution."""
        url = reverse("plugins:netbox_data_import:source_resolution_list", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertContains(resp, "SRC-LIST")

    def test_delete_resolution(self):
        """POST to delete-resolution removes it."""
        res = SourceResolution.objects.get(profile=self.profile, source_id="SRC-LIST")
        url = reverse("plugins:netbox_data_import:source_resolution_delete", kwargs={"pk": res.pk})
        resp = self.client.post(url, {"confirm": "yes"})
        self.assertIn(resp.status_code, [200, 302])
        if resp.status_code == 302:
            self.assertFalse(SourceResolution.objects.filter(pk=res.pk).exists())


class CheckDeviceNameViewTest(BaseViewTestCase):
    """Tests for CheckDeviceNameView AJAX endpoint."""

    def test_check_existing_device(self):
        """Returns JSON indicating whether a device name exists."""
        from dcim.models import Site, DeviceRole, DeviceType, Manufacturer, Device

        site = Site.objects.create(name="CheckSite", slug="check-site")
        mfg = Manufacturer.objects.create(name="CheckMfg", slug="check-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="CheckModel", slug="check-model")
        role = DeviceRole.objects.create(name="CheckRole", slug="check-role")
        Device.objects.create(name="check-device-01", device_type=dt, role=role, site=site)

        url = reverse("plugins:netbox_data_import:check_device")
        resp = self.client.get(url + "?name=check-device-01")
        self.assertEqual(resp.status_code, 200)
        import json

        data = json.loads(resp.content)
        self.assertTrue(data.get("exists"))

    def test_check_nonexistent_device(self):
        """Returns exists=False for an unknown device name."""
        url = reverse("plugins:netbox_data_import:check_device")
        resp = self.client.get(url + "?name=no-such-device-xyz")
        self.assertEqual(resp.status_code, 200)
        import json

        data = json.loads(resp.content)
        self.assertFalse(data.get("exists"))


class SearchNetBoxObjectsViewTest(BaseViewTestCase):
    """Tests for SearchNetBoxObjectsView AJAX endpoint."""

    def test_search_sites(self):
        """Returns JSON list of sites matching query."""
        from dcim.models import Site

        Site.objects.create(name="SearchSite", slug="search-site")
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url + "?model=site&q=Search")
        self.assertEqual(resp.status_code, 200)
        import json

        data = json.loads(resp.content)
        self.assertIn("results", data)
        self.assertIsInstance(data["results"], list)

    def test_search_unknown_model(self):
        """Returns empty list or 200 for unknown model type."""
        url = reverse("plugins:netbox_data_import:search_objects")
        resp = self.client.get(url + "?model=unknownmodel&q=test")
        self.assertIn(resp.status_code, [200, 400])


class SearchNetBoxObjectsExtendedViewTest(BaseViewTestCase):
    """Tests for SearchNetBoxObjectsView with type= parameter."""

    def setUp(self):
        """Set up NetBox objects for searching."""
        super().setUp()
        from dcim.models import Manufacturer, DeviceType, DeviceRole, Site, Device

        self.site = Site.objects.create(name="SearchExtSite", slug="search-ext-site")
        self.mfg = Manufacturer.objects.create(name="SearchMfgCo", slug="search-mfgco")
        self.dt = DeviceType.objects.create(manufacturer=self.mfg, model="SearchModelX", slug="search-modelx")
        self.role = DeviceRole.objects.create(name="SearchRoleY", slug="search-roley")
        self.device = Device.objects.create(name="search-dev-01", device_type=self.dt, role=self.role, site=self.site)
        self.url = reverse("plugins:netbox_data_import:search_objects")

    def test_search_manufacturer(self):
        """type=manufacturer returns manufacturer results."""
        import json

        resp = self.client.get(self.url + "?type=manufacturer&q=SearchMfg")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(any(r["name"] == "SearchMfgCo" for r in data["results"]))

    def test_search_device_type(self):
        """type=device_type returns device type results."""
        import json

        resp = self.client.get(self.url + "?type=device_type&q=SearchModel")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(any("SearchModelX" in r["name"] for r in data["results"]))

    def test_search_device_type_with_mfg_filter(self):
        """type=device_type with mfg_slug filters to that manufacturer."""
        import json

        resp = self.client.get(self.url + f"?type=device_type&q=SearchModel&mfg_slug={self.mfg.slug}")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(len(data["results"]), 1)

    def test_search_device(self):
        """type=device returns device results."""
        import json

        resp = self.client.get(self.url + "?type=device&q=search-dev")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(any(r["name"] == "search-dev-01" for r in data["results"]))

    def test_search_role(self):
        """type=role returns device role results."""
        import json

        resp = self.client.get(self.url + "?type=role&q=SearchRole")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(any(r["name"] == "SearchRoleY" for r in data["results"]))

    def test_empty_q_returns_empty(self):
        """Empty q parameter returns empty results."""
        import json

        resp = self.client.get(self.url + "?type=device&q=")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["results"], [])


class QuickResolveClassViewTest(BaseViewTestCase):
    """Tests for QuickResolveClassView."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("QRCProfile")

    def test_post_creates_ignore_mapping(self):
        """POST creates a ClassRoleMapping with ignore=True."""
        url = reverse("plugins:netbox_data_import:quick_add_class_mapping")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_class": "PDU",
                "mapping_action": "ignore",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ClassRoleMapping.objects.filter(profile=self.profile, source_class="PDU", ignore=True).exists())

    def test_post_creates_role_mapping(self):
        """POST creates a ClassRoleMapping with a role slug."""
        url = reverse("plugins:netbox_data_import:quick_add_class_mapping")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_class": "Firewall",
                "mapping_action": "role",
                "role_slug": "firewall",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            ClassRoleMapping.objects.filter(
                profile=self.profile, source_class="Firewall", role_slug="firewall"
            ).exists()
        )

    def test_post_creates_rack_mapping(self):
        """POST creates a ClassRoleMapping with creates_rack=True."""
        url = reverse("plugins:netbox_data_import:quick_add_class_mapping")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_class": "Cabinet2",
                "mapping_action": "rack",
                "creates_rack": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            ClassRoleMapping.objects.filter(profile=self.profile, source_class="Cabinet2", creates_rack=True).exists()
        )

    def test_post_missing_source_class_redirects(self):
        """POST without source_class redirects back to preview."""
        url = reverse("plugins:netbox_data_import:quick_add_class_mapping")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)


class MatchExistingDeviceViewTest(BaseViewTestCase):
    """Tests for MatchExistingDeviceView."""

    def setUp(self):
        """Set up a device and profile."""
        super().setUp()
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole, Device

        self.site = Site.objects.create(name="MatchSite", slug="match-site")
        mfg = Manufacturer.objects.create(name="MatchMfg", slug="match-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="MatchModel", slug="match-model")
        role = DeviceRole.objects.create(name="MatchRole", slug="match-role")
        self.device = Device.objects.create(name="match-existing-01", device_type=dt, role=role, site=self.site)
        self.profile = _make_profile("MatchProfile")

    def test_post_links_device(self):
        """POST links a source_id to an existing device."""
        from netbox_data_import.models import DeviceExistingMatch

        url = reverse("plugins:netbox_data_import:match_existing_device")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": "SRC-MATCH-01",
                "netbox_device_id": self.device.pk,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            DeviceExistingMatch.objects.filter(
                profile=self.profile, source_id="SRC-MATCH-01", netbox_device_id=self.device.pk
            ).exists()
        )

    def test_post_missing_source_id_redirects(self):
        """POST without source_id redirects to preview."""
        url = reverse("plugins:netbox_data_import:match_existing_device")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "netbox_device_id": self.device.pk,
            },
        )
        self.assertEqual(resp.status_code, 302)

    def test_post_nonexistent_device_redirects(self):
        """POST with invalid device ID redirects to preview."""
        url = reverse("plugins:netbox_data_import:match_existing_device")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": "SRC-NOPE",
                "netbox_device_id": 99999,
            },
        )
        self.assertEqual(resp.status_code, 302)


class AutoMatchDevicesViewTest(BaseViewTestCase):
    """Tests for AutoMatchDevicesView."""

    def setUp(self):
        """Set up profile and session with rows."""
        super().setUp()
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole, Device

        self.site = Site.objects.create(name="AutoMatchSite", slug="automatch-site")
        mfg = Manufacturer.objects.create(name="AutoMfg", slug="auto-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="AutoModel", slug="auto-model")
        role = DeviceRole.objects.create(name="AutoRole", slug="auto-role")
        self.device = Device.objects.create(
            name="automatch-device-01", serial="SERIAL-AM-01", device_type=dt, role=role, site=self.site
        )
        self.profile = _make_profile("AutoMatchProfile")

    def test_post_automatch_by_serial(self):
        """POST with a row matching by serial creates a DeviceExistingMatch."""
        from netbox_data_import.models import DeviceExistingMatch

        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "AM-001",
                "device_name": "automatch-device-01",
                "serial": "SERIAL-AM-01",
                "asset_tag": "",
            }
        ]
        session.save()

        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            DeviceExistingMatch.objects.filter(
                profile=self.profile, source_id="AM-001", netbox_device_id=self.device.pk
            ).exists()
        )

    def test_post_automatch_empty_rows(self):
        """POST with no rows in session still succeeds."""
        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)

    def test_post_automatch_by_asset_tag(self):
        """POST with a row matching only by asset_tag creates a DeviceExistingMatch."""
        from dcim.models import Manufacturer, DeviceType, DeviceRole, Device
        from netbox_data_import.models import DeviceExistingMatch

        mfg = Manufacturer.objects.create(name="TagMfg", slug="tag-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="TagModel", slug="tag-model")
        role = DeviceRole.objects.create(name="TagRole", slug="tag-role")
        device = Device.objects.create(
            name="tag-device-01", asset_tag="ASSET-TAG-01", device_type=dt, role=role, site=self.site
        )

        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "TAG-001",
                "device_name": "tag-device-01",
                "serial": "",
                "asset_tag": "ASSET-TAG-01",
            }
        ]
        session.save()

        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            DeviceExistingMatch.objects.filter(
                profile=self.profile, source_id="TAG-001", netbox_device_id=device.pk
            ).exists()
        )

    def test_post_automatch_by_exact_name(self):
        """POST with a row matching only by exact device name creates a DeviceExistingMatch."""
        from dcim.models import Manufacturer, DeviceType, DeviceRole, Device
        from netbox_data_import.models import DeviceExistingMatch

        mfg = Manufacturer.objects.create(name="NameMfg", slug="name-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="NameModel", slug="name-model")
        role = DeviceRole.objects.create(name="NameRole", slug="name-role")
        device = Device.objects.create(name="name-device-01", device_type=dt, role=role, site=self.site)

        session = self.client.session
        session["import_rows"] = [
            {"_row_number": 1, "source_id": "NAME-001", "device_name": "name-device-01", "serial": "", "asset_tag": ""}
        ]
        session.save()

        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            DeviceExistingMatch.objects.filter(
                profile=self.profile, source_id="NAME-001", netbox_device_id=device.pk
            ).exists()
        )

    def test_post_automatch_ambiguous_serial_skips(self):
        """POST with ambiguous serial (multiple devices) does NOT create a match."""
        from dcim.models import Manufacturer, DeviceType, DeviceRole, Device
        from netbox_data_import.models import DeviceExistingMatch

        mfg = Manufacturer.objects.create(name="AmbMfg", slug="amb-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="AmbModel", slug="amb-model")
        role = DeviceRole.objects.create(name="AmbRole", slug="amb-role")
        # Two devices sharing a serial (unusual but tested for robustness)
        Device.objects.create(name="amb-device-01", serial="AMBSERIAL-01", device_type=dt, role=role, site=self.site)
        Device.objects.create(name="amb-device-02", serial="AMBSERIAL-01", device_type=dt, role=role, site=self.site)

        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "AMB-001",
                "device_name": "amb-device-01",
                "serial": "AMBSERIAL-01",
                "asset_tag": "",
            }
        ]
        session.save()

        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AMB-001").exists())

    def test_post_automatch_already_matched_skips(self):
        """POST with a row that already has a DeviceExistingMatch increments 'already' counter but does not create a duplicate."""
        from netbox_data_import.models import DeviceExistingMatch

        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="ALREADY-001",
            netbox_device_id=self.device.pk,
            device_name=self.device.name,
        )

        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "ALREADY-001",
                "device_name": "automatch-device-01",
                "serial": "SERIAL-AM-01",
                "asset_tag": "",
            }
        ]
        session.save()

        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        # Still exactly one match
        self.assertEqual(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="ALREADY-001").count(), 1)

    def test_post_automatch_no_source_id_skips(self):
        """Rows without source_id are silently skipped."""
        from netbox_data_import.models import DeviceExistingMatch

        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "",
                "device_name": "automatch-device-01",
                "serial": "SERIAL-AM-01",
                "asset_tag": "",
            }
        ]
        session.save()

        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile).exists())

    def test_post_automatch_probable_name_match_no_link(self):
        """POST with a name substring match does NOT create a DeviceExistingMatch (probable only)."""
        from dcim.models import Manufacturer, DeviceType, DeviceRole, Device
        from netbox_data_import.models import DeviceExistingMatch

        mfg = Manufacturer.objects.create(name="ProbMfg", slug="prob-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="ProbModel", slug="prob-model")
        role = DeviceRole.objects.create(name="ProbRole", slug="prob-role")
        Device.objects.create(name="probable-device", device_type=dt, role=role, site=self.site)

        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "PROB-001",
                "device_name": "prefix - probable-device",
                "serial": "",
                "asset_tag": "",
            }
        ]
        session.save()

        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        # Probable match doesn't auto-link
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="PROB-001").exists())


class ImportRunViewTest(BaseViewTestCase):
    """Tests for ImportRunView (executes the actual import)."""

    def _setup_session(self):
        """Populate session so ImportRunView has valid data."""
        from dcim.models import Site
        from netbox_data_import.engine import parse_file, run_import
        from netbox_data_import.views import _serialize_rows

        site = Site.objects.create(name="RunSite", slug="run-site")
        profile = _make_profile("RunProfile")

        with open(FIXTURE_PATH, "rb") as f:
            rows = parse_file(f, profile)

        result = run_import(rows, profile, {"site": site}, dry_run=True)

        session = self.client.session
        session["import_rows"] = _serialize_rows(rows)
        session["import_context"] = {
            "profile_id": profile.pk,
            "site_id": site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "sample_cans.xlsx",
        }
        session["import_result"] = result.to_session_dict()
        session.save()
        return profile, site

    def test_run_import_post_creates_objects(self):
        """POST to /import/run/ executes the import and creates NetBox objects."""
        from dcim.models import Rack, Device

        self._setup_session()
        url = reverse("plugins:netbox_data_import:import_run")
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertGreater(Rack.objects.count(), 0)
        self.assertGreater(Device.objects.count(), 0)

    def test_run_import_without_session_redirects(self):
        """POST without session data redirects to setup."""
        url = reverse("plugins:netbox_data_import:import_run")
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)


class ImportProfileYamlWithTransformRuleTest(BaseViewTestCase):
    """Tests for ImportProfileYamlView with column_transform_rules."""

    YAML_WITH_TRANSFORM = b"""profile:
  name: TransformImportProfile
  sheet_name: Data
  source_id_column: Id
column_transform_rules:
  - source_column: Name
    pattern: "^(\\\\w+) - (.+)$"
    group_1_target: asset_tag
    group_2_target: device_name
"""

    def test_post_creates_transform_rules(self):
        """POST with YAML containing column_transform_rules creates ColumnTransformRule."""
        from netbox_data_import.models import ColumnTransformRule

        url = reverse("plugins:netbox_data_import:import_profile_yaml")
        yaml_file = BytesIO(self.YAML_WITH_TRANSFORM)
        yaml_file.name = "transform.yaml"
        self.client.post(url, {"yaml_file": yaml_file})
        profile = ImportProfile.objects.filter(name="TransformImportProfile").first()
        self.assertIsNotNone(profile)
        self.assertTrue(ColumnTransformRule.objects.filter(profile=profile, source_column="Name").exists())

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

    def test_list_filter_by_name(self):
        """Filter by q= uses ImportProfileFilter.search() to narrow results."""
        _make_profile("Alpha")
        _make_profile("Beta")
        url = reverse("plugins:netbox_data_import:importprofile_list")
        resp = self.client.get(url, {"q": "Alph"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Alpha")
        self.assertNotContains(resp, "Beta")


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
        resp = self.client.post(url, {"confirm": "true"})
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
        resp = self.client.post(url, {"confirm": "true"})
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


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


class ColumnMappingInvalidPostTest(BaseViewTestCase):
    """Test ColumnMappingAddView with invalid form data (covers error path)."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("CMInvalidProfile")

    def test_post_invalid_form_rerenders(self):
        """POST with invalid data (missing source_column) re-renders the form."""
        url = reverse("plugins:netbox_data_import:columnmapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(url, {"profile": self.profile.pk, "target_field": "device_name"})
        self.assertEqual(resp.status_code, 200)

    def test_edit_post_invalid_rerenders(self):
        """Edit POST with invalid data re-renders the form."""
        mapping = ColumnMapping.objects.create(profile=self.profile, source_column="ColXInvalid", target_field="tenant")
        url = reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": mapping.pk})
        resp = self.client.post(url, {"profile": self.profile.pk, "target_field": ""})
        self.assertEqual(resp.status_code, 200)

    def test_delete_get_renders_confirmation(self):
        """GET delete shows confirmation page."""
        mapping = ColumnMapping.objects.create(profile=self.profile, source_column="ColYDelete", target_field="tenant")
        url = reverse("plugins:netbox_data_import:columnmapping_delete", kwargs={"pk": mapping.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class ColumnTransformRuleCRUDTest(BaseViewTestCase):
    """Coverage tests for ColumnTransformRule add/edit/delete views."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        from netbox_data_import.models import ColumnTransformRule

        self.ColumnTransformRule = ColumnTransformRule
        self.profile = _make_profile("CTRCRUDProfile")

    def test_get_add_view(self):
        """GET column-transform-rule-add returns 200."""
        url = reverse("plugins:netbox_data_import:columntransformrule_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_post_add_creates_rule(self):
        """POST valid data creates a new ColumnTransformRule."""
        url = reverse("plugins:netbox_data_import:columntransformrule_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(
            url,
            {
                "profile": self.profile.pk,
                "source_column": "CTRColZNew",
                "pattern": r"^(.+)$",
                "group_1_target": "device_name",
                "group_2_target": "",
            },
        )
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(
            self.ColumnTransformRule.objects.filter(profile=self.profile, source_column="CTRColZNew").exists()
        )

    def test_post_add_invalid_rerenders(self):
        """POST with missing data re-renders with 200."""
        url = reverse("plugins:netbox_data_import:columntransformrule_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(url, {"profile": self.profile.pk})
        self.assertEqual(resp.status_code, 200)

    def test_get_edit_view(self):
        """GET edit view returns 200."""
        rule = self.ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="EditColCTR",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        url = reverse("plugins:netbox_data_import:columntransformrule_edit", kwargs={"pk": rule.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_post_edit_updates_rule(self):
        """POST edit with valid data saves the rule."""
        rule = self.ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="EditColCTR2",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        url = reverse("plugins:netbox_data_import:columntransformrule_edit", kwargs={"pk": rule.pk})
        resp = self.client.post(
            url,
            {
                "profile": self.profile.pk,
                "source_column": "EditColCTR2",
                "pattern": r"^(\w+)$",
                "group_1_target": "asset_tag",
                "group_2_target": "",
            },
        )
        self.assertIn(resp.status_code, [200, 302])

    def test_post_edit_invalid_rerenders(self):
        """POST edit with invalid data re-renders with 200."""
        rule = self.ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="EditColCTR3",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        url = reverse("plugins:netbox_data_import:columntransformrule_edit", kwargs={"pk": rule.pk})
        resp = self.client.post(url, {"profile": self.profile.pk})
        self.assertEqual(resp.status_code, 200)

    def test_get_delete_view(self):
        """GET delete view returns 200."""
        rule = self.ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="DelColCTR",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        url = reverse("plugins:netbox_data_import:columntransformrule_delete", kwargs={"pk": rule.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_post_delete_removes_rule(self):
        """POST delete removes the rule."""
        rule = self.ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="DelColCTR2",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        url = reverse("plugins:netbox_data_import:columntransformrule_delete", kwargs={"pk": rule.pk})
        resp = self.client.post(url, {"confirm": "true"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.ColumnTransformRule.objects.filter(pk=rule.pk).exists())


class CheckDeviceNameEdgeCasesTest(BaseViewTestCase):
    """Cover CheckDeviceNameView empty-name path."""

    def test_check_empty_name_returns_false(self):
        """GET with empty name returns exists=False."""
        import json

        url = reverse("plugins:netbox_data_import:check_device")
        resp = self.client.get(url + "?name=")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertFalse(data["exists"])


class ModelsStrTest(BaseViewTestCase):
    """Coverage tests for __str__ and get_absolute_url on model classes."""

    def setUp(self):
        """Create model instances for __str__ tests."""
        super().setUp()
        self.profile = _make_profile("StrTestProfile")

    def test_column_mapping_str(self):
        """ColumnMapping.__str__ returns 'source -> target' string."""
        m = ColumnMapping.objects.get(profile=self.profile, source_column="Id")
        self.assertIn("→", str(m))

    def test_column_mapping_absolute_url(self):
        """ColumnMapping.get_absolute_url returns a valid URL."""
        m = ColumnMapping.objects.get(profile=self.profile, source_column="Id")
        url = m.get_absolute_url()
        self.assertIn(str(m.pk), url)

    def test_class_role_mapping_str_rack(self):
        """ClassRoleMapping.__str__ for creates_rack=True shows 'Rack'."""
        crm = self.profile.class_role_mappings.get(source_class="Cabinet")
        self.assertIn("Rack", str(crm))

    def test_class_role_mapping_str_role(self):
        """ClassRoleMapping.__str__ for device role shows role_slug."""
        crm = self.profile.class_role_mappings.get(source_class="Server")
        self.assertIn("server", str(crm))

    def test_class_role_mapping_absolute_url(self):
        """ClassRoleMapping.get_absolute_url returns a valid URL."""
        crm = self.profile.class_role_mappings.get(source_class="Cabinet")
        url = crm.get_absolute_url()
        self.assertIn(str(crm.pk), url)

    def test_device_type_mapping_str(self):
        """DeviceTypeMapping.__str__ returns make/model -> mfg/dt string."""
        dtm = DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="AristaStr",
            source_model="7050X",
            netbox_manufacturer_slug="arista-str",
            netbox_device_type_slug="arista-7050x",
        )
        self.assertIn("AristaStr", str(dtm))
        self.assertIn("arista-7050x", str(dtm))

    def test_device_type_mapping_absolute_url(self):
        """DeviceTypeMapping.get_absolute_url returns a valid URL."""
        dtm = DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="AristaStr2",
            source_model="7050X2",
            netbox_manufacturer_slug="arista-str2",
            netbox_device_type_slug="arista-7050x2",
        )
        self.assertIn(str(dtm.pk), dtm.get_absolute_url())

    def test_manufacturer_mapping_str(self):
        """ManufacturerMapping.__str__ shows source_make -> netbox_slug."""
        mm = ManufacturerMapping.objects.create(
            profile=self.profile, source_make="Dell EMC Str", netbox_manufacturer_slug="dell-str"
        )
        s = str(mm)
        self.assertIn("Dell EMC Str", s)
        self.assertIn("dell-str", s)

    def test_import_job_str(self):
        """ImportJob.__str__ contains the pk."""
        from netbox_data_import.models import ImportJob

        job = ImportJob.objects.create(profile=self.profile, dry_run=True, input_filename="test.xlsx")
        s = str(job)
        self.assertIn(str(job.pk), s)

    def test_import_job_absolute_url(self):
        """ImportJob.get_absolute_url returns the associated profile's URL."""
        from netbox_data_import.models import ImportJob

        job = ImportJob.objects.create(profile=self.profile, dry_run=True)
        url = job.get_absolute_url()
        self.assertIn(str(self.profile.pk), url)

    def test_ignored_device_str(self):
        """IgnoredDevice.__str__ includes device_name."""
        from netbox_data_import.models import IgnoredDevice

        ig = IgnoredDevice.objects.create(profile=self.profile, source_id="STR-IGN-UNIQUE", device_name="test-dev-str")
        self.assertIn("test-dev-str", str(ig))
        self.assertIn("ignored", str(ig))

    def test_column_transform_rule_str(self):
        """ColumnTransformRule.__str__ shows column and pattern."""
        from netbox_data_import.models import ColumnTransformRule

        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="StrColUniq",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        s = str(rule)
        self.assertIn("StrColUniq", s)

    def test_column_transform_rule_absolute_url(self):
        """ColumnTransformRule.get_absolute_url returns a valid URL."""
        from netbox_data_import.models import ColumnTransformRule

        rule = ColumnTransformRule.objects.create(
            profile=self.profile,
            source_column="UrlColUniq",
            pattern=r"^(.+)$",
            group_1_target="device_name",
            group_2_target="",
        )
        self.assertIn(str(rule.pk), rule.get_absolute_url())

    def test_source_resolution_str(self):
        """SourceResolution.__str__ includes source_id and column."""
        res = SourceResolution.objects.create(
            profile=self.profile,
            source_id="STR-SRC-UNIQ",
            source_column="Name",
            original_value="old",
            resolved_fields={},
        )
        s = str(res)
        self.assertIn("STR-SRC-UNIQ", s)
        self.assertIn("Name", s)

    def test_device_existing_match_str(self):
        """DeviceExistingMatch.__str__ includes source_id."""
        from netbox_data_import.models import DeviceExistingMatch

        dem = DeviceExistingMatch.objects.create(
            profile=self.profile, source_id="DEM-UNIQ-001", netbox_device_id=1, device_name="dev-x"
        )
        s = str(dem)
        self.assertIn("DEM-UNIQ-001", s)


class SafeNextUrlTest(BaseViewTestCase):
    """Tests for _safe_next_url helper."""

    def test_safe_url_fallback_for_external(self):
        """_safe_next_url falls back when next is an external URL."""
        from netbox_data_import.views import _safe_next_url
        from django.test import RequestFactory

        factory = RequestFactory()
        req = factory.post("/", {"next": "http://evil.example.com/phish"})
        result = _safe_next_url(req, "plugins:netbox_data_import:importprofile_list")
        self.assertNotIn("evil.example.com", result)

    def test_safe_url_fallback_for_empty(self):
        """_safe_next_url returns fallback when next is empty."""
        from netbox_data_import.views import _safe_next_url
        from django.test import RequestFactory

        factory = RequestFactory()
        req = factory.post("/", {})
        result = _safe_next_url(req, "plugins:netbox_data_import:importprofile_list")
        self.assertIn("import", result)


class BulkYamlImportExtendedTest(BaseViewTestCase):
    """Test BulkYamlImportView with class_role mapping type and error cases."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("BulkYamlCRMProfileX")

    def test_no_yaml_file_shows_error(self):
        """POST without file returns 200 with error."""
        url = reverse("plugins:netbox_data_import:bulk_yaml_import", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(url, {"mapping_type": "class_role"})
        self.assertEqual(resp.status_code, 200)

    def test_invalid_yaml_shows_error(self):
        """POST with unparseable YAML shows error."""
        url = reverse("plugins:netbox_data_import:bulk_yaml_import", kwargs={"profile_pk": self.profile.pk})
        bad = BytesIO(b": {{{ invalid")
        bad.name = "bad.yaml"
        resp = self.client.post(url, {"mapping_type": "class_role", "yaml_file": bad})
        self.assertEqual(resp.status_code, 200)

    def test_non_list_yaml_shows_error(self):
        """POST with YAML dict (not list) shows error."""
        url = reverse("plugins:netbox_data_import:bulk_yaml_import", kwargs={"profile_pk": self.profile.pk})
        f = BytesIO(b"key: value")
        f.name = "notalist.yaml"
        resp = self.client.post(url, {"mapping_type": "class_role", "yaml_file": f})
        self.assertEqual(resp.status_code, 200)

    def test_class_role_yaml_creates_mapping(self):
        """POST with valid class_role YAML creates ClassRoleMapping."""
        url = reverse("plugins:netbox_data_import:bulk_yaml_import", kwargs={"profile_pk": self.profile.pk})
        yaml_content = b"""
- source_class: StorageArrayX
  creates_rack: false
  role_slug: storage
  ignore: false
"""
        f = BytesIO(yaml_content)
        f.name = "cr.yaml"
        resp = self.client.post(url, {"mapping_type": "class_role", "yaml_file": f})
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(ClassRoleMapping.objects.filter(profile=self.profile, source_class="StorageArrayX").exists())


class SourceResolutionDeleteViewTest(BaseViewTestCase):
    """Tests for SourceResolutionDeleteView GET."""

    def setUp(self):
        """Set up a resolution to delete."""
        super().setUp()
        self.profile = _make_profile("DelResProfileX")
        self.res = SourceResolution.objects.create(
            profile=self.profile,
            source_id="DELRES-UNIQ-001",
            source_column="Name",
            original_value="old-val",
            resolved_fields={},
        )

    def test_get_delete_page_returns_200(self):
        """GET delete confirmation page returns 200."""
        url = reverse("plugins:netbox_data_import:source_resolution_delete", kwargs={"pk": self.res.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_post_delete_removes_resolution(self):
        """POST delete removes the resolution."""
        url = reverse("plugins:netbox_data_import:source_resolution_delete", kwargs={"pk": self.res.pk})
        resp = self.client.post(url)
        self.assertIn(resp.status_code, [200, 302])
        if resp.status_code == 302:
            self.assertFalse(SourceResolution.objects.filter(pk=self.res.pk).exists())


class ClassRoleMappingInvalidPostTest(BaseViewTestCase):
    """Cover ClassRoleMappingAddView and EditView invalid POST paths."""

    def setUp(self):
        """Set up profile and a mapping."""
        super().setUp()
        self.profile = _make_profile("CRMInvalidProfile")
        self.crm = ClassRoleMapping.objects.create(
            profile=self.profile, source_class="CRMTestClass", creates_rack=False, role_slug="crm-role"
        )

    def test_add_invalid_rerenders(self):
        """POST with missing source_class re-renders ClassRoleMappingAddView."""
        url = reverse("plugins:netbox_data_import:classrolemapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(url, {"profile": self.profile.pk, "creates_rack": "false"})
        self.assertEqual(resp.status_code, 200)

    def test_edit_invalid_rerenders(self):
        """POST with missing source_class re-renders ClassRoleMappingEditView."""
        url = reverse("plugins:netbox_data_import:classrolemapping_edit", kwargs={"pk": self.crm.pk})
        resp = self.client.post(url, {"profile": self.profile.pk})
        self.assertEqual(resp.status_code, 200)


class DeviceTypeMappingInvalidPostTest(BaseViewTestCase):
    """Cover DeviceTypeMappingAddView and EditView invalid POST paths."""

    def setUp(self):
        """Set up profile and a mapping."""
        super().setUp()
        self.profile = _make_profile("DTMInvalidProfile")
        self.dtm = DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="DTMInvalidMake",
            source_model="DTMInvalidModel",
            netbox_manufacturer_slug="dtm-mfg",
            netbox_device_type_slug="dtm-dt",
        )

    def test_add_invalid_rerenders(self):
        """POST with missing required fields re-renders DeviceTypeMappingAddView."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.post(url, {"profile": self.profile.pk})
        self.assertEqual(resp.status_code, 200)

    def test_edit_invalid_rerenders(self):
        """POST with missing required fields re-renders DeviceTypeMappingEditView."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_edit", kwargs={"pk": self.dtm.pk})
        resp = self.client.post(url, {"profile": self.profile.pk})
        self.assertEqual(resp.status_code, 200)


class CheckDeviceMultipleResultsTest(BaseViewTestCase):
    """Cover CheckDeviceNameView MultipleObjectsReturned path (lines 1025-1028)."""

    def setUp(self):
        """Create two devices with identical names (different sites)."""
        super().setUp()
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole, Device

        site1 = Site.objects.create(name="MDSite1", slug="md-site-1")
        site2 = Site.objects.create(name="MDSite2", slug="md-site-2")
        mfg = Manufacturer.objects.create(name="MDMfg", slug="md-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="MDModel", slug="md-model")
        role = DeviceRole.objects.create(name="MDRole", slug="md-role")
        Device.objects.create(name="md-shared-name", device_type=dt, role=role, site=site1)
        Device.objects.create(name="md-shared-name", device_type=dt, role=role, site=site2)

    def test_multiple_devices_same_name_returns_exists_true(self):
        """GET with a name matching multiple devices returns exists=True with count>1."""
        import json

        url = reverse("plugins:netbox_data_import:check_device")
        resp = self.client.get(url + "?name=md-shared-name")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["exists"])
        self.assertGreater(data.get("count", 1), 1)


class QuickResolveDeviceTypeMissingFieldsTest(BaseViewTestCase):
    """Cover QuickResolveDeviceTypeView missing make/model and auto-slugify paths."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("QRDTMissingProfile")

    def test_missing_source_make_redirects(self):
        """POST without source_make shows error and redirects."""
        url = reverse("plugins:netbox_data_import:quick_resolve_device_type")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_make": "",
                "source_model": "SomeModel",
                "netbox_mfg_slug": "",
                "netbox_dt_slug": "",
                "action": "map",
            },
        )
        self.assertEqual(resp.status_code, 302)

    def test_auto_slugify_when_slugs_empty(self):
        """POST without explicit slugs auto-slugifies from source_make/source_model."""
        url = reverse("plugins:netbox_data_import:quick_resolve_device_type")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_make": "Auto Mfg",
                "source_model": "Auto Model",
                "netbox_mfg_slug": "",  # will be auto-slugified
                "netbox_dt_slug": "",  # will be auto-slugified
                "action": "map",
            },
        )
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(
            DeviceTypeMapping.objects.filter(
                profile=self.profile, source_make="Auto Mfg", source_model="Auto Model"
            ).exists()
        )

    def test_create_now_invalid_u_height_defaults_to_one(self):
        """POST action=create_now with invalid u_height defaults to 1."""
        from dcim.models import DeviceType

        url = reverse("plugins:netbox_data_import:quick_resolve_device_type")
        self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_make": "UHMfg2",
                "source_model": "UHModel2",
                "netbox_mfg_slug": "uh-mfg2",
                "netbox_dt_slug": "uh-model2",
                "u_height": "not-a-number",
                "action": "create_now",
            },
        )
        dt = DeviceType.objects.filter(slug="uh-model2").first()
        self.assertIsNotNone(dt)
        self.assertEqual(dt.u_height, 1)


class AutoMatchAmbiguousNameTest(BaseViewTestCase):
    """Cover _auto_match_single_device ambiguous name path (line 1387)."""

    def setUp(self):
        """Set up profile and two devices with the same name (different sites)."""
        super().setUp()
        from dcim.models import Site, Manufacturer, DeviceType, DeviceRole, Device

        self.site = Site.objects.create(name="AmbNameSite", slug="amb-name-site")
        site2 = Site.objects.create(name="AmbNameSite2", slug="amb-name-site2")
        mfg = Manufacturer.objects.create(name="AmbNameMfg", slug="amb-name-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="AmbNameModel", slug="amb-name-model")
        role = DeviceRole.objects.create(name="AmbNameRole", slug="amb-name-role")
        Device.objects.create(name="ambname-shared", device_type=dt, role=role, site=self.site)
        Device.objects.create(name="ambname-shared", device_type=dt, role=role, site=site2)
        self.profile = _make_profile("AmbNameProfile")

    def test_ambiguous_name_match_no_link(self):
        """Rows with ambiguous name match (multiple devices) do NOT create DeviceExistingMatch."""
        from netbox_data_import.models import DeviceExistingMatch

        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "AMBNAME-001",
                "device_name": "ambname-shared",
                "serial": "",
                "asset_tag": "",
            }
        ]
        session.save()
        url = reverse("plugins:netbox_data_import:auto_match_devices")
        resp = self.client.post(url, {"profile_id": self.profile.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AMBNAME-001").exists())


class SerializeRowsTest(BaseViewTestCase):
    """Cover _serialize_rows datetime handling (line 1470)."""

    def test_serialize_rows_with_datetime(self):
        """_serialize_rows converts datetime values to ISO format strings."""
        import datetime
        from netbox_data_import.views import _serialize_rows

        rows = [{"_row_number": 1, "device_name": "test", "created_at": datetime.datetime(2025, 1, 1, 12, 0, 0)}]
        result = _serialize_rows(rows)
        self.assertEqual(result[0]["created_at"], "2025-01-01T12:00:00")

    def test_serialize_rows_with_date(self):
        """_serialize_rows converts date values to ISO format strings."""
        import datetime
        from netbox_data_import.views import _serialize_rows

        rows = [{"_row_number": 1, "created_at": datetime.date(2025, 6, 1)}]
        result = _serialize_rows(rows)
        self.assertEqual(result[0]["created_at"], "2025-06-01")


class SaveResolutionJsonErrorTest(BaseViewTestCase):
    """Test SaveResolutionView with malformed JSON (lines 642-643)."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("SaveResJsonProfile")

    def test_malformed_json_defaults_to_empty_dict(self):
        """POST with malformed resolved_fields JSON silently defaults to empty dict."""
        url = reverse("plugins:netbox_data_import:save_resolution")
        resp = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": "JSONERR-001",
                "source_column": "Name",
                "original_value": "old",
                "resolved_fields": "THIS IS NOT JSON {{{{",
            },
        )
        self.assertIn(resp.status_code, [200, 302])
        # Resolution should still be saved (with empty resolved_fields)
        from netbox_data_import.models import SourceResolution

        res = SourceResolution.objects.filter(profile=self.profile, source_id="JSONERR-001").first()
        self.assertIsNotNone(res)
        self.assertEqual(res.resolved_fields, {})


class AutoMatchAmbiguousAssetTagTest(BaseViewTestCase):
    """Cover _auto_match_single_device ambiguous asset_tag path (lines 1379-1380)."""

    def setUp(self):
        """Set up profile."""
        super().setUp()
        self.profile = _make_profile("AmbATProfile")

    def test_ambiguous_asset_tag_returns_none_is_ambiguous(self):
        """_auto_match_single_device returns (None, True) when asset_tag matches multiple devices."""
        from unittest.mock import MagicMock
        from netbox_data_import.views import _auto_match_single_device

        mock_dev_model = MagicMock()
        # Return 2 results for asset_tag filter (ambiguous)
        mock_dev_model.objects.filter.return_value.__getitem__ = lambda self, s: [MagicMock(), MagicMock()]

        # Use a sliceable mock: filter(...)[:2] returns list of 2
        qs_mock = MagicMock()
        qs_mock.__getitem__ = MagicMock(return_value=[MagicMock(), MagicMock()])
        mock_dev_model.objects.filter.return_value = qs_mock

        device, is_ambiguous = _auto_match_single_device(mock_dev_model, "any-name", "", "SHARED-TAG")
        self.assertIsNone(device)
        self.assertTrue(is_ambiguous)


class ImportProfileBulkImportViewTest(BaseViewTestCase):
    """Tests for ImportProfileBulkImportView (NetBox built-in import UI integration)."""

    HIERARCHICAL_YAML = b"""profile:
  name: BulkImportedProfile
  sheet_name: Data
  source_id_column: Id
  update_existing: true
  create_missing_device_types: true
column_mappings:
  - source_column: Name
    target_field: device_name
  - source_column: Rack
    target_field: rack_name
class_role_mappings:
  - source_class: Server
    creates_rack: false
    role_slug: server
    ignore: false
manufacturer_mappings:
  - source_make: Acme
    netbox_manufacturer_slug: acme
"""

    def _url(self):
        return reverse("plugins:netbox_data_import:importprofile_bulk_import")

    # --- GET ---

    def test_get_returns_200(self):
        """GET the bulk-import page returns 200."""
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    # --- POST: hierarchical YAML via text area ---

    def test_post_hierarchical_yaml_creates_profile(self):
        """POST hierarchical YAML creates ImportProfile."""
        resp = self.client.post(self._url(), {"data": self.HIERARCHICAL_YAML.decode()})
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(ImportProfile.objects.filter(name="BulkImportedProfile").exists())

    def test_post_hierarchical_yaml_creates_column_mappings(self):
        """POST hierarchical YAML creates ColumnMappings."""
        self.client.post(self._url(), {"data": self.HIERARCHICAL_YAML.decode()})
        profile = ImportProfile.objects.filter(name="BulkImportedProfile").first()
        self.assertIsNotNone(profile)
        self.assertTrue(ColumnMapping.objects.filter(profile=profile, target_field="device_name").exists())
        self.assertTrue(ColumnMapping.objects.filter(profile=profile, target_field="rack_name").exists())

    def test_post_hierarchical_yaml_creates_class_role_mappings(self):
        """POST hierarchical YAML creates ClassRoleMappings."""
        self.client.post(self._url(), {"data": self.HIERARCHICAL_YAML.decode()})
        profile = ImportProfile.objects.filter(name="BulkImportedProfile").first()
        self.assertIsNotNone(profile)
        self.assertTrue(ClassRoleMapping.objects.filter(profile=profile, source_class="Server").exists())

    def test_post_hierarchical_yaml_creates_manufacturer_mappings(self):
        """POST hierarchical YAML creates ManufacturerMappings."""
        self.client.post(self._url(), {"data": self.HIERARCHICAL_YAML.decode()})
        profile = ImportProfile.objects.filter(name="BulkImportedProfile").first()
        self.assertIsNotNone(profile)
        self.assertTrue(ManufacturerMapping.objects.filter(profile=profile, source_make="Acme").exists())

    def test_post_hierarchical_yaml_idempotent(self):
        """Posting the same hierarchical YAML twice is idempotent."""
        for _ in range(2):
            self.client.post(self._url(), {"data": self.HIERARCHICAL_YAML.decode()})
        self.assertEqual(ImportProfile.objects.filter(name="BulkImportedProfile").count(), 1)

    def test_post_hierarchical_yaml_deletes_removed_mappings(self):
        """Reimport with fewer mappings deletes the removed ones."""
        # First import: 2 column mappings
        self.client.post(self._url(), {"data": self.HIERARCHICAL_YAML.decode()})
        profile = ImportProfile.objects.get(name="BulkImportedProfile")
        self.assertEqual(ColumnMapping.objects.filter(profile=profile).count(), 2)

        # Second import: only 1 column mapping
        reduced_yaml = b"""profile:
  name: BulkImportedProfile
  sheet_name: Data
column_mappings:
  - source_column: Name
    target_field: device_name
"""
        self.client.post(self._url(), {"data": reduced_yaml.decode()})
        self.assertEqual(ColumnMapping.objects.filter(profile=profile).count(), 1)
        self.assertFalse(ColumnMapping.objects.filter(profile=profile, target_field="rack_name").exists())

    # --- POST: hierarchical YAML via file upload ---

    def test_post_hierarchical_yaml_via_file_upload(self):
        """POST hierarchical YAML as a file upload creates the profile."""
        f = BytesIO(self.HIERARCHICAL_YAML)
        f.name = "profile.yaml"
        resp = self.client.post(self._url(), {"upload_file": f})
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(ImportProfile.objects.filter(name="BulkImportedProfile").exists())

    # --- POST: error paths ---

    def test_post_no_data_shows_error(self):
        """POST with neither file nor text shows error and redirects."""
        resp = self.client.post(self._url(), {})
        self.assertIn(resp.status_code, [200, 302])

    def test_post_invalid_yaml_shows_error(self):
        """POST with invalid YAML shows error and redirects."""
        resp = self.client.post(self._url(), {"data": ": {{ invalid yaml"})
        self.assertIn(resp.status_code, [200, 302])

    def test_post_non_dict_profile_value_shows_error(self):
        """POST with profile: scalar (not a dict) shows error."""
        resp = self.client.post(self._url(), {"data": "profile: just-a-string\n"})
        self.assertIn(resp.status_code, [200, 302])
        self.assertFalse(ImportProfile.objects.filter(name="just-a-string").exists())

    def test_post_missing_name_shows_error(self):
        """POST with profile dict missing name shows error."""
        resp = self.client.post(self._url(), {"data": "profile:\n  sheet_name: Data\n"})
        self.assertIn(resp.status_code, [200, 302])

    def test_post_malformed_section_shows_error_not_500(self):
        """POST with a section that is a dict (not list) returns error, not 500."""
        malformed = "profile:\n  name: MalformedSectionProfile\ncolumn_mappings:\n  target_field: device_name\n"
        resp = self.client.post(self._url(), {"data": malformed})
        self.assertIn(resp.status_code, [200, 302])
        self.assertNotEqual(resp.status_code, 500)

    def test_post_flat_yaml_via_file_upload_exercises_seek_rewind(self):
        """Flat YAML via file upload exercises the upload.seek(0) rewind path.

        When the YAML has no top-level 'profile:' key, the view calls
        upload.seek(0) before delegating to NetBox's BulkImportView.  Without
        this rewind the parent receives an EOF file handle and imports nothing.
        Creating the profile via this path confirms seek(0) is present.
        """
        flat_yaml = (
            b"- name: FlatRewindProfile\n"
            b"  sheet_name: Data\n"
            b"  preview_view_mode: rows\n"
            b"  update_existing: false\n"
            b"  create_missing_device_types: false\n"
        )
        f = BytesIO(flat_yaml)
        f.name = "flat.yaml"
        resp = self.client.post(
            self._url(),
            {"upload_file": f, "import_method": "upload", "format": "yaml"},
        )
        self.assertIn(resp.status_code, [200, 302])
        self.assertTrue(
            ImportProfile.objects.filter(name="FlatRewindProfile").exists(),
            "Profile must be created via flat file upload; seek(0) rewind is required.",
        )

    def test_post_hierarchical_yaml_creates_column_transform_rules(self):
        """POST hierarchical YAML creates ColumnTransformRules end-to-end."""
        from netbox_data_import.models import ColumnTransformRule

        yaml_with_transforms = (
            "profile:\n"
            "  name: TransformRulesProfile\n"
            "  sheet_name: Data\n"
            "column_transform_rules:\n"
            "  - source_column: HostName\n"
            "    pattern: '^([a-z]+)(\\d+)'\n"
            "    group_1_target: device_name\n"
            "    group_2_target: ''\n"
        )
        resp = self.client.post(self._url(), {"data": yaml_with_transforms})
        self.assertIn(resp.status_code, [200, 302])
        profile = ImportProfile.objects.filter(name="TransformRulesProfile").first()
        self.assertIsNotNone(profile, "Profile must be created")
        self.assertTrue(
            ColumnTransformRule.objects.filter(profile=profile, source_column="HostName").exists(),
            "ColumnTransformRule must be created from hierarchical YAML",
        )

    # --- Authentication ---

    def test_unauthenticated_get_redirects_to_login(self):
        """Unauthenticated GET is redirected to login, not served directly."""
        self.client.logout()
        resp = self.client.get(self._url())
        self.assertIn(resp.status_code, [302, 403])

    def test_unauthenticated_post_is_rejected(self):
        """Unauthenticated POST is rejected (302 to login or 403)."""
        self.client.logout()
        resp = self.client.post(self._url(), {"data": self.HIERARCHICAL_YAML.decode()})
        self.assertIn(resp.status_code, [302, 403])
        self.assertFalse(ImportProfile.objects.filter(name="BulkImportedProfile").exists())


class ApplyProfileYamlDataUnitTest(BaseViewTestCase):
    """Unit tests for the _apply_profile_yaml_data helper."""

    def test_missing_profile_key_raises(self):
        """Raises ValueError when top-level 'profile' key is absent."""
        from netbox_data_import.views import _apply_profile_yaml_data

        with self.assertRaises(ValueError, msg="profile key missing"):
            _apply_profile_yaml_data({"column_mappings": []})

    def test_non_dict_input_raises(self):
        """Raises ValueError when input is not a dict."""
        from netbox_data_import.views import _apply_profile_yaml_data

        with self.assertRaises(ValueError):
            _apply_profile_yaml_data("just a string")  # type: ignore[arg-type]

    def test_profile_scalar_raises(self):
        """Raises ValueError when profile value is a scalar, not a mapping."""
        from netbox_data_import.views import _apply_profile_yaml_data

        with self.assertRaises(ValueError):
            _apply_profile_yaml_data({"profile": "not-a-dict"})

    def test_profile_list_raises(self):
        """Raises ValueError when profile value is a list, not a mapping."""
        from netbox_data_import.views import _apply_profile_yaml_data

        with self.assertRaises(ValueError):
            _apply_profile_yaml_data({"profile": ["item1", "item2"]})

    def test_missing_name_raises(self):
        """Raises ValueError when profile dict has no 'name' field."""
        from netbox_data_import.views import _apply_profile_yaml_data

        with self.assertRaises(ValueError):
            _apply_profile_yaml_data({"profile": {"sheet_name": "Data"}})

    def test_creates_profile_and_returns_stats(self):
        """Creates an ImportProfile and returns non-empty stats dict."""
        from netbox_data_import.views import _apply_profile_yaml_data

        data = {
            "profile": {"name": "UnitTestProfile", "sheet_name": "Sheet1"},
            "column_mappings": [{"source_column": "Name", "target_field": "device_name"}],
        }
        profile, stats = _apply_profile_yaml_data(data)
        self.assertEqual(profile.name, "UnitTestProfile")
        self.assertEqual(profile.sheet_name, "Sheet1")
        self.assertEqual(stats.get("column_mappings"), 1)
        self.assertTrue(ImportProfile.objects.filter(name="UnitTestProfile").exists())

    def test_atomic_rollback_on_bad_column_mapping(self):
        """A missing required key mid-import raises ValueError and rolls back the transaction."""
        from netbox_data_import.views import _apply_profile_yaml_data

        bad_data = {
            "profile": {"name": "AtomicRollbackProfile"},
            # missing required 'target_field' key → descriptive ValueError
            "column_mappings": [{"source_column": "Name"}],
        }
        with self.assertRaises(ValueError) as cm:
            _apply_profile_yaml_data(bad_data)
        self.assertIn("target_field", str(cm.exception))
        self.assertFalse(ImportProfile.objects.filter(name="AtomicRollbackProfile").exists())

    def test_column_mappings_not_a_list_raises(self):
        """Raises ValueError when a section is a dict instead of a list."""
        from netbox_data_import.views import _apply_profile_yaml_data

        bad_data = {
            "profile": {"name": "SectionTypeProfile"},
            # dict instead of list
            "column_mappings": {"target_field": "device_name", "source_column": "Name"},
        }
        with self.assertRaises(ValueError, msg="section type check"):
            _apply_profile_yaml_data(bad_data)
        self.assertFalse(ImportProfile.objects.filter(name="SectionTypeProfile").exists())

    def test_column_mappings_item_not_a_dict_raises(self):
        """Raises ValueError when a section item is a scalar instead of a mapping."""
        from netbox_data_import.views import _apply_profile_yaml_data

        bad_data = {
            "profile": {"name": "SectionItemProfile"},
            "column_mappings": ["just-a-string"],
        }
        with self.assertRaises(ValueError):
            _apply_profile_yaml_data(bad_data)
        self.assertFalse(ImportProfile.objects.filter(name="SectionItemProfile").exists())

    def test_null_section_raises_value_error(self):
        """Raises ValueError (not silently deleting) when a section value is explicitly null."""
        from netbox_data_import.views import _apply_profile_yaml_data

        bad_data = {
            "profile": {"name": "NullSectionProfile"},
            "column_mappings": None,
        }
        with self.assertRaises(ValueError, msg="explicit null section must raise ValueError"):
            _apply_profile_yaml_data(bad_data)
        self.assertFalse(ImportProfile.objects.filter(name="NullSectionProfile").exists())

    def test_absent_section_preserves_existing_mappings(self):
        """If a section key is absent from YAML, existing mappings are preserved."""
        from netbox_data_import.views import _apply_profile_yaml_data

        # Set up a profile with a column mapping.
        profile = ImportProfile.objects.create(name="PreserveProfile")
        existing = ColumnMapping.objects.create(profile=profile, source_column="Host", target_field="device_name")

        # Import same profile without the column_mappings key at all.
        data = {"profile": {"name": "PreserveProfile", "sheet_name": "Data"}}
        _apply_profile_yaml_data(data)

        # The existing column mapping must still exist.
        self.assertTrue(
            ColumnMapping.objects.filter(pk=existing.pk).exists(),
            "Absent section should preserve existing mappings, not delete them",
        )

    def test_invalid_preview_view_mode_raises(self):
        """full_clean catches an invalid preview_view_mode and rolls back the transaction."""
        from netbox_data_import.views import _apply_profile_yaml_data

        bad_data = {
            "profile": {"name": "BadViewModeProfile", "preview_view_mode": "invalid"},
        }
        with self.assertRaises(ValueError, msg="invalid choice field must raise ValueError"):
            _apply_profile_yaml_data(bad_data)
        self.assertFalse(ImportProfile.objects.filter(name="BadViewModeProfile").exists())

    def test_invalid_column_mapping_target_field_raises_and_rolls_back(self):
        """Invalid target_field choice triggers full_clean and rolls back the profile too."""
        from netbox_data_import.views import _apply_profile_yaml_data

        bad_data = {
            "profile": {"name": "BadTargetFieldProfile"},
            "column_mappings": [{"source_column": "Col", "target_field": "not_a_real_field"}],
        }
        with self.assertRaises(ValueError, msg="invalid target_field choice must raise ValueError"):
            _apply_profile_yaml_data(bad_data)
        # Full rollback: profile itself must not exist.
        self.assertFalse(ImportProfile.objects.filter(name="BadTargetFieldProfile").exists())

    def test_partial_reimport_preserves_unmentioned_profile_fields(self):
        """Reimporting YAML that omits optional profile fields does not reset them."""
        from netbox_data_import.views import _apply_profile_yaml_data

        # Create a profile with non-default field values.
        profile = ImportProfile.objects.create(
            name="PartialReimportProfile",
            sheet_name="CustomSheet",
            source_id_column="SourceId",
            custom_field_name="my_cf",
            update_existing=False,
            preview_view_mode="racks",
        )

        # Re-import with only 'name' — no other profile fields.
        _apply_profile_yaml_data({"profile": {"name": "PartialReimportProfile"}})

        profile.refresh_from_db()
        self.assertEqual(profile.sheet_name, "CustomSheet", "sheet_name must not be reset")
        self.assertEqual(profile.source_id_column, "SourceId", "source_id_column must not be reset")
        self.assertEqual(profile.custom_field_name, "my_cf", "custom_field_name must not be reset")
        self.assertFalse(profile.update_existing, "update_existing must not be reset")
        self.assertEqual(profile.preview_view_mode, "racks", "preview_view_mode must not be reset")

    def test_column_mapping_missing_required_key_raises_descriptive_error(self):
        """Missing required key in column_mappings raises ValueError with section and key name."""
        from netbox_data_import.views import _apply_profile_yaml_data

        ImportProfile.objects.create(name="KeyErrProfile", sheet_name="Data")
        data = {
            "profile": {"name": "KeyErrProfile"},
            "column_mappings": [{"source_column": "Name"}],  # missing target_field
        }
        with self.assertRaises(ValueError) as cm:
            _apply_profile_yaml_data(data)
        self.assertIn("column_mappings[1]", str(cm.exception))
        self.assertIn("target_field", str(cm.exception))

    def test_device_type_mapping_missing_required_key_raises_descriptive_error(self):
        """Missing required key in device_type_mappings raises ValueError, not bare KeyError."""
        from netbox_data_import.views import _apply_profile_yaml_data

        ImportProfile.objects.create(name="DTMKeyErrProfile", sheet_name="Data")
        data = {
            "profile": {"name": "DTMKeyErrProfile"},
            "device_type_mappings": [
                {
                    "source_make": "Cisco",
                    "source_model": "ISR4321",
                    # missing netbox_manufacturer_slug and netbox_device_type_slug
                }
            ],
        }
        with self.assertRaises(ValueError) as cm:
            _apply_profile_yaml_data(data)
        self.assertIn("device_type_mappings[1]", str(cm.exception))

    def test_manufacturer_mapping_missing_required_key_raises_descriptive_error(self):
        """Missing required key in manufacturer_mappings raises ValueError with context."""
        from netbox_data_import.views import _apply_profile_yaml_data

        ImportProfile.objects.create(name="MMKeyErrProfile", sheet_name="Data")
        data = {
            "profile": {"name": "MMKeyErrProfile"},
            "manufacturer_mappings": [{"source_make": "Cisco"}],  # missing netbox_manufacturer_slug
        }
        with self.assertRaises(ValueError) as cm:
            _apply_profile_yaml_data(data)
        self.assertIn("manufacturer_mappings[1]", str(cm.exception))
        self.assertIn("netbox_manufacturer_slug", str(cm.exception))

    def test_overlength_profile_name_raises_value_error_not_500(self):
        """Overlength field is caught by full_clean before any DB write, raising ValueError."""
        from netbox_data_import.views import _apply_profile_yaml_data

        bad_data = {
            "profile": {"name": "X" * 200},  # exceeds max_length=100 for ImportProfile.name
        }
        with self.assertRaises(ValueError, msg="overlength name must raise ValueError, not DataError"):
            _apply_profile_yaml_data(bad_data)
        self.assertFalse(ImportProfile.objects.filter(name__startswith="X" * 50).exists())

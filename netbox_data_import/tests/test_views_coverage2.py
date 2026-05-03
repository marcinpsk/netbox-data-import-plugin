# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Additional view coverage tests targeting specific uncovered lines in views.py."""

from io import BytesIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceTypeMapping,
    ImportProfile,
    SourceResolution,
)
from netbox_data_import.views import _save_or_refetch, _validate_model_instance

User = get_user_model()


def _make_profile(name="VCov2Test") -> ImportProfile:
    return ImportProfile.objects.create(
        name=name,
        sheet_name="Data",
        source_id_column="Id",
        update_existing=True,
        create_missing_device_types=True,
    )


def _make_superuser(username):
    return User.objects.create_superuser(username, f"{username}@test.com", "testpass")


class ValidateModelInstanceNonDictTest(TestCase):
    """Tests for _validate_model_instance — line 211: non-dict ValidationError."""

    def test_string_validation_error_joined_via_messages(self):
        """DjangoValidationError raised with a plain string uses exc.messages — line 211."""
        instance = MagicMock()
        instance.full_clean.side_effect = DjangoValidationError("plain error message")
        with self.assertRaises(ValueError) as cm:
            _validate_model_instance(instance, "test_label")
        self.assertIn("plain error message", str(cm.exception))
        self.assertIn("test_label", str(cm.exception))


class SaveOrRefetchIntegrityErrorTest(TestCase):
    """Tests for _save_or_refetch — lines 243-244: IntegrityError causes refetch."""

    def test_integrity_error_returns_pre_existing_instance(self):
        """On IntegrityError, _save_or_refetch returns the pre-existing object — lines 243-244."""
        profile = _make_profile("SORFCov2")
        existing = ClassRoleMapping.objects.create(profile=profile, source_class="IntegErrClass", creates_rack=False)
        duplicate = ClassRoleMapping(profile=profile, source_class="IntegErrClass", creates_rack=False)
        result = _save_or_refetch(duplicate, ClassRoleMapping, profile=profile, source_class="IntegErrClass")
        self.assertEqual(result.pk, existing.pk)


class ApplyProfileYamlRackTypeNullTest(TestCase):
    """Tests for _import_class_role_mappings — line 307: rack_type: null sets instance.rack_type=None."""

    def setUp(self):
        self.user = _make_superuser("vcov2_racknull_user")
        self.client = Client()
        self.client.login(username="vcov2_racknull_user", password="testpass")

    def test_rack_type_null_clears_rack_type_field(self):
        """YAML class_role_mapping with rack_type: null sets rack_type=None — line 307."""
        yaml_data = (
            "profile:\n"
            "  name: RackTypeNullProfile2\n"
            "  sheet_name: Data\n"
            "class_role_mappings:\n"
            "  - source_class: Cabinet\n"
            "    creates_rack: true\n"
            "    rack_type: null\n"
        )
        url = reverse("plugins:netbox_data_import:importprofile_bulk_import")
        resp = self.client.post(url, {"data": yaml_data})
        self.assertEqual(resp.status_code, 302)
        crm = ClassRoleMapping.objects.filter(source_class="Cabinet").first()
        self.assertIsNotNone(crm, "ClassRoleMapping for 'Cabinet' was not created")
        self.assertIsNone(crm.rack_type)


class BulkImportSeekOnYamlErrorTest(TestCase):
    """Tests for ImportProfileBulkImportView.post — line 456: seek(0) on YAML error."""

    def setUp(self):
        self.user = _make_superuser("vcov2_bulkseek_user")
        self.client = Client()
        self.client.login(username="vcov2_bulkseek_user", password="testpass")

    def test_invalid_yaml_file_triggers_seek_and_fallback(self):
        """POST with an invalid YAML file triggers seek(0) + super().post() — line 456."""
        invalid_yaml = BytesIO(b"key: [unclosed bracket: {bad yaml")
        invalid_yaml.name = "bad.yaml"
        url = reverse("plugins:netbox_data_import:importprofile_bulk_import")
        # NetBox's BulkImportView form requires a 'format' field; include it so
        # the form validation doesn't raise KeyError before we can test the seek path.
        resp = self.client.post(url, {"upload_file": invalid_yaml, "format": "yaml"}, follow=True)
        # After following any redirects, the final response should be 200
        self.assertEqual(resp.status_code, 200)
        # Verify the seek(0)+super().post() fallback actually ran:
        # super().post() redirects after processing (or renders form errors).
        # With follow=True, a non-empty redirect_chain proves the fallback path
        # executed — it didn't crash and did delegate to the parent view.
        self.assertTrue(
            resp.redirect_chain or (resp.context and resp.context.get("form") is not None),
            "seek(0)+super().post() fallback must produce a valid HTTP response",
        )


class ProfileChildEditViewPermissionTest(TestCase):
    """Tests for _ProfileChildEditView.get_required_permission — lines 495-496."""

    def setUp(self):
        self.user = _make_superuser("vcov2_childperm_user")
        self.client = Client()
        self.client.login(username="vcov2_childperm_user", password="testpass")
        self.profile = _make_profile("ChildPermProfile2")
        ColumnMapping.objects.create(profile=self.profile, source_column="Name", target_field="device_name")
        self.cm = ColumnMapping.objects.get(profile=self.profile, source_column="Name")

    def test_edit_url_with_pk_requires_change_permission(self):
        """GET to edit URL (pk present) exercises get_required_permission returning 'change' — line 495."""
        url = reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": self.cm.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_add_url_with_profile_pk_requires_add_permission(self):
        """GET to add URL (profile_pk present, no pk) exercises get_required_permission returning 'add' — line 496."""
        url = reverse("plugins:netbox_data_import:columnmapping_add", kwargs={"profile_pk": self.profile.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class ImportPreviewViewProfileNotFoundTest(TestCase):
    """Tests for ImportPreviewView.get — lines 713-714: missing profile redirects."""

    def setUp(self):
        self.user = _make_superuser("vcov2_prevnf_user")
        self.client = Client()
        self.client.login(username="vcov2_prevnf_user", password="testpass")
        from dcim.models import Site

        self.site = Site.objects.create(name="PrevNF2-Site", slug="prev-nf2-site")

    def test_session_with_nonexistent_profile_id_redirects(self):
        """profile_id pointing to non-existent profile triggers warning + redirect — lines 713-714."""
        session = self.client.session
        session["import_rows"] = [{"_row_number": 1, "source_id": "X", "device_name": "d1"}]
        session["import_context"] = {
            "profile_id": 999999,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "test.xlsx",
        }
        session.save()
        url = reverse("plugins:netbox_data_import:import_preview")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        expected = reverse("plugins:netbox_data_import:import_setup")
        self.assertEqual(resp["Location"], expected)


class ImportPreviewViewExistingResolutionsTest(TestCase):
    """Tests for ImportPreviewView.get — line 735: SourceResolution entries populate existing_resolutions."""

    def setUp(self):
        self.user = _make_superuser("vcov2_prevres_user")
        self.client = Client()
        self.client.login(username="vcov2_prevres_user", password="testpass")
        from dcim.models import DeviceRole, Site

        self.site = Site.objects.create(name="PrevRes2-Site", slug="prev-res2-site")
        DeviceRole.objects.get_or_create(name="server", slug="server", defaults={"color": "000000"})
        self.profile = _make_profile("PrevResProfile2")
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug="server"
        )

    def test_source_resolutions_appear_in_existing_resolutions(self):
        """SourceResolution rows for profile populate existing_resolutions — line 735."""
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="RES2-001",
            source_column="Name",
            original_value="old-name",
            resolved_fields={"device_name": "new-name"},
        )
        session = self.client.session
        session["import_rows"] = [
            {
                "_row_number": 1,
                "source_id": "RES2-001",
                "device_name": "old-name",
                "device_class": "Server",
                "rack_name": "",
                "make": "TestMake",
                "model": "TestModel",
                "u_height": "1",
                "status": "active",
                "u_position": "",
                "serial": "",
                "asset_tag": "",
            }
        ]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "test.xlsx",
        }
        session.save()
        url = reverse("plugins:netbox_data_import:import_preview")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"RES2-001", resp.content)
        # Assert the view actually passed the resolution into context
        existing = resp.context.get("existing_resolutions", {})
        self.assertIn("RES2-001", existing)
        self.assertEqual(
            existing["RES2-001"]["Name"]["resolved_fields"],
            {"device_name": "new-name"},
        )


class ImportJobListViewPermissionTest(TestCase):
    """Tests for ImportJobListView.get_required_permission — line 861."""

    def setUp(self):
        self.user = _make_superuser("vcov2_joblist_user")
        self.client = Client()
        self.client.login(username="vcov2_joblist_user", password="testpass")

    def test_import_job_list_returns_200(self):
        """GET importjob_list with view_importjob perm returns 200 — line 861."""
        url = reverse("plugins:netbox_data_import:importjob_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class RemoveExtraIpValidNextTest(TestCase):
    """Tests for RemoveExtraIpView._safe_return — line 970: valid next param causes redirect."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        self.user = _make_superuser("vcov2_ipnext_user")
        self.client = Client()
        self.client.login(username="vcov2_ipnext_user", password="testpass")

        site = Site.objects.create(name="IPNext2-Site", slug="ipnext2-site")
        mfg = Manufacturer.objects.create(name="IPNext2Mfg", slug="ipnext2-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IPNext2Model", slug="ipnext2-model", u_height=1)
        role = DeviceRole.objects.create(name="IPNext2Role", slug="ipnext2-role", color="000000")
        device_ct = ContentType.objects.get_for_model(Device)
        cf, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            cf.object_types.set([device_ct])
        self.device = Device.objects.create(name="ipnext2-device", site=site, device_type=dt, role=role)
        self.device.custom_field_data["data_import_source"] = {
            "_ip": {"primary_ip4": "10.2.3.4/32"},
        }
        self.device.save()

    def test_valid_next_param_redirects_to_next(self):
        """Valid same-host next param causes redirect to that URL — line 970."""
        next_url = reverse("plugins:netbox_data_import:importprofile_list")
        url = reverse("plugins:netbox_data_import:remove_extra_ip")
        resp = self.client.post(
            url,
            {"device_id": self.device.pk, "ip_field": "primary_ip4", "next": next_url},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(next_url, resp["Location"])


class RemoveExtraIpFieldNotInDataTest(TestCase):
    """Tests for RemoveExtraIpView.post — line 997: ip_field not in ip_data sends info message."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        self.user = _make_superuser("vcov2_ipnotfound_user")
        self.client = Client()
        self.client.login(username="vcov2_ipnotfound_user", password="testpass")

        site = Site.objects.create(name="IPNotFound2-Site", slug="ipnotfound2-site")
        mfg = Manufacturer.objects.create(name="IPNotFound2Mfg", slug="ipnotfound2-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IPNotFound2Model", slug="ipnotfound2-model", u_height=1)
        role = DeviceRole.objects.create(name="IPNotFound2Role", slug="ipnotfound2-role", color="000000")
        device_ct = ContentType.objects.get_for_model(Device)
        cf, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            cf.object_types.set([device_ct])
        self.device = Device.objects.create(name="ipnotfound2-device", site=site, device_type=dt, role=role)
        self.device.custom_field_data["data_import_source"] = {
            "_ip": {"oob_ip": "10.3.4.5/32"},
        }
        self.device.save()

    def test_ip_field_not_in_data_sends_info_message(self):
        """When ip_field not in _ip dict, messages.info is sent — line 997."""
        url = reverse("plugins:netbox_data_import:remove_extra_ip")
        resp = self.client.post(url, {"device_id": self.device.pk, "ip_field": "primary_ip4"})
        self.assertEqual(resp.status_code, 302)
        messages = list(get_messages(resp.wsgi_request))
        self.assertTrue(any("not in JSON storage" in str(m) for m in messages))


class SyncDeviceFieldBareExceptionTest(TestCase):
    """Tests for SyncDeviceFieldView.post — lines 1039-1045: bare Exception returns 500."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        self.user = _make_superuser("vcov2_syncexc_user")
        self.client = Client()
        self.client.login(username="vcov2_syncexc_user", password="testpass")

        site = Site.objects.create(name="SyncExc2-Site", slug="syncexc2-site")
        mfg = Manufacturer.objects.create(name="SyncExc2Mfg", slug="syncexc2-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SyncExc2Model", slug="syncexc2-model", u_height=1)
        role = DeviceRole.objects.create(name="SyncExc2Role", slug="syncexc2-role", color="000000")
        self.device = Device.objects.create(name="syncexc2-device", site=site, device_type=dt, role=role)
        self.url = reverse("plugins:netbox_data_import:sync_device_field")

    def test_unexpected_exception_returns_500(self):
        """RuntimeError inside _apply_field returns 500 JSON response — lines 1039-1045."""
        from netbox_data_import.views import SyncDeviceFieldView

        with patch.object(SyncDeviceFieldView, "_apply_field", side_effect=RuntimeError("unexpected")):
            resp = self.client.post(self.url, {"device_id": self.device.pk, "field": "serial", "value": "X"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("internal", resp.json()["error"].lower())


class SyncDeviceFieldErrorPathsTest(TestCase):
    """Tests for SyncDeviceFieldView._apply_field error paths — lines 1061-1062, 1074, 1083-1084, 1100-1108."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        self.user = _make_superuser("vcov2_syncerr_user")
        self.client = Client()
        self.client.login(username="vcov2_syncerr_user", password="testpass")

        site = Site.objects.create(name="SyncErr2-Site", slug="syncerr2-site")
        mfg = Manufacturer.objects.create(name="SyncErr2Mfg", slug="syncerr2-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SyncErr2Model", slug="syncerr2-model", u_height=1)
        role = DeviceRole.objects.create(name="SyncErr2Role", slug="syncerr2-role", color="000000")
        self.device = Device.objects.create(name="syncerr2-device", site=site, device_type=dt, role=role)
        self.url = reverse("plugins:netbox_data_import:sync_device_field")

    def test_u_position_non_integer_returns_error(self):
        """u_position='not-a-number' raises ValueError → ok=False — lines 1061-1062."""
        resp = self.client.post(self.url, {"device_id": self.device.pk, "field": "u_position", "value": "not-a-number"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("parse", data["error"].lower())

    def test_status_unknown_value_returns_error(self):
        """Completely unknown status value raises ValueError → ok=False — line 1074."""
        resp = self.client.post(
            self.url, {"device_id": self.device.pk, "field": "status", "value": "completely-unknown-xyz"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("unknown", data["error"].lower())

    def test_u_height_not_syncable_returns_error(self):
        """u_height is not in _ALLOWED_FIELDS → ok=False with explicit error — lines 1025-1026."""
        resp = self.client.post(self.url, {"device_id": self.device.pk, "field": "u_height", "value": "abc"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("not syncable", data["error"].lower())

    def test_face_front_sets_face_and_returns_ok(self):
        """face='front' is valid → ok=True — lines 1100-1108."""
        resp = self.client.post(self.url, {"device_id": self.device.pk, "field": "face", "value": "front"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_face_invalid_value_returns_error(self):
        """face='sideways' raises ValueError → ok=False — lines 1104-1105."""
        resp = self.client.post(self.url, {"device_id": self.device.pk, "field": "face", "value": "sideways"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("face", data["error"].lower())


class DeviceTypeAnalysisExistsInNetboxTest(TestCase):
    """Tests for DeviceTypeAnalysisView.get — lines 1198-1202: exists_in_netbox=True when DT present."""

    def setUp(self):
        self.user = _make_superuser("vcov2_dtanalysis_user")
        self.client = Client()
        self.client.login(username="vcov2_dtanalysis_user", password="testpass")
        self.profile = _make_profile("DTAnalysis2Profile")

    def test_device_type_mapping_with_matching_netbox_dt_shows_exists_true(self):
        """DeviceTypeMapping whose DT exists in NetBox shows exists_in_netbox=True — lines 1198-1202."""
        from dcim.models import DeviceType, Manufacturer

        mfg = Manufacturer.objects.create(name="DTAnalysisMfg2", slug="dtanalysis2-mfg")
        DeviceType.objects.create(manufacturer=mfg, model="DTAnalysisModel2", slug="dtanalysis2-dt", u_height=1)
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="DTAnalysisMfg2",
            source_model="DTAnalysisModel2",
            netbox_manufacturer_slug="dtanalysis2-mfg",
            netbox_device_type_slug="dtanalysis2-dt",
        )
        url = reverse(
            "plugins:netbox_data_import:device_type_analysis_profile",
            kwargs={"profile_pk": self.profile.pk},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"dtanalysis2-dt", resp.content)


class BulkYamlImportKeyErrorTest(TestCase):
    """Tests for BulkYamlImportView — KeyError paths in class_role and device_type rows."""

    def setUp(self):
        self.user = _make_superuser("vcov2_byamlkey_user")
        self.client = Client()
        self.client.login(username="vcov2_byamlkey_user", password="testpass")
        self.profile = _make_profile("BYamlKey2Profile")
        self.url = reverse(
            "plugins:netbox_data_import:bulk_yaml_import",
            kwargs={"profile_pk": self.profile.pk},
        )

    def test_class_role_row_missing_source_class_key_adds_error(self):
        """class_role row with missing source_class key → KeyError appended to errors — lines 1251-1252."""
        yaml_content = b"- creates_rack: false\n  role_slug: server\n"
        yaml_file = BytesIO(yaml_content)
        yaml_file.name = "bad_crm.yaml"
        resp = self.client.post(
            self.url,
            {"yaml_file": yaml_file, "mapping_type": "class_role"},
        )
        self.assertEqual(resp.status_code, 302)
        messages = list(get_messages(resp.wsgi_request))
        self.assertTrue(any("error" in str(m).lower() or "source_class" in str(m) for m in messages))

    def test_device_type_row_missing_source_make_key_adds_error(self):
        """device_type row with missing source_make key → KeyError appended to errors — lines 1276-1277."""
        yaml_content = b"- source_model: MyModel\n  netbox_manufacturer_slug: mfg\n  netbox_device_type_slug: dt\n"
        yaml_file = BytesIO(yaml_content)
        yaml_file.name = "bad_dtm.yaml"
        resp = self.client.post(
            self.url,
            {"yaml_file": yaml_file, "mapping_type": "device_type"},
        )
        self.assertEqual(resp.status_code, 302)
        messages = list(get_messages(resp.wsgi_request))
        self.assertTrue(any("error" in str(m).lower() or "source_make" in str(m) for m in messages))

    def test_class_role_row_bare_exception_adds_error(self):
        """Bare Exception in class_role row processing appended to errors — lines 1253-1255."""
        yaml_content = b"- source_class: BareExcClass\n  creates_rack: false\n"
        yaml_file = BytesIO(yaml_content)
        yaml_file.name = "exc_crm.yaml"
        with patch.object(
            ClassRoleMapping.objects,
            "get_or_create",
            side_effect=Exception("unexpected db error"),
        ):
            resp = self.client.post(
                self.url,
                {"yaml_file": yaml_file, "mapping_type": "class_role"},
            )
        self.assertEqual(resp.status_code, 302)
        messages = list(get_messages(resp.wsgi_request))
        self.assertTrue(any("error" in str(m).lower() for m in messages))


class BulkYamlImportNonListAndOSErrorTest(TestCase):
    """Tests for BulkYamlImportView.post — lines 1300-1307: non-list YAML and OSError paths."""

    def setUp(self):
        self.user = _make_superuser("vcov2_byamlnl_user")
        self.client = Client()
        self.client.login(username="vcov2_byamlnl_user", password="testpass")
        self.profile = _make_profile("BYamlNonList2Profile")
        self.url = reverse(
            "plugins:netbox_data_import:bulk_yaml_import",
            kwargs={"profile_pk": self.profile.pk},
        )

    def test_yaml_dict_instead_of_list_returns_error(self):
        """YAML root object is a dict → 'YAML must be a list' error — lines 1305-1307."""
        yaml_content = b"key: value\nanother: item\n"
        yaml_file = BytesIO(yaml_content)
        yaml_file.name = "dict.yaml"
        resp = self.client.post(self.url, {"yaml_file": yaml_file, "mapping_type": "class_role"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"list", resp.content.lower())

    def test_oserror_reading_file_shows_error_message(self):
        """OSError while reading uploaded file shows error message — lines 1300-1303."""
        import yaml

        yaml_file = BytesIO(b"- source_class: Test\n")
        yaml_file.name = "oserr.yaml"
        with patch.object(yaml, "safe_load", side_effect=OSError("disk error")):
            resp = self.client.post(self.url, {"yaml_file": yaml_file, "mapping_type": "class_role"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"read", resp.content.lower())


class BulkYamlImportUnknownMappingTypeTest(TestCase):
    """Tests for BulkYamlImportView.post — line 1315: unknown mapping_type → created=skipped=0."""

    def setUp(self):
        self.user = _make_superuser("vcov2_byamlunk_user")
        self.client = Client()
        self.client.login(username="vcov2_byamlunk_user", password="testpass")
        self.profile = _make_profile("BYamlUnknown2Profile")
        self.url = reverse(
            "plugins:netbox_data_import:bulk_yaml_import",
            kwargs={"profile_pk": self.profile.pk},
        )

    def test_unknown_mapping_type_shows_error_message(self):
        """Unknown mapping_type takes else branch: returns error message — line 1319."""
        yaml_content = b"- source_class: Anything\n"
        yaml_file = BytesIO(yaml_content)
        yaml_file.name = "unk.yaml"
        resp = self.client.post(self.url, {"yaml_file": yaml_file, "mapping_type": "unknown_type"})
        self.assertEqual(resp.status_code, 302)
        messages = list(get_messages(resp.wsgi_request))
        self.assertTrue(any("unknown mapping type" in str(m).lower() for m in messages))


class BulkYamlImportErrorsPathTest(TestCase):
    """Tests for BulkYamlImportView.post — lines 1317-1319: errors list triggers warning message."""

    def setUp(self):
        self.user = _make_superuser("vcov2_byamlerr_user")
        self.client = Client()
        self.client.login(username="vcov2_byamlerr_user", password="testpass")
        self.profile = _make_profile("BYamlErrors2Profile")
        self.url = reverse(
            "plugins:netbox_data_import:bulk_yaml_import",
            kwargs={"profile_pk": self.profile.pk},
        )

    def test_errors_in_processing_shows_warning_message(self):
        """Non-empty errors list triggers messages.warning — lines 1317-1319."""
        yaml_content = b"- creates_rack: false\n  role_slug: server\n"
        yaml_file = BytesIO(yaml_content)
        yaml_file.name = "err.yaml"
        resp = self.client.post(self.url, {"yaml_file": yaml_file, "mapping_type": "class_role"})
        self.assertEqual(resp.status_code, 302)
        messages = list(get_messages(resp.wsgi_request))
        # The warning message format is "Created X, skipped Y, N errors: ..."
        # Only the warning path (not success path) contains "errors:"
        self.assertTrue(any("errors:" in str(m) for m in messages))

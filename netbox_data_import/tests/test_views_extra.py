# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for unused-columns feature: fuzzy matching helper + QuickAddColumnMappingView."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import ColumnMapping, DeviceTypeMapping, ImportProfile
from netbox_data_import.views import _fuzzy_match_netbox_field

User = get_user_model()


def _make_profile(name="QMapTest") -> ImportProfile:
    return ImportProfile.objects.create(
        name=name,
        adapter_config={
            "sheet_name": "Data",
            "source_id_column": "Id",
            "update_existing": False,
            "create_missing_device_types": False,
        },
    )


class FuzzyMatchNetboxFieldTest(TestCase):
    """_fuzzy_match_netbox_field returns sensible canonical names."""

    def test_exact_alias_match(self):
        self.assertEqual(_fuzzy_match_netbox_field("serial"), "serial")
        self.assertEqual(_fuzzy_match_netbox_field("rack"), "rack_name")
        self.assertEqual(_fuzzy_match_netbox_field("hostname"), "device_name")
        self.assertEqual(_fuzzy_match_netbox_field("vendor"), "make")

    def test_case_insensitive(self):
        self.assertEqual(_fuzzy_match_netbox_field("Rack"), "rack_name")
        self.assertEqual(_fuzzy_match_netbox_field("SERIAL"), "serial")
        self.assertEqual(_fuzzy_match_netbox_field("Hostname"), "device_name")

    def test_whitespace_stripped(self):
        self.assertEqual(_fuzzy_match_netbox_field("  serial  "), "serial")

    def test_no_match_returns_none(self):
        self.assertIsNone(_fuzzy_match_netbox_field("xyzzy_totally_unknown_column_xyz"))

    def test_fuzzy_close_match(self):
        # "serial_num" is close to "serial_number"
        result = _fuzzy_match_netbox_field("serial_num")
        self.assertEqual(result, "serial")


class QuickAddColumnMappingViewTest(TestCase):
    """Tests for QuickAddColumnMappingView."""

    def setUp(self):
        self.user = User.objects.create_superuser("qmapuser", "qmap@example.com", "testpass")
        self.client = Client()
        self.client.login(username="qmapuser", password="testpass")
        self.profile = _make_profile()
        self.url = reverse("plugins:netbox_data_import:quick_add_column_mapping")

    def test_creates_new_column_mapping(self):
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "serial",
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)
        self.assertTrue(
            ColumnMapping.objects.filter(profile=self.profile, source_column="JiraID", target_field="serial").exists()
        )

    def test_an_overlength_target_field_is_refused(self):
        """CATALOG.is_valid accepts any name after a family prefix, but the column is 100 chars."""
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "extra_json:" + ("x" * 200),
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)
        self.assertFalse(ColumnMapping.objects.filter(profile=self.profile, source_column="JiraID").exists())

    def test_an_overlength_source_column_is_refused(self):
        """The source column is read straight from the request and the column is 200 chars."""
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "J" * 300,
                "target_field": "serial",
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)
        self.assertFalse(ColumnMapping.objects.filter(profile=self.profile, target_field="serial").exists())

    def test_a_refused_mapping_does_not_delete_the_displaced_row(self):
        """The delete runs before the create, so an invalid write must not strand the profile."""
        ColumnMapping.objects.create(profile=self.profile, source_column="OldCol", target_field="asset_tag")
        self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "N" * 300,
                "target_field": "asset_tag",
            },
        )
        self.assertTrue(
            ColumnMapping.objects.filter(profile=self.profile, source_column="OldCol").exists(),
            "an invalid replacement must leave the existing mapping in place",
        )

    def test_keeps_existing_direct_mapping_for_another_target(self):
        """One source column can provide more than one direct target."""
        ColumnMapping.objects.create(profile=self.profile, source_column="JiraID", target_field="asset_tag")
        self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "serial",
            },
        )
        self.assertEqual(
            set(
                ColumnMapping.objects.filter(profile=self.profile, source_column="JiraID").values_list(
                    "target_field", flat=True
                )
            ),
            {"asset_tag", "serial"},
        )

    def test_invalid_target_field_rejected(self):
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "not_a_real_field",
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)
        self.assertFalse(ColumnMapping.objects.filter(profile=self.profile, source_column="JiraID").exists())

    def test_empty_source_column_rejected(self):
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "",
                "target_field": "serial",
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "serial",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("import_preview", resp.url)

    def test_valid_extra_json_key_accepted(self):
        """extra_json:<valid_key> is accepted and stored."""
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "extra_json:jira_id",
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)
        self.assertTrue(
            ColumnMapping.objects.filter(
                profile=self.profile, source_column="JiraID", target_field="extra_json:jira_id"
            ).exists()
        )

    def test_invalid_extra_json_key_rejected(self):
        """An extra_json: key with no name after the prefix is rejected by the catalog validator."""
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "extra_json:   ",
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)
        self.assertFalse(ColumnMapping.objects.filter(profile=self.profile, source_column="JiraID").exists())

    def test_displaced_mapping_gets_reassigned_message(self):
        """When a different source already maps to the same target, it is displaced with a message."""
        ColumnMapping.objects.create(profile=self.profile, source_column="OldSerial", target_field="serial")
        resp = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "NewSerial",
                "target_field": "serial",
            },
        )
        self.assertRedirects(resp, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False)
        self.assertFalse(ColumnMapping.objects.filter(profile=self.profile, source_column="OldSerial").exists())
        self.assertTrue(
            ColumnMapping.objects.filter(
                profile=self.profile, source_column="NewSerial", target_field="serial"
            ).exists()
        )

    def test_candidate_target_keeps_multiple_source_columns(self):
        """Candidate mappings add eligible columns instead of displacing them."""
        ColumnMapping.objects.create(
            profile=self.profile,
            source_column="Primary Contact",
            target_field="candidate:contact",
        )

        response = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "Owner",
                "target_field": "candidate:contact",
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            set(
                ColumnMapping.objects.filter(
                    profile=self.profile,
                    target_field="candidate:contact",
                ).values_list("source_column", flat=True)
            ),
            {"Primary Contact", "Owner"},
        )

    def test_direct_target_keeps_other_mappings_for_the_source_column(self):
        """A direct mapping can coexist with candidate and other direct targets."""
        ColumnMapping.objects.bulk_create(
            [
                ColumnMapping(
                    profile=self.profile,
                    source_column="Primary Contact",
                    target_field="candidate:contact",
                ),
                ColumnMapping(
                    profile=self.profile,
                    source_column="Primary Contact",
                    target_field="asset_tag",
                ),
            ]
        )

        response = self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "Primary Contact",
                "target_field": "serial",
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            set(
                ColumnMapping.objects.filter(
                    profile=self.profile,
                    source_column="Primary Contact",
                ).values_list("target_field", flat=True)
            ),
            {"candidate:contact", "asset_tag", "serial"},
        )


class QuickResolveDeviceTypeValidationTest(TestCase):
    """The device-type quick action reads slugs and names straight from the request."""

    def setUp(self):
        self.user = User.objects.create_superuser("qdtuser", "qdt@example.com", "testpass")
        self.client = Client()
        self.client.login(username="qdtuser", password="testpass")
        self.profile = _make_profile("QDeviceTypeTest")
        self.url = reverse("plugins:netbox_data_import:quick_resolve_device_type")
        self.preview = reverse("plugins:netbox_data_import:import_preview")

    def _post(self, **overrides):
        payload = {
            "profile_id": self.profile.pk,
            "source_make": "Acme",
            "source_model": "Widget",
            "netbox_mfg_slug": "acme",
            "netbox_dt_slug": "widget",
        }
        payload.update(overrides)
        return self.client.post(self.url, payload)

    def test_an_overlength_device_type_slug_is_refused(self):
        """The slug is posted directly and the mapping column holds 100 characters."""
        response = self._post(netbox_dt_slug="d" * 300)

        self.assertRedirects(response, self.preview, fetch_redirect_response=False)
        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.profile).exists())

    def test_an_overlength_manufacturer_slug_is_refused(self):
        """The manufacturer slug shares the same 100 character column."""
        response = self._post(netbox_mfg_slug="m" * 300)

        self.assertRedirects(response, self.preview, fetch_redirect_response=False)
        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.profile).exists())

    def test_an_overlength_source_make_is_refused(self):
        """The source make is read from the request and the column holds 200 characters."""
        response = self._post(source_make="M" * 300)

        self.assertRedirects(response, self.preview, fetch_redirect_response=False)
        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.profile).exists())

    def test_creating_now_refuses_a_make_the_manufacturer_name_cannot_hold(self):
        """The mapping accepts 200 characters, but the NetBox manufacturer name holds 100."""
        from dcim.models import Manufacturer

        response = self._post(source_make="M" * 150, action="create_now")

        self.assertRedirects(response, self.preview, fetch_redirect_response=False)
        self.assertFalse(Manufacturer.objects.filter(slug="acme").exists())
        self.assertFalse(
            DeviceTypeMapping.objects.filter(profile=self.profile).exists(),
            "a refused create_now must not leave the mapping behind",
        )

    def test_creating_now_refuses_a_model_the_device_type_cannot_hold(self):
        """The posted device type name is never bounded and the NetBox model holds 100."""
        from dcim.models import DeviceType

        response = self._post(netbox_dt_name="D" * 150, action="create_now")

        self.assertRedirects(response, self.preview, fetch_redirect_response=False)
        self.assertFalse(DeviceType.objects.filter(slug="widget").exists())
        self.assertFalse(
            DeviceTypeMapping.objects.filter(profile=self.profile).exists(),
            "a refused create_now must not leave the mapping behind",
        )

    def test_a_valid_mapping_is_still_saved(self):
        """The guard must not refuse the values the preview page actually posts."""
        response = self._post()

        self.assertRedirects(response, self.preview, fetch_redirect_response=False)
        self.assertTrue(
            DeviceTypeMapping.objects.filter(
                profile=self.profile, source_make="Acme", netbox_device_type_slug="widget"
            ).exists()
        )

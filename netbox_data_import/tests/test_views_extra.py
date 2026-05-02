# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for unused-columns feature: fuzzy matching helper + QuickAddColumnMappingView."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import ColumnMapping, ImportProfile
from netbox_data_import.views import _fuzzy_match_netbox_field

User = get_user_model()


def _make_profile(name="QMapTest") -> ImportProfile:
    return ImportProfile.objects.create(
        name=name,
        sheet_name="Data",
        source_id_column="Id",
        update_existing=False,
        create_missing_device_types=False,
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

    def test_updates_existing_column_mapping(self):
        ColumnMapping.objects.create(profile=self.profile, source_column="JiraID", target_field="asset_tag")
        self.client.post(
            self.url,
            {
                "profile_id": self.profile.pk,
                "source_column": "JiraID",
                "target_field": "serial",
            },
        )
        mapping = ColumnMapping.objects.get(profile=self.profile, source_column="JiraID")
        self.assertEqual(mapping.target_field, "serial")

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

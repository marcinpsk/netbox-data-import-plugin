# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A quick action takes its profile from the Preview Claim, never from a posted profile ID."""

from dcim.models import Site
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceTypeMapping,
    IgnoredDevice,
    ImportProfile,
    ManufacturerMapping,
)
from netbox_data_import.tests.helpers import preview_claim, preview_coordinator, seed_workbook_preview

QUICK_ACTIONS = {
    "quick_add_class_mapping": {"source_class": "Controller", "mapping_action": "ignore"},
    "quick_add_column_mapping": {"source_column": "Depth", "target_field": "serial"},
    "quick_resolve_manufacturer": {"source_make": "Acme", "netbox_mfg_slug": "acme"},
    "quick_resolve_device_type": {"source_make": "Acme", "source_model": "Widget"},
    "ignore_device": {"source_id": "SRC-1", "device_name": "widget-1"},
    "unignore_device": {"source_id": "SRC-1"},
}
POLICY_MODELS = (ClassRoleMapping, ColumnMapping, DeviceTypeMapping, IgnoredDevice, ManufacturerMapping)


def _policy_state():
    return {model.__name__: sorted(model.objects.values_list("pk", flat=True)) for model in POLICY_MODELS}


class QuickActionProfileIdTest(TestCase):
    """The profile a quick action writes to is the one the preview claim names."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="quick-action-user", email="quick@example.invalid", password="testpass"
        )
        cls.profile = ImportProfile.objects.create(
            name="Quick Action Profile", adapter_config={"sheet_name": "Data", "source_id_column": "Id"}
        )
        cls.other_profile = ImportProfile.objects.create(
            name="Other Quick Action Profile", adapter_config={"sheet_name": "Data", "source_id_column": "Id"}
        )
        cls.site = Site.objects.create(name="Quick Action Site", slug="quick-action-site")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        IgnoredDevice.objects.create(profile=self.profile, source_id="SRC-1", device_name="widget-1")
        seed_workbook_preview(self.client, self.profile, self.site, ["Id"], [["SRC-1"]])

    def _post(self, url_name, **claim):
        return self.client.post(
            reverse(f"plugins:netbox_data_import:{url_name}"),
            {**preview_claim(self.client), **claim, **QUICK_ACTIONS[url_name]},
        )

    def test_every_quick_action_refuses_a_claim_for_another_profile(self):
        """A page that shows another profile's preview is stale: 409 and no write."""
        for url_name in QUICK_ACTIONS:
            with self.subTest(url_name=url_name):
                before, revision = _policy_state(), preview_coordinator(self.client).revision

                response = self._post(url_name, preview_profile=str(self.other_profile.pk))

                self.assertEqual(response.status_code, 409)
                self.assertEqual(_policy_state(), before)
                self.assertEqual(preview_coordinator(self.client).revision, revision)

    def test_every_quick_action_refuses_a_malformed_claim_profile(self):
        """A forged profile value in the claim is refused the same way."""
        for url_name in QUICK_ACTIONS:
            with self.subTest(url_name=url_name):
                before = _policy_state()

                response = self._post(url_name, preview_profile="not-a-number")

                self.assertEqual(response.status_code, 409)
                self.assertEqual(_policy_state(), before)

    def test_a_posted_profile_id_does_not_redirect_the_write(self):
        """The view ignores a posted profile_id and writes to the claimed profile."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:quick_add_class_mapping"),
            {
                **preview_claim(self.client),
                "profile_id": self.other_profile.pk,
                **QUICK_ACTIONS["quick_add_class_mapping"],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ClassRoleMapping.objects.filter(profile=self.profile, source_class="Controller").exists())
        self.assertFalse(ClassRoleMapping.objects.filter(profile=self.other_profile).exists())

    def test_a_valid_claim_still_saves_the_mapping(self):
        """The guard does not block the working path."""
        response = self._post("quick_add_class_mapping")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ClassRoleMapping.objects.filter(profile=self.profile, source_class="Controller", ignore=True).exists()
        )

    def test_a_valid_claim_still_saves_a_column_mapping(self):
        """The second most used quick action keeps working too."""
        response = self._post("quick_add_column_mapping")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ColumnMapping.objects.filter(profile=self.profile, source_column="Depth", target_field="serial").exists()
        )

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A quick action must reject an unusable profile ID instead of raising."""

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile

# The preview posts these form actions with the active profile ID.
QUICK_ACTIONS = {
    "quick_add_class_mapping": {"source_class": "Controller", "mapping_action": "ignore"},
    "quick_add_column_mapping": {"source_column": "Depth", "target_field": "serial"},
    "quick_resolve_manufacturer": {"source_make": "Acme", "netbox_mfg_slug": "acme"},
    "quick_resolve_device_type": {"source_make": "Acme", "source_model": "Widget"},
    "ignore_device": {"source_id": "SRC-1", "device_name": "widget-1"},
    "unignore_device": {"source_id": "SRC-1"},
}


class QuickActionProfileIdTest(TestCase):
    """An empty or malformed profile ID reaches these views whenever the modal script is stale."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="quick-action-user", email="quick@example.invalid", password="testpass"
        )
        cls.profile = ImportProfile.objects.create(
            name="Quick Action Profile", adapter_config={"sheet_name": "Data", "source_id_column": "Id"}
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _post(self, url_name, profile_id):
        return self.client.post(
            reverse(f"plugins:netbox_data_import:{url_name}"),
            {"profile_id": profile_id, **QUICK_ACTIONS[url_name]},
        )

    def test_every_quick_action_rejects_an_empty_profile_id(self):
        """The modal posts an empty field when its script did not run; that is not a crash."""
        for url_name in QUICK_ACTIONS:
            with self.subTest(url_name=url_name):
                response = self._post(url_name, "")

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("plugins:netbox_data_import:import_preview"))
                messages = [str(message) for message in get_messages(response.wsgi_request)]
                self.assertTrue(
                    any("import profile" in message for message in messages),
                    f"{url_name} reported {messages}",
                )

    def test_every_quick_action_rejects_a_non_numeric_profile_id(self):
        """A forged profile ID is refused the same way."""
        for url_name in QUICK_ACTIONS:
            with self.subTest(url_name=url_name):
                response = self._post(url_name, "not-a-number")

                self.assertEqual(response.status_code, 302)

    def test_an_unknown_profile_id_is_not_found(self):
        """A well-formed ID for a profile that does not exist stays a 404."""
        response = self._post("quick_add_class_mapping", self.profile.pk + 1000)

        self.assertEqual(response.status_code, 404)

    def test_a_valid_profile_id_still_saves_the_mapping(self):
        """The guard does not block the working path."""
        response = self._post("quick_add_class_mapping", self.profile.pk)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ClassRoleMapping.objects.filter(profile=self.profile, source_class="Controller", ignore=True).exists()
        )

    def test_a_valid_profile_id_still_saves_a_column_mapping(self):
        """The second most used quick action keeps working too."""
        response = self._post("quick_add_column_mapping", self.profile.pk)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ColumnMapping.objects.filter(profile=self.profile, source_column="Depth", target_field="serial").exists()
        )

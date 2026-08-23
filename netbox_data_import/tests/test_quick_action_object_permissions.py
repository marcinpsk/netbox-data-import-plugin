# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Preview quick actions enforce constrained ObjectPermission rows at their write seam."""

from dcim.models import DeviceRole, DeviceType, Manufacturer
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import (
    ColumnMapping,
    DeviceTypeMapping,
    IgnoredDevice,
    ImportProfile,
    ManufacturerMapping,
)
from netbox_data_import.tests.helpers import user_with_object_permission


class QuickActionObjectPermissionTest(TestCase):
    """Exercise each authorization decision through a real HTTP request and database."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(
            name="Quick Action Scope Profile",
            adapter_config={"sheet_name": "Data", "source_id_column": "Id"},
        )
        self.preview_url = reverse("plugins:netbox_data_import:import_preview")

    def _client_with(self, username, *grants):
        user = user_with_object_permission(
            username,
            [
                (ImportProfile, ["change"], {"pk": self.profile.pk}),
                *grants,
            ],
        )
        client = Client()
        client.force_login(user)
        return client

    def _post(self, client, url_name, payload):
        return client.post(
            reverse(f"plugins:netbox_data_import:{url_name}"),
            {"profile_id": self.profile.pk, **payload},
        )

    def _assert_preview_redirect(self, response):
        self.assertRedirects(response, self.preview_url, fetch_redirect_response=False)

    def test_a_constrained_manufacturer_slug_is_refused(self):
        client = self._client_with(
            "quick-mfg-scope",
            (Manufacturer, ["add"], {"slug": "allowed-manufacturer"}),
        )

        response = self._post(
            client,
            "quick_create_manufacturer",
            {"mfg_name": "Refused Manufacturer", "mfg_slug": "refused-manufacturer"},
        )

        self._assert_preview_redirect(response)
        self.assertFalse(Manufacturer.objects.filter(slug="refused-manufacturer").exists())

    def test_a_constrained_device_role_slug_is_refused_with_json_403(self):
        client = self._client_with(
            "quick-role-scope",
            (DeviceRole, ["add"], {"slug": "allowed-role"}),
        )

        response = self._post(
            client,
            "quick_create_role",
            {"name": "Refused Role", "slug": "refused-role"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(DeviceRole.objects.filter(slug="refused-role").exists())

    def test_a_constrained_ignored_device_source_id_is_refused(self):
        client = self._client_with(
            "quick-ignore-scope",
            (IgnoredDevice, ["add"], {"source_id": "allowed-source"}),
        )

        response = self._post(
            client,
            "ignore_device",
            {"source_id": "refused-source", "device_name": "Refused Device"},
        )

        self._assert_preview_redirect(response)
        self.assertFalse(IgnoredDevice.objects.filter(profile=self.profile).exists())

    def test_profile_change_without_ignored_device_add_cannot_ignore(self):
        client = self._client_with("quick-ignore-no-add")

        response = self._post(
            client,
            "ignore_device",
            {"source_id": "no-add-source", "device_name": "No Add Device"},
        )

        self._assert_preview_redirect(response)
        self.assertFalse(IgnoredDevice.objects.filter(profile=self.profile).exists())

    def test_a_constrained_mapping_add_is_refused(self):
        client = self._client_with(
            "quick-mapping-scope",
            (ManufacturerMapping, ["add"], {"source_make": "Allowed Make"}),
        )

        response = self._post(
            client,
            "quick_resolve_manufacturer",
            {"source_make": "Refused Make", "netbox_mfg_slug": "refused"},
        )

        self._assert_preview_redirect(response)
        self.assertFalse(ManufacturerMapping.objects.filter(profile=self.profile).exists())

    def test_add_only_cannot_update_an_existing_mapping(self):
        mapping = ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Acme",
            netbox_manufacturer_slug="before",
        )
        client = self._client_with(
            "quick-mapping-add-only",
            (ManufacturerMapping, ["add"], None),
        )

        response = self._post(
            client,
            "quick_resolve_manufacturer",
            {"source_make": "Acme", "netbox_mfg_slug": "after"},
        )

        self._assert_preview_redirect(response)
        mapping.refresh_from_db()
        self.assertEqual(mapping.netbox_manufacturer_slug, "before")

    def test_change_only_can_update_an_existing_mapping(self):
        mapping = ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Acme",
            netbox_manufacturer_slug="before",
        )
        client = self._client_with(
            "quick-mapping-change-only",
            (ManufacturerMapping, ["change"], None),
        )

        response = self._post(
            client,
            "quick_resolve_manufacturer",
            {"source_make": "Acme", "netbox_mfg_slug": "after"},
        )

        self._assert_preview_redirect(response)
        mapping.refresh_from_db()
        self.assertEqual(mapping.netbox_manufacturer_slug, "after")

    def test_a_change_constraint_cannot_move_a_mapping_out_of_scope(self):
        mapping = ManufacturerMapping.objects.create(
            profile=self.profile,
            source_make="Acme",
            netbox_manufacturer_slug="inside",
        )
        client = self._client_with(
            "quick-mapping-move-scope",
            (ManufacturerMapping, ["change"], {"netbox_manufacturer_slug": "inside"}),
        )

        response = self._post(
            client,
            "quick_resolve_manufacturer",
            {"source_make": "Acme", "netbox_mfg_slug": "outside"},
        )

        self._assert_preview_redirect(response)
        mapping.refresh_from_db()
        self.assertEqual(mapping.netbox_manufacturer_slug, "inside")

    def test_column_mapping_add_without_delete_cannot_replace_a_mapping(self):
        displaced = ColumnMapping.objects.create(
            profile=self.profile,
            source_column="Old Serial",
            target_field="serial",
        )
        client = self._client_with(
            "quick-column-no-delete",
            (ColumnMapping, ["add"], None),
        )

        response = self._post(
            client,
            "quick_add_column_mapping",
            {"source_column": "New Serial", "target_field": "serial"},
        )

        self._assert_preview_redirect(response)
        self.assertTrue(ColumnMapping.objects.filter(pk=displaced.pk).exists())
        self.assertFalse(ColumnMapping.objects.filter(profile=self.profile, source_column="New Serial").exists())

    def test_a_denied_manufacturer_rolls_back_the_create_now_composite(self):
        client = self._client_with(
            "quick-device-type-mfg-denied",
            (DeviceTypeMapping, ["add"], None),
            (Manufacturer, ["add"], {"slug": "allowed-manufacturer"}),
            (DeviceType, ["add"], None),
        )

        response = self._post(
            client,
            "quick_resolve_device_type",
            {
                "source_make": "Refused Manufacturer",
                "source_model": "Widget",
                "netbox_mfg_slug": "refused-manufacturer",
                "netbox_dt_slug": "widget",
                "action": "create_now",
            },
        )

        self._assert_preview_redirect(response)
        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.profile).exists())
        self.assertFalse(Manufacturer.objects.filter(slug="refused-manufacturer").exists())
        self.assertFalse(DeviceType.objects.filter(slug="widget").exists())

    def test_a_denied_device_type_rolls_back_the_create_now_composite(self):
        client = self._client_with(
            "quick-device-type-denied",
            (DeviceTypeMapping, ["add"], None),
            (Manufacturer, ["add"], None),
            (DeviceType, ["add"], {"slug": "allowed-device-type"}),
        )

        response = self._post(
            client,
            "quick_resolve_device_type",
            {
                "source_make": "Acme",
                "source_model": "Refused Widget",
                "netbox_mfg_slug": "acme",
                "netbox_dt_slug": "refused-widget",
                "action": "create_now",
            },
        )

        self._assert_preview_redirect(response)
        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.profile).exists())
        self.assertFalse(Manufacturer.objects.filter(slug="acme").exists())
        self.assertFalse(DeviceType.objects.filter(slug="refused-widget").exists())

    def test_a_constrained_ignored_device_delete_leaves_the_row(self):
        ignored = IgnoredDevice.objects.create(
            profile=self.profile,
            source_id="refused-source",
            device_name="Refused Device",
        )
        client = self._client_with(
            "quick-unignore-scope",
            (IgnoredDevice, ["delete"], {"source_id": "allowed-source"}),
        )

        response = self._post(client, "unignore_device", {"source_id": "refused-source"})

        self._assert_preview_redirect(response)
        self.assertTrue(IgnoredDevice.objects.filter(pk=ignored.pk).exists())

    def test_profile_change_without_ignored_device_delete_cannot_unignore(self):
        ignored = IgnoredDevice.objects.create(
            profile=self.profile,
            source_id="no-delete-source",
            device_name="No Delete Device",
        )
        client = self._client_with("quick-unignore-no-delete")

        response = self._post(client, "unignore_device", {"source_id": ignored.source_id})

        self._assert_preview_redirect(response)
        self.assertTrue(IgnoredDevice.objects.filter(pk=ignored.pk).exists())

    def test_unignore_rejects_a_non_numeric_profile_id_without_a_server_error(self):
        client = self._client_with("quick-unignore-invalid-profile")

        response = client.post(
            reverse("plugins:netbox_data_import:unignore_device"),
            {"profile_id": "not-an-id", "source_id": "source"},
        )

        self._assert_preview_redirect(response)

    def test_a_refusal_does_not_echo_the_permission_name(self):
        """CodeQL caught the exception text reaching the response; the operator log keeps it.

        The permission names an object the caller may not be allowed to know exists.
        """
        client = self._client_with("scope-leak-user")

        response = client.post(
            reverse("plugins:netbox_data_import:quick_create_manufacturer"),
            {"profile_id": self.profile.pk, "mfg_name": "Acme", "mfg_slug": "acme"},
        )

        # The refusal rides on a Django message, so it is only visible once the redirect renders.
        shown = " ".join(str(message) for message in get_messages(response.wsgi_request))
        self.assertIn("Permission denied", shown)
        self.assertNotIn("dcim.add_manufacturer", shown)
        self.assertFalse(Manufacturer.objects.filter(slug="acme").exists())

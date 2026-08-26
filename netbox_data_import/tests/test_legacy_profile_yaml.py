# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A profile YAML exported by v1.5.2 still imports after the adapter cutover."""

from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import ImportProfile

# The scalar keys v1.5.2 wrote at the top level of the `profile` mapping, in its own order.
LEGACY_YAML = b"""profile:
  name: Legacy Export Profile
  description: Exported by 1.5.2
  sheet_name: Inventory
  source_id_column: Id
  custom_field_name: cans_id
  update_existing: true
  create_missing_device_types: false
  preview_view_mode: racks
  capture_extra_data: true
  primary_contact_role: legacy-owner
  primary_contact_lookup_field: name
column_mappings:
- source_column: Name
  target_field: device_name
class_role_mappings:
- source_class: Server
  role_slug: legacy-role
  rack_type: null
device_type_mappings:
- source_make: Cisco
  source_model: C9300
  netbox_manufacturer_slug: cisco
  netbox_device_type_slug: cisco-c9300
manufacturer_mappings:
- source_make: Cisco
  netbox_manufacturer_slug: cisco
column_transform_rules:
- source_column: Name
  pattern: ^(\\w+)$
  group_1_target: asset_tag
  group_2_target: ''
"""


class LegacyProfileYamlImportTest(TestCase):
    """`_profile_defaults_from_yaml` translates the pre-cutover scalar keys."""

    def setUp(self):
        """Create an authenticated test client and the contact role referenced by the legacy profile."""
        from tenancy.models import ContactRole

        self.user = get_user_model().objects.create_superuser("legacy-yaml", "l@example.invalid", "testpass")
        self.client = Client()
        self.client.force_login(self.user)
        self.role = ContactRole.objects.create(name="Legacy Owner", slug="legacy-owner")

    def _upload(self, payload, name="legacy.yaml"):
        """POST one YAML document to the profile import view."""
        yaml_file = BytesIO(payload)
        yaml_file.name = name
        return self.client.post(
            reverse("plugins:netbox_data_import:import_profile_yaml"),
            {"yaml_file": yaml_file},
        )

    def test_a_v152_export_imports_into_the_adapter_configuration(self):
        """The legacy scalars land in adapter_config under the flat_workbook adapter."""
        response = self._upload(LEGACY_YAML)
        self.assertIn(response.status_code, [200, 302])

        profile = ImportProfile.objects.get(name="Legacy Export Profile")
        self.assertEqual(profile.description, "Exported by 1.5.2")
        self.assertEqual(profile.source_adapter, "flat_workbook")
        self.assertEqual(
            profile.adapter_config,
            {
                "sheet_name": "Inventory",
                "source_id_column": "Id",
                "custom_field_name": "cans_id",
                "update_existing": True,
                "create_missing_device_types": False,
                "capture_extra_data": True,
                "preview_view_mode": "racks",
                # The slug in the file resolves to the name adapter_config stores.
                "primary_contact_role": "Legacy Owner",
                "primary_contact_lookup_field": "name",
            },
        )
        self.assertEqual(profile.resolved_primary_contact_role, self.role)

    def test_a_v152_export_still_creates_its_child_rows(self):
        """Translating the profile scalars must not disturb the nested sections."""
        self._upload(LEGACY_YAML)

        profile = ImportProfile.objects.get(name="Legacy Export Profile")
        self.assertEqual(profile.column_mappings.count(), 1)
        self.assertEqual(profile.class_role_mappings.count(), 1)
        self.assertEqual(profile.device_type_mappings.count(), 1)
        self.assertEqual(profile.manufacturer_mappings.count(), 1)
        self.assertEqual(profile.column_transform_rules.count(), 1)

    def test_an_unresolvable_contact_role_slug_is_named(self):
        """A missing Contact Role must fail with its slug, not stamp a null role."""
        payload = LEGACY_YAML.replace(b"primary_contact_role: legacy-owner", b"primary_contact_role: no-such-role")
        response = self._upload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no-such-role")
        self.assertFalse(ImportProfile.objects.filter(name="Legacy Export Profile").exists())

    def test_mixing_legacy_scalars_with_adapter_config_is_refused(self):
        """
        Rejects legacy scalar settings when combined with ``adapter_config``.
        """
        payload = LEGACY_YAML.replace(
            b"  sheet_name: Inventory\n",
            b"  sheet_name: Inventory\n  adapter_config:\n    sheet_name: Other\n",
        )
        response = self._upload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be combined with adapter_config")
        self.assertFalse(ImportProfile.objects.filter(name="Legacy Export Profile").exists())

    def test_mixing_legacy_scalars_with_source_adapter_is_refused(self):
        """A legacy file names no adapter, so an explicit one means the file was hand-edited."""
        payload = LEGACY_YAML.replace(
            b"  sheet_name: Inventory\n",
            b"  sheet_name: Inventory\n  source_adapter: trace_workbook\n",
        )
        response = self._upload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be combined with source_adapter")
        self.assertFalse(ImportProfile.objects.filter(name="Legacy Export Profile").exists())

    def test_an_unknown_key_is_still_refused(self):
        """The translation widens the accepted set by exactly the legacy keys, nothing more."""
        payload = LEGACY_YAML.replace(b"  sheet_name: Inventory\n", b"  sheet_name: Inventory\n  nonsense_key: 1\n")
        response = self._upload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nonsense_key")
        self.assertFalse(ImportProfile.objects.filter(name="Legacy Export Profile").exists())

    def test_a_current_export_is_unaffected(self):
        """A post-cutover file carries adapter_config and must keep importing unchanged."""
        payload = b"""profile:
  name: Current Export Profile
  adapter_config:
    sheet_name: Data
    primary_contact_role: Legacy Owner
"""
        response = self._upload(payload)
        self.assertIn(response.status_code, [200, 302])

        profile = ImportProfile.objects.get(name="Current Export Profile")
        self.assertEqual(profile.adapter_settings.sheet_name, "Data")
        self.assertEqual(profile.resolved_primary_contact_role, self.role)

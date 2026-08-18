# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Run the custom-field to plugin-table migration against real stored payloads."""

from django.test import TestCase

from importlib import import_module
from netbox_data_import.models import DeviceImportSource, ImportProfile
from netbox_data_import.tests.helpers import make_dcim_objects


migration_module = import_module("netbox_data_import.migrations.0020_migrate_import_source_custom_field")


class _Apps:
    """Return the current models, which the data migration only reads by label."""

    @staticmethod
    def get_model(app_label, model_name):
        from django.apps import apps

        return apps.get_model(app_label, model_name)


class MigrateImportSourceCustomFieldTest(TestCase):
    """The migration must move every payload before the custom field disappears."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, Rack

        cls.site, _mfg, cls.device_type, cls.role = make_dcim_objects("Migrate")
        cls.profile = ImportProfile.objects.create(
            name="Migrate Profile", adapter_config={"sheet_name": "Data", "source_id_column": "Id"}
        )
        cls.device = Device.objects.create(
            name="migrate-device", site=cls.site, device_type=cls.device_type, role=cls.role
        )
        cls.orphaned_device = Device.objects.create(
            name="migrate-orphan", site=cls.site, device_type=cls.device_type, role=cls.role
        )
        cls.rack = Rack.objects.create(name="Migrate Rack", site=cls.site, u_height=42)

    def _seed(self, obj, payload):
        obj.custom_field_data["data_import_source"] = payload
        obj.save(update_fields=["custom_field_data"])

    def _run_migration(self):
        migration_module.move_import_source_to_plugin_table(_Apps, None)

    def test_moves_the_payload_into_the_plugin_table(self):
        """Source ID, extra columns, and unassigned IPs survive the move."""
        self._seed(
            self.device,
            {
                "source_id": "SRC-1",
                "profile_id": self.profile.pk,
                "profile_name": self.profile.name,
                "extra": {"jira_id": "J-42"},
                "_ip": {"primary_ip4": "10.0.0.1/32"},
            },
        )

        self._run_migration()

        record = DeviceImportSource.objects.get(device=self.device)
        self.assertEqual(record.profile, self.profile)
        self.assertEqual(record.source_id, "SRC-1")
        self.assertEqual(record.extra_columns, {"jira_id": "J-42"})
        self.assertEqual(record.unassigned_ips, {"primary_ip4": "10.0.0.1/32"})

    def test_truncates_a_source_id_that_exceeds_the_column(self):
        """The column holds 200 characters, so a longer source ID is cut rather than rejected."""
        self._seed(self.device, {"source_id": "S" * 250, "profile_id": self.profile.pk})

        self._run_migration()

        self.assertEqual(DeviceImportSource.objects.get(device=self.device).source_id, "S" * 200)

    def test_the_migration_refuses_to_reverse(self):
        """A rollback drops the new table and cannot restore the custom field, so Django must refuse."""
        operation = migration_module.Migration.operations[0]

        self.assertFalse(operation.reversible)

    def test_clears_the_custom_field_from_devices_and_racks(self):
        """No object keeps the plugin key, and the custom field itself is gone."""
        from dcim.models import Device, Rack
        from extras.models import CustomField

        CustomField.objects.create(name="data_import_source", type="json")
        self._seed(self.device, {"source_id": "SRC-1", "profile_id": self.profile.pk})
        self._seed(self.rack, {"source_id": "RACK-1", "profile_id": self.profile.pk})

        self._run_migration()

        self.assertNotIn("data_import_source", Device.objects.get(pk=self.device.pk).custom_field_data)
        self.assertNotIn("data_import_source", Rack.objects.get(pk=self.rack.pk).custom_field_data)
        self.assertFalse(CustomField.objects.filter(name="data_import_source").exists())

    def test_drops_a_payload_that_names_a_deleted_profile(self):
        """A binding to a profile that no longer exists cannot be restored, so it is not kept."""
        self._seed(self.orphaned_device, {"source_id": "SRC-GONE", "profile_id": self.profile.pk + 1000})

        with self.assertLogs("netbox_data_import.migrations", level="WARNING") as logs:
            self._run_migration()

        self.assertFalse(DeviceImportSource.objects.filter(device=self.orphaned_device).exists())
        self.assertIn("deleted import profile", logs.output[0])

    def test_keeps_other_custom_field_values(self):
        """The per-profile custom field an operator configured is left alone."""
        from dcim.models import Device

        self.device.custom_field_data["cans_id"] = "CANS-9"
        self._seed(self.device, {"source_id": "SRC-1", "profile_id": self.profile.pk})

        self._run_migration()

        self.assertEqual(Device.objects.get(pk=self.device.pk).custom_field_data["cans_id"], "CANS-9")

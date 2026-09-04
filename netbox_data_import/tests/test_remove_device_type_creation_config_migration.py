# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Verify removal of the Device Type creation adapter setting."""

from importlib import import_module

from django.db import connection
from django.db.migrations import RunPython
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from netbox_data_import.adapter_forms import FlatWorkbookConfigForm

APP = "netbox_data_import"
BEFORE = "0029_alter_cableimportsource_from_text_and_more"
REMOVE_DEVICE_TYPE_CREATION_CONFIG = "0030_remove_device_type_creation_config"


def _migration(step):
    """Return one adapter configuration removal migration class."""
    return import_module(f"{APP}.migrations.{step}").Migration


def _migrate(target, *, fake=False):
    """Migrate the plugin app to one target and return its historical app registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)], fake=fake)
    executor.loader.build_graph()
    return executor.loader.project_state([(APP, target)]).apps


def _restore_every_leaf():
    """Restore every leaf because a migration test can leave later worker tests incomplete."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(executor.loader.graph.leaf_nodes(APP)))


class DeviceTypeCreationConfigMigrationStructureTest(SimpleTestCase):
    """The authored data migration is ordered and refuses a lossy rollback."""

    def test_the_removal_reverses_without_restoring_the_retired_key(self):
        """The migration graph is walked backwards elsewhere, so every migration must reverse."""
        operation = _migration(REMOVE_DEVICE_TYPE_CREATION_CONFIG).operations[0]

        self.assertIsInstance(operation, RunPython)
        self.assertEqual(operation.code.__name__, "remove_device_type_creation_config")
        self.assertIs(operation.reverse_code, RunPython.noop)
        self.assertIn((APP, BEFORE), _migration(REMOVE_DEVICE_TYPE_CREATION_CONFIG).dependencies)


class DeviceTypeCreationConfigMigrationTest(TransactionTestCase):
    """The migration removes only the retired adapter configuration key."""

    def setUp(self):
        super().setUp()
        self.addCleanup(_restore_every_leaf)
        _migrate(BEFORE, fake=True)

    def test_the_retired_key_is_removed_from_every_profile(self):
        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        ImportProfile = apps.get_model(APP, "ImportProfile")
        ImportProfile.objects.create(
            name="Creation Enabled",
            adapter_config={"sheet_name": "Inventory", "create_missing_device_types": True},
        )
        ImportProfile.objects.create(
            name="Creation Disabled",
            adapter_config={"sheet_name": "Assets", "create_missing_device_types": False},
        )
        ImportProfile.objects.create(name="Key Absent", adapter_config={"sheet_name": "Devices"})

        migrated_apps = _migrate(REMOVE_DEVICE_TYPE_CREATION_CONFIG)
        MigratedProfile = migrated_apps.get_model(APP, "ImportProfile")

        migrated_profiles = list(MigratedProfile.objects.order_by("name").values_list("name", "adapter_config"))
        self.assertEqual(
            migrated_profiles,
            [
                ("Creation Disabled", {"sheet_name": "Assets"}),
                ("Creation Enabled", {"sheet_name": "Inventory"}),
                ("Key Absent", {"sheet_name": "Devices"}),
            ],
        )
        for _name, adapter_config in migrated_profiles:
            FlatWorkbookConfigForm.validate_config(adapter_config)

    def test_a_rollback_leaves_the_retired_key_gone(self):
        """Rolling back restores no setting, and the old code read a missing key as off."""
        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        ImportProfile = apps.get_model(APP, "ImportProfile")
        ImportProfile.objects.create(
            name="Rolled Back",
            adapter_config={"sheet_name": "Inventory", "create_missing_device_types": True},
        )
        _migrate(REMOVE_DEVICE_TYPE_CREATION_CONFIG)

        _migrate(BEFORE)

        profile = ImportProfile.objects.get(name="Rolled Back")
        self.assertNotIn("create_missing_device_types", profile.adapter_config)
        self.assertEqual(profile.adapter_config["sheet_name"], "Inventory")

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Run the ordered Import Profile cutover against the real migration executor."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

APP = "netbox_data_import"
BEFORE = "0021_importprofile_adapter_config_and_more"
DATA_STEP = "0022_migrate_profile_adapter_config"
AFTER = "0023_drop_moved_profile_columns"

MOVED_COLUMNS = (
    "sheet_name",
    "source_id_column",
    "custom_field_name",
    "update_existing",
    "create_missing_device_types",
    "capture_extra_data",
    "primary_contact_lookup_field",
    "preview_view_mode",
)


def _migrate(target):
    """Migrate the plugin app to *target* and return the resulting historical app registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)])
    executor.loader.build_graph()
    return executor.loader.project_state([(APP, target)]).apps


class ProfileAdapterConfigMigrationTest(TransactionTestCase):
    """The three ordered steps move every column and drop the originals."""

    def tearDown(self):
        """Leave the database at the latest migration for the rest of the suite."""
        _migrate(AFTER)

    def test_it_moves_every_column_for_every_existing_profile(self):
        """The data step stamps the adapter key and copies each declared column."""
        apps = _migrate(BEFORE)
        ContactRole = apps.get_model("tenancy", "ContactRole")
        ImportProfile = apps.get_model(APP, "ImportProfile")
        role = ContactRole.objects.create(name="Migrated Owner", slug="migrated-owner")
        ImportProfile.objects.create(
            name="Legacy Full",
            sheet_name="Inventory",
            source_id_column="Source ID",
            custom_field_name="cans_id",
            update_existing=False,
            create_missing_device_types=False,
            capture_extra_data=True,
            primary_contact_role=role,
            primary_contact_lookup_field="name",
            preview_view_mode="racks",
        )
        ImportProfile.objects.create(name="Legacy Defaults")

        _migrate(AFTER)

        from netbox_data_import.models import ImportProfile as CurrentProfile

        full = CurrentProfile.objects.get(name="Legacy Full")
        self.assertEqual(full.source_adapter, "flat_workbook")
        self.assertEqual(
            full.adapter_config,
            {
                "sheet_name": "Inventory",
                "source_id_column": "Source ID",
                "custom_field_name": "cans_id",
                "update_existing": False,
                "create_missing_device_types": False,
                "capture_extra_data": True,
                "primary_contact_lookup_field": "name",
                "preview_view_mode": "racks",
                "primary_contact_role": "Migrated Owner",
            },
        )

        defaults = CurrentProfile.objects.get(name="Legacy Defaults")
        self.assertEqual(defaults.source_adapter, "flat_workbook")
        self.assertEqual(defaults.adapter_config["sheet_name"], "Data")
        self.assertTrue(defaults.adapter_config["update_existing"])
        self.assertIsNone(defaults.adapter_config["primary_contact_role"])

    def test_the_moved_columns_are_gone_afterwards(self):
        """No dual read path survives the sequence."""
        _migrate(AFTER)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                [f"{APP}_importprofile"],
            )
            columns = {row[0] for row in cursor.fetchall()}
        self.assertEqual(columns & set(MOVED_COLUMNS), set())
        self.assertNotIn("primary_contact_role_id", columns)
        self.assertIn("adapter_config", columns)
        self.assertIn("source_adapter", columns)

    def test_the_data_step_carries_data_operations_only(self):
        """The middle step never touches the schema."""
        from django.db.migrations.operations.base import Operation
        from importlib import import_module

        migration = import_module(f"{APP}.migrations.{DATA_STEP}").Migration
        for operation in migration.operations:
            self.assertIsInstance(operation, Operation)
            self.assertFalse(operation.reduces_to_sql, operation)

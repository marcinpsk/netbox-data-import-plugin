# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Run the squashed Import Profile cutover against the real migration executor."""

from importlib import import_module

from django.db import connection
from django.db.migrations import AddField, RemoveField, RunPython
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from netbox_data_import.tests.helpers import reverse_squashed_schema_operations

APP = "netbox_data_import"
BEFORE = "0020_migrate_import_source_custom_field"
SQUASHED = "0021_importprofile_adapter_config"

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
REMOVED_FIELDS = (*MOVED_COLUMNS, "primary_contact_role")


def _migrate(target, *, fake=False):
    """Migrate the plugin app to *target* and return the resulting historical app registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)], fake=fake)
    executor.loader.build_graph()
    return executor.loader.project_state([(APP, target)]).apps


def _applied_steps():
    """Return the plugin migration names currently held by the recorder."""
    return {name for app, name in MigrationRecorder(connection).applied_migrations() if app == APP}


def _rewind_to_before_the_squash():
    """Reverse only the squash's schema so its real forward data operations can run again."""
    executor = MigrationExecutor(connection)
    reverse_squashed_schema_operations(executor, APP, SQUASHED, BEFORE, expected_data_operations=2)
    return _migrate(BEFORE, fake=True)


def _restore_every_leaf():
    """Migrate the plugin app forward to every leaf of its migration graph.

    tearDown restores these rather than AFTER: a later migration would otherwise leave the worker
    database short of its newest tables for every test that follows. A merge can leave two leaves,
    so the executor gets all of them instead of one name picked by sort order.
    """
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(executor.loader.graph.leaf_nodes(APP)))


class ProfileAdapterConfigMigrationTest(TransactionTestCase):
    """The squash moves the legacy settings, repairs them, and drops their columns in order."""

    def setUp(self):
        super().setUp()
        # A TransactionTestCase does not roll back schema changes, and a failure inside setUp
        # skips tearDown. Register before walking down, so a rewound worker always recovers.
        self.addCleanup(_restore_every_leaf)
        _rewind_to_before_the_squash()

    def test_it_moves_every_column_and_repairs_blank_required_settings(self):
        """A fresh upgrade preserves legacy values and repairs invalid values 0020 can store."""
        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
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
        ImportProfile.objects.create(
            name="Legacy Blank Required Settings",
            sheet_name="",
            primary_contact_lookup_field="",
            preview_view_mode="",
        )

        migrated_apps = _migrate(SQUASHED)
        MigratedProfile = migrated_apps.get_model(APP, "ImportProfile")

        full = MigratedProfile.objects.get(name="Legacy Full")
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

        defaults = MigratedProfile.objects.get(name="Legacy Defaults")
        self.assertEqual(defaults.source_adapter, "flat_workbook")
        self.assertEqual(defaults.adapter_config["sheet_name"], "Data")
        self.assertTrue(defaults.adapter_config["update_existing"])
        self.assertEqual(defaults.adapter_config["primary_contact_lookup_field"], "email")
        self.assertIsNone(defaults.adapter_config["primary_contact_role"])

        blank = MigratedProfile.objects.get(name="Legacy Blank Required Settings")
        self.assertEqual(blank.adapter_config["sheet_name"], "Data")
        self.assertEqual(blank.adapter_config["primary_contact_lookup_field"], "email")
        self.assertEqual(blank.adapter_config["preview_view_mode"], "rows")

    def test_the_moved_columns_are_gone_afterwards(self):
        """No dual read path survives the squash."""
        _migrate(SQUASHED)
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

    def test_the_squash_refuses_a_rollback(self):
        """Neither frozen data transformation has a safe reverse operation."""
        _migrate(SQUASHED)

        with self.assertRaises(IrreversibleError):
            _migrate(BEFORE)

        migration = import_module(f"{APP}.migrations.{SQUASHED}").Migration
        replaced_steps = {name for app, name in migration.replaces if app == APP}
        self.assertTrue(replaced_steps.issubset(_applied_steps()))

    def test_the_squash_preserves_the_cutover_operation_order(self):
        """Data moves only after its targets exist and before its source columns disappear."""
        migration = import_module(f"{APP}.migrations.{SQUASHED}").Migration
        operations = migration.operations
        add_indices = {
            operation.name: index for index, operation in enumerate(operations) if isinstance(operation, AddField)
        }
        remove_indices = {
            operation.name: index for index, operation in enumerate(operations) if isinstance(operation, RemoveField)
        }
        data_indices = {
            operation.code.__name__: index
            for index, operation in enumerate(operations)
            if isinstance(operation, RunPython)
        }

        move_index = data_indices["move_columns_into_adapter_config"]
        repair_index = data_indices["repair_empty_required_settings"]
        self.assertEqual(set(add_indices), {"adapter_config", "source_adapter"})
        self.assertLess(max(add_indices.values()), move_index)
        self.assertEqual(set(remove_indices), set(REMOVED_FIELDS))
        self.assertLess(move_index, min(remove_indices.values()))
        self.assertLess(max(remove_indices.values()), repair_index)
        self.assertEqual(repair_index, len(operations) - 1)
        self.assertIsNone(operations[move_index].reverse_code)
        self.assertIsNone(operations[repair_index].reverse_code)

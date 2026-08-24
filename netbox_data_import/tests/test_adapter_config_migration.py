# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Run the Import Profile cutover against the real migration executor."""

from importlib import import_module

from django.db import connection
from django.db.migrations import AddField, RemoveField, RunPython
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

APP = "netbox_data_import"
BEFORE = "0020_migrate_import_source_custom_field"
ADD_CONFIG_FIELDS = "0021_importprofile_adapter_config"
MOVE_CONFIG_DATA = "0022_migrate_profile_adapter_config"
DROP_MOVED_COLUMNS = "0023_drop_moved_profile_columns"
LEAF = DROP_MOVED_COLUMNS

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


def _migration(step):
    """Return one Import Profile cutover migration class."""
    return import_module(f"{APP}.migrations.{step}").Migration


def _migrate(target, *, fake=False):
    """Migrate the plugin app to *target* and return the resulting historical app registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)], fake=fake)
    executor.loader.build_graph()
    return executor.loader.project_state([(APP, target)]).apps


def _rewind_to_before_the_cutover():
    """Reverse schema operations and fake only the two irreversible data operations."""
    _migrate(DROP_MOVED_COLUMNS, fake=True)
    _migrate(MOVE_CONFIG_DATA)
    _migrate(ADD_CONFIG_FIELDS, fake=True)
    return _migrate(BEFORE)


class ProfileAdapterConfigMigrationStructureTest(SimpleTestCase):
    """The generated schema steps and hand-written data steps stay separate and ordered."""

    def test_generated_schema_migration_contains_no_data_operations(self):
        """Keep generated schema operations separate from hand-written data operations."""
        for step in (ADD_CONFIG_FIELDS, DROP_MOVED_COLUMNS):
            operations = _migration(step).operations
            self.assertTrue(operations)
            self.assertFalse(any(isinstance(operation, RunPython) for operation in operations))

        operations = _migration(MOVE_CONFIG_DATA).operations
        self.assertTrue(operations)
        self.assertTrue(all(isinstance(operation, RunPython) for operation in operations))

    def test_the_cutover_preserves_the_operation_order(self):
        """Data moves only after its targets exist and before its source columns disappear."""
        add_operations = _migration(ADD_CONFIG_FIELDS).operations
        move_operations = _migration(MOVE_CONFIG_DATA).operations
        remove_operations = _migration(DROP_MOVED_COLUMNS).operations

        self.assertEqual(
            {operation.name for operation in add_operations if isinstance(operation, AddField)},
            {"adapter_config", "source_adapter"},
        )
        self.assertEqual(
            [operation.code.__name__ for operation in move_operations],
            ["move_columns_into_adapter_config"],
        )
        self.assertEqual(
            {operation.name for operation in remove_operations if isinstance(operation, RemoveField)},
            set(REMOVED_FIELDS),
        )
        self.assertIsNone(move_operations[0].reverse_code)
        self.assertIn((APP, ADD_CONFIG_FIELDS), _migration(MOVE_CONFIG_DATA).dependencies)
        self.assertIn((APP, MOVE_CONFIG_DATA), _migration(DROP_MOVED_COLUMNS).dependencies)


class ProfileAdapterConfigMigrationTest(TransactionTestCase):
    """The migration chain moves legacy settings, repairs them, and drops their columns."""

    def setUp(self):
        super().setUp()
        # A TransactionTestCase does not roll back schema changes, and a failure inside setUp
        # skips tearDown. Register before walking down, so a rewound worker always recovers.
        self.addCleanup(_migrate, LEAF)
        _rewind_to_before_the_cutover()

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

        migrated_apps = _migrate(LEAF)
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
        """No dual read path survives the cutover."""
        _migrate(LEAF)
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

    def test_the_cutover_refuses_a_rollback(self):
        """Neither frozen data transformation has a safe reverse operation."""
        _migrate(LEAF)

        with self.assertRaises(IrreversibleError):
            _migrate(BEFORE)

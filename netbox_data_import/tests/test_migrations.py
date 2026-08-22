# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Migration tests for identity constraints."""

from contextlib import contextmanager

from django.apps import apps
from django.db import connection
from django.db.migrations import Migration, RunPython
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DeviceExistingMatchConstraintMigrationTest(TransactionTestCase):
    """Verify that legacy duplicate bindings do not block an upgrade."""

    available_apps = ["netbox_data_import"]
    migrate_from = ("netbox_data_import", "0014_alter_columnmapping_target_field_and_more")
    migrate_to = ("netbox_data_import", "0016_deviceexistingmatch_ndi_devicematch_profile_device")
    # Django refuses to reverse these data migrations, so the walk back fakes each one, newest
    # first. A test-only Migration reverses the squash's real schema operations, then its recorder
    # state is faked down. Faking the older data-only migration is safe because it changes no schema.
    irreversible_data_steps = (
        ("0021_importprofile_adapter_config", "0020_migrate_import_source_custom_field"),
        ("0020_migrate_import_source_custom_field", "0019_deviceimportsource"),
    )
    squashed_data_and_schema_step = "0021_importprofile_adapter_config"

    @contextmanager
    def _migration_apps(self):
        """Expose dependency migrations while keeping teardown scoped to this plugin."""
        apps.unset_available_apps()
        try:
            yield
        finally:
            apps.set_available_apps(self.available_apps)

    def _reverse_squashed_schema_operations(self, executor, step, below):
        """Reverse the real schema operations without running either irreversible data operation."""
        squashed_migration = executor.loader.get_migration("netbox_data_import", step)
        data_operations = [operation for operation in squashed_migration.operations if isinstance(operation, RunPython)]
        self.assertEqual(len(data_operations), 2)

        schema_migration = Migration(step, "netbox_data_import")
        schema_migration.atomic = squashed_migration.atomic
        schema_migration.operations = [
            operation for operation in squashed_migration.operations if not isinstance(operation, RunPython)
        ]
        before_state = executor.loader.project_state([("netbox_data_import", below)])
        with connection.schema_editor(atomic=schema_migration.atomic) as schema_editor:
            schema_migration.unapply(before_state, schema_editor)

    def _unapply_the_irreversible_data_migrations(self):
        """
        Reverses migration state past irreversible data migrations while preserving their schema changes.
        
        The migrations are faked after any reversible schema operations have been unapplied, so historical data values are not reconstructed.
        """
        for step, below in self.irreversible_data_steps:
            MigrationExecutor(connection).migrate([("netbox_data_import", step)])
            if step == self.squashed_data_and_schema_step:
                executor = MigrationExecutor(connection)
                plan = executor.migration_plan([("netbox_data_import", below)])
                self.assertEqual([migration.name for migration, _backwards in plan], [step])
                self._reverse_squashed_schema_operations(executor, step, below)
                executor.migrate([("netbox_data_import", below)], fake=True)
                continue

            executor = MigrationExecutor(connection)
            plan = executor.migration_plan([("netbox_data_import", below)])
            self.assertEqual(
                [migration.name for migration, _backwards in plan],
                [step],
                "Only the irreversible data migration may be faked. A later migration needs a real reverse.",
            )
            executor.migrate([("netbox_data_import", below)], fake=True)

    def setUp(self):
        """
        Prepare the database with duplicate legacy device bindings for migration testing.
        
        Registers cleanup to restore the leaf migrations and removes the test profile afterward.
        """
        super().setUp()
        self.profile_pk = None
        # Register before the first walk down: a failure inside setUp skips tearDown, and a worker
        # left below the leaf fails every later test that reads a current column.
        self.addCleanup(self._restore_the_leaf_migrations)
        with self._migration_apps():
            self._unapply_the_irreversible_data_migrations()
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_from])
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            profile = old_apps.get_model("netbox_data_import", "ImportProfile").objects.create(
                name="Legacy Duplicate Binding Profile"
            )
            match_model = old_apps.get_model("netbox_data_import", "DeviceExistingMatch")
            match_model.objects.create(
                profile=profile,
                source_id="LEGACY-SOURCE-A",
                netbox_device_id=987654,
                device_name="legacy-device",
            )
            match_model.objects.create(
                profile=profile,
                source_id="LEGACY-SOURCE-B",
                netbox_device_id=987654,
                device_name="legacy-device",
            )
            self.profile_pk = profile.pk

    def _restore_the_leaf_migrations(self):
        """Restore the application to its leaf migrations and remove the profile created for the test."""
        with self._migration_apps():
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes("netbox_data_import"))
        if self.profile_pk is None:
            return
        from netbox_data_import.models import ImportProfile

        ImportProfile.objects.filter(pk=self.profile_pk).delete()

    def test_migration_removes_all_ambiguous_bindings(self):
        """
        Verify that migration removes ambiguous device bindings and logs their legacy source IDs.
        """
        with self._migration_apps():
            executor = MigrationExecutor(connection)

            with self.assertLogs(
                "netbox_data_import.migrations.0015_cleanup_duplicate_device_matches", level="WARNING"
            ) as logs:
                executor.migrate([self.migrate_to])

            migration_apps = executor.loader.project_state([self.migrate_to]).apps
            match_model = migration_apps.get_model("netbox_data_import", "DeviceExistingMatch")
            matches = list(
                match_model.objects.filter(
                    profile_id=self.profile_pk,
                    netbox_device_id=987654,
                )
            )
        self.assertEqual(matches, [])
        self.assertIn("LEGACY-SOURCE-A", logs.output[0])
        self.assertIn("LEGACY-SOURCE-B", logs.output[0])

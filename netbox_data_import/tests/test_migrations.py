# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Migration tests for identity constraints."""

from contextlib import contextmanager

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DeviceExistingMatchConstraintMigrationTest(TransactionTestCase):
    """Verify that legacy duplicate bindings do not block an upgrade."""

    available_apps = ["netbox_data_import"]
    migrate_from = ("netbox_data_import", "0014_alter_columnmapping_target_field_and_more")
    migrate_to = ("netbox_data_import", "0016_deviceexistingmatch_ndi_devicematch_profile_device")
    # Django refuses to reverse these data migrations, so the walk back fakes each one, newest
    # first. Faking is safe here because none changes the schema.
    irreversible_data_steps = (
        ("0024_repair_empty_primary_contact_lookup_field", "0023_drop_moved_profile_columns"),
        ("0022_migrate_profile_adapter_config", "0021_importprofile_adapter_config_and_more"),
        ("0020_migrate_import_source_custom_field", "0019_deviceimportsource"),
    )

    @contextmanager
    def _migration_apps(self):
        """Expose dependency migrations while keeping teardown scoped to this plugin."""
        apps.unset_available_apps()
        try:
            yield
        finally:
            apps.set_available_apps(self.available_apps)

    def _fake_unapply_the_irreversible_data_migrations(self):
        """Step past each data migration without reversing it: Django refuses, and data would go."""
        for step, below in self.irreversible_data_steps:
            MigrationExecutor(connection).migrate([("netbox_data_import", step)])
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan([("netbox_data_import", below)])
            self.assertEqual(
                [migration.name for migration, _backwards in plan],
                [step],
                "Only the irreversible data migration may be faked. A later migration needs a real reverse.",
            )
            executor.migrate([("netbox_data_import", below)], fake=True)

    def setUp(self):
        super().setUp()
        self.profile_pk = None
        # Register before the first walk down: a failure inside setUp skips tearDown, and a worker
        # left below the leaf fails every later test that reads a current column.
        self.addCleanup(self._restore_the_leaf_migrations)
        with self._migration_apps():
            self._fake_unapply_the_irreversible_data_migrations()
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
        """Walk back up to the leaf and drop the legacy profile the walk down created."""
        with self._migration_apps():
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        if self.profile_pk is None:
            return
        from netbox_data_import.models import ImportProfile

        ImportProfile.objects.filter(pk=self.profile_pk).delete()

    def test_migration_removes_all_ambiguous_bindings(self):
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

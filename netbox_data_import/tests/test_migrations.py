# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Migration tests for identity constraints."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DeviceExistingMatchConstraintMigrationTest(TransactionTestCase):
    """Verify that legacy duplicate bindings do not block an upgrade."""

    migrate_from = ("netbox_data_import", "0014_alter_columnmapping_target_field_and_more")
    migrate_to = ("netbox_data_import", "0016_deviceexistingmatch_ndi_devicematch_profile_device")

    def setUp(self):
        super().setUp()
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
        ).pk
        match_model.objects.create(
            profile=profile,
            source_id="LEGACY-SOURCE-B",
            netbox_device_id=987654,
            device_name="legacy-device",
        )
        self.profile_pk = profile.pk

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        from netbox_data_import.models import ImportProfile

        ImportProfile.objects.filter(pk=self.profile_pk).delete()
        super().tearDown()

    def _fixture_teardown(self):
        """Skip NetBox's global flush after explicit test-data cleanup."""

    def test_migration_removes_all_ambiguous_bindings(self):
        executor = MigrationExecutor(connection)

        executor.migrate([self.migrate_to])

        apps = executor.loader.project_state([self.migrate_to]).apps
        match_model = apps.get_model("netbox_data_import", "DeviceExistingMatch")
        matches = list(
            match_model.objects.filter(
                profile_id=self.profile_pk,
                netbox_device_id=987654,
            )
        )
        self.assertEqual(matches, [])

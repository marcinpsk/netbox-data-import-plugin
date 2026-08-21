# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Run the ordered Import Profile cutover against the real migration executor."""

from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

APP = "netbox_data_import"
BEFORE = "0021_importprofile_adapter_config_and_more"
DATA_STEP = "0022_migrate_profile_adapter_config"
AFTER = "0023_drop_moved_profile_columns"
REPAIR_STEP = "0024_repair_empty_primary_contact_lookup_field"

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


def _applied_steps():
    """Return the plugin migrations the recorder currently holds, in order."""
    return sorted(name for app, name in MigrationRecorder(connection).applied_migrations() if app == APP)


def _rewind_to_before_the_data_step():
    """Restore the moved columns, then forget the data step so the executor runs it again.

    Unapplying 0023 is a real schema rollback. The data step has no reverse, so only its recorder
    row is dropped: nothing it wrote is undone, and the columns it read are back.
    """
    apps = _migrate(DATA_STEP)
    MigrationRecorder(connection).record_unapplied(APP, DATA_STEP)
    return apps


def _rewind_to_before_the_repair_step():
    """Forget only the repair recorder row so its irreversible data operation runs again."""
    apps = _migrate(REPAIR_STEP)
    MigrationRecorder(connection).record_unapplied(APP, REPAIR_STEP)
    return apps


class ProfileAdapterConfigMigrationTest(TransactionTestCase):
    """The three ordered steps move every column and drop the originals."""

    def setUp(self):
        super().setUp()
        # A TransactionTestCase does not roll back schema changes, and a failure inside setUp
        # skips tearDown. Register before anything migrates, so a rewound worker always recovers.
        self.addCleanup(_migrate, REPAIR_STEP)
        # The repair is data-only and irreversible. Forget its recorder row before walking down to
        # the older cutover, then let cleanup reapply it at the leaf.
        _migrate(REPAIR_STEP)
        MigrationRecorder(connection).record_unapplied(APP, REPAIR_STEP)

    def test_it_moves_every_column_for_every_existing_profile(self):
        """The data step stamps the adapter key and copies each declared column."""
        apps = _rewind_to_before_the_data_step()
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

    def test_the_data_step_refuses_a_rollback(self):
        """A renamed or deleted ContactRole cannot be resolved back to its id, so no reverse exists."""
        _migrate(AFTER)

        with self.assertRaises(IrreversibleError):
            _migrate(BEFORE)

        # 0023 reverses cleanly and comes off first, so the run stops on the data step. Pinning
        # that keeps a wider partial rollback from surfacing as an unrelated failure later.
        self.assertEqual(_applied_steps()[-1], DATA_STEP)

    def test_the_data_step_carries_data_operations_only(self):
        """The middle step never touches the schema."""
        from importlib import import_module

        from django.db.migrations import RunPython

        migration = import_module(f"{APP}.migrations.{DATA_STEP}").Migration
        self.assertTrue(migration.operations)
        for operation in migration.operations:
            self.assertIsInstance(operation, RunPython)


class EmptyPrimaryContactLookupMigrationTest(TransactionTestCase):
    """The repair freezes the default only for the two invalid stored values."""

    def setUp(self):
        super().setUp()
        self.addCleanup(_migrate, REPAIR_STEP)

    def test_it_repairs_only_explicitly_empty_flat_workbook_lookup_fields(self):
        apps = _rewind_to_before_the_repair_step()
        ImportProfile = apps.get_model(APP, "ImportProfile")
        fixtures = {
            "Blank Lookup": {"primary_contact_lookup_field": ""},
            "Null Lookup": {"primary_contact_lookup_field": None},
            "Missing Lookup": {"sheet_name": "Data"},
            "Email Lookup": {"primary_contact_lookup_field": "email"},
            "Name Lookup": {"primary_contact_lookup_field": "name"},
        }
        for name, adapter_config in fixtures.items():
            ImportProfile.objects.create(
                name=name,
                source_adapter="flat_workbook",
                adapter_config=adapter_config,
            )
        ImportProfile.objects.create(
            name="Trace Blank Lookup",
            source_adapter="trace_workbook",
            adapter_config={"primary_contact_lookup_field": ""},
        )

        migrated_apps = _migrate(REPAIR_STEP)
        MigratedProfile = migrated_apps.get_model(APP, "ImportProfile")
        migrated = dict(MigratedProfile.objects.values_list("name", "adapter_config"))

        self.assertEqual(migrated["Blank Lookup"], {"primary_contact_lookup_field": "email"})
        self.assertEqual(migrated["Null Lookup"], {"primary_contact_lookup_field": "email"})
        self.assertEqual(migrated["Missing Lookup"], {"sheet_name": "Data"})
        self.assertEqual(migrated["Email Lookup"], {"primary_contact_lookup_field": "email"})
        self.assertEqual(migrated["Name Lookup"], {"primary_contact_lookup_field": "name"})
        self.assertEqual(migrated["Trace Blank Lookup"], {"primary_contact_lookup_field": ""})

    def test_the_repair_refuses_a_rollback(self):
        _migrate(REPAIR_STEP)

        with self.assertRaises(IrreversibleError):
            _migrate(AFTER)

        self.assertEqual(_applied_steps()[-1], REPAIR_STEP)

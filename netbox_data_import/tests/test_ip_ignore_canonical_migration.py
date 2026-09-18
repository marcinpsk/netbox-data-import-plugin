# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""An ignored IP difference keeps applying after the preview changed how it compares an address.

The preview used to canonicalize `198.18.0.10` as `198.18.0.10/32`, and an ignored difference stores
that spelling. It now compares on the host, the way the writer matches a held address. Without this
migration every stored IP ignore stops matching and the field reads as actionable again.
"""

from importlib import import_module

from django.db import connection
from django.db.migrations import RunPython
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from netbox_data_import.tests.helpers import restore_plugin_migrations

APP = "netbox_data_import"
BEFORE = "0035_retire_superseded_proposals"
IP_IGNORE_HOSTS = "0036_ip_ignore_canonical_hosts"


def _migration(step):
    """Return one migration class by its step name."""
    return import_module(f"{APP}.migrations.{step}").Migration


def _migrate(target, *, fake=False):
    """Migrate the plugin app to one target and return its historical app registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)], fake=fake)
    executor.loader.build_graph()
    return executor.loader.project_state([(APP, target)]).apps


class IpIgnoreCanonicalStructureTest(SimpleTestCase):
    """The authored data migration is ordered and can be rolled back."""

    def test_the_rewrite_is_reversible_and_follows_the_previous_step(self):
        operation = _migration(IP_IGNORE_HOSTS).operations[0]

        self.assertIsInstance(operation, RunPython)
        self.assertEqual(operation.code.__name__, "to_host")
        # The display value keeps the original spelling, so the rollback is not lossy.
        self.assertEqual(operation.reverse_code.__name__, "to_interface")
        self.assertTrue(operation.reversible)
        self.assertIn((APP, BEFORE), _migration(IP_IGNORE_HOSTS).dependencies)


class IpIgnoreCanonicalMigrationTest(TransactionTestCase):
    """Only an IP field's canonical is rewritten, and the row still matches the new preview."""

    def setUp(self):
        super().setUp()
        self.addCleanup(restore_plugin_migrations)
        _migrate(BEFORE, fake=True)

    def _seed(self, apps):
        """Store one ignored difference per field kind the upgrade can meet."""
        ImportProfile = apps.get_model(APP, "ImportProfile")
        IgnoredFieldDifference = apps.get_model(APP, "IgnoredFieldDifference")

        profile = ImportProfile.objects.create(name="Ignore Canonical Profile", adapter_config={})
        rows = (
            ("primary_ip4", "198.18.0.20/32", "198.18.0.20", "198.18.0.19/24", "198.18.0.19/24"),
            ("primary_ip6", "2001:db8::1/128", "2001:db8::1", "2001:db8::2/64", "2001:db8::2/64"),
            ("oob_ip", "198.18.9.1/32", "198.18.9.1", "198.18.9.2/24", "198.18.9.2/24"),
            ("serial", "SER-1", "SER-1", "SER-2", "SER-2"),
        )
        for index, (field, file_canonical, file_display, nb_canonical, nb_display) in enumerate(rows):
            IgnoredFieldDifference.objects.create(
                profile=profile,
                source_id=f"ROW-{index}",
                netbox_device_id=index + 1,
                target_field=field,
                file_snapshot={"canonical": file_canonical, "display": file_display},
                netbox_snapshot={"canonical": nb_canonical, "display": nb_display},
            )

    def _canonicals(self, apps):
        """Return each stored pair of canonicals by target field."""
        Ignored = apps.get_model(APP, "IgnoredFieldDifference")
        return {
            record.target_field: (record.file_snapshot["canonical"], record.netbox_snapshot["canonical"])
            for record in Ignored.objects.all()
        }

    def test_every_ip_canonical_becomes_its_host_and_other_fields_are_untouched(self):
        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        self._seed(apps)

        migrated = _migrate(IP_IGNORE_HOSTS)

        self.assertEqual(
            self._canonicals(migrated),
            {
                "primary_ip4": ("198.18.0.20", "198.18.0.19"),
                "primary_ip6": ("2001:db8::1", "2001:db8::2"),
                "oob_ip": ("198.18.9.1", "198.18.9.2"),
                "serial": ("SER-1", "SER-2"),
            },
        )

    def test_the_rewritten_row_matches_what_the_preview_now_computes(self):
        """The migration is only correct if its output equals the live normalization."""
        from netbox_data_import.device_field_review import _ip_normalize

        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        self._seed(apps)

        migrated = _migrate(IP_IGNORE_HOSTS)
        stored = self._canonicals(migrated)

        self.assertEqual(stored["primary_ip4"][0], _ip_normalize("198.18.0.20"))
        self.assertEqual(stored["primary_ip4"][1], _ip_normalize("198.18.0.19/24"))
        self.assertEqual(stored["primary_ip6"][1], _ip_normalize("2001:db8::2/64"))

    def test_the_rollback_restores_the_host_and_prefix_spelling(self):
        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        self._seed(apps)
        _migrate(IP_IGNORE_HOSTS)

        rolled_back = _migrate(BEFORE)

        self.assertEqual(
            self._canonicals(rolled_back),
            {
                "primary_ip4": ("198.18.0.20/32", "198.18.0.19/24"),
                "primary_ip6": ("2001:db8::1/128", "2001:db8::2/64"),
                "oob_ip": ("198.18.9.1/32", "198.18.9.2/24"),
                "serial": ("SER-1", "SER-2"),
            },
        )

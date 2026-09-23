# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Migration tests for identity constraints."""

import ast
import tokenize

from contextlib import contextmanager
from pathlib import Path

from django.apps import apps
from django.db import connection
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase, TransactionTestCase

APP = "netbox_data_import"
_DEPENDENCY_COMMENT_EXCEPTIONS = frozenset(
    {
        "0001_initial",
        "0022_migrate_profile_adapter_config",
        "0031_inferencebackend",
        "0036_resolutionproposal_job",
    }
)


def _migrations_with_dependency_comments():
    """Return migrations with comments inside the generated dependency declaration."""
    migrations = Path(__file__).parents[1] / "migrations"
    found = set()
    for path in migrations.glob("[0-9]*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        migration_class = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Migration"
        )
        dependencies = next(
            node
            for node in migration_class.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "dependencies" for target in node.targets)
        )
        tokens = tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__)
        if any(
            token.type == tokenize.COMMENT and dependencies.lineno <= token.start[0] <= dependencies.end_lineno
            for token in tokens
        ):
            found.add(path.stem)
    return found


def upgrade_note_sql(heading: str) -> list[str]:
    """Return the SQL blocks one installation upgrade note tells an operator to run."""
    notes = (Path(__file__).parents[2] / "docs" / "installation.md").read_text(encoding="utf-8")
    section = notes.split(f"### {heading}\n", 1)[1].split("\n### ", 1)[0]
    return [block.split("```", 1)[0] for block in section.split("```sql\n")[1:]]


class GeneratedMigrationDependencyTest(SimpleTestCase):
    def test_only_approved_compatibility_migrations_have_dependency_comments(self):
        self.assertEqual(_migrations_with_dependency_comments(), _DEPENDENCY_COMMENT_EXCEPTIONS)


class DeviceExistingMatchConstraintMigrationTest(TransactionTestCase):
    """Verify that legacy duplicate bindings do not block an upgrade."""

    available_apps = ["netbox_data_import"]
    migrate_from = ("netbox_data_import", "0014_alter_columnmapping_target_field_and_more")
    migrate_to = ("netbox_data_import", "0016_deviceexistingmatch_ndi_devicematch_profile_device")
    # Django refuses to reverse these data migrations, so the walk back fakes each one, newest
    # first. The generated schema migrations between them still run their real reverse operations.
    irreversible_data_steps = (
        ("0039_remove_job_plan_copies", "0038_cable_tag_integrity"),
        ("0035_retire_superseded_proposals", "0034_tracedeviceresolution"),
        ("0022_migrate_profile_adapter_config", "0021_importprofile_adapter_config"),
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

    def _unapply_the_irreversible_data_migrations(self):
        """Step past each data operation without attempting to reconstruct its old values."""
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
        """Walk back up to the leaf and drop the legacy profile the walk down created."""
        with self._migration_apps():
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes("netbox_data_import"))
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


class MigrationGraphDescribesTheModelsTest(SimpleTestCase):
    """The committed migrations must fully describe the models, as `makemigrations --check` asks."""

    def test_no_model_change_is_missing_a_migration(self):
        """A stale migration silently drops field state that the autodetector still sees."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        autodetector = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(apps),
            NonInteractiveMigrationQuestioner(specified_apps={APP}, dry_run=True, verbosity=0),
        )
        guidance = "Run `netbox-manage makemigrations netbox_data_import` and commit the result."
        try:
            changes = autodetector.changes(graph=loader.graph, trim_to_apps={APP}, convert_apps={APP})
        except SystemExit:
            self.fail(guidance)
        described = [
            f"{migration.app_label}: {operation.describe()}"
            for migration in changes.get(APP, [])
            for operation in migration.operations
        ]
        self.assertEqual(described, [], guidance)


#: Django resolves these itself; they are never literal graph nodes.
_SENTINELS = frozenset({"__first__", "__latest__"})

#: A core app whose graph is known good, so the check is observed to pass and not merely to fail.
_CONTROL_APP = "ipam"


def _reachable_pins(disk, app_label, name, other_app, seen=None):
    """Return every live node of `other_app` this migration reaches through its own app's chain."""
    seen = seen or set()
    reached = []
    for parent in disk[(app_label, name)].dependencies:
        if parent[0] == other_app and parent in disk:
            reached.append(parent[1])
        elif parent[0] == app_label and parent[1] not in seen and parent in disk:
            seen.add(parent[1])
            reached.extend(_reachable_pins(disk, app_label, parent[1], other_app, seen))
    return reached


def _newest_live_ancestor_pin(disk, app_label, name, other_app):
    """Return the newest node of `other_app` this migration already reaches, or None."""
    return max(_reachable_pins(disk, app_label, name, other_app), default=None)


def _dangling_reports(disk, app_label):
    """Return one report per dangling cross-app dependency in `disk`, with what decides the fix.

    `initial` and the newest same-app ancestor pin are what separate the three remediations: a
    migration that already reaches a newer node of that app can drop the edge, while an initial one
    whose only edge this is has to keep the ordering through the `__first__` sentinel. Reaching is
    transitive, so an ancestor in this app's own chain counts.

    Takes the mapping rather than reading one, so a test can state a graph and watch this report it.
    """
    missing = []
    for key in sorted(node for node in disk if node[0] == app_label):
        migration = disk[key]
        for parent in migration.dependencies:
            if parent[0] == "__setting__" or parent[1] in _SENTINELS:
                continue
            if parent[0] != app_label and parent not in disk:
                ancestor = _newest_live_ancestor_pin(disk, app_label, key[1], parent[0])
                missing.append(
                    f"{key[1]} -> {parent[0]}.{parent[1]} "
                    f"(initial={bool(getattr(migration, 'initial', False))}, "
                    f"newest live {parent[0]} ancestor={ancestor or 'none'})"
                )
    return missing


def _unresolved_dependencies(app_label):
    """Return the dangling cross-app dependencies this app ships on disk."""
    # `load=False` matters: `build_graph()` validates every installed app, not just this one.
    loader = MigrationLoader(None, load=False)
    loader.load_disk()
    return _dangling_reports(loader.disk_migrations, app_label)


class MigrationGraphResolvesWithoutReplacementTest(SimpleTestCase):
    """Every cross-app dependency must resolve without squash replacement.

    `migrate` builds the graph with `replace_migrations=True`, so a squash's `replaces` list remaps a
    dependency on a migration NetBox has since squashed away and the graph still builds. The suite,
    test-database creation and `migrate --plan` are therefore all green while the node is genuinely
    absent; only a graph built without replacement, which is what `sqlmigrate` uses, shows it. It
    becomes a live failure the moment NetBox drops that `replaces` list.
    """

    def test_no_dependency_needs_squash_replacement(self):
        """Scoped to this app: a co-installed plugin's defect must not fail us.

        The graph raises on the first dangling node it validates, so an unscoped check reports
        whichever sibling plugin happens to be broken and every app looks broken.
        """
        missing = _unresolved_dependencies(APP)

        self.assertEqual(
            missing,
            [],
            "These dependencies only resolve because a squash remaps them, so they break as soon as "
            "NetBox drops its `replaces` list: " + "; ".join(missing),
        )

    def test_the_check_can_report_success(self):
        """A known-good app comes back clean, so a false positive would show here."""
        self.assertEqual(_unresolved_dependencies(_CONTROL_APP), [])

    def test_the_check_reports_a_dangling_edge_it_is_given(self):
        """Both assertions above are `== []`, which a check that always returned [] would pass.

        Only this one exercises detection, so it is what stops the guard from going quietly blind.
        """
        squash = Migration("0001_squashed", "extras")
        first = Migration("0001_initial", APP)
        first.initial = True
        first.dependencies = [("extras", "0001_squashed")]
        later = Migration("0002_later", APP)
        later.dependencies = [(APP, "0001_initial"), ("extras", "9999_absent")]

        reports = _dangling_reports(
            {("extras", "0001_squashed"): squash, (APP, "0001_initial"): first, (APP, "0002_later"): later},
            APP,
        )

        self.assertEqual(
            reports, ["0002_later -> extras.9999_absent (initial=False, newest live extras ancestor=0001_squashed)"]
        )


class CableTagIntegrityMigrationTest(TransactionTestCase):
    """The Cable tag migration preserves existing associations and rejects orphans."""

    def test_reverse_preserves_tag_rows_and_upgrade_backfills_cable_ids(self):
        from netbox_data_import.tests.test_cable_module import CableTopologyMixin
        from extras.models import Tag, TaggedItem

        topology = CableTopologyMixin()
        topology.build_topology()
        cable = topology.connect(topology.eth0, topology.eth1)
        tag = Tag.objects.create(name="Upgrade", slug="upgrade")
        cable.tags.add(tag)
        tagged_item = TaggedItem.objects.get(tag=tag, object_id=cable.pk)
        previous = (APP, "0037_cablesegmentoverride")
        leaf = (APP, "0038_cable_tag_integrity")
        final = (APP, "0039_remove_job_plan_copies")
        self.addCleanup(lambda: MigrationExecutor(connection).migrate([final]))

        MigrationExecutor(connection).migrate([leaf], fake=True)
        MigrationExecutor(connection).migrate([previous])

        self.assertTrue(TaggedItem.objects.filter(pk=tagged_item.pk).exists())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'extras_taggeditem' AND column_name = 'ndi_cable_id'"
            )
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname IN "
                "('ndi_derive_cable_tag_id', 'ndi_guard_cable_content_type_identity')"
            )
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute(
                "SELECT count(*) FROM pg_proc WHERE proname IN ('ndi_set_cable_tag_id', 'ndi_guard_cable_content_type')"
            )
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute("SELECT count(*) FROM pg_constraint WHERE conname = 'ndi_taggeditem_cable_fk'")
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'ndi_taggeditem_cable_id'")
            self.assertEqual(cursor.fetchone()[0], 0)

        MigrationExecutor(connection).migrate([leaf])

        with connection.cursor() as cursor:
            cursor.execute("SELECT ndi_cable_id FROM extras_taggeditem WHERE id = %s", [tagged_item.pk])
            self.assertEqual(cursor.fetchone()[0], cable.pk)

    def test_orphan_cable_tag_refuses_upgrade_without_partial_schema(self):
        from core.models import ObjectType
        from dcim.models import Cable
        from django.db import IntegrityError
        from extras.models import Tag, TaggedItem

        previous = (APP, "0037_cablesegmentoverride")
        leaf = (APP, "0038_cable_tag_integrity")
        final = (APP, "0039_remove_job_plan_copies")
        orphan_pk = None

        def restore_leaf():
            if orphan_pk is not None:
                TaggedItem.objects.filter(pk=orphan_pk).delete()
            MigrationExecutor(connection).migrate([final])

        self.addCleanup(restore_leaf)
        tag = Tag.objects.create(name="Orphan upgrade", slug="orphan-upgrade")
        cable_type = ObjectType.objects.get_for_model(Cable)
        MigrationExecutor(connection).migrate([leaf], fake=True)
        MigrationExecutor(connection).migrate([previous])
        orphan_pk = TaggedItem.objects.create(tag=tag, content_type=cable_type, object_id=2_147_483_647).pk

        with self.assertRaises(IntegrityError) as raised:
            MigrationExecutor(connection).migrate([leaf])

        self.assertEqual(getattr(raised.exception.__cause__, "sqlstate", None), "23503")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'extras_taggeditem' AND column_name = 'ndi_cable_id'"
            )
            self.assertEqual(cursor.fetchone()[0], 0)
        self.assertTrue(TaggedItem.objects.filter(pk=orphan_pk).exists())

        # The upgrade note is what an operator runs, so the documented SQL itself must clear the way.
        find, delete = upgrade_note_sql("Cable tag integrity")
        with connection.cursor() as cursor:
            cursor.execute(find)
            self.assertEqual([row[0] for row in cursor.fetchall()], [orphan_pk])
            cursor.execute(delete)
        MigrationExecutor(connection).migrate([leaf])
        self.assertFalse(TaggedItem.objects.filter(pk=orphan_pk).exists())

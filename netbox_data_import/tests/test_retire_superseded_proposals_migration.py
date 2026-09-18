# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A proposal queued under the previous request contract is retired by the upgrade, not by a worker.

Release 2.3.0 shipped `prompt_version` 1 and `response_schema_version` 1. Raising either one without
retiring the stored requests below it leaves a queued proposal that no worker can answer, so each
bump owes a migration that retires them.
"""

import ast
import pathlib

from importlib import import_module

from django.db import connection
from django.db.migrations import RunPython
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from netbox_data_import.proposal_contract import RESPONSE_SCHEMA_VERSION
from netbox_data_import.proposal_jobs import PROMPT_VERSION
from netbox_data_import.tests.helpers import restore_plugin_migrations

APP = "netbox_data_import"
MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
BEFORE = "0034_tracedeviceresolution"
RETIRE_SUPERSEDED_PROPOSALS = "0035_retire_superseded_proposals"


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


class RetireSupersededProposalsStructureTest(SimpleTestCase):
    """The authored data migration is ordered and refuses a lossy rollback."""

    def test_the_retirement_refuses_to_reverse(self):
        operation = _migration(RETIRE_SUPERSEDED_PROPOSALS).operations[0]

        self.assertIsInstance(operation, RunPython)
        self.assertEqual(operation.code.__name__, "retire_superseded_proposals")
        # A noop reverse would report a successful rollback while every row stayed failed.
        self.assertIsNone(operation.reverse_code)
        self.assertFalse(operation.reversible)
        self.assertIn((APP, BEFORE), _migration(RETIRE_SUPERSEDED_PROPOSALS).dependencies)


class RetireSupersededProposalsMigrationTest(TransactionTestCase):
    """Only an in-flight proposal below the current contract is retired."""

    def setUp(self):
        super().setUp()
        self.addCleanup(restore_plugin_migrations)
        _migrate(BEFORE, fake=True)

    def _seed(self, apps):
        """Create one proposal per (status, contract version) combination the upgrade can meet."""
        ImportProfile = apps.get_model(APP, "ImportProfile")
        ResolutionProposal = apps.get_model(APP, "ResolutionProposal")
        ObjectType = apps.get_model("core", "ObjectType")

        profile = ImportProfile.objects.create(name="Proposal Profile", adapter_config={})
        device_type = ObjectType.objects.get(app_label="dcim", model="device")
        rows = (
            ("queued-v1", "queued", 1, 1, ""),
            ("running-v1", "running", 1, 1, ""),
            ("queued-mixed", "queued", 1, 2, ""),
            ("queued-v2", "queued", 2, 2, ""),
            ("running-v2", "running", 2, 2, ""),
            ("failed-v1", "failed", 1, 1, "timeout"),
            ("cancelled-v1", "cancelled", 1, 1, ""),
        )
        for key, status, prompt_version, schema_version, failure_reason in rows:
            ResolutionProposal.objects.create(
                profile=profile,
                task_type="select_termination",
                field_key=key,
                field_key_digest=key,
                status=status,
                failure_reason=failure_reason,
                source_evidence={},
                resolved_device_type=device_type,
                resolved_device_id=1,
                prompt_version=prompt_version,
                response_schema_version=schema_version,
                candidate_snapshot={},
            )

    def test_only_in_flight_rows_below_the_current_contract_are_retired(self):
        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        self._seed(apps)

        migrated = _migrate(RETIRE_SUPERSEDED_PROPOSALS)
        Migrated = migrated.get_model(APP, "ResolutionProposal")

        self.assertEqual(
            sorted(Migrated.objects.values_list("field_key", "status", "failure_reason")),
            [
                ("cancelled-v1", "cancelled", ""),
                ("failed-v1", "failed", "timeout"),
                ("queued-mixed", "failed", "superseded_request"),
                ("queued-v1", "failed", "superseded_request"),
                ("queued-v2", "queued", ""),
                ("running-v1", "failed", "superseded_request"),
                ("running-v2", "running", ""),
            ],
        )

    def test_a_retired_row_frees_the_one_active_slot_it_held(self):
        """The operator has to be able to ask again for the same field after the upgrade."""
        apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        self._seed(apps)

        migrated = _migrate(RETIRE_SUPERSEDED_PROPOSALS)
        Migrated = migrated.get_model(APP, "ResolutionProposal")
        retired = Migrated.objects.get(field_key="queued-v1")

        Migrated.objects.create(
            profile_id=retired.profile_id,
            task_type=retired.task_type,
            field_key=retired.field_key,
            field_key_digest=retired.field_key_digest,
            status="queued",
            source_evidence={},
            resolved_device_type_id=retired.resolved_device_type_id,
            resolved_device_id=retired.resolved_device_id,
            prompt_version=2,
            response_schema_version=2,
            candidate_snapshot={},
        )

        self.assertEqual(Migrated.objects.filter(field_key="queued-v1", status="queued").count(), 1)


CONTRACT_CONSTANTS = ("PROMPT_VERSION", "RESPONSE_SCHEMA_VERSION")


def _declared_contract(path):
    """Return the contract pair one migration retires up to, or None when it retires none."""
    declared = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in CONTRACT_CONSTANTS:
                declared[target.id] = node.value.value
    if len(declared) != len(CONTRACT_CONSTANTS):
        return None
    return tuple(declared[name] for name in CONTRACT_CONSTANTS)


class ProposalContractRetirementTest(SimpleTestCase):
    """Every shipped contract version has a migration that retires the requests below it."""

    def test_the_retirement_migrations_reach_the_current_contract(self):
        retired = [pair for pair in map(_declared_contract, sorted(MIGRATIONS.glob("0*.py"))) if pair is not None]

        self.assertTrue(retired, "no migration retires superseded proposals")
        self.assertEqual(
            max(retired),
            (PROMPT_VERSION, RESPONSE_SCHEMA_VERSION),
            "a contract bump needs a migration retiring the proposals queued under the previous contract",
        )

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A proposal queued under the previous request contract is retired by the upgrade, not by a worker.

Release 2.3.0 shipped `prompt_version` 1 and `response_schema_version` 1. Raising either one without
retiring the stored requests below it leaves a queued proposal that no worker can answer, so each
bump owes a migration that retires them. Every test here runs for each migration that declares the
contract pair it retires up to, so a new retirement migration is tested without a new test.
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
CONTRACT_CONSTANTS = ("PROMPT_VERSION", "RESPONSE_SCHEMA_VERSION")
IN_FLIGHT = ("queued", "running")


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


RETIREMENTS = tuple(
    (path.stem, contract)
    for path in sorted(MIGRATIONS.glob("0*.py"))
    if (contract := _declared_contract(path)) is not None
)


def _migration(step):
    """Return one migration class by its step name."""
    return import_module(f"{APP}.migrations.{step}").Migration


def _previous_step(step):
    """Return the one plugin migration that *step* depends on."""
    (previous,) = [name for app, name in _migration(step).dependencies if app == APP]
    return previous


def _migrate(target, *, fake=False):
    """Migrate the plugin app to one target and return its historical app registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)], fake=fake)
    executor.loader.build_graph()
    return executor.loader.project_state([(APP, target)]).apps


def _rows(contract):
    """Return one row per (status, contract part) shape the upgrade can meet, each version 1 or more."""
    prompt, schema = contract
    rows = (
        ("current-queued", "queued", prompt, schema, ""),
        ("current-running", "running", prompt, schema, ""),
        ("prompt-below", "queued", prompt - 1, schema, ""),
        ("schema-below", "running", prompt, schema - 1, ""),
        ("both-below", "queued", prompt - 1, schema - 1, ""),
        ("failed-below", "failed", prompt - 1, schema - 1, "timeout"),
        ("cancelled-below", "cancelled", prompt - 1, schema - 1, ""),
    )
    return tuple(row for row in rows if min(row[2], row[3]) >= 1)


def _expected(rows, contract):
    """Return the stored state the upgrade must leave for each seeded row."""
    return sorted(
        (key, "failed", "superseded_request")
        if status in IN_FLIGHT and (prompt, schema) != contract
        else (key, status, failure_reason)
        for key, status, prompt, schema, failure_reason in rows
    )


class RetirementStructureTest(SimpleTestCase):
    """Each retirement migration follows one plugin migration and refuses a lossy rollback."""

    def test_every_retirement_is_one_irreversible_run_python(self):
        self.assertTrue(RETIREMENTS, "no migration retires superseded proposals")
        for step, _contract in RETIREMENTS:
            with self.subTest(step=step):
                (operation,) = _migration(step).operations

                self.assertIsInstance(operation, RunPython)
                # A noop reverse would report a successful rollback while every row stayed failed.
                self.assertIsNone(operation.reverse_code)
                self.assertFalse(operation.reversible)
                _previous_step(step)


class RetirementMigrationTest(TransactionTestCase):
    """Each upgrade retires only the in-flight requests below the contract it declares."""

    def setUp(self):
        super().setUp()
        self.addCleanup(restore_plugin_migrations)

    def test_each_retirement_fails_only_in_flight_rows_below_either_contract_part(self):
        for step, contract in RETIREMENTS:
            with self.subTest(step=step, contract=contract):
                previous = _previous_step(step)
                _migrate(previous, fake=True)
                apps = MigrationExecutor(connection).loader.project_state([(APP, previous)]).apps
                rows = _rows(contract)
                self._seed(apps, rows)

                Migrated = _migrate(step).get_model(APP, "ResolutionProposal")

                self.assertEqual(
                    sorted(Migrated.objects.values_list("field_key", "status", "failure_reason")),
                    _expected(rows, contract),
                )
                self._ask_again(Migrated, "prompt-below", contract)

    def _seed(self, apps, rows):
        """Replace every stored proposal with *rows*, under one profile."""
        ImportProfile = apps.get_model(APP, "ImportProfile")
        ResolutionProposal = apps.get_model(APP, "ResolutionProposal")
        ObjectType = apps.get_model("core", "ObjectType")
        ResolutionProposal.objects.all().delete()
        ImportProfile.objects.all().delete()
        profile = ImportProfile.objects.create(name="Proposal Profile", adapter_config={})
        device_type = ObjectType.objects.get(app_label="dcim", model="device")
        for key, status, prompt, schema, failure_reason in rows:
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
                prompt_version=prompt,
                response_schema_version=schema,
                candidate_snapshot={},
            )

    def _ask_again(self, Migrated, key, contract):
        """Assert that a retired row frees the one active slot it held, so the operator can ask again."""
        retired = Migrated.objects.get(field_key=key)
        prompt, schema = contract

        Migrated.objects.create(
            profile_id=retired.profile_id,
            task_type=retired.task_type,
            field_key=retired.field_key,
            field_key_digest=retired.field_key_digest,
            status="queued",
            source_evidence={},
            resolved_device_type_id=retired.resolved_device_type_id,
            resolved_device_id=retired.resolved_device_id,
            prompt_version=prompt,
            response_schema_version=schema,
            candidate_snapshot={},
        )

        self.assertEqual(Migrated.objects.filter(field_key=key, status="queued").count(), 1)


class ProposalContractRetirementTest(SimpleTestCase):
    """Every shipped contract version has a migration that retires the requests below it."""

    def test_the_retirement_migrations_reach_the_current_contract(self):
        self.assertTrue(RETIREMENTS, "no migration retires superseded proposals")
        self.assertEqual(
            max(contract for _step, contract in RETIREMENTS),
            (PROMPT_VERSION, RESPONSE_SCHEMA_VERSION),
            "a contract bump needs a migration retiring the proposals queued under the previous contract",
        )

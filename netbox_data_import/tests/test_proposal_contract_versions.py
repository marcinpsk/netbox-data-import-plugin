# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Bumping a request contract strands every proposal queued under the previous one.

Release 2.3.0 shipped `prompt_version` 1 and `response_schema_version` 1. Raising either one
without retiring the stored requests below it leaves a queued proposal that no worker can answer,
so each bump owes a migration that retires them.
"""

import ast
import pathlib

from django.test import SimpleTestCase

from netbox_data_import.proposal_contract import RESPONSE_SCHEMA_VERSION
from netbox_data_import.proposal_jobs import PROMPT_VERSION

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
CONTRACT_CONSTANTS = ("PROMPT_VERSION", "RESPONSE_SCHEMA_VERSION")


def _declared_contract(path):
    """Return the contract pair one migration retires up to, or None when it retires none."""
    declared = {}
    for node in ast.parse(path.read_text()).body:
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

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every workspace writer that takes the profile policy lock compares the reviewed fingerprint inside it.

Specification section 10.2: a preview revision is per session, so only the profile fingerprint
under the lock shows that another operator moved the policy. Two writers once took the lock and
skipped the comparison, so a second session silently replaced the first session's decision.
"""

import ast
import pathlib

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
WRITER_MODULES = ("review_workspace.py", "proposal_decisions.py")
LOCK = "locked_profile_policy"
REFUSAL = "refuse_moved_policy"

# Writer function -> why it takes the lock without comparing the reviewed fingerprint.
EXEMPT_WRITERS = {
    "reject_proposal": "A rejection writes no profile policy and no Row Resolution (specification 7.6).",
}


def _name(node) -> str | None:
    """Return the called name of a bare or attribute call target."""
    return getattr(node, "id", None) or getattr(node, "attr", None)


def _takes_lock(node) -> bool:
    """Return whether one `with` statement opens the profile policy lock."""
    return isinstance(node, ast.With) and any(
        isinstance(item.context_expr, ast.Call) and _name(item.context_expr.func) == LOCK for item in node.items
    )


def _calls_refusal(body) -> bool:
    """Return whether a statement list calls the refusal, outside any nested function or class."""
    pending = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call) and _name(node.func) == REFUSAL:
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def lock_holders(source: str) -> dict[str, list[ast.With]]:
    """Return each module-level function's profile-lock blocks."""
    holders: dict[str, list[ast.With]] = {}
    for function in ast.parse(source).body:
        if isinstance(function, ast.FunctionDef):
            blocks = [node for node in ast.walk(function) if isinstance(node, ast.With) and _takes_lock(node)]
            if blocks:
                holders[function.name] = blocks
    return holders


def unguarded_writers(source: str, exempt=None) -> list[str]:
    """Return each lock-taking function with a lock block that never compares the reviewed fingerprint."""
    exempt = EXEMPT_WRITERS if exempt is None else exempt
    return [
        name
        for name, blocks in lock_holders(source).items()
        if name not in exempt and not all(_calls_refusal(block.body) for block in blocks)
    ]


class PolicyFingerprintRefusalScannerTest(SimpleTestCase):
    """The scanner's own contract, driven by source strings rather than the package."""

    def test_a_writer_that_skips_the_refusal_is_reported(self):
        source = "def save():\n    with locked_profile_policy(pk):\n        row.save()\n"
        self.assertEqual(unguarded_writers(source, exempt={}), ["save"])

    def test_a_refusal_before_the_lock_does_not_count(self):
        source = (
            "def save():\n"
            "    refuse_moved_policy(profile, reviewed)\n"
            "    with locked_profile_policy(pk):\n"
            "        row.save()\n"
        )
        self.assertEqual(unguarded_writers(source, exempt={}), ["save"])

    def test_a_refusal_inside_a_nested_function_does_not_count(self):
        source = (
            "def save():\n"
            "    with locked_profile_policy(pk):\n"
            "        def later():\n"
            "            refuse_moved_policy(profile, reviewed)\n"
            "        row.save()\n"
        )
        self.assertEqual(unguarded_writers(source, exempt={}), ["save"])

    def test_every_lock_block_of_one_writer_needs_the_refusal(self):
        source = (
            "def save():\n"
            "    with locked_profile_policy(pk):\n"
            "        module.refuse_moved_policy(profile, reviewed)\n"
            "    with locked_profile_policy(pk):\n"
            "        row.save()\n"
        )
        self.assertEqual(unguarded_writers(source, exempt={}), ["save"])

    def test_a_refusal_inside_the_lock_clears_the_writer(self):
        source = (
            "def save():\n"
            "    with transaction.atomic(), locked_profile_policy(pk):\n"
            "        if stored:\n"
            "            refuse_moved_policy(profile, reviewed)\n"
            "        row.save()\n"
        )
        self.assertEqual(unguarded_writers(source, exempt={}), [])


class WorkspaceWriterRefusalTest(SimpleTestCase):
    """The package's workspace writers all compare the reviewed fingerprint under the lock."""

    def test_every_workspace_writer_compares_the_reviewed_fingerprint_under_the_lock(self):
        for module in WRITER_MODULES:
            with self.subTest(module=module):
                source = (PACKAGE / module).read_text(encoding="utf-8")
                self.assertEqual(unguarded_writers(source), [])

    def test_every_exemption_names_a_writer_that_takes_the_lock(self):
        holders = set()
        for module in WRITER_MODULES:
            holders.update(lock_holders((PACKAGE / module).read_text(encoding="utf-8")))
        self.assertLessEqual(set(EXEMPT_WRITERS), holders)

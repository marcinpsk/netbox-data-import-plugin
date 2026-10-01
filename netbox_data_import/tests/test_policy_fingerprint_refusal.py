# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every workspace writer that takes the profile policy lock compares the reviewed fingerprint inside it.

Specification section 10.2: a preview revision is per session, so only the profile fingerprint
under the lock shows that another operator moved the policy. Two writers once took the lock and
skipped the comparison, so a second session silently replaced the first session's decision.

The refusal must be a statement of the lock block itself, before any statement that writes, so no
branch can skip it. The scan reads only WRITER_MODULES and does not follow a lock taken inside a helper.
It follows aliases of the lock and of an opened lock through assignments to plain names only.
"""

import ast
import pathlib

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
WRITER_MODULES = ("review_workspace.py", "proposal_decisions.py")
LOCK = "locked_profile_policy"
REFUSAL = "refuse_moved_policy"
# Calls that write a row; a statement before the refusal must make none of them.
WRITE_CALLS = frozenset(
    {
        "bulk_create",
        "bulk_update",
        "create",
        "decide_proposal",
        "delete",
        "delete_permission_scoped_objects",
        "get_or_create",
        "save",
        "save_permission_scoped_object",
        "update",
        "update_or_create",
        "write_resolution",
        "write_resolution_if_fresh",
    }
)

# Writer function -> why it takes the lock without comparing the reviewed fingerprint.
EXEMPT_WRITERS = {
    "reject_proposal": "A rejection writes no profile policy and no Row Resolution (specification 7.6).",
}


def _name(node) -> str | None:
    """Return the called name of a bare or attribute call target."""
    return getattr(node, "id", None) or getattr(node, "attr", None)


def _assignments(tree):
    """Yield (target names, value) for every plain and annotated assignment."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            yield [target.id for target in node.targets if isinstance(target, ast.Name)], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            yield [node.target.id], node.value


def _lock_names(tree) -> tuple[set[str], set[str]]:
    """Return the names that call the lock and the names that hold an opened lock, aliases followed to a fixpoint."""
    callers = {LOCK}
    held: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            callers.update(alias.asname for alias in node.names if alias.name == LOCK and alias.asname)
    assignments = list(_assignments(tree))
    size = -1
    while size != len(callers) + len(held):
        size = len(callers) + len(held)
        for targets, value in assignments:
            if isinstance(value, (ast.Name, ast.Attribute)) and _name(value) in callers:
                callers.update(targets)
            elif (isinstance(value, ast.Call) and _name(value.func) in callers) or _name(value) in held:
                held.update(targets)
    return callers, held


def _takes_lock(node, callers: set[str], held: set[str]) -> bool:
    """Return whether one `with` statement opens the profile policy lock, under any of its names."""
    return isinstance(node, ast.With) and any(
        (isinstance(item.context_expr, ast.Call) and _name(item.context_expr.func) in callers)
        or (isinstance(item.context_expr, ast.Name) and item.context_expr.id in held)
        for item in node.items
    )


def _writes(statement) -> bool:
    """Return whether a statement makes a write call, outside any nested function or class."""
    pending = [statement]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call) and _name(node.func) in WRITE_CALLS:
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _refuses_before_writing(body) -> bool:
    """Return whether the block itself calls the refusal before any statement that writes."""
    for statement in body:
        call = statement.value if isinstance(statement, ast.Expr) else None
        if isinstance(call, ast.Call) and _name(call.func) == REFUSAL:
            return True
        if _writes(statement):
            return False
    return False


def lock_holders(source: str) -> dict[str, list[ast.With]]:
    """Return each module-level function's profile-lock blocks."""
    tree = ast.parse(source)
    callers, held = _lock_names(tree)
    holders: dict[str, list[ast.With]] = {}
    for function in tree.body:
        if isinstance(function, ast.FunctionDef):
            blocks = [
                node for node in ast.walk(function) if isinstance(node, ast.With) and _takes_lock(node, callers, held)
            ]
            if blocks:
                holders[function.name] = blocks
    return holders


def unguarded_writers(source: str, exempt=None) -> list[str]:
    """Return each lock-taking function with a lock block that can write before it refuses."""
    exempt = EXEMPT_WRITERS if exempt is None else exempt
    return [
        name
        for name, blocks in lock_holders(source).items()
        if name not in exempt and not all(_refuses_before_writing(block.body) for block in blocks)
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
            "        stored = Row.objects.filter(pk=pk).first()\n"
            "        if stored is None:\n"
            "            return False\n"
            "        refuse_moved_policy(profile, reviewed)\n"
            "        row.save()\n"
        )
        self.assertEqual(unguarded_writers(source, exempt={}), [])

    def test_a_conditional_refusal_does_not_count(self):
        source = (
            "def save():\n"
            "    with locked_profile_policy(pk):\n"
            "        if stored:\n"
            "            refuse_moved_policy(profile, reviewed)\n"
            "        row.save()\n"
        )
        self.assertEqual(unguarded_writers(source, exempt={}), ["save"])

    def test_a_refusal_after_a_write_does_not_count(self):
        source = (
            "def save():\n"
            "    with locked_profile_policy(pk):\n"
            "        save_permission_scoped_object(actor, Row, lookup, values)\n"
            "        refuse_moved_policy(profile, reviewed)\n"
        )
        self.assertEqual(unguarded_writers(source, exempt={}), ["save"])

    def test_an_aliased_lock_is_still_the_lock(self):
        sources = (
            (
                "from .policy_lock import locked_profile_policy as hold\ndef save():\n    with hold(pk):\n        row.save()\n"
            ),
            "hold = policy_lock.locked_profile_policy\ndef save():\n    with hold(pk):\n        row.save()\n",
            "def save():\n    held = locked_profile_policy(pk)\n    with held:\n        row.save()\n",
            (
                "from .policy_lock import locked_profile_policy as hold\nlock = hold\n"
                "def save():\n    with lock(pk):\n        row.save()\n"
            ),
            "hold: Callable = locked_profile_policy\ndef save():\n    with hold(pk):\n        row.save()\n",
            "def save():\n    held: Lock = locked_profile_policy(pk)\n    with held:\n        row.save()\n",
            "def save():\n    held = locked_profile_policy(pk)\n    again = held\n    with again:\n        row.save()\n",
            (
                "def save():\n    if flag:\n        held = locked_profile_policy(pk)\n"
                "    again = held\n    with again:\n        row.save()\n"
            ),
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(unguarded_writers(source, exempt={}), ["save"])


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

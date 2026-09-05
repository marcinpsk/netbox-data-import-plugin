# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A planning module never loads a placement reference by primary key without a lock."""

import ast
from pathlib import Path

from django.test import SimpleTestCase

PACKAGE = Path(__file__).resolve().parent.parent

# Their u_height and is_full_depth decide a placement Device.full_clean() revalidates at the write.
GUARDED_MODELS = frozenset({"DeviceType", "Rack"})
GUARDED_MODULES = ("target_modules.py", "cable_target.py")
PRIMARY_KEY_ARGUMENTS = frozenset({"pk", "pk__in", "id", "id__in"})
LOADERS = frozenset({"filter", "get"})


def _receiver_model(node) -> str | None:
    """Return the guarded model a queryset expression starts from, if it starts at one."""
    while True:
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "objects"
            and isinstance(node.value, ast.Name)
            and node.value.id in GUARDED_MODELS
        ):
            return node.value.id
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return None


def _loads_by_primary_key(call) -> bool:
    """Return whether one call selects rows by primary key."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in LOADERS
        and any(keyword.arg in PRIMARY_KEY_ARGUMENTS for keyword in call.keywords)
    )


def _statement_locks(statement) -> bool:
    """Return whether the statement holding a load also takes the row lock."""
    return any(isinstance(node, ast.Attribute) and node.attr == "select_for_update" for node in ast.walk(statement))


def _unlocked_loads(path: Path) -> list[str]:
    """Return one report line per primary-key load of a guarded model that takes no lock."""
    tree = ast.parse(path.read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    unlocked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _loads_by_primary_key(node):
            continue
        model = _receiver_model(node.func.value)
        if model is None:
            continue
        statement = node
        while statement is not None and not isinstance(statement, ast.stmt):
            statement = parents.get(statement)
        if statement is None or not _statement_locks(statement):
            unlocked.append(f"{path.name}:{node.lineno}: {model} loaded by primary key without a lock")
    return unlocked


class PlacementReferencesAreLoadedThroughOneLockedSeamTest(SimpleTestCase):
    """The same race was filed three times, so the seam is now enforced instead of remembered."""

    maxDiff = None

    def test_no_planning_module_loads_a_placement_reference_by_primary_key_unlocked(self):
        """Route a new load through _DeviceBatch.placement_reference, which locks when the replan does."""
        found = [line for name in GUARDED_MODULES for line in _unlocked_loads(PACKAGE / name)]

        self.assertEqual(
            found,
            [],
            "Load these through _DeviceBatch.placement_reference, or take select_for_update in the same statement.",
        )

    def test_the_guard_reports_a_load_that_drops_its_lock(self):
        """A guard that cannot fail protects nothing, so prove it sees the shape it forbids."""
        source = "rack = Rack.objects.filter(pk=rack_id).first()\n"
        tree = ast.parse(source)
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and _loads_by_primary_key(node))

        self.assertEqual(_receiver_model(call.func.value), "Rack")
        self.assertFalse(_statement_locks(tree.body[0]))

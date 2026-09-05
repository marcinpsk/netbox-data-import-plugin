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


def _parents_of(tree) -> dict:
    """Return each node's parent, which an expression needs to find the rest of its own chain."""
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _chain_root(call, parents):
    """Return the outermost expression of the one queryset chain a call belongs to."""
    node: ast.AST = call
    while True:
        parent = parents.get(node)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            node = parent
        elif isinstance(parent, ast.Call) and parent.func is node:
            node = parent
        else:
            return node


def _chain_takes_lock(call, parents) -> bool:
    """Return whether the queryset chain holding a load also takes the row lock.

    Only the chain counts: a `select_for_update()` elsewhere in the same statement locks another
    queryset, so it cannot clear this load.
    """
    node = _chain_root(call, parents)
    while True:
        if isinstance(node, ast.Attribute):
            if node.attr == "select_for_update":
                return True
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return False


def _unlocked_loads(path: Path) -> list[str]:
    """Return one report line per primary-key load of a guarded model that takes no lock."""
    tree = ast.parse(path.read_text())
    parents = _parents_of(tree)
    unlocked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not _loads_by_primary_key(node):
            continue
        model = _receiver_model(node.func.value)
        if model is None:
            continue
        if not _chain_takes_lock(node, parents):
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
        assert isinstance(call.func, ast.Attribute)

        self.assertEqual(_receiver_model(call.func.value), "Rack")
        self.assertFalse(_chain_takes_lock(call, _parents_of(tree)))

    def test_the_guard_reports_a_load_beside_an_unrelated_lock(self):
        """A lock elsewhere in the statement locks another queryset, so it cannot clear this one."""
        source = "rack, cable = Rack.objects.filter(pk=rack_id).first(), Cable.objects.select_for_update().first()\n"
        tree = ast.parse(source)
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and _loads_by_primary_key(node))

        self.assertFalse(_chain_takes_lock(call, _parents_of(tree)))

    def test_the_guard_accepts_a_lock_taken_before_the_load(self):
        """`select_for_update()` may come first in the chain, and it still locks the rows loaded."""
        source = 'rack = Rack.objects.select_for_update(of=("self",)).filter(pk=rack_id).first()\n'
        tree = ast.parse(source)
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and _loads_by_primary_key(node))

        self.assertTrue(_chain_takes_lock(call, _parents_of(tree)))

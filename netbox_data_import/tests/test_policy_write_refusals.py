# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every policy write renders a deleted profile as a refusal, never as a server error.

`save_permission_scoped_object()` and `locked_profile_policy()` both raise
`ImportProfile.DoesNotExist` when the profile row is gone by the time the lock is taken. The preview
gate reads the profile earlier in the request, so a delete that lands between the two reaches the
lock and leaves the view. A view that does not answer it returns HTTP 500.

`_PermissionScopedWriteMixin` answers it once for every view that inherits it. A view that does not
inherit it has to catch the exception itself. One view lost its handler to a cleanup that assumed
removing an outer lock removed the exception, which it did not, so this scanner exists.
"""

import ast
import pathlib

from django.test import SimpleTestCase

VIEWS = pathlib.Path(__file__).resolve().parents[1] / "views.py"
#: A mixin whose `dispatch()` answers the exception for every view that inherits it.
ANSWERING_MIXINS = frozenset({"_PermissionScopedWriteMixin", "_TraceProposalMixin"})
RAISING_CALLS = frozenset({"save_permission_scoped_object", "locked_profile_policy"})
HANDLED = "ImportProfile.DoesNotExist"


def _caught_by(node):
    """Return every exception expression the `except` clauses of *node* name."""
    names = set()
    for handler in node.handlers:
        if handler.type is None:
            names.add("<bare>")
            continue
        parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        names.update(ast.unparse(part) for part in parts)
    return names


def _scan(tree, raising):
    """Return unanswered call sites, and the module-level helpers that hold one.

    A helper that carries an unanswered write raises through its own callers, so the next pass
    treats a call to it as a write too. That is how a view stays accountable for a helper it calls.
    """
    unanswered, carriers = [], set()

    def walk(node, tries, owner, function):
        if isinstance(node, ast.ClassDef):
            owner = node
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node if owner is None else function
        if isinstance(node, ast.Try):
            for child in node.body:
                walk(child, [*tries, node], owner, function)
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                walk(child, tries, owner, function)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in raising:
            inherits = owner is not None and any(ast.unparse(base) in ANSWERING_MIXINS for base in owner.bases)
            caught = {name for block in tries for name in _caught_by(block)}
            if not inherits and HANDLED not in caught and "<bare>" not in caught:
                if owner is not None:
                    unanswered.append(f"{owner.name}:{node.lineno}")
                elif function is not None:
                    carriers.add(function.name)
                else:
                    unanswered.append(f"<module>:{node.lineno}")
        for child in ast.iter_child_nodes(node):
            walk(child, tries, owner, function)

    walk(tree, [], None, None)
    return unanswered, carriers


def _unanswered_policy_writes(tree):
    """Return every view call site that leaves a deleted profile unanswered."""
    raising = set(RAISING_CALLS)
    while True:
        unanswered, carriers = _scan(tree, raising)
        if carriers <= raising:
            return sorted(unanswered)
        raising |= carriers


class PolicyWriteRefusalTest(SimpleTestCase):
    """A deleted import profile is an answered refusal at every policy write."""

    def test_every_policy_write_answers_a_deleted_profile(self):
        unanswered = _unanswered_policy_writes(ast.parse(VIEWS.read_text()))

        self.assertEqual(unanswered, [], "these policy writes would return HTTP 500 for a deleted profile")

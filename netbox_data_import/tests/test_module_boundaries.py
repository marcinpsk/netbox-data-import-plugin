# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Views and jobs call the engine's public seam, never its private helpers.

Section 2.4 of the architecture lists private engine helpers among the things a view may not call.
The coupling is also invisible to the linter: `engine._helper` is an attribute lookup on a module, so
renaming or moving the helper leaves ruff silent and fails at request time instead. One such rename
reached the full suite before this guard existed.

`PERMITTED` records the calls that already exist. It is a permit list, so removing a call needs no
edit here, and a new one fails until it is either a public seam or a deliberate entry.
"""

import ast
import pathlib

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
CALLERS = ("views.py", "jobs.py")

# Layer 6 of the engine cutover removes these. Until then they are inherited, not new.
PERMITTED = {
    "views.py": {
        "_coerce_position",
        "_device_placement_differs",
        "_effective_device_name",
        "_has_below_rack_position",
        "_identity_text",
        "_normalize_for_compare",
        "_str_val",
    },
    "jobs.py": set(),
}


def _private_engine_attributes(path: pathlib.Path) -> set[str]:
    """Return every `engine._name` attribute this module reads."""
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "engine"
            and node.attr.startswith("_")
        ):
            found.add(node.attr)
    return found


class ViewsUsePublicEngineSeamTest(SimpleTestCase):
    """A new private-helper call has to be justified, because the linter cannot see it."""

    maxDiff = None

    def test_no_caller_reaches_a_new_private_engine_helper(self):
        """The set only shrinks as the cutover lands, so anything unlisted is new coupling."""
        added = sorted(
            f"{name}: engine.{attribute}"
            for name in CALLERS
            for attribute in _private_engine_attributes(PACKAGE / name) - PERMITTED[name]
        )

        self.assertEqual(
            added,
            [],
            "Call the engine's public seam, or record the helper in PERMITTED with the reason.",
        )

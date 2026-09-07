# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Only `preview_row_actions` may write the session keys the preview guard is made of.

The guard that holds a preview while its retained trace sync runs lives inside the two writers that
can break it. That only holds while those writers are the single way the keys change: a view that
assigns `session[PREVIEW_PLAN_SESSION_KEY]` itself clears the guard without consulting anything.

Reads are unrestricted. A view may ask whether the preview is dirty or read the stored plan; it may
not decide on its own that the preview has been recalculated, retained or released.
"""

import ast
import pathlib

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
OWNER = "preview_row_actions.py"

# The keys whose invariant the owner enforces. The other preview keys carry no guard.
GUARDED_CONSTANTS = frozenset(
    {
        "PREVIEW_PLAN_SESSION_KEY",
        "PREVIEW_DIRTY_SESSION_KEY",
        "PREVIEW_REVISION_SESSION_KEY",
        "RETAINED_SYNC_JOB_SESSION_KEY",
    }
)
GUARDED_LITERALS = frozenset(
    {"import_plan", "import_preview_dirty", "import_preview_revision", "import_retained_sync_job_id"}
)


def _is_session(node) -> bool:
    """Return whether one expression names a session, by attribute or by bare name."""
    if isinstance(node, ast.Attribute):
        return node.attr == "session"
    return isinstance(node, ast.Name) and node.id == "session"


def _guarded_key(node) -> str | None:
    """Return the guarded key one subscript or argument names, or None."""
    if isinstance(node, ast.Name) and node.id in GUARDED_CONSTANTS:
        return node.id
    if isinstance(node, ast.Constant) and node.value in GUARDED_LITERALS:
        return str(node.value)
    return None


def _writes_in(path: pathlib.Path) -> list[str]:
    """Return one entry per statement that writes a guarded key through a session."""
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript) and _is_session(target.value) and (key := _guarded_key(target.slice)):
                found.append(f"{path.name}:{target.lineno}: assigns session[{key}]")
        # `session.pop(KEY, None)` removes the key, which is a write by another name.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("pop", "setdefault", "update")
            and _is_session(node.func.value)
            and node.args
            and (key := _guarded_key(node.args[0]))
        ):
            found.append(f"{path.name}:{node.lineno}: session.{node.func.attr}({key})")
        # A guarded key inside a collection that is then iterated to pop is the same write.
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            keys = [key for element in node.elts if (key := _guarded_key(element))]
            if keys:
                found.append(f"{path.name}:{node.lineno}: collects {', '.join(keys)} for removal")
    return found


class PreviewStateHasOneWriterTest(SimpleTestCase):
    """The preview guard is only as good as the number of places that can clear it."""

    def test_no_module_outside_the_owner_writes_a_guarded_session_key(self):
        offenders: list[str] = []
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name == OWNER:
                continue
            offenders.extend(_writes_in(path))

        self.assertEqual(
            offenders,
            [],
            "Route these through a named operation in preview_row_actions.py, which owns the guard.",
        )

    def test_the_owner_does_write_them_so_the_scan_is_meaningful(self):
        """A scan that matches nothing would pass this suite while proving nothing."""
        self.assertTrue(_writes_in(PACKAGE / OWNER))

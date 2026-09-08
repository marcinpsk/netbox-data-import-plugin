# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Only `preview_row_actions` may write the session keys the preview guard is made of.

The guard that holds a preview while its retained trace sync runs lives inside the two writers that
can break it. That only holds while those writers are the single way the keys change: a view that
assigns `session[PREVIEW_PLAN_SESSION_KEY]` itself clears the guard without consulting anything.

Reads are unrestricted. A view may ask whether the preview is dirty or read the stored plan; it may
not decide on its own that the preview has been recalculated, retained or released.

The scan is syntactic, so a key it cannot see written is a key it cannot flag: `session.update(**x)`
over a name built elsewhere passes. Naming a guarded key at the call site is what it stops.
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
    }
)
GUARDED_LITERALS = frozenset({"import_plan", "import_preview_dirty", "import_preview_revision"})


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


def _is_dict_call(node) -> bool:
    """Return whether one expression is a `dict(...)` call, which builds a mapping in place."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"


def _pair_sequence_keys(node) -> list[str]:
    """Return the guarded keys a literal sequence of key/value pairs names.

    `dict.update()` takes an iterable of pairs as readily as a mapping, so `[(KEY, value)]` is the
    same write spelled another way.
    """
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return []
    keys: list[str] = []
    for pair in node.elts:
        if isinstance(pair, (ast.Tuple, ast.List)) and pair.elts and (key := _guarded_key(pair.elts[0])):
            keys.append(key)
    return keys


def _mapping_keys(node) -> list[str]:
    """Return the guarded keys one mapping expression names, through `**` and through `dict(...)`."""
    keys: list[str] = []
    if pairs := _pair_sequence_keys(node):
        return pairs
    if _is_dict_call(node):
        for argument in node.args:
            keys.extend(_mapping_keys(argument))
        for keyword in node.keywords:
            if keyword.arg is None:
                keys.extend(_mapping_keys(keyword.value))
            elif keyword.arg in GUARDED_LITERALS:
                keys.append(keyword.arg)
        return keys
    if not isinstance(node, ast.Dict):
        return keys
    for element, value in zip(node.keys, node.values, strict=True):
        if element is None:
            # `{**{KEY: ...}}` names the key just as plainly as `{KEY: ...}` does.
            keys.extend(_mapping_keys(value))
        elif key := _guarded_key(element):
            keys.append(key)
    return keys


def _update_keys(node: ast.Call) -> list[str]:
    """Return the guarded keys one `session.update(...)` names, by mapping or by keyword."""
    keys: list[str] = []
    for argument in node.args:
        if isinstance(argument, (ast.Dict, ast.Call, ast.List, ast.Tuple, ast.Set)):
            keys.extend(_mapping_keys(argument))
        elif key := _guarded_key(argument):
            keys.append(key)
    for keyword in node.keywords:
        if keyword.arg is None:
            keys.extend(_mapping_keys(keyword.value))
        elif keyword.arg in GUARDED_LITERALS:
            keys.append(keyword.arg)
    return keys


def _writes_in_source(source: str, name: str) -> list[str]:
    """Return one entry per statement in `source` that writes a guarded key through a session."""
    found: list[str] = []
    tree = ast.parse(source)
    # The collection branch below is for keys gathered to be popped, not for an update's own keys.
    claimed = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
        if _is_session(node.func.value)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
        for inner in ast.walk(argument)
    }
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript) and _is_session(target.value) and (key := _guarded_key(target.slice)):
                found.append(f"{name}:{target.lineno}: assigns session[{key}]")
        # `session.pop(KEY, None)` removes the key, which is a write by another name.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _is_session(node.func.value):
            if node.func.attr == "update":
                found.extend(f"{name}:{node.lineno}: session.update({key})" for key in _update_keys(node))
            elif node.func.attr == "clear":
                found.append(f"{name}:{node.lineno}: session.clear() drops every guarded key")
            elif node.func.attr in ("pop", "setdefault") and node.args and (key := _guarded_key(node.args[0])):
                found.append(f"{name}:{node.lineno}: session.{node.func.attr}({key})")
        # `del session[KEY]` removes the key without ever assigning to it.
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and _is_session(target.value)
                    and (key := _guarded_key(target.slice))
                ):
                    found.append(f"{name}:{target.lineno}: deletes session[{key}]")
        # A guarded key inside a collection that is then iterated to pop is the same write.
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and id(node) not in claimed:
            keys = [key for element in node.elts if (key := _guarded_key(element))]
            if keys:
                found.append(f"{name}:{node.lineno}: collects {', '.join(keys)} for removal")
    return found


def _writes_in(path: pathlib.Path) -> list[str]:
    """Return one entry per statement in one file that writes a guarded key through a session."""
    return _writes_in_source(path.read_text(encoding="utf-8"), path.name)


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


class WritesScanTest(SimpleTestCase):
    """Self-tests of the scan, on real sources, so a bypass is caught before it is used."""

    def test_finds_a_plain_subscript_assignment(self):
        source = "def f(session, plan):\n    session[PREVIEW_PLAN_SESSION_KEY] = plan\n"
        self.assertEqual(len(_writes_in_source(source, "t.py")), 1)

    def test_finds_a_key_popped_by_literal_name(self):
        source = 'def f(session):\n    session.pop("import_plan", None)\n'
        self.assertEqual(len(_writes_in_source(source, "t.py")), 1)

    def test_finds_a_guarded_key_in_an_update_mapping(self):
        source = "def f(session, plan):\n    session.update({PREVIEW_PLAN_SESSION_KEY: plan})\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(PREVIEW_PLAN_SESSION_KEY)"])

    def test_finds_a_guarded_key_passed_to_update_as_a_keyword(self):
        source = "def f(session, plan):\n    session.update(import_plan=plan)\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(import_plan)"])

    def test_finds_a_guarded_key_in_a_mapping_unpacked_into_update(self):
        source = "def f(session, plan):\n    session.update(**{PREVIEW_DIRTY_SESSION_KEY: plan})\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(PREVIEW_DIRTY_SESSION_KEY)"])

    def test_ignores_an_update_that_names_no_guarded_key(self):
        source = "def f(session, state):\n    session.update(state)\n    session.update(other=1)\n"
        self.assertEqual(_writes_in_source(source, "t.py"), [])

    def test_finds_a_guarded_key_deleted_from_the_session(self):
        source = "def f(session):\n    del session[PREVIEW_PLAN_SESSION_KEY]\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: deletes session[PREVIEW_PLAN_SESSION_KEY]"])

    def test_finds_a_session_cleared_wholesale(self):
        source = "def f(session):\n    session.clear()\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.clear() drops every guarded key"])

    def test_finds_a_guarded_key_in_a_mapping_unpacked_inside_another(self):
        source = "def f(session, plan):\n    session.update({**{PREVIEW_PLAN_SESSION_KEY: plan}})\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(PREVIEW_PLAN_SESSION_KEY)"])

    def test_ignores_a_delete_of_an_unguarded_key(self):
        source = "def f(session):\n    del session['import_rows']\n"
        self.assertEqual(_writes_in_source(source, "t.py"), [])

    def test_finds_a_guarded_key_in_a_dict_call_passed_to_update(self):
        source = "def f(session, plan):\n    session.update(dict(import_plan=plan))\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(import_plan)"])

    def test_finds_a_guarded_key_in_a_dict_call_unpacked_into_update(self):
        source = "def f(session, plan):\n    session.update(**dict(import_plan=plan))\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(import_plan)"])

    def test_finds_a_guarded_key_in_a_mapping_passed_to_a_dict_call(self):
        source = "def f(session, plan):\n    session.update(dict({PREVIEW_PLAN_SESSION_KEY: plan}))\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(PREVIEW_PLAN_SESSION_KEY)"])

    def test_ignores_a_dict_call_naming_no_guarded_key(self):
        source = "def f(session, plan):\n    session.update(dict(import_rows=plan))\n"
        self.assertEqual(_writes_in_source(source, "t.py"), [])

    def test_finds_a_guarded_key_in_a_list_of_pairs_passed_to_update(self):
        source = 'def f(session, plan):\n    session.update([("import_plan", plan)])\n'
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(import_plan)"])

    def test_finds_a_guarded_key_in_a_tuple_of_pairs_passed_to_update(self):
        source = "def f(session, plan):\n    session.update(((PREVIEW_PLAN_SESSION_KEY, plan),))\n"
        self.assertEqual(_writes_in_source(source, "t.py"), ["t.py:2: session.update(PREVIEW_PLAN_SESSION_KEY)"])

    def test_ignores_a_pair_sequence_naming_no_guarded_key(self):
        source = 'def f(session, plan):\n    session.update([("import_rows", plan)])\n'
        self.assertEqual(_writes_in_source(source, "t.py"), [])

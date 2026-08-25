# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every normal exit from a `transaction.atomic()` block has to be audited.

`atomic()` commits when its block ends normally, and `return`, `break` and `continue` all end it
normally. A denial written as an early `return` therefore commits whatever the request already
wrote. Two such bugs shipped before this guard existed.

The scanner refuses to guess whether a write happened: the write that caused one of those bugs sat
behind a helper call, not a visible `.save()`. So it reports every normal exit unless a
`transaction.set_rollback(True)` dominates it, or the exit carries a reviewed marker. `raise` needs
no marker, because an exception leaves the block abnormally and rolls it back.
"""

import ast
import io
import pathlib
import tokenize

from django.test import SimpleTestCase

MARKER = "atomic-exit-safe"
PACKAGE = pathlib.Path(__file__).resolve().parents[1]

# (qualified function, marker id) -> why this exit commits nothing it should not.
# A new exit fails the guard until it is audited and listed here.
AUDITED_EXITS = {
    ("save_permission_scoped_object", "existing-row-kept-unwritten"): (
        "on_existing is keep, so the row is returned untouched after its view check."
    ),
    ("save_permission_scoped_object", "scoped-write-committed"): (
        "The success path: every check passed, so committing the write is the point."
    ),
    ("delete_permission_scoped_objects", "scoped-delete-committed"): (
        "Every row cleared its delete check before any row was deleted."
    ),
    ("IgnoreFieldDifferenceView.post", "binding-refused-before-write"): (
        "The binding helper either wrote nothing or rolled back its own savepoint."
    ),
    ("UnignoreFieldDifferenceView.post", "record-absent-before-write"): (
        "The review row is gone, so nothing has been written yet."
    ),
    ("UnignoreFieldDifferenceView.post", "delete-denied-before-write"): (
        "The delete permission is refused before record.delete() runs."
    ),
    ("UnignoreFieldDifferenceView.post", "binding-refused-before-delete"): (
        "The binding helper either wrote nothing or rolled back, and the delete has not run."
    ),
    ("SyncSingleRowView.post", "stale-preview-after-dry-run"): (
        "The only call before this return is run_import with dry_run True, which writes nothing."
    ),
    ("PrimaryContactResolver.apply", "success-commit-intended"): (
        "The success path of the block: committing the planned Contact writes is the point."
    ),
}

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _markers_by_line(source):
    """Map each line carrying a marker comment to its audit id."""
    markers = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and MARKER in token.string:
            markers[token.start[0]] = token.string.split(MARKER, 1)[1].lstrip(": ").strip()
    return markers


def _is_atomic_reference(node):
    """Return True for `transaction.atomic` and for a bare imported `atomic`."""
    return getattr(node, "attr", None) == "atomic" or getattr(node, "id", None) == "atomic"


def _opens_atomic(node):
    """Return True for an atomic context manager or synchronous function decorator."""
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return any(
            isinstance(item.context_expr, ast.Call) and _is_atomic_reference(item.context_expr.func)
            for item in node.items
        )
    if isinstance(node, ast.FunctionDef):
        return any(
            _is_atomic_reference(decorator.func if isinstance(decorator, ast.Call) else decorator)
            for decorator in node.decorator_list
        )
    return False


def _sets_rollback(statement):
    """Return True for a bare `transaction.set_rollback(True)` statement."""
    if not (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and getattr(statement.value.func, "attr", None) == "set_rollback"
    ):
        return False
    call = statement.value
    argument = (
        call.args[0]
        if call.args
        else next(
            (keyword.value for keyword in call.keywords if keyword.arg == "rollback"),
            None,
        )
    )
    return isinstance(argument, ast.Constant) and argument.value is True


def _normal_exits(body, loop_depth, rolled_back, in_nested_atomic=False):
    """Yield every statement that leaves the enclosing atomic block normally.

    `rolled_back` tracks a `set_rollback` that dominates the rest of this statement list. A
    rollback inside a branch does not dominate what follows the branch, and a rollback inside a
    nested atomic block rolls back that savepoint only, so `in_nested_atomic` stops it counting
    for the block being scanned.
    """
    dominated = rolled_back
    for statement in body:
        if _sets_rollback(statement):
            dominated = dominated or not in_nested_atomic
            continue
        if isinstance(statement, _SCOPES):
            continue
        if isinstance(statement, ast.Return) or (isinstance(statement, (ast.Break, ast.Continue)) and loop_depth == 0):
            yield statement, dominated
            continue
        # A nested atomic block still exits this one, so its exits are reported here too.
        nested = in_nested_atomic or _opens_atomic(statement)
        depth = loop_depth + 1 if isinstance(statement, _LOOPS) else loop_depth
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(statement, field, None)
            if inner:
                yield from _normal_exits(inner, depth, dominated, nested)
        for case in getattr(statement, "cases", []):
            yield from _normal_exits(case.body, depth, dominated, nested)
        for handler in getattr(statement, "handlers", []):
            yield from _normal_exits(handler.body, loop_depth, dominated, nested)


def _atomic_exits(source):
    """Yield the shared scope, rollback, and marker facts consumed by both audits."""
    markers = _markers_by_line(source)

    def report(node, scope):
        qualified = ".".join(scope)
        for exit_node, dominated in _normal_exits(node.body, 0, False):
            marker = markers.get(exit_node.lineno) or markers.get(exit_node.lineno - 1)
            yield qualified, exit_node, dominated, marker

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = [*scope, child.name]
                if _opens_atomic(child):
                    yield from report(child, child_scope)
                yield from walk(child, child_scope)
                continue
            if _opens_atomic(child):
                yield from report(child, scope)
            yield from walk(child, scope)

    yield from walk(ast.parse(source), [])


def unaudited_atomic_exits(source, audited=None):
    """Return one entry per normal atomic exit that no rollback dominates and no marker covers."""
    audited = AUDITED_EXITS if audited is None else audited
    reported = []

    for qualified, exit_node, dominated, marker in _atomic_exits(source):
        if dominated:
            continue
        if marker is not None and (qualified, marker) in audited:
            continue
        row = (qualified, exit_node.lineno, marker)
        if row not in reported:
            reported.append(row)

    return reported


def used_markers(source):
    """Return the (qualified function, marker id) pairs the source actually carries."""
    return {
        (qualified, marker) for qualified, _exit_node, _dominated, marker in _atomic_exits(source) if marker is not None
    }


class AtomicExitScannerTest(SimpleTestCase):
    """The scanner's own contract, driven by source strings rather than the package."""

    AUDITED = {("f", "reviewed"): "for the scanner tests"}

    def _report(self, source):
        return unaudited_atomic_exits(source, audited=self.AUDITED)

    def test_one_walker_owns_atomic_exit_discovery(self):
        tree = ast.parse(pathlib.Path(__file__).read_text())
        callers = {
            function.name
            for function in tree.body
            if isinstance(function, ast.FunctionDef)
            and function.name != "_normal_exits"
            and any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_normal_exits"
                for node in ast.walk(function)
            )
        }

        self.assertEqual(callers, {"_atomic_exits"})

    def test_a_write_then_an_unguarded_return_is_reported(self):
        """This is the shape of both bugs that shipped."""
        source = "def f():\n    with transaction.atomic():\n        obj.save()\n        return 1\n"
        self.assertEqual([row[0] for row in self._report(source)], ["f"])

    def test_a_dominating_rollback_clears_the_exit(self):
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        obj.save()\n"
            "        transaction.set_rollback(True)\n"
            "        status = 1\n"
            "        return status\n"
        )
        self.assertEqual(self._report(source), [])

    def test_a_keyword_true_rollback_clears_the_exit(self):
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        obj.save()\n"
            "        transaction.set_rollback(rollback=True)\n"
            "        return 1\n"
        )
        self.assertEqual(self._report(source), [])

    def test_a_conditional_rollback_does_not_dominate_what_follows_it(self):
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        obj.save()\n"
            "        if denied:\n"
            "            transaction.set_rollback(True)\n"
            "        return denied\n"
        )
        self.assertEqual([row[0] for row in self._report(source)], ["f"])

    def test_a_raise_needs_no_marker(self):
        """An exception leaves the block abnormally, so Django rolls it back."""
        source = "def f():\n    with transaction.atomic():\n        obj.save()\n        raise Denied()\n"
        self.assertEqual(self._report(source), [])

    def test_a_reviewed_marker_clears_the_exit(self):
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        obj.save()\n"
            "        # atomic-exit-safe: reviewed\n"
            "        return 1\n"
        )
        self.assertEqual(self._report(source), [])

    def test_an_unknown_marker_is_still_reported(self):
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        obj.save()\n"
            "        # atomic-exit-safe: invented\n"
            "        return 1\n"
        )
        self.assertEqual([row[2] for row in self._report(source)], ["invented"])

    def test_a_second_exit_in_an_audited_function_is_reported(self):
        """Auditing one exit must not license the next one added beside it."""
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        obj.save()\n"
            "        if a:\n"
            "            # atomic-exit-safe: reviewed\n"
            "            return 1\n"
            "        return 2\n"
        )
        self.assertEqual([row[1] for row in self._report(source)], [7])

    def test_a_return_from_a_nested_function_is_not_an_exit(self):
        source = (
            "def f():\n    with transaction.atomic():\n        def inner():\n            return 1\n        obj.save()\n"
        )
        self.assertEqual(self._report(source), [])

    def test_a_break_inside_a_loop_within_the_block_is_not_an_exit(self):
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        for row in rows:\n"
            "            obj.save()\n"
            "            break\n"
        )
        self.assertEqual(self._report(source), [])

    def test_a_break_whose_loop_encloses_the_block_is_an_exit(self):
        source = (
            "def f():\n"
            "    for row in rows:\n"
            "        with transaction.atomic():\n"
            "            obj.save()\n"
            "            break\n"
        )
        self.assertEqual([row[1] for row in self._report(source)], [5])

    def test_a_nested_rollback_does_not_clear_the_outer_block(self):
        """The inner savepoint rolls back; the outer block still commits on the way out."""
        source = (
            "def f():\n"
            "    with transaction.atomic():\n"
            "        obj.save()\n"
            "        with transaction.atomic():\n"
            "            transaction.set_rollback(True)\n"
            "            return 1\n"
        )
        self.assertEqual([row[1] for row in self._report(source)], [6])

    def test_a_bare_imported_atomic_is_recognized(self):
        source = "def f():\n    with atomic():\n        obj.save()\n        return 1\n"
        self.assertEqual([row[0] for row in self._report(source)], ["f"])

    def test_a_transaction_atomic_decorator_is_recognized(self):
        source = "@transaction.atomic\ndef f():\n    obj.save()\n    return 1\n"
        self.assertEqual([row[0] for row in self._report(source)], ["f"])

    def test_a_called_atomic_decorator_is_recognized(self):
        source = "@atomic()\ndef f():\n    obj.save()\n    return 1\n"
        self.assertEqual([row[0] for row in self._report(source)], ["f"])

    def test_only_a_constant_true_rollback_clears_the_exit(self):
        for arguments in ("False", "should_rollback", "rollback=False", "rollback=should_rollback"):
            with self.subTest(arguments=arguments):
                source = (
                    "def f():\n"
                    "    with transaction.atomic():\n"
                    "        obj.save()\n"
                    f"        transaction.set_rollback({arguments})\n"
                    "        return 1\n"
                )
                self.assertEqual([row[0] for row in self._report(source)], ["f"])

    def test_a_return_in_a_match_case_is_reported(self):
        source = (
            "def f(value):\n"
            "    with transaction.atomic():\n"
            "        match value:\n"
            "            case 1:\n"
            "                obj.save()\n"
            "                return 1\n"
        )
        self.assertEqual([row[0] for row in self._report(source)], ["f"])


class PackageAtomicExitsTest(SimpleTestCase):
    """The package itself must carry no unaudited atomic exit."""

    def _modules(self):
        tests = PACKAGE / "tests"
        return sorted(path for path in PACKAGE.rglob("*.py") if not path.is_relative_to(tests))

    def test_module_scan_includes_nested_non_test_modules(self):
        modules = self._modules()

        self.assertIn(PACKAGE / "api" / "serializers.py", modules)
        self.assertNotIn(PACKAGE / "tests" / "helpers.py", modules)

    def test_no_module_leaves_an_atomic_block_unaudited(self):
        """A new early return inside a transaction has to be reviewed before it can land."""
        offenders = []
        for path in self._modules():
            for qualified, line, marker in unaudited_atomic_exits(path.read_text()):
                seen = f" (marker {marker!r} is not in AUDITED_EXITS)" if marker else " (no marker)"
                offenders.append(f"{path.relative_to(PACKAGE)}:{line} in {qualified}{seen}")
        self.assertEqual(
            offenders,
            [],
            "Audit each exit, then add its (function, marker) to AUDITED_EXITS with the reason.",
        )

    def test_the_audit_list_carries_no_stale_entry(self):
        """A marker removed from the source must not leave its reason behind."""
        used = set()
        for path in self._modules():
            used |= used_markers(path.read_text())
        self.assertEqual(sorted(set(AUDITED_EXITS) - used), [])

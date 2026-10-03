# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""No view renders the text of a caught exception whose type does not promise plugin-written text.

PublicRefusal exposes only operator_message, status and reason. Other plugin exceptions retain
the audited curated-type policy: their authored text may be shown. A `ValidationError` is shown through `messages` or `message_dict`,
never `str()`. An Import Plan error (`PlanError`) and a `DatabaseError` name session data or the
schema, so no view shows their text, and `import_engine.operator_failure_message` words them for a
Job. A builtin or third-party exception shows its text only where an audit below says why.

A plugin type stops being curated when some handler wraps a caught `PlanError`'s text into it,
because its text then repeats the plan error. That search follows local assignments, an exception
bound to a name and raised later, and calls into a helper function of the same module; a method,
another module's helper, and a factory that returns the exception are not followed.

The scan is syntactic. A caught exception passed whole to a helper is not followed: the helpers
listed in `SANITIZERS` word it, and any other bare argument counts as a text use.
"""

import ast
import builtins
import importlib
import importlib.util
import pathlib
from collections.abc import Mapping
from typing import cast

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.test import SimpleTestCase

from netbox_data_import.plan import PlanError
from netbox_data_import.public_refusal import PublicRefusal

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_NAME = PACKAGE.name
RESPONSE_MODULES = ("views.py",)
NEVER_RENDERED = (PlanError, DatabaseError)
SANITIZERS = frozenset(
    {
        "operator_failure_message",
        "_refused_row_write_response",
        "_placement_error_text",
        "_stale_response",
        "_stale_json",
    }
)
VALIDATION_ATTRIBUTES = frozenset({"messages", "message_dict"})
_LOGGER_NAMES = frozenset({"logger", "logging"})

# (qualified function, caught class name) -> why its text may reach the operator.
AUDITED_RENDERS = {
    ("BulkYamlImportView.post", "YAMLError"): (
        "PyYAML describes only the uploaded document: a line, a column and the token it could not read."
    ),
    ("ImportProfileYamlView.post", "YAMLError"): (
        "PyYAML describes only the uploaded document: a line, a column and the token it could not read."
    ),
}


def _names_in(node) -> set[str]:
    """Return every loaded name inside one expression."""
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _is_logger_call(node) -> bool:
    """Return whether one call writes to a logger, which never reaches a response."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _LOGGER_NAMES
    )


def _call_name(node) -> str | None:
    """Return the called name of a bare or attribute call."""
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def text_uses(handler: ast.ExceptHandler) -> list[tuple[str, ast.AST]]:
    """Return each use of the caught exception that can turn it into text, as (kind, node).

    The kind is the attribute name for an attribute read, and "text" for every other use.
    """
    name = handler.name
    uses: list[tuple[str, ast.AST]] = []

    def visit(node, parent=None):
        if _is_logger_call(node):
            return
        if isinstance(node, ast.Raise) and node.cause is not None:
            if node.exc is not None:
                visit(node.exc, node)
            return
        if isinstance(node, ast.Call) and _call_name(node) in {"hasattr", "isinstance", *SANITIZERS}:
            return
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == name:
            uses.append((node.attr, node))
            return
        if isinstance(node, ast.Name) and node.id == name:
            uses.append(("text", node))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, node)

    for statement in handler.body:
        visit(statement)
    return uses


def _qualified_handlers(tree):
    """Yield (qualified function, handler) for every named handler in one module."""

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = [*scope, child.name]
            if isinstance(child, ast.ExceptHandler) and child.name:
                yield ".".join(child_scope), child
            yield from walk(child, child_scope)

    yield from walk(tree, [])


class ModuleNames(Mapping):
    """The names one module can catch: its globals, any name it imports inside a function, then builtins.

    An import is resolved only when a handler names it, so an optional dependency that is absent
    here never has to load.
    """

    def __init__(self, tree, module_name: str, package: str):
        self._globals = vars(importlib.import_module(module_name))
        self._imports: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._imports.setdefault(alias.asname or alias.name.partition(".")[0], (alias.name, None))
            elif isinstance(node, ast.ImportFrom):
                source = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                for alias in node.names:
                    self._imports.setdefault(alias.asname or alias.name, (source, alias.name))

    def get(self, name: str, default=None):
        """Return the object one name means in this module, or *default*."""
        if name in self._globals:
            return self._globals[name]
        if name not in self._imports:
            return getattr(builtins, name, default)
        module, attribute = self._imports[name]
        if attribute is None:
            return importlib.import_module(module)
        source = importlib.import_module(module)
        return (
            getattr(source, attribute)
            if hasattr(source, attribute)
            else importlib.import_module(f"{module}.{attribute}")
        )

    def __getitem__(self, name: str):
        found = self.get(name, _MISSING)
        if found is _MISSING:
            raise KeyError(name)
        return found

    def __iter__(self):
        return iter({*self._globals, *self._imports})

    def __len__(self) -> int:
        return len({*self._globals, *self._imports})


_MISSING = object()


def caught_classes(handler: ast.ExceptHandler, namespace) -> list[type | str]:
    """Resolve the classes one handler catches, keeping the spelling of one bound only at run time."""
    if handler.type is None:
        return [BaseException]
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    classes: list[type | str] = []
    for node in nodes:
        parts = []
        target = node
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        value = namespace.get(target.id, _MISSING) if isinstance(target, ast.Name) else _MISSING
        if value is _MISSING:
            classes.append(ast.unparse(node))
            continue
        for part in reversed(parts):
            value = getattr(value, part)
        classes.append(cast(type, value))
    return classes


def _assignments(body):
    """Yield (value, assigned names) for every assignment form in one statement list."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
            value, targets = node.value, [node.target]
        else:
            continue
        yield value, {child.id for target in targets for child in ast.walk(target) if isinstance(child, ast.Name)}


def _tainted_names(body, names: set[str]) -> set[str]:
    """Return *names* and every name a statement list assigns from one of them, followed to a fixpoint."""
    tainted = set(names)
    assignments = list(_assignments(body))
    changed = True
    while changed:
        changed = False
        for value, assigned in assignments:
            if _names_in(value) & tainted and not assigned <= tainted:
                tainted |= assigned
                changed = True
    return tainted


def _carries(call: ast.Call, tainted: set[str]) -> bool:
    """Return whether one call receives a tainted value."""
    return any(_names_in(argument) & tainted for argument in (*call.args, *(item.value for item in call.keywords)))


def _built_with(body, tainted: set[str]) -> dict[str, str]:
    """Return each name bound to a call that receives a tainted value, mapped to the called name, aliases followed."""
    built: dict[str, str] = {}
    assignments = list(_assignments(body))
    size = -1
    while size != len(built):
        size = len(built)
        for value, assigned in assignments:
            if isinstance(value, ast.Call) and _carries(value, tainted):
                built.update(dict.fromkeys(assigned, _call_name(value) or ""))
            elif isinstance(value, ast.Name) and value.id in built:
                built.update(dict.fromkeys(assigned, built[value.id]))
    return built


def _tainted_parameters(helper: ast.FunctionDef, call: ast.Call, tainted: set[str]) -> set[str]:
    """Return the helper parameters one call binds to a tainted value, `*args` and `**kwargs` included."""
    signature = helper.args
    positional = [parameter.arg for parameter in (*signature.posonlyargs, *signature.args)]
    named = {*positional, *(parameter.arg for parameter in signature.kwonlyargs)}
    variadic = {signature.vararg.arg} if signature.vararg else set()
    keywords = {signature.kwarg.arg} if signature.kwarg else set()
    bound: set[str] = set()
    for index, argument in enumerate(call.args):
        if not _names_in(argument) & tainted:
            continue
        # An unpacked argument can fill this parameter and every later one.
        unpacked = isinstance(argument, ast.Starred)
        bound |= set(positional[index:] if unpacked else positional[index : index + 1])
        if unpacked or index >= len(positional):
            bound |= variadic
    for keyword in call.keywords:
        if not _names_in(keyword.value) & tainted:
            continue
        if keyword.arg is None:
            bound |= named | keywords
        else:
            bound |= {keyword.arg} if keyword.arg in named else keywords
    return bound


def _raised_with(body, names: set[str], functions: dict, seen: frozenset = frozenset()) -> set[str]:
    """Return the class names a statement list raises with the text of *names*, through same-module helpers."""
    tainted = _tainted_names(body, names)
    built = _built_with(body, tainted)
    raised: set[str] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and _carries(node.exc, tainted):
            raised.add(_call_name(node.exc) or "")
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Name) and node.exc.id in built:
            raised.add(built[node.exc.id])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            helper = functions[node.func.id]
            parameters = _tainted_parameters(helper, node, tainted)
            if parameters and helper.name not in seen:
                raised |= _raised_with(helper.body, parameters, functions, seen | {helper.name})
    return raised


def plan_error_carriers(sources: dict[str, tuple[ast.Module, Mapping]]) -> set[type]:
    """Return the classes some handler raises with the text of a caught `PlanError`, followed to a fixpoint."""
    carriers: set[type] = {PlanError}
    changed = True
    while changed:
        changed = False
        for tree, namespace in sources.values():
            functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
            for _qualified, handler in _qualified_handlers(tree):
                caught = caught_classes(handler, namespace)
                if not any(isinstance(cls, type) and issubclass(cls, tuple(carriers)) for cls in caught):
                    continue
                for name in _raised_with(handler.body, {handler.name}, functions):
                    raised = namespace.get(name)
                    if isinstance(raised, type) and raised not in carriers:
                        carriers.add(raised)
                        changed = True
    return carriers


def _render_allowed(cls: type, kinds: set[str], carriers: set[type]) -> bool:
    """Return whether the policy lets a response show these uses of one caught class."""
    if issubclass(cls, (*NEVER_RENDERED, *carriers)):
        return False
    if issubclass(cls, PublicRefusal):
        return kinds <= {"operator_message", "status", "reason"}
    if issubclass(cls, ValidationError):
        return kinds <= VALIDATION_ATTRIBUTES
    return cls.__module__.startswith(f"{PACKAGE_NAME}.")


def exposed_renders(responses, sources, audited=None) -> list[tuple[str, str, int]]:
    """Return (qualified function, class name, line) for each caught text a response may not show."""
    audited = AUDITED_RENDERS if audited is None else audited
    carriers = plan_error_carriers(sources)
    reported = []
    for tree, namespace in responses:
        for qualified, handler in _qualified_handlers(tree):
            uses = text_uses(handler)
            if not uses:
                continue
            kinds = {kind for kind, _node in uses}
            for cls in caught_classes(handler, namespace):
                # A class the scan cannot name is reported, since nothing shows that its text is curated.
                name = cls.__name__ if isinstance(cls, type) else cls
                if (isinstance(cls, type) and _render_allowed(cls, kinds, carriers)) or (qualified, name) in audited:
                    continue
                reported.append((qualified, name, handler.lineno))
    return reported


def _package_sources() -> dict[str, tuple[ast.Module, Mapping]]:
    """Parse every production module of the package with the names it can catch."""
    sources: dict[str, tuple[ast.Module, Mapping]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] in {"tests", "migrations"}:
            continue
        parts = (PACKAGE_NAME, *relative.with_suffix("").parts)
        package = ".".join(parts[:-1])
        module = package if parts[-1] == "__init__" else ".".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sources[str(relative)] = (tree, ModuleNames(tree, module, package))
    return sources


class ExceptionTextScannerTest(SimpleTestCase):
    """The scanner's own contract, driven by source strings rather than the package."""

    def scan(self, view_source, engine_source=""):
        """Scan one fixture view module, with an optional fixture engine module as a wrapping source."""
        from netbox_data_import.import_engine import SelectionError, StalePlan
        from netbox_data_import.plan import PlanInvalid

        namespace = {
            "DatabaseError": DatabaseError,
            "PlanError": PlanError,
            "PlanInvalid": PlanInvalid,
            "SelectionError": SelectionError,
            "StalePlan": StalePlan,
            "ValidationError": ValidationError,
            "ValueError": ValueError,
        }
        view = ast.parse(view_source)
        sources = {"views.py": (view, namespace)}
        if engine_source:
            sources["engine.py"] = (ast.parse(engine_source), namespace)
        return [(row[0], row[1]) for row in exposed_renders([(view, namespace)], sources, audited={})]

    def test_a_rendered_plan_error_is_reported(self):
        source = "def get():\n    try:\n        load()\n    except PlanInvalid as exc:\n        return Json(str(exc))\n"
        self.assertEqual(self.scan(source), [("get", "PlanInvalid")])

    def test_a_curated_type_that_carries_a_plan_error_is_reported(self):
        engine = "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n        raise SelectionError(str(exc)) from exc\n"
        view = "def post():\n    try:\n        run()\n    except (SelectionError, StalePlan) as exc:\n        return Json(exc.operator_message)\n"
        self.assertEqual(self.scan(view), [])
        self.assertEqual(self.scan(view, engine), [("post", "SelectionError")])

    def test_a_plan_error_text_carried_through_a_variable_or_a_helper_is_reported(self):
        view = "def post():\n    try:\n        run()\n    except SelectionError as exc:\n        return Json(exc.operator_message)\n"
        engines = (
            (
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n"
                "        detail = str(exc)\n        message = f'{detail}.'\n        raise SelectionError(message) from exc\n"
            ),
            (
                "def _wrap(error):\n    raise SelectionError(str(error))\n"
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n        _wrap(exc)\n"
            ),
            (
                "def _wrap(prefix, *, error):\n    text = repr(error)\n    raise SelectionError(prefix + text)\n"
                "def merge():\n    try:\n        order()\n    except PlanInvalid as caught:\n"
                "        _wrap('x', error=caught)\n"
            ),
            (
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n"
                "        wrapped = SelectionError(str(exc))\n        raise wrapped from exc\n"
            ),
            (
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n"
                "        wrapped: Exception = SelectionError(str(exc))\n        again = wrapped\n        raise again\n"
            ),
            (
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n"
                "        if flag:\n            wrapped = SelectionError(str(exc))\n        again = wrapped\n        raise again\n"
            ),
            (
                "def _wrap(*parts):\n    raise SelectionError(' '.join(str(part) for part in parts))\n"
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n        _wrap('x', exc)\n"
            ),
            (
                "def _wrap(prefix, error):\n    raise SelectionError(str(error))\n"
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n        _wrap(*('x', exc))\n"
            ),
            (
                "def _wrap(error):\n    raise SelectionError(str(error))\n"
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n        _wrap(**{'error': exc})\n"
            ),
            (
                "def _wrap(**parts):\n    raise SelectionError(str(parts))\n"
                "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n        _wrap(error=exc)\n"
            ),
        )
        self.assertEqual(self.scan(view), [])
        for engine in engines:
            with self.subTest(engine=engine):
                self.assertEqual(self.scan(view, engine), [("post", "SelectionError")])

    def test_a_curated_type_raised_with_a_fixed_sentence_stays_curated(self):
        engine = "def merge():\n    try:\n        order()\n    except PlanInvalid as exc:\n        raise SelectionError(FIXED) from exc\n"
        view = "def post():\n    try:\n        run()\n    except SelectionError as exc:\n        return Json(exc.operator_message)\n"
        self.assertEqual(self.scan(view, engine), [])

    def test_public_refusals_use_only_the_authored_message(self):
        raw = "def post():\n    try:\n        run()\n    except SelectionError as exc:\n        return str(exc)\n"
        public = "def post():\n    try:\n        run()\n    except SelectionError as exc:\n        return exc.operator_message\n"
        for expression in ("str(exc)", "repr(exc)", "exc.args[0]"):
            with self.subTest(expression=expression):
                self.assertEqual(self.scan(raw.replace("str(exc)", expression)), [("post", "SelectionError")])
        self.assertEqual(self.scan(public), [])

    def test_a_builtin_text_is_reported_and_a_logged_one_is_not(self):
        shown = "def post():\n    try:\n        run()\n    except ValueError as exc:\n        messages.error(request, exc)\n"
        logged = (
            "def post():\n    try:\n        run()\n    except ValueError as exc:\n        logger.warning('%s', exc)\n"
        )
        self.assertEqual(self.scan(shown), [("post", "ValueError")])
        self.assertEqual(self.scan(logged), [])

    def test_a_validation_error_is_shown_through_its_messages_only(self):
        joined = "def post():\n    try:\n        run()\n    except ValidationError as exc:\n        return '; '.join(exc.messages)\n"
        raw = "def post():\n    try:\n        run()\n    except ValidationError as exc:\n        return str(exc)\n"
        self.assertEqual(self.scan(joined), [])
        self.assertEqual(self.scan(raw), [("post", "ValidationError")])

    def test_a_database_error_passed_to_its_sanitizer_is_not_reported(self):
        sanitized = "def post():\n    try:\n        run()\n    except DatabaseError as exc:\n        return operator_failure_message(exc)\n"
        shown = "def post():\n    try:\n        run()\n    except DatabaseError as exc:\n        return {'error': exc.args[0]}\n"
        self.assertEqual(self.scan(sanitized), [])
        self.assertEqual(self.scan(shown), [("post", "DatabaseError")])


class ViewExceptionTextTest(SimpleTestCase):
    """The package's views follow the curated-type policy."""

    maxDiff = None

    def test_no_view_renders_exception_text_the_policy_does_not_allow(self):
        sources = _package_sources()
        responses = [sources[module] for module in RESPONSE_MODULES]

        self.assertEqual(exposed_renders(responses, sources), [])

    def test_every_audited_render_is_still_in_the_views(self):
        sources = _package_sources()
        caught = {
            (qualified, cls.__name__)
            for module in RESPONSE_MODULES
            for qualified, handler in _qualified_handlers(sources[module][0])
            for cls in caught_classes(handler, sources[module][1])
        }
        self.assertLessEqual(set(AUDITED_RENDERS), caught)

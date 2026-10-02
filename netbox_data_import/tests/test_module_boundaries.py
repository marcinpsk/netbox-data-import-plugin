# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Views and jobs use only the public target-neutral import seams."""

import ast
import pathlib
import re
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_NAME = PACKAGE.name
CALLERS = ("views.py", "jobs.py", "preview_coordinator.py")
INTERPRETERS = ("flat_workbook.py", "trace_workbook.py")
TARGET_MODULES = ("target_modules.py", "cable_target.py")
FORBIDDEN_TARGET_MODULE_IMPORTS = frozenset({"flat_workbook", "trace_workbook", "views"})
FORBIDDEN_INTERPRETER_IMPORTS = frozenset(
    {
        "django",
        "circuits",
        "core",
        "dcim",
        "extras",
        "ipam",
        "netbox",
        "tenancy",
        "utilities",
        "adapter_config",
        "adapter_forms",
        "api",
        "forms",
        "import_engine",
        "jobs",
        "models",
        "netbox_reader",
        "plan",
        "target_modules",
        "views",
    }
)
PERMISSION_CONSTRAINT_INTERNALS = frozenset({"qs_filter_from_constraints", "_object_perm_cache"})


def _import_engine_calls(path: pathlib.Path) -> set[str]:
    """Return attributes referenced directly on `ImportEngine` in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ImportEngine"
    }


def _import_root(name: str) -> str:
    """Return the module a dotted name belongs to, with the plugin's own package stripped."""
    parts = name.removeprefix(f"{PACKAGE_NAME}.").partition(".")
    return parts[0]


def _imported_roots(path: pathlib.Path) -> set[str]:
    """Return the module of every import in one file, including imports inside a function."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(_import_root(name.name) for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            # `from netbox_data_import import models` names the module in the imported names.
            if node.module in (None, PACKAGE_NAME):
                roots.update(_import_root(name.name) for name in node.names)
            else:
                roots.add(_import_root(node.module))
    return roots


NAME_FOLDS = frozenset({"casefold", "Upper", "Lower", "__iexact"})


def _name_folds(source: str) -> set[str]:
    """Return each way one module folds a name outside the identity module: casefold, Upper, Lower, iexact."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "casefold":
            found.add("casefold")
        elif isinstance(node, ast.Name) and node.id in NAME_FOLDS:
            found.add(node.id)
        elif isinstance(node, ast.alias) and node.name.rpartition(".")[2] in NAME_FOLDS:
            found.add(node.name.rpartition(".")[2])
        elif (isinstance(node, ast.keyword) and (node.arg or "").endswith("__iexact")) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and "__iexact" in node.value
        ):
            found.add("__iexact")
    return found


# NetBox models whose `name` the planner compares by name identity.
NAME_IDENTITY_MODELS = frozenset(
    {
        "Device",
        "Rack",
        "Location",
        "Interface",
        "FrontPort",
        "RearPort",
        "ConsolePort",
        "ConsoleServerPort",
        "PowerPort",
        "PowerOutlet",
    }
)
EXACT_NAME_KEYWORDS = frozenset({"name", "name__in", "name__exact"})
QUERY_METHODS = frozenset({"filter", "exclude", "get", "get_or_create", "update_or_create"})
SHORTCUTS = frozenset({"get_object_or_404", "get_list_or_404"})


def _callee(call: ast.Call) -> str:
    """Return the name a call reaches, bare or qualified."""
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _query_root(node) -> str:
    """Return the model a queryset chain or model reference names, `type` for `type(obj)`, or an empty string."""
    while isinstance(node, (ast.Attribute, ast.Call)):
        if isinstance(node, ast.Call):
            if _callee(node) == "type" and isinstance(node.func, ast.Name):
                return "type"
            node = node.func
        elif node.attr in NAME_IDENTITY_MODELS:
            return node.attr
        else:
            node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _q_predicates(node):
    """Yield every `Q(...)` call of a filter argument, through `&`, `|`, `~` and nested `Q` arguments."""
    if isinstance(node, ast.BinOp):
        yield from (*_q_predicates(node.left), *_q_predicates(node.right))
    elif isinstance(node, ast.UnaryOp):
        yield from _q_predicates(node.operand)
    elif isinstance(node, ast.Call) and _callee(node) == "Q":
        yield node
        for argument in node.args:
            yield from _q_predicates(argument)


def _names_exactly(call: ast.Call, arguments) -> bool:
    """Return whether a call, or a `Q` among *arguments*, has an exact `name` keyword or literal `**{...}` key."""
    for predicate in (call, *(q for argument in arguments for q in _q_predicates(argument))):
        for keyword in predicate.keywords:
            literal = keyword.value.keys if keyword.arg is None and isinstance(keyword.value, ast.Dict) else []
            keys = [keyword.arg, *(key.value for key in literal if isinstance(key, ast.Constant))]
            if EXACT_NAME_KEYWORDS.intersection(keys):
                return True
    return False


def _exact_name_lookups(source: str) -> set[int]:
    """Return the line of each exact `name` lookup on a named NetBox model or a `type(obj)` queryset.

    A queryset held in a variable or returned by a helper, such as `reader.devices()`, or a computed key escapes it.
    """
    found = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in QUERY_METHODS:
            root, arguments = _query_root(node.func.value), node.args
        elif _callee(node) in SHORTCUTS and node.args:
            root, arguments = _query_root(node.args[0]), node.args[1:]
        else:
            continue
        if root in {*NAME_IDENTITY_MODELS, "type"} and _names_exactly(node, arguments):
            found.add(node.lineno)
    return found


JS_FOLD = re.compile(
    r"\.(toLowerCase|toUpperCase|toLocaleLowerCase|toLocaleUpperCase|localeCompare)\s*\(|Intl\.Collator"
)
# Each browser case fold that is not a name comparison, by file and stripped line, with the reason it may fold.
JS_FOLD_ALLOWED = {
    (
        "static/netbox_data_import/js/contact_candidate_modal.js",
        (
            "blank.setCustomValidity('Give this row a ' + (ROLE_LABELS[role] || role).toLowerCase() + ', or select no "
            "contact.');"
        ),
    ): "message text",
    (
        "static/netbox_data_import/js/preview_row_controls.js",
        "var text = (filterInput ? filterInput.value : '').toLowerCase().trim();",
    ): "free-text row filter of the rendered table",
    (
        "static/netbox_data_import/js/preview_row_controls.js",
        "var action = (actionSelect ? actionSelect.value : '').toLowerCase();",
    ): "action filter value",
    (
        "static/netbox_data_import/js/preview_row_controls.js",
        "var textMatch = !text || row.textContent.toLowerCase().includes(text);",
    ): "free-text row filter of the rendered table",
    (
        "static/netbox_data_import/js/preview_row_controls.js",
        "var rowAction = (row.dataset.action || '').toLowerCase();",
    ): "action filter value",
    (
        "templates/netbox_data_import/import_preview.html",
        "var slug = name.toLowerCase()",
    ): "slug suggestion for a new Contact Role",
}


def _js_folds(root: pathlib.Path) -> set[tuple[str, str]]:
    """Return each browser case fold under *root*, as its file path and its stripped line."""
    found = set()
    for path in sorted([*root.glob("static/**/*.js"), *root.glob("templates/**/*.html")]):
        for line in path.read_text(encoding="utf-8").splitlines():
            if JS_FOLD.search(line):
                found.add((str(path.relative_to(root)), line.strip()))
    return found


def _referenced_names(path: pathlib.Path) -> set[str]:
    """Return every name one module imports or reads, ignoring comments and docstrings."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.name.rpartition(".")[2] for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _constraint_offenders(root: pathlib.Path) -> dict[str, list[str]]:
    """Return every module under `root` except its own owner that reads NetBox constraint state."""
    owner = root / "object_permissions.py"
    return {
        str(path.relative_to(root)): sorted(names)
        for path in root.rglob("*.py")
        if "tests" not in path.relative_to(root).parts
        and path != owner
        and (names := _referenced_names(path) & PERMISSION_CONSTRAINT_INTERNALS)
    }


def _imports_target_modules(path: pathlib.Path) -> bool:
    """Return whether a caller bypasses the coordinator for a Target Module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in {"target_modules", "netbox_data_import.target_modules"}:
                return True
            package_import = node.module == "netbox_data_import" or (node.level and node.module is None)
            if package_import and any(name.name == "target_modules" for name in node.names):
                return True
        if isinstance(node, ast.Import) and any(name.name.endswith("target_modules") for name in node.names):
            return True
    return False


class TargetNeutralCallerBoundaryTest(SimpleTestCase):
    """The cutover leaves no route back to fixed passes or Target Module writes."""

    def test_the_legacy_engine_is_deleted(self):
        self.assertFalse((PACKAGE / "engine.py").exists())

    def test_proposal_protocol_and_persistence_share_one_contract(self):
        """Response validation, prompting, and storage cannot drift independently."""
        from netbox_data_import import proposal_contract, proposal_jobs, proposal_response
        from netbox_data_import.models import ProposalOutcome

        self.assertIs(ProposalOutcome.CHOICES, proposal_contract.OUTCOME_CHOICES)
        self.assertIs(proposal_response.OUTCOMES, proposal_contract.OUTCOMES)
        self.assertIs(proposal_response.RESPONSE_MEMBERS, proposal_contract.RESPONSE_MEMBERS)
        for member in proposal_contract.RESPONSE_MEMBER_NAMES:
            self.assertIn(member, proposal_jobs.SYSTEM_INSTRUCTION)

    def test_the_architecture_guidance_names_the_public_coordinator(self):
        guidance = PACKAGE.parent / "AGENTS.md"
        architecture = (
            guidance.read_text(encoding="utf-8")
            .partition("## Architecture")[2]
            .partition("## Development environment")[0]
        )

        self.assertIn("`import_engine.py`", architecture)
        self.assertNotIn("`engine.py`", architecture)
        self.assertIn("plain Django models use suitable DRF bases", architecture)

    def test_views_and_jobs_call_only_the_public_coordinator_methods(self):
        calls = {name: _import_engine_calls(PACKAGE / name) for name in CALLERS}

        # A preview command runs a single row through the operation that replans in its own savepoint.
        self.assertEqual(
            calls,
            {"views.py": {"plan", "execute_and_replan"}, "jobs.py": {"execute"}, "preview_coordinator.py": {"plan"}},
        )

    def test_private_coordinator_attribute_references_are_detected(self):
        """The boundary rejects private access even when it is not a call."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "caller.py"
            path.write_text(
                "ImportEngine.plan()\nImportEngine.execute()\ncallback = ImportEngine._private_helper\n",
                encoding="utf-8",
            )

            self.assertIn("_private_helper", _import_engine_calls(path))

    def test_views_and_jobs_do_not_import_target_modules(self):
        self.assertEqual([name for name in CALLERS if _imports_target_modules(PACKAGE / name)], [])

    def test_relative_package_imports_of_target_modules_are_detected(self):
        """The boundary guard recognizes ``from . import target_modules``."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "caller.py"
            path.write_text("from . import target_modules\n", encoding="utf-8")

            self.assertTrue(_imports_target_modules(path))

    def test_absolute_package_imports_of_target_modules_are_detected(self):
        """The boundary guard recognizes imports from the absolute package."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "caller.py"
            path.write_text("from netbox_data_import import target_modules\n", encoding="utf-8")

            self.assertTrue(_imports_target_modules(path))

    def test_source_resolution_helpers_have_one_line_purpose_docstrings(self):
        """Implementation helpers keep their explanation at the module boundary."""
        from netbox_data_import.source_resolution import _apply_one_resolution, derive_effective_rows

        self.assertNotIn("\n", _apply_one_resolution.__doc__ or "")
        self.assertNotIn("\n", derive_effective_rows.__doc__ or "")

    def test_netbox_reader_has_one_line_purpose_docstrings(self):
        """The reader keeps architecture rationale outside implementation docstrings."""
        from netbox_data_import import netbox_reader

        for docstring in (
            netbox_reader.__doc__,
            netbox_reader.NetBoxReader.for_target.__doc__,
            netbox_reader.NetBoxReader.for_planning_context.__doc__,
        ):
            self.assertNotIn("\n", docstring or "")

    def test_device_identity_has_a_one_line_purpose_docstring(self):
        """The shared resolver keeps design rationale outside its module docstring."""
        from netbox_data_import import device_identity

        self.assertNotIn("\n", device_identity.__doc__ or "")

    def test_source_adapters_do_not_project_profile_policy(self):
        """A Source Adapter receives plain settings and never reads an Import Profile."""
        from netbox_data_import.adapters import FlatWorkbookAdapter, SourceAdapter

        self.assertFalse(hasattr(SourceAdapter, "config_for"))
        self.assertFalse(hasattr(FlatWorkbookAdapter, "config_for"))

    def test_source_interpretation_imports_no_target_state(self):
        """Section 2.2 gives a Source Adapter parsing libraries and the catalog, nothing else."""
        for name in INTERPRETERS:
            with self.subTest(module=name):
                self.assertEqual(_imported_roots(PACKAGE / name) & FORBIDDEN_INTERPRETER_IMPORTS, set())

    def test_the_interpreter_guard_reads_every_import_form(self):
        """A lazy import inside a function must not escape the boundary guard."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "interpreter.py"
            path.write_text("def parse():\n    from dcim.models import Device\n    return Device\n", encoding="utf-8")

            self.assertEqual(_imported_roots(path) & FORBIDDEN_INTERPRETER_IMPORTS, {"dcim"})

    def test_target_modules_import_no_source_adapter_implementation(self):
        """Section 2.2: a Target Module consumes typed source items, never a parser."""
        for name in TARGET_MODULES:
            with self.subTest(module=name):
                self.assertEqual(_imported_roots(PACKAGE / name) & FORBIDDEN_TARGET_MODULE_IMPORTS, set())

    def test_the_import_plan_imports_no_plugin_or_netbox_module(self):
        """ADR 0001: the plan is target-neutral, so a Target Module registers with it instead."""
        first_party = {path.stem for path in PACKAGE.glob("*.py")} | {
            path.name for path in PACKAGE.iterdir() if path.is_dir()
        }

        self.assertEqual(_imported_roots(PACKAGE / "plan.py") & (first_party | FORBIDDEN_INTERPRETER_IMPORTS), set())

    def test_only_the_identity_module_folds_names(self):
        """A second case rule in Python or SQL lets a key and a database comparison disagree."""
        offenders = {
            str(path.relative_to(PACKAGE)): folds
            for path in sorted(PACKAGE.rglob("*.py"))
            if not {"tests", "migrations"} & set(path.relative_to(PACKAGE).parts) and path.name != "identity.py"
            if (folds := _name_folds(path.read_text(encoding="utf-8")))
        }

        self.assertEqual(offenders, {})

    def test_the_name_fold_guard_reads_every_form(self):
        source = (
            "from django.db.models.functions import Upper\n"
            "def lookups(value, rows, field):\n"
            "    rows.filter(name__iexact=value)\n"
            "    rows.filter(**{f'{field}__iexact': value})\n"
            "    return value.casefold(), Lower('name')\n"
        )

        self.assertEqual(_name_folds(source), NAME_FOLDS)

    def test_no_netbox_name_is_looked_up_exactly(self):
        """The planner matches Devices, Racks, Locations and ports by name identity, so every other read does too."""
        offenders = {
            str(path.relative_to(PACKAGE)): lines
            for path in sorted(PACKAGE.rglob("*.py"))
            if not {"tests", "migrations"} & set(path.relative_to(PACKAGE).parts)
            if (lines := _exact_name_lookups(path.read_text(encoding="utf-8")))
        }

        self.assertEqual(offenders, {}, "compare the name with identity_in")

    def test_the_exact_name_guard_reads_model_chains_and_type_calls(self):
        source = (
            "def lookups(device, name, user):\n"
            "    Device.objects.filter(name=name)\n"
            "    Rack.objects.restrict(user, 'view').filter(site=device.site, name=name)\n"
            "    type(device).objects.filter(name__in=[name])\n"
            "    ContactRole.objects.filter(name=name)\n"
            "    Device.objects.filter(identity_in('name', [name]))\n"
        )

        self.assertEqual(_exact_name_lookups(source), {2, 3, 4})

    def test_the_exact_name_guard_reads_q_objects_literal_keywords_qualified_models_and_shortcuts(self):
        source = (
            "def lookups(device, name, user, site, field):\n"
            "    Device.objects.filter(Q(site=site) | Q(name=name))\n"
            "    Device.objects.exclude(~Q(Q(name__in=[name]), site=site))\n"
            "    Rack.objects.filter(**{'name': name})\n"
            "    dcim.models.Device.objects.get(name=name)\n"
            "    get_object_or_404(Device, name=name)\n"
            "    shortcuts.get_object_or_404(Rack.objects.restrict(user, 'view'), Q(name=name))\n"
            "    Device.objects.filter(Q(site=site), **{'site': site})\n"
            "    Device.objects.filter(rack__in=ContactRole.objects.filter(Q(name=name)))\n"
            "    get_object_or_404(ContactRole, name=name)\n"
            "    get_object_or_404(Device.objects.restrict(user, 'view'), pk=name)\n"
        )

        self.assertEqual(_exact_name_lookups(source), {2, 3, 4, 5, 6, 7})

    def test_browser_code_folds_case_only_where_the_allowlist_says_why(self):
        """A browser carries its own Unicode version, so it compares raw values and leaves identity to the server."""
        found = _js_folds(PACKAGE)

        self.assertEqual(sorted(found - set(JS_FOLD_ALLOWED)), [], "compare raw values; the server decides identity")
        self.assertEqual(sorted(set(JS_FOLD_ALLOWED) - found), [], "remove the allowlist entries that match nothing")

    def test_the_browser_fold_guard_reads_scripts_and_templates(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "static").mkdir()
            (root / "templates").mkdir()
            (root / "static" / "a.js").write_text("if (a.toLowerCase() === b.toUpperCase()) {}\n", encoding="utf-8")
            (root / "templates" / "b.html").write_text(
                "<script>names.sort((a, b) => a.localeCompare(b, undefined, {sensitivity: 'base'}));</script>\n",
                encoding="utf-8",
            )

            self.assertEqual(
                {path for path, _line in _js_folds(root)},
                {"static/a.js", "templates/b.html"},
            )

    def test_permission_constraint_parsing_has_one_owner(self):
        """Only the object permission module interprets NetBox constraint state."""
        self.assertEqual(_constraint_offenders(PACKAGE), {})

    def test_the_owner_exemption_covers_one_module_only(self):
        """A nested module cannot take the exemption by reusing the owner's file name."""
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "object_permissions.py").write_text(
                "from utilities.permissions import qs_filter_from_constraints\n", encoding="utf-8"
            )
            nested = root / "nested"
            nested.mkdir()
            (nested / "object_permissions.py").write_text(
                "from utilities.permissions import qs_filter_from_constraints\n", encoding="utf-8"
            )

            self.assertEqual(
                _constraint_offenders(root), {"nested/object_permissions.py": ["qs_filter_from_constraints"]}
            )

    def test_permission_constraint_owner_guard_reads_imports_and_attributes(self):
        """The ownership guard detects both supported access forms."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "permission_reader.py"
            path.write_text(
                "from utilities.permissions import qs_filter_from_constraints\nconstraints = actor._object_perm_cache\n",
                encoding="utf-8",
            )

            self.assertEqual(_referenced_names(path) & PERMISSION_CONSTRAINT_INTERNALS, PERMISSION_CONSTRAINT_INTERNALS)

    def test_no_first_party_module_names_the_cable_path_model(self):
        """Section 6.4: the plugin writes Cables and NetBox derives every path from them."""
        sources = (
            source for source in sorted(PACKAGE.rglob("*.py")) if "tests" not in source.relative_to(PACKAGE).parts
        )
        offenders = [str(source.relative_to(PACKAGE)) for source in sources if "CablePath" in _referenced_names(source)]

        self.assertEqual(offenders, [])

    def test_the_cable_path_guard_reads_an_import_and_a_call(self):
        """The guard reads code, so the rule can still be stated in prose."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "writer.py"
            path.write_text('"""Never touch CablePath."""\nfrom dcim.models import CablePath\n', encoding="utf-8")

            self.assertIn("CablePath", _referenced_names(path))

    def test_the_cable_path_guard_reads_an_attribute_reference(self):
        """A module that never imports the name still reads it through the package it imports."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "attribute_writer.py"
            path.write_text(
                '"""Reach it through the module."""\nfrom dcim import models\n\nmodels.CablePath.objects.all()\n',
                encoding="utf-8",
            )

            self.assertIn("CablePath", _referenced_names(path))

    def test_the_interpreter_guard_reads_a_package_root_import(self):
        """`from netbox_data_import import models` names the module in the imported names."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "interpreter.py"
            path.write_text("from netbox_data_import import models, target_modules\n", encoding="utf-8")

            self.assertEqual(
                _imported_roots(path) & FORBIDDEN_INTERPRETER_IMPORTS,
                {"models", "target_modules"},
            )

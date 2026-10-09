# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Preview state lives only in the Preview Coordinator, and every preview route reaches it (ADR 0004).

The session scan permits two uses: the coordinator reads the session key, and the inference-discovery
cache in `views.py` reads and writes its one key. Every other session access fails, so an alias, a
computed key, a nested write or a session passed to a helper cannot carry preview state either. The
literal scan refuses the retired session key names, and the ownership scan refuses the coordinator
model outside its owner. The route inventory classifies every URL, and checks that each preview
command and each claimed read reaches the coordinator interface.
"""

import ast
import inspect
import pathlib
import textwrap

from django.test import SimpleTestCase
from django.urls import URLPattern

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
OWNER = "preview_coordinator.py"
# The session attributes the coordinator reads to bind a preview to its session.
OWNER_SESSION_ATTRIBUTES = frozenset({"session_key", "save", "get_expiry_date"})
DISCOVERY_KEY = "_INFERENCE_MODELS_SESSION_KEY"
# `import_execution_id` is not listed: the import Job stores its execution under that key.
RETIRED_KEYS = frozenset(
    {
        "import_plan",
        "import_preview_revision",
        "import_preview_dirty",
        "import_preview_pending",
        "import_preview_use_materialized_once",
        "import_context",
        "import_rows",
        "import_unused_columns",
        "import_preview_source_job_id",
        "import_background_job_id",
        "import_idempotency_key",
        "import_restored_execution_id",
    }
)
MODEL_OWNERS = frozenset({"models.py", OWNER})

COMMANDS = frozenset(
    {
        "import_setup",
        "import_run",
        "preview_reread",
        "preview_discard",
        "import_restore",
        "ignore_device",
        "unignore_device",
        "ignore_field_difference",
        "unignore_field_difference",
        "sync_device_field",
        "sync_placement",
        "save_resolution",
        "resolve_duplicate_name",
        "ignore_duplicate_serial",
        "ignore_position",
        "quick_resolve_manufacturer",
        "quick_resolve_device_type",
        "quick_add_class_mapping",
        "quick_add_column_mapping",
        "quick_create_role",
        "match_existing_device",
        "auto_match_devices",
        "trace_sync",
        "trace_sync_cancel",
        "trace_cable_policy",
        "trace_segment_policy",
        "trace_resolve_termination",
        "trace_resolve_device",
        "trace_location_mapping",
        "trace_request_proposal",
        "trace_request_all_proposals",
        "trace_cancel_proposal",
        "trace_accept_proposal",
        "trace_reject_proposal",
        "sync_single_row",
        "unlink_device",
    }
)
# A read that answers a question its page displays, so it must name that exact preview.
CLAIMED_READS = frozenset(
    {
        "contact_suggestion",
        "trace_termination_candidates",
        "trace_device_candidates",
        "trace_location_candidates",
        "trace_proposal",
        "trace_sync_status",
    }
)
# A page load reads the preview and writes nothing.
PAGES = frozenset({"import_preview", "trace_workspace", "import_progress", "import_progress_status"})
STANDALONE = frozenset(
    {
        "importprofile_list",
        "importprofile_add",
        "importprofile_bulk_import",
        "importprofile_bulk_edit",
        "importprofile",
        "importprofile_edit",
        "importprofile_delete",
        "importprofile_changelog",
        "importprofile_bulk_delete",
        "columnmapping_add",
        "columnmapping_edit",
        "columnmapping_delete",
        "classrolemapping_add",
        "classrolemapping_edit",
        "classrolemapping_delete",
        "cableclassmapping_add",
        "cableclassmapping_edit",
        "cableclassmapping_delete",
        "devicetypemapping_add",
        "devicetypemapping_edit",
        "devicetypemapping_delete",
        "inferencebackend_list",
        "inferencebackend_add",
        "inferencebackend",
        "inferencebackend_edit",
        "inferencebackend_delete",
        "inferencebackend_changelog",
        "inferencebackend_connection_test",
        "columntransformrule_add",
        "columntransformrule_edit",
        "columntransformrule_delete",
        "import_results",
        "remove_extra_ip",
        "contact_lookup",
        "source_resolution_list",
        "source_resolution_delete",
        "device_type_analysis",
        "device_type_analysis_profile",
        "bulk_yaml_import",
        "exportprofile_yaml",
        "import_profile_yaml",
        "check_device",
        "search_objects",
        "importexecution_list",
    }
)


def _production_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        if (PACKAGE / "tests") in path.parents or (PACKAGE / "migrations") in path.parents:
            continue
        yield path


def _discovery_key(node) -> bool:
    return isinstance(node, ast.Name) and node.id == DISCOVERY_KEY


def _permitted_session_use(node: ast.Attribute, parents, name: str) -> bool:
    """Return whether one `<expr>.session` is one of the two permitted uses."""
    parent = parents.get(node)
    if name == OWNER:
        return isinstance(parent, ast.Attribute) and parent.attr in OWNER_SESSION_ATTRIBUTES
    if name != "views.py":
        return False
    if isinstance(parent, ast.Subscript):
        return _discovery_key(parent.slice)
    if isinstance(parent, ast.Attribute) and parent.attr == "pop":
        call = parents.get(parent)
        return isinstance(call, ast.Call) and bool(call.args) and _discovery_key(call.args[0])
    return False


def session_findings(source: str, name: str) -> list[str]:
    """Return one entry per session access in `source` that is not one of the permitted uses."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dynamic_session_lookup(node):
            found.append(f"{name}:{node.lineno}: reaches the session by a computed attribute name")
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "session"
            and not _permitted_session_use(node, parents, name)
        ):
            found.append(f"{name}:{node.lineno}: uses the session")
    return sorted(found, key=lambda entry: int(entry.split(":")[1]))


def _dynamic_session_lookup(node) -> bool:
    """Return whether a call reaches `session` by name, or reaches a request attribute by a computed name."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "setattr", "delattr", "hasattr"}
        and len(node.args) >= 2
    ):
        return False
    attribute = node.args[1]
    if isinstance(attribute, ast.Constant):
        return attribute.value == "session"
    receiver = node.args[0]
    return (isinstance(receiver, ast.Name) and receiver.id in {"request", "req"}) or (
        isinstance(receiver, ast.Attribute) and receiver.attr == "request"
    )


def literal_findings(source: str, name: str) -> list[str]:
    """Return one entry per retired preview session key spelled in `source`."""
    return [
        f"{name}:{node.lineno}: names the retired session key {node.value!r}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and node.value in RETIRED_KEYS
    ]


def model_findings(source: str, name: str) -> list[str]:
    """Return one entry per reference to the coordinator model outside its owners."""
    if name in MODEL_OWNERS:
        return []
    return [
        f"{name}:{node.lineno}: reaches PreviewCoordinator outside its owner"
        for node in ast.walk(ast.parse(source))
        if (isinstance(node, ast.Name) and node.id == "PreviewCoordinator")
        or (isinstance(node, ast.Attribute) and node.attr == "PreviewCoordinator")
        or (isinstance(node, ast.alias) and node.name == "PreviewCoordinator")
    ]


def _scan(finder) -> list[str]:
    found = []
    for path in _production_files():
        found.extend(finder(path.read_text(encoding="utf-8"), str(path.relative_to(PACKAGE))))
    return found


class PreviewStateHasOneOwnerTest(SimpleTestCase):
    """No production module keeps preview state anywhere but the coordinator."""

    def test_no_module_uses_the_session_beyond_the_two_permitted_uses(self):
        self.assertEqual(_scan(session_findings), [], "Keep preview state in the Preview Coordinator.")

    def test_no_module_names_a_retired_session_key(self):
        self.assertEqual(_scan(literal_findings), [])

    def test_only_the_owner_reaches_the_coordinator_model(self):
        self.assertEqual(_scan(model_findings), [], "Go through setup_claim, read_preview or apply_preview_command.")

    def test_the_permitted_uses_are_present_so_the_scan_is_meaningful(self):
        """A scan that sees no session at all would pass this suite while proving nothing."""
        for name in (OWNER, "views.py"):
            with self.subTest(module=name):
                source = (PACKAGE / name).read_text(encoding="utf-8")
                self.assertIn(".session", source)
                self.assertEqual(session_findings(source, name), [])


class SessionScanTest(SimpleTestCase):
    """Self-tests of the scans on deliberate bypasses, so one is caught before it is used."""

    def test_finds_an_alias_of_the_session(self):
        source = "def f(request):\n    s = request.session\n    s['x'] = 1\n"
        self.assertEqual(session_findings(source, "views.py"), ["views.py:2: uses the session"])

    def test_finds_a_computed_key(self):
        source = "def f(request, key):\n    request.session[key + 'plan'] = 1\n"
        self.assertEqual(session_findings(source, "views.py"), ["views.py:2: uses the session"])

    def test_finds_nested_mutation_under_the_permitted_key(self):
        source = f"def f(request):\n    request.session[{DISCOVERY_KEY}]['plan'] = 1\n    request.session.get('x')\n"
        self.assertEqual(session_findings(source, "views.py"), ["views.py:3: uses the session"])

    def test_finds_update_setdefault_clear_and_a_session_passed_along(self):
        source = (
            "def f(request, keep):\n"
            "    request.session.update(plan=1)\n"
            "    request.session.setdefault('k', 1)\n"
            "    request.session.clear()\n"
            "    keep(request.session)\n"
        )
        self.assertEqual(
            session_findings(source, "views.py"),
            [f"views.py:{line}: uses the session" for line in (2, 3, 4, 5)],
        )

    def test_finds_a_dynamic_attribute_lookup(self):
        source = "def f(request, row, name):\n    getattr(request, 'session')\n    getattr(request, name)\n    getattr(row, name)\n"
        self.assertEqual(len(session_findings(source, "views.py")), 2)

    def test_finds_computed_attributes_on_request_receivers(self):
        for receiver in ("request", "req", "self.request", "view.request"):
            for function in ("getattr", "setattr", "delattr", "hasattr"):
                with self.subTest(receiver=receiver, function=function):
                    source = f"def f(request, req, self, view, name):\n    {function}({receiver}, name)\n"
                    self.assertEqual(
                        session_findings(source, "views.py"),
                        ["views.py:2: reaches the session by a computed attribute name"],
                    )

    def test_permits_only_the_discovery_key_in_views_and_only_the_key_in_the_owner(self):
        views_source = f"def f(request):\n    request.session.pop({DISCOVERY_KEY}, None)\n    request.session[{DISCOVERY_KEY}] = 1\n"
        owner_source = "def f(request):\n    return request.session.session_key\n"
        self.assertEqual(session_findings(views_source, "views.py"), [])
        self.assertEqual(session_findings(owner_source, OWNER), [])
        self.assertEqual(len(session_findings(owner_source, "jobs.py")), 1)
        self.assertEqual(len(session_findings("def f(request):\n    request.session.pop('k')\n", OWNER)), 1)

    def test_finds_a_retired_key_and_the_model_outside_its_owner(self):
        source = "from .models import PreviewCoordinator\nKEY = 'import_context'\nPreviewCoordinator.objects.all()\n"
        self.assertEqual(len(literal_findings(source, "views.py")), 1)
        self.assertEqual(len(model_findings(source, "views.py")), 2)
        self.assertEqual(model_findings(source, OWNER), [])


def _calls(function) -> list[ast.Call]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def reaches(view_class, method_name: str, target: str, *, keyword: str | None = None) -> bool:
    """Return whether one view method calls `target`, through its class and `views` module helpers."""
    from netbox_data_import import views

    queue, seen = [getattr(view_class, method_name)], set()
    while queue:
        function = queue.pop()
        if function in seen:
            continue
        seen.add(function)
        for call in _calls(function):
            func = call.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == target and (keyword is None or any(item.arg == keyword for item in call.keywords)):
                return True
            if isinstance(func, ast.Name) and inspect.isfunction(helper := getattr(views, name, None)):
                if helper.__module__ == views.__name__:
                    queue.append(helper)
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in {"self", "cls"}
                and callable(getattr(view_class, func.attr, None))
            ):
                queue.append(getattr(view_class, func.attr))
    return False


def _routes() -> dict:
    from netbox_data_import import urls

    return {
        pattern.name: pattern.callback.view_class
        for pattern in urls.urlpatterns
        if isinstance(pattern, URLPattern) and hasattr(pattern.callback, "view_class")
    }


class RouteInventoryTest(SimpleTestCase):
    """Every route is classified, and every preview route reaches the coordinator interface."""

    def test_every_route_is_classified_once(self):
        classified = [COMMANDS, CLAIMED_READS, PAGES, STANDALONE]
        routes = _routes()
        self.assertEqual(sorted(set(routes) - set().union(*classified)), [], "Classify each new route above.")
        self.assertEqual(sorted(set().union(*classified) - set(routes)), [], "Remove each retired route above.")
        self.assertEqual(sum(len(group) for group in classified), len(set().union(*classified)))

    def test_every_preview_command_runs_through_the_coordinator(self):
        routes = _routes()
        for name in sorted(COMMANDS):
            with self.subTest(route=name):
                self.assertTrue(reaches(routes[name], "post", "apply_preview_command"))

    def test_every_claimed_read_names_the_preview_its_page_displays(self):
        routes = _routes()
        for name in sorted(CLAIMED_READS):
            with self.subTest(route=name):
                self.assertTrue(reaches(routes[name], "get", "read_preview", keyword="expected"))

    def test_every_page_reads_the_coordinator_and_runs_no_command(self):
        routes = _routes()
        for name in sorted(PAGES | {"import_setup"}):
            with self.subTest(route=name):
                self.assertTrue(reaches(routes[name], "get", "read_preview"))
                self.assertFalse(reaches(routes[name], "get", "apply_preview_command"))

    def test_the_route_scan_follows_helpers_and_can_fail(self):
        """A scan that answers True for everything would pass the three tests above."""
        from netbox_data_import import views

        self.assertTrue(reaches(views.TraceDeviceCandidatesView, "get", "read_preview", keyword="expected"))
        self.assertFalse(reaches(views.ImportResultsView, "get", "read_preview"))
        self.assertFalse(reaches(views.RemoveExtraIpView, "post", "apply_preview_command"))

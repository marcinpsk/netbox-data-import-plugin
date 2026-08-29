# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Views and jobs use only the public target-neutral import seams."""

import ast
import pathlib

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
CALLERS = ("views.py", "jobs.py")


def _import_engine_calls(path: pathlib.Path) -> set[str]:
    """Return methods called directly on `ImportEngine` in one module."""
    tree = ast.parse(path.read_text())
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ImportEngine"
    }


def _imports_target_modules(path: pathlib.Path) -> bool:
    """Return whether a caller bypasses the coordinator for a Target Module."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"target_modules", "netbox_data_import.target_modules"}:
            return True
        if isinstance(node, ast.Import) and any(name.name.endswith("target_modules") for name in node.names):
            return True
    return False


class TargetNeutralCallerBoundaryTest(SimpleTestCase):
    """The cutover leaves no route back to fixed passes or Target Module writes."""

    def test_the_legacy_engine_is_deleted(self):
        self.assertFalse((PACKAGE / "engine.py").exists())

    def test_the_architecture_guidance_names_the_public_coordinator(self):
        guidance = PACKAGE.parent / "AGENTS.md"
        architecture = guidance.read_text().partition("## Architecture")[2].partition("## Development environment")[0]

        self.assertIn("`import_engine.py`", architecture)
        self.assertNotIn("`engine.py`", architecture)

    def test_views_and_jobs_call_only_the_public_coordinator_methods(self):
        calls = {name: _import_engine_calls(PACKAGE / name) for name in CALLERS}

        self.assertEqual(calls, {"views.py": {"plan", "execute"}, "jobs.py": {"execute"}})

    def test_views_and_jobs_do_not_import_target_modules(self):
        self.assertEqual([name for name in CALLERS if _imports_target_modules(PACKAGE / name)], [])

    def test_source_adapters_do_not_project_profile_policy(self):
        """A Source Adapter receives plain settings and never reads an Import Profile."""
        from netbox_data_import.adapters import FlatWorkbookAdapter, SourceAdapter

        self.assertFalse(hasattr(SourceAdapter, "config_for"))
        self.assertFalse(hasattr(FlatWorkbookAdapter, "config_for"))

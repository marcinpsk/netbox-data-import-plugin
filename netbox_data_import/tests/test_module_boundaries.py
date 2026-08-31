# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Views and jobs use only the public target-neutral import seams."""

import ast
import pathlib
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
CALLERS = ("views.py", "jobs.py")


def _import_engine_calls(path: pathlib.Path) -> set[str]:
    """Return attributes referenced directly on `ImportEngine` in one module."""
    tree = ast.parse(path.read_text())
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ImportEngine"
    }


def _imports_target_modules(path: pathlib.Path) -> bool:
    """Return whether a caller bypasses the coordinator for a Target Module."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in {"target_modules", "netbox_data_import.target_modules"}:
                return True
            package_import = node.module == "netbox_data_import" or node.level and node.module is None
            if package_import and any(name.name == "target_modules" for name in node.names):
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
        self.assertIn("plain Django models use suitable DRF bases", architecture)

    def test_views_and_jobs_call_only_the_public_coordinator_methods(self):
        calls = {name: _import_engine_calls(PACKAGE / name) for name in CALLERS}

        self.assertEqual(calls, {"views.py": {"plan", "execute"}, "jobs.py": {"execute"}})

    def test_private_coordinator_attribute_references_are_detected(self):
        """The boundary rejects private access even when it is not a call."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "caller.py"
            path.write_text("ImportEngine.plan()\nImportEngine.execute()\ncallback = ImportEngine._private_helper\n")

            self.assertIn("_private_helper", _import_engine_calls(path))

    def test_views_and_jobs_do_not_import_target_modules(self):
        self.assertEqual([name for name in CALLERS if _imports_target_modules(PACKAGE / name)], [])

    def test_relative_package_imports_of_target_modules_are_detected(self):
        """The boundary guard recognizes ``from . import target_modules``."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "caller.py"
            path.write_text("from . import target_modules\n")

            self.assertTrue(_imports_target_modules(path))

    def test_absolute_package_imports_of_target_modules_are_detected(self):
        """The boundary guard recognizes imports from the absolute package."""
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "caller.py"
            path.write_text("from netbox_data_import import target_modules\n")

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

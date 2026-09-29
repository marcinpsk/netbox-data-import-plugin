# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every place that records the release version must agree.

semantic-release writes only the files in its `version_toml` and `version_variables` settings, so
a version source it does not know about drifts silently until someone reads it.
"""

import importlib.util
import tomllib
from pathlib import Path

from netbox_data_import import __version__


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DISTRIBUTION_NAME = "netbox-data-import"


MARKDOWN_INSERTION_FLAG = "<!-- version list -->"


def _pyproject():
    return tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _changelog_config():
    return _pyproject()["tool"]["semantic_release"].get("changelog", {})


def _changelog_path():
    templates = _changelog_config().get("default_templates", {})
    return REPOSITORY_ROOT / templates.get("changelog_file", "CHANGELOG.md")


def _locked_project_version():
    """Return the version uv.lock records for this project's own package entry."""
    lockfile = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    entries = [package for package in lockfile["package"] if package["name"] == DISTRIBUTION_NAME]
    assert len(entries) == 1, f"uv.lock holds {len(entries)} entries for {DISTRIBUTION_NAME}"
    return entries[0]["version"]


def test_the_package_version_matches_pyproject():
    """A NetBox plugin reports __version__, so it must not lag the released version."""
    assert __version__ == _pyproject()["project"]["version"]


def test_the_lockfile_records_the_released_version():
    """A stale uv.lock installs the project under the wrong version in every locked environment."""
    assert _locked_project_version() == _pyproject()["project"]["version"]


def test_untrusted_workbooks_require_hardened_xml_parsing():
    """Every supported installation protects openpyxl from XML expansion attacks."""
    dependencies = _pyproject()["project"]["dependencies"]

    assert any(dependency.startswith("defusedxml") for dependency in dependencies)


def test_the_release_rewrites_the_lockfile():
    """semantic-release must regenerate and commit uv.lock, or the check above fails after a release.

    The version bump makes uv.lock stale, so `uv lock` re-resolves. `uv sync` caches wheels and not
    index metadata, so `--offline` has nothing to resolve against and fails the release.
    """
    semantic_release = _pyproject()["tool"]["semantic_release"]
    build_command = semantic_release["build_command"]

    assert "uv lock" in build_command
    assert "--offline" not in build_command
    assert "uv.lock" in semantic_release["assets"]


def test_the_changelog_carries_the_insertion_flag():
    """semantic-release defaults to update mode, which splits the changelog on this flag.

    A changelog that holds content but no flag is rendered back unchanged. The release then
    reports success and commits no changelog, which is how this file went 18 tags without one.
    """
    flag = _changelog_config().get("insertion_flag") or MARKDOWN_INSERTION_FLAG

    assert flag in _changelog_path().read_text(encoding="utf-8")


def _compatibility_generator():
    """Load scripts/gen_compatibility.py, which is a script and not a package module."""
    spec = importlib.util.spec_from_file_location(
        "gen_compatibility", REPOSITORY_ROOT / "scripts" / "gen_compatibility.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_compatibility_matrix_names_each_release_once():
    """An unreleased floor change must not relabel the range of the version already released."""
    releases = [release for release, _minimum, _maximum in _compatibility_generator().compatibility_rows()]

    assert len(releases) == len(set(releases)), releases


def test_a_version_bump_publishes_the_changed_range(monkeypatch):
    """The release build regenerates the matrix after the bump, so the new range gets the new version."""
    generator = _compatibility_generator()
    _version, min_version = generator.read_plugin_config()
    monkeypatch.setattr(generator, "read_plugin_config", lambda: ("99.0.0", min_version))

    top = generator.compatibility_rows()[0]

    assert top == ("99.0.0", min_version, generator.newest_tested_netbox())

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every place that records the release version must agree.

semantic-release writes only the files in its `version_toml` and `version_variables` settings, so
a version source it does not know about drifts silently until someone reads it.
"""

import tomllib
from pathlib import Path

from netbox_data_import import __version__


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DISTRIBUTION_NAME = "netbox-data-import"


def _pyproject():
    return tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())


def _locked_project_version():
    """Return the version uv.lock records for this project's own package entry."""
    lockfile = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text())
    entries = [package for package in lockfile["package"] if package["name"] == DISTRIBUTION_NAME]
    assert len(entries) == 1, f"uv.lock holds {len(entries)} entries for {DISTRIBUTION_NAME}"
    return entries[0]["version"]


def test_the_package_version_matches_pyproject():
    """A NetBox plugin reports __version__, so it must not lag the released version."""
    assert __version__ == _pyproject()["project"]["version"]


def test_the_lockfile_records_the_released_version():
    """A stale uv.lock installs the project under the wrong version in every locked environment."""
    assert _locked_project_version() == _pyproject()["project"]["version"]


def test_the_release_writes_the_lockfile():
    """semantic-release must regenerate and commit uv.lock, or the check above fails after a release."""
    semantic_release = _pyproject()["tool"]["semantic_release"]

    assert "uv lock" in semantic_release["build_command"]
    assert "uv.lock" in semantic_release["assets"]

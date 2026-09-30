# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pytest fixtures for isolated parallel test workers and the session guards."""

import os

import pytest

from netbox_data_import.tests.parallel import isolated_test_database_name


_TEST_DATABASE_BASE_NAME = os.environ["TEST_DB_NAME"]


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings):
    """Give each pytest worker a private PostgreSQL database."""
    from django.conf import settings

    test_config = dict(settings.DATABASES["default"].get("TEST") or {})
    test_config["NAME"] = isolated_test_database_name(
        _TEST_DATABASE_BASE_NAME,
        os.environ.get("PYTEST_XDIST_WORKER"),
    )
    settings.DATABASES["default"]["TEST"] = test_config


@pytest.fixture(scope="session", autouse=True)
def changelog_snapshot_guard():
    """Check every save and many-to-many change in the session for a current prechange snapshot."""
    from netbox_data_import.tests import snapshot_guard

    snapshot_guard.connect()
    yield
    snapshot_guard.disconnect()


@pytest.fixture(scope="session", autouse=True)
def plugins_config_guard():
    """Fail every PLUGINS_CONFIG override in the session that drops the entry of another plugin."""
    from netbox_data_import.tests import plugins_config

    plugins_config.connect()
    yield
    plugins_config.disconnect()


@pytest.fixture(autouse=True)
def changelog_snapshot_violations(changelog_snapshot_guard):
    """Fail a test whose code swallowed a snapshot guard failure."""
    from netbox_data_import.tests import snapshot_guard

    snapshot_guard.take_violations()
    yield
    violations = snapshot_guard.take_violations()
    if violations:
        pytest.fail("\n".join(violations), pytrace=False)

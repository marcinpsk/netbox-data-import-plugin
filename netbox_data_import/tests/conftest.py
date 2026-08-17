# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pytest fixtures for isolated parallel test workers."""

import os

import pytest

from netbox_data_import.tests.parallel import MAX_PARALLEL_WORKERS, isolated_test_database_name


_TEST_DATABASE_BASE_NAME = os.environ["TEST_DB_NAME"]


def pytest_xdist_auto_num_workers(config):
    """Cap `-n auto` at the worker count that still gets private Redis databases."""
    from xdist.plugin import pytest_xdist_auto_num_workers as detected_num_workers

    return min(detected_num_workers(config), MAX_PARALLEL_WORKERS)


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

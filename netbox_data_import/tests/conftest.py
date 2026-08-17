# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pytest fixtures for isolated parallel test workers."""

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

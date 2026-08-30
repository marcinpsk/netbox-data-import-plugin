# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox test settings that require caller-selected database and Redis targets."""

import os

from netbox_data_import.tests.parallel import isolated_redis_databases


_test_redis_host = os.environ["TEST_REDIS_HOST"]
if not _test_redis_host.strip():
    raise ValueError("TEST_REDIS_HOST must not be empty.")
_tasks_redis_database, _cache_redis_database = isolated_redis_databases(os.environ.get("PYTEST_XDIST_WORKER"))
os.environ["REDIS_HOST"] = _test_redis_host
os.environ["REDIS_CACHE_HOST"] = _test_redis_host
os.environ["REDIS_DATABASE"] = str(_tasks_redis_database)
os.environ["REDIS_CACHE_DATABASE"] = str(_cache_redis_database)
os.environ["NETBOX_CONFIGURATION"] = "netbox_data_import.tests.netbox_configuration"

from netbox.settings import *  # noqa: E402, F403


# NetBox API test cases create v2 tokens and require one HMAC pepper.
API_TOKEN_PEPPERS = {1: "netbox-data-import-test-pepper"}

_test_database_name = os.environ["TEST_DB_NAME"]
if not _test_database_name.startswith("test_"):
    raise ValueError("TEST_DB_NAME must start with 'test_'.")

# DATABASES comes from the starred NetBox settings, which mypy is not given.
DATABASES["default"].setdefault("TEST", {})["NAME"] = _test_database_name  # type: ignore[name-defined]  # noqa: F405

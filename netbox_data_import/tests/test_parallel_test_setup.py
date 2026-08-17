# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for parallel test worker isolation."""

import os
import subprocess
from pathlib import Path

import pytest

from netbox_data_import.tests.conftest import pytest_xdist_auto_num_workers
from netbox_data_import.tests.parallel import (
    MAX_PARALLEL_WORKERS,
    isolated_redis_databases,
    isolated_test_database_name,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_xdist_worker_gets_private_postgresql_and_redis_databases():
    """Assign one PostgreSQL database and two Redis databases to a worker."""
    assert isolated_test_database_name("test_netbox_data_import", "gw3") == "test_netbox_data_import_gw3"
    assert isolated_redis_databases("gw3") == (3, 11)


def test_serial_run_keeps_default_database_targets():
    """Keep the caller's targets when pytest does not use xdist."""
    assert isolated_test_database_name("test_netbox_data_import", None) == "test_netbox_data_import"
    assert isolated_redis_databases(None) == (0, 1)


def test_database_name_stays_within_postgresql_limit():
    """Keep a worker suffix when the base name reaches PostgreSQL's limit."""
    database_name = isolated_test_database_name(f"test_{'x' * 70}", "gw7")

    assert len(database_name) == 63
    assert database_name.endswith("_gw7")


def test_more_than_eight_workers_is_rejected():
    """Reject workers that cannot receive a private Redis database pair."""
    with pytest.raises(ValueError, match="At most 8 pytest workers are supported"):
        isolated_redis_databases("gw8")


@pytest.mark.parametrize(
    ("detected_workers", "expected"),
    [("2", 2), (str(MAX_PARALLEL_WORKERS), MAX_PARALLEL_WORKERS), ("32", MAX_PARALLEL_WORKERS)],
)
def test_auto_worker_count_never_exceeds_the_isolated_worker_ceiling(monkeypatch, detected_workers, expected):
    """`-n auto` on a big machine must stop at the last worker with private Redis databases."""
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", detected_workers)

    assert pytest_xdist_auto_num_workers(None) == expected
    isolated_redis_databases(f"gw{expected - 1}")  # the highest worker this count starts


def _run_netbox_test_alias(worker_value=None):
    """Run the local test alias with pytest and the venv activation stubbed out."""
    script = "\n".join(
        (
            f'source "{REPOSITORY_ROOT}/.devcontainer/scripts/load-aliases.sh"',
            "source() { :; }",  # skip the venv activation
            "pytest() { printf 'PYTEST %s\\n' \"$*\"; }",
            "netbox-test",
            'printf "STATUS %s\\n" "$?"',
        )
    )
    environment = {
        **os.environ,
        "TEST_DB_NAME": "test_alias_contract",
        "TEST_REDIS_HOST": "redis-alias-contract",
    }
    if worker_value is None:
        environment.pop("NETBOX_TEST_WORKERS", None)
    else:
        environment["NETBOX_TEST_WORKERS"] = worker_value
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPOSITORY_ROOT,
        check=False,
    )


def test_test_alias_sizes_the_worker_pool_to_the_machine():
    """The local entry point must request auto workers so the conftest cap applies."""
    result = _run_netbox_test_alias()

    assert "STATUS 0" in result.stdout
    assert "-n auto --maxschedchunk=1" in result.stdout


def test_test_alias_passes_an_explicit_worker_count_through():
    """An explicit count must reach pytest, where it replaces `-n auto`."""
    result = _run_netbox_test_alias("1")

    assert "STATUS 0" in result.stdout
    assert "-n 1 --maxschedchunk=1" in result.stdout


@pytest.mark.django_db
def test_active_worker_uses_its_private_database_targets(settings):
    """Apply the worker identity to the real Django database settings."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    tasks_database, cache_database = isolated_redis_databases(worker_id)

    assert settings.DATABASES["default"]["TEST"]["NAME"] == isolated_test_database_name(
        os.environ["TEST_DB_NAME"],
        worker_id,
    )
    assert settings.RQ_QUEUES["default"]["DB"] == tasks_database
    assert settings.CACHES["default"]["LOCATION"].endswith(f"/{cache_database}")

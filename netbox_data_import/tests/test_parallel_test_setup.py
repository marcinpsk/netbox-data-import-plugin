# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for parallel test worker isolation."""

import os
import re
import subprocess
import sys
from inspect import signature
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from netbox_data_import.tests.parallel import (
    MAX_PARALLEL_WORKERS,
    isolated_redis_databases,
    isolated_test_database_name,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _root_conftest():
    """Load the root conftest by path: it is not importable as a package module."""
    spec = spec_from_file_location("netbox_data_import_root_conftest", REPOSITORY_ROOT / "conftest.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_empty_pytest(*arguments, timeout=120):
    """Run pytest without collecting this plugin's test suite."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "COV_"))}
    environment["TEST_DB_NAME"] = "test_worker_pool_contract"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *arguments,
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "--ignore=netbox_data_import",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPOSITORY_ROOT,
        check=False,
        timeout=timeout,
    )


def test_xdist_worker_gets_private_postgresql_and_redis_databases():
    """Assign one PostgreSQL database and two Redis databases to a worker."""
    assert isolated_test_database_name("test_netbox_data_import", "gw3") == "test_netbox_data_import_gw3"
    assert isolated_redis_databases("gw3") == (3, 11)


def test_empty_pytest_runs_have_a_bounded_default_timeout():
    """A broken nested test run cannot hang the outer suite indefinitely."""
    assert signature(_run_empty_pytest).parameters["timeout"].default == 120


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

    assert _root_conftest().pytest_xdist_auto_num_workers(None) == expected
    isolated_redis_databases(f"gw{expected - 1}")  # the highest worker this count starts


def test_a_bare_pytest_run_caps_the_auto_worker_pool():
    """The cap must reach an invocation that names no test path, which the addopts `-n auto` targets.

    pytest loads `netbox_data_import/tests/conftest.py` only during collection, after the workers
    start, so a hook placed there would leave this run uncapped.
    """
    result = _run_empty_pytest("-n", "auto", "-v")

    # `--ignore` leaves nothing to collect, so pytest exits 5. Any other status means the run broke
    # before the cap could apply, and the `created:` line alone would still pass the check below.
    assert result.returncode in (0, 5), f"exit {result.returncode}\n{result.stdout[-3000:]}"
    created = re.search(r"created: (\d+)/\d+ workers", result.stdout)
    assert created is not None, result.stdout[-3000:]
    # A small runner detects fewer workers than the cap, which is already correct.
    assert 0 < int(created.group(1)) <= MAX_PARALLEL_WORKERS


def test_an_explicit_worker_count_above_the_ceiling_is_rejected():
    """`-n 9` never reaches `pytest_xdist_auto_num_workers`, which xdist calls only for auto counts.

    Without a second check the run starts `gw8`, and that worker fails in the isolation helper during
    collection, after the databases of the other workers already exist.
    """
    result = _run_empty_pytest("-n", str(MAX_PARALLEL_WORKERS + 1))

    # 4 is pytest's usage-error status. It must refuse the run instead of collecting and failing later.
    assert result.returncode == 4, f"exit {result.returncode}\n{(result.stdout + result.stderr)[-3000:]}"
    assert f"at most {MAX_PARALLEL_WORKERS} pytest workers" in result.stdout + result.stderr


def test_a_gateway_specification_above_the_ceiling_is_rejected():
    """Reject an explicit gateway list before workers exceed the isolation limit."""
    result = _run_empty_pytest(
        "-o",
        "addopts=",
        "--tx",
        f"{MAX_PARALLEL_WORKERS + 1}*popen",
        "--dist",
        "load",
        timeout=120,
    )

    # 4 is pytest's usage-error status. It must refuse the run instead of collecting and failing later.
    assert result.returncode == 4, f"exit {result.returncode}\n{(result.stdout + result.stderr)[-3000:]}"
    assert f"at most {MAX_PARALLEL_WORKERS} pytest workers" in result.stdout + result.stderr


def test_a_signed_gateway_multiplier_is_counted_the_way_xdist_counts_it():
    """xdist parses the multiplier with `int()`, which takes a sign this repository must not miss."""
    result = _run_empty_pytest(
        "-o",
        "addopts=",
        "--tx",
        f"+{MAX_PARALLEL_WORKERS + 1}*popen",
        "--dist",
        "load",
        timeout=120,
    )

    # 4 is pytest's usage-error status. It must refuse the run instead of collecting and failing later.
    assert result.returncode == 4, f"exit {result.returncode}\n{(result.stdout + result.stderr)[-3000:]}"
    assert f"at most {MAX_PARALLEL_WORKERS} pytest workers" in result.stdout + result.stderr


def test_collecting_without_running_is_left_alone():
    """xdist starts no worker for `--collect-only`, so the ceiling has nothing to refuse."""
    result = _run_empty_pytest(
        "-o",
        "addopts=",
        "--collect-only",
        "--tx",
        f"{MAX_PARALLEL_WORKERS + 1}*popen",
        "--dist",
        "load",
        timeout=120,
    )

    # Nothing to collect means exit 5; exit 4 would mean the ceiling refused a no-worker run.
    assert result.returncode == 5, f"exit {result.returncode}\n{(result.stdout + result.stderr)[-3000:]}"


def test_a_gateway_specification_without_distribution_is_left_alone():
    """`--tx` with distribution off starts no worker, so the ceiling has nothing to refuse."""
    result = _run_empty_pytest(
        "-o",
        "addopts=",
        "--tx",
        f"{MAX_PARALLEL_WORKERS + 1}*popen",
        timeout=120,
    )

    # Nothing to collect means exit 5; exit 4 would mean the ceiling refused a no-worker run.
    assert result.returncode == 5, f"exit {result.returncode}\n{(result.stdout + result.stderr)[-3000:]}"


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

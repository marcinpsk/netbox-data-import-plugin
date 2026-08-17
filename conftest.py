# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Root pytest configuration.

pytest loads the root conftest before pytest-xdist resolves the `-n auto` in the addopts, and it
loads `netbox_data_import/tests/conftest.py` only during collection, after the workers start. The
worker cap therefore has to live here to reach an invocation that names no test path.
"""

import pytest

from netbox_data_import.tests.parallel import MAX_PARALLEL_WORKERS


def pytest_xdist_auto_num_workers(config):
    """Cap `-n auto` at the worker count that still gets private Redis databases."""
    from xdist.plugin import pytest_xdist_auto_num_workers as detected_num_workers

    return min(detected_num_workers(config), MAX_PARALLEL_WORKERS)


def pytest_configure(config):
    """Refuse an explicit worker count above the ceiling before any worker starts.

    xdist calls the hook above only for `auto` and `logical`, and it keeps those two as strings. An
    explicit count arrives as an int and skips the cap, so every worker crashes during collection.
    """
    requested = config.option.numprocesses
    if isinstance(requested, int) and requested > MAX_PARALLEL_WORKERS:
        raise pytest.UsageError(
            f"-n {requested} exceeds the isolation limit: at most {MAX_PARALLEL_WORKERS} pytest "
            "workers get private test databases."
        )

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Root pytest configuration.

pytest loads the root conftest before pytest-xdist resolves the `-n auto` in the addopts, and it
loads `netbox_data_import/tests/conftest.py` only during collection, after the workers start. The
worker cap therefore has to live here to reach an invocation that names no test path.
"""

from netbox_data_import.tests.parallel import MAX_PARALLEL_WORKERS


def pytest_xdist_auto_num_workers(config):
    """Cap `-n auto` at the worker count that still gets private Redis databases."""
    from xdist.plugin import pytest_xdist_auto_num_workers as detected_num_workers

    return min(detected_num_workers(config), MAX_PARALLEL_WORKERS)

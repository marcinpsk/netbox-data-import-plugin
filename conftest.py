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


def _requested_worker_count(config):
    """Return how many workers xdist will start, read from the gateway specs it has resolved.

    xdist owns this grammar, down to a signed multiplier in `+9*popen`, so its own parser answers
    here rather than a copy of it. A rename in xdist then breaks the import instead of quietly
    miscounting.
    """
    from xdist.workermanage import parse_tx_spec_config

    return len(parse_tx_spec_config(config))


def pytest_configure(config):
    """Refuse more workers than the isolation covers, before any of them starts.

    The hook above caps `auto` and `logical`, which is all xdist asks it about. Reading the
    resolved specs instead reaches an explicit `-n` and a `--tx` gateway list as well, and each of
    those otherwise starts a worker that fails in the isolation helper during collection, after
    the databases of the other workers already exist.
    """
    # The two conditions xdist itself starts workers on, and the list it reads them from.
    if config.getoption("dist", "no") == "no" or config.getoption("collectonly", False):
        return
    if not config.getoption("tx", []):
        return
    requested = _requested_worker_count(config)
    if requested > MAX_PARALLEL_WORKERS:
        raise pytest.UsageError(
            f"{requested} workers exceed the isolation limit: at most {MAX_PARALLEL_WORKERS} "
            "pytest workers get private test databases."
        )

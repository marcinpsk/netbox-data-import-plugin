# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Without netbox-branching, the plugin's branching functions do nothing."""

import sys

import pytest
from django.test import SimpleTestCase

from netbox_data_import import branching

if branching.installed():
    pytest.skip("netbox-branching is an installed app: test_branching.py covers this run", allow_module_level=True)


class WithoutBranchingTest(SimpleTestCase):
    """netbox-branching stays optional."""

    def test_no_branch_is_active(self):
        self.assertIsNone(branching.active_branch())

    def test_register_does_not_import_netbox_branching(self):
        branching.register()

        self.assertNotIn("netbox_branching", sys.modules)

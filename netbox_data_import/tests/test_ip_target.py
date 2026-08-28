# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""An IP target that says the device already holds an address must carry the row it holds."""

from django.test import SimpleTestCase

from netbox_data_import.ip_assignment import IPAssignmentError, IPTarget


class HeldRowTest(SimpleTestCase):
    """`already_held` and `existing` state one fact between them, so they cannot disagree."""

    def test_a_held_target_returns_the_row_it_holds(self):
        """Both writers read the stored row to decide whether the device field has to move."""
        row = object()
        target = IPTarget(address="192.0.2.10/24", interface=None, existing=row, already_held=True)

        self.assertIs(target.held, row)

    def test_a_held_target_without_a_row_is_refused(self):
        """`resolve` never builds this, so reaching it means the invariant broke upstream."""
        target = IPTarget(address="192.0.2.10/24", interface=None, existing=None, already_held=True)

        with self.assertRaises(IPAssignmentError):
            target.held

    def test_an_unheld_target_is_refused_too(self):
        """An address that exists but is unassigned is not one the device already carries."""
        target = IPTarget(address="192.0.2.10/24", interface=None, existing=object(), already_held=False)

        with self.assertRaises(IPAssignmentError):
            target.held

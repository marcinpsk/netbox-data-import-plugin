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


class UnplaceableTargetTest(SimpleTestCase):
    """A target with nowhere to go still has to describe itself to the operator."""

    def _target(self):
        return IPTarget(address="192.0.2.10/24", interface=None, existing=None, already_held=False)

    def test_the_summary_is_just_the_address(self):
        """There is no interface to name, so the sync reports the address alone."""
        self.assertEqual(self._target().summary, "192.0.2.10/24")

    def test_the_preview_shows_no_placement(self):
        """The row already prints the address, so an empty hint adds nothing beside it."""
        self.assertEqual(self._target().placement, "")


class ReviewNormalizationTest(SimpleTestCase):
    """The preview compares IP values, and a source cell need not hold an address."""

    def test_a_value_that_is_not_an_address_compares_as_its_own_text(self):
        """A malformed cell still has to reach the diff, or the row loses the difference."""
        from netbox_data_import.device_field_review import _ip_normalize

        self.assertEqual(_ip_normalize("not-an-address"), "not-an-address")

    def test_an_address_compares_in_its_canonical_form(self):
        """`192.0.2.1/24` and its stored form have to compare equal."""
        from netbox_data_import.device_field_review import _ip_normalize

        self.assertEqual(_ip_normalize("192.0.2.1/24"), "192.0.2.1/24")
        self.assertEqual(_ip_normalize(" 192.0.2.1/24 "), "192.0.2.1/24")

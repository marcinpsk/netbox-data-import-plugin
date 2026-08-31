# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Navigation exposes links only to actors who can use their views."""

from django.test import SimpleTestCase

from netbox_data_import.navigation import menu


class ImportHistoryNavigationTest(SimpleTestCase):
    """The Import Execution history link follows its view permission."""

    def test_history_link_requires_execution_view_permission(self):
        """An actor without history access must not see a link that the view rejects."""
        history_item = next(
            item
            for group in menu.groups
            for item in group.items
            if item.link == "plugins:netbox_data_import:importexecution_list"
        )

        self.assertEqual(set(history_item.permissions), {"netbox_data_import.view_importexecution"})

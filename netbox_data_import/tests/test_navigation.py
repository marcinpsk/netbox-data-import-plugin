# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Navigation exposes links only to actors who can use their views."""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from netbox_data_import.models import InferenceBackend
from netbox_data_import.navigation import menu
from netbox_data_import.tests.helpers import user_with_object_permission


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


class InferenceBackendNavigationTest(TestCase):
    """The AI backends Add button follows the add permission, not the menu item's view permission."""

    def test_view_only_user_does_not_see_add_link(self):
        """NetBox shows a button whose `permissions` are empty to anyone who can see its menu item."""
        user = user_with_object_permission("viewer", [(InferenceBackend, ["view"], {})])
        self.client.force_login(user)

        response = self.client.get(reverse("plugins:netbox_data_import:inferencebackend_list"))

        self.assertNotContains(response, reverse("plugins:netbox_data_import:inferencebackend_add"), status_code=200)

    def test_user_with_view_and_add_permissions_sees_add_link(self):
        """A misspelled permission would hide the button from everyone, so assert the other direction too."""
        user = user_with_object_permission("creator", [(InferenceBackend, ["view", "add"], {})])
        self.client.force_login(user)

        response = self.client.get(reverse("plugins:netbox_data_import:inferencebackend_list"))

        self.assertContains(response, reverse("plugins:netbox_data_import:inferencebackend_add"), status_code=200)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Navigation exposes links only to actors who can use their views."""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from netbox_data_import.models import ImportProfile, InferenceBackend
from netbox_data_import.navigation import menu
from netbox_data_import.tests.helpers import user_with_object_permission
from netbox_data_import.tests.test_inference_backend import make_row


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


class InferenceBackendConnectionTestNavigationTest(TestCase):
    def test_view_only_user_does_not_see_connection_test(self):
        backend = make_row()
        user = user_with_object_permission("viewer", [(InferenceBackend, ["view"], {})])
        self.client.force_login(user)

        response = self.client.get(backend.get_absolute_url())

        self.assertNotContains(
            response,
            reverse("plugins:netbox_data_import:inferencebackend_connection_test", kwargs={"pk": backend.pk}),
            status_code=200,
        )

    def test_user_with_view_and_change_permissions_sees_connection_test(self):
        backend = make_row()
        user = user_with_object_permission("editor", [(InferenceBackend, ["view", "change"], {})])
        self.client.force_login(user)

        response = self.client.get(backend.get_absolute_url())

        self.assertContains(
            response,
            reverse("plugins:netbox_data_import:inferencebackend_connection_test", kwargs={"pk": backend.pk}),
            status_code=200,
        )


class ImportProfileNavigationTest(TestCase):
    """Every Import Profile menu entry follows the permission its own view enforces."""

    ENTRY_PERMISSION = {
        # menu entry -> the permission its view requires
        "importprofile_list": "view",
        "device_type_analysis": "view",
        "importprofile_add": "add",
        "import_setup": "change",
    }

    def _sidebar(self, username, profile_actions):
        """Render a page holding no Import Profile content, so the sidebar is the only link source."""
        grants = [(InferenceBackend, ["view"], {})]
        if profile_actions:
            grants.append((ImportProfile, profile_actions, {}))
        self.client.force_login(user_with_object_permission(username, grants))
        return self.client.get(reverse("plugins:netbox_data_import:inferencebackend_list"))

    def _assert_links(self, response, expected_actions):
        """Assert each entry appears exactly when the actor holds the action its view requires."""
        for entry, action in self.ENTRY_PERMISSION.items():
            # A profile URL is a prefix of several others, so match the rendered href exactly.
            href = f'href="{reverse(f"plugins:netbox_data_import:{entry}")}"'
            with self.subTest(entry=entry):
                if action in expected_actions:
                    self.assertContains(response, href, status_code=200)
                else:
                    self.assertNotContains(response, href, status_code=200)

    def test_actor_without_profile_permissions_sees_no_profile_entry(self):
        """An ungated entry renders for anyone, so it offers links every profile view rejects."""
        self._assert_links(self._sidebar("profile-outsider", []), expected_actions=set())

    def test_view_permission_shows_only_the_read_only_entries(self):
        """A misspelled permission would hide an entry from everyone, so assert both directions."""
        self._assert_links(self._sidebar("profile-viewer", ["view"]), expected_actions={"view"})

    def test_add_permission_shows_the_add_button(self):
        self._assert_links(self._sidebar("profile-creator", ["view", "add"]), expected_actions={"view", "add"})

    def test_change_permission_shows_run_import(self):
        self._assert_links(self._sidebar("profile-editor", ["view", "change"]), expected_actions={"view", "change"})

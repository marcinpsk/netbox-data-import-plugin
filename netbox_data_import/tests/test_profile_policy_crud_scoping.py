# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The shared profile-child CRUD views must respect object permissions and the policy lock."""

from django.db import DatabaseError, transaction
from django.db.models.signals import post_save
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import ColumnMapping, DeviceTypeMapping, ImportProfile
from netbox_data_import.tests.helpers import run_on_separate_connection, user_with_object_permission

SCOPED_ACTIONS = ["view", "add", "change", "delete"]


def _profile(name):
    return ImportProfile.objects.create(
        name=name,
        adapter_config={"sheet_name": "Data", "source_id_column": "Id"},
    )


class ProfileChildObjectScopeTest(TestCase):
    """An operator scoped to one profile must not reach another profile's policy rows."""

    def setUp(self):
        """Give one operator change rights on profile A only, and put rows in both profiles."""
        self.mine = _profile("ScopeMine")
        self.theirs = _profile("ScopeTheirs")
        self.my_mapping = ColumnMapping.objects.create(
            profile=self.mine, source_column="Serial Number", target_field="serial"
        )
        self.their_mapping = ColumnMapping.objects.create(
            profile=self.theirs, source_column="Serial Number", target_field="serial"
        )
        self.their_device_type = DeviceTypeMapping.objects.create(
            profile=self.theirs,
            source_make="Cisco",
            source_model="C9300",
            netbox_manufacturer_slug="cisco",
            netbox_device_type_slug="cisco-c9300",
        )
        self.user = user_with_object_permission(
            "scoped-operator",
            [
                (ColumnMapping, SCOPED_ACTIONS, {"profile": self.mine.pk}),
                (DeviceTypeMapping, SCOPED_ACTIONS, {"profile": self.mine.pk}),
                (ImportProfile, ["view"], {}),
            ],
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_editing_a_row_in_another_profile_is_refused(self):
        """The primary key is operator-supplied, so the row it names has to be in scope."""
        url = reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": self.their_mapping.pk})

        response = self.client.post(url, {"source_column": "Stolen", "target_field": "serial"})

        self.assertIn(response.status_code, (403, 404), response.status_code)
        self.their_mapping.refresh_from_db()
        self.assertEqual(self.their_mapping.source_column, "Serial Number")

    def test_deleting_a_row_in_another_profile_is_refused(self):
        """Delete reads the same unrestricted queryset the edit view does."""
        url = reverse("plugins:netbox_data_import:columnmapping_delete", kwargs={"pk": self.their_mapping.pk})

        response = self.client.post(url, {"confirm": "true"})

        self.assertIn(response.status_code, (403, 404), response.status_code)
        self.assertTrue(ColumnMapping.objects.filter(pk=self.their_mapping.pk).exists())

    def test_the_same_scope_holds_for_a_second_policy_table(self):
        """The base view serves every policy table, so one fix has to answer for all of them."""
        url = reverse("plugins:netbox_data_import:devicetypemapping_edit", kwargs={"pk": self.their_device_type.pk})

        response = self.client.post(
            url,
            {
                "source_make": "Cisco",
                "source_model": "Stolen",
                "netbox_manufacturer_slug": "cisco",
                "netbox_device_type_slug": "cisco-c9300",
            },
        )

        self.assertIn(response.status_code, (403, 404), response.status_code)
        self.their_device_type.refresh_from_db()
        self.assertEqual(self.their_device_type.source_model, "C9300")

    def test_a_row_inside_the_operator_scope_is_still_editable(self):
        """Scoping must refuse the rows outside the grant, not the ones inside it."""
        url = reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": self.my_mapping.pk})

        response = self.client.post(url, {"source_column": "SerialNo", "target_field": "serial"})

        self.assertEqual(response.status_code, 302, response.content[:300])
        self.my_mapping.refresh_from_db()
        self.assertEqual(self.my_mapping.source_column, "SerialNo")


class MissingProfileIsNotAServerErrorTest(TestCase):
    """The lock names a profile the URL supplies, which may already be gone."""

    def setUp(self):
        """Log in a superuser; each test names a profile that does not exist."""
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_superuser("gone-user", "gone@example.invalid", "testpass")
        self.client = Client(raise_request_exception=False)
        self.client.force_login(self.user)

    def test_adding_to_a_profile_that_is_gone_is_a_404(self):
        """`locked_profile_policy` refuses an absent profile, and that is a 404 like any missing row."""
        url = reverse("plugins:netbox_data_import:columnmapping_add", kwargs={"profile_pk": 9_999_999})

        response = self.client.post(url, {"source_column": "Serial Number", "target_field": "serial"})

        self.assertEqual(response.status_code, 404, response.status_code)


class ProfileChildPolicyLockTest(TransactionTestCase):
    """A policy edit must hold the same profile lock an execution takes, or it can race a replan."""

    def setUp(self):
        """Log in a superuser and give the profile one mapping to edit."""
        from django.contrib.auth import get_user_model

        self.profile = _profile("LockProfile")
        self.mapping = ColumnMapping.objects.create(
            profile=self.profile, source_column="Serial Number", target_field="serial"
        )
        self.user = get_user_model().objects.create_superuser("lockuser", "lock@example.invalid", "testpass")
        self.client = Client()
        self.client.force_login(self.user)

    def _lock_state_during_write(self, url, data):
        """Return what a second connection sees of the profile row while the view is writing."""
        seen = []

        def probe_from_another_connection(sender, instance, **kwargs):
            if seen or getattr(instance, "profile_id", None) != self.profile.pk:
                return

            def probe():
                try:
                    with transaction.atomic():
                        ImportProfile.objects.select_for_update(nowait=True).get(pk=self.profile.pk)
                    seen.append("unlocked")
                except DatabaseError:
                    seen.append("locked")

            with run_on_separate_connection(probe):
                pass

        post_save.connect(probe_from_another_connection, sender=ColumnMapping)
        try:
            response = self.client.post(url, data)
        finally:
            post_save.disconnect(probe_from_another_connection, sender=ColumnMapping)
        return seen, response

    def test_an_edit_holds_the_profile_policy_lock_while_it_writes(self):
        """`locked_profile_policy` claims every policy write takes it, so an edit has to."""
        url = reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": self.mapping.pk})

        seen, response = self._lock_state_during_write(url, {"source_column": "SerialNo", "target_field": "serial"})

        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual(seen, ["locked"], "the profile row was free while the policy write ran")

    def test_an_add_holds_the_profile_policy_lock_while_it_writes(self):
        """An added row changes the same policy an executing import already read."""
        url = reverse("plugins:netbox_data_import:columnmapping_add", kwargs={"profile_pk": self.profile.pk})

        seen, response = self._lock_state_during_write(url, {"source_column": "Name", "target_field": "device_name"})

        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual(seen, ["locked"], "the profile row was free while the policy write ran")

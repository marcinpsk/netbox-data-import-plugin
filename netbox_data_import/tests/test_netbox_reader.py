# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Use real ObjectPermission rows because NetBox object access ignores Django user permissions."""

from django.test import TestCase

from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.tests.helpers import user_with_object_permission


class NetBoxReaderScopeTest(TestCase):
    """The reader returns what the actor may see, and nothing else."""

    def setUp(self):
        """Two sites, one device and one rack in each."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.visible_site = Site.objects.create(name="Reader Visible", slug="reader-visible")
        self.hidden_site = Site.objects.create(name="Reader Hidden", slug="reader-hidden")
        manufacturer = Manufacturer.objects.create(name="Reader Mfg", slug="reader-mfg")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Reader Model", slug="reader-model", u_height=1
        )
        self.role = DeviceRole.objects.create(name="Reader Role", slug="reader-role")
        for site, name in ((self.visible_site, "reader-visible-device"), (self.hidden_site, "reader-hidden-device")):
            Device.objects.create(name=name, site=site, device_type=self.device_type, role=self.role, status="active")
            Rack.objects.create(name=f"{name}-rack", site=site, u_height=42)
        self.actor = user_with_object_permission(
            "reader-actor",
            [
                (Device, ["view"], {"site__name": "Reader Visible"}),
                (Rack, ["view"], {"site__name": "Reader Visible"}),
            ],
        )

    def test_devices_are_limited_to_what_the_actor_may_view(self):
        """A device in a site the actor cannot view must not reach planning at all."""
        reader = NetBoxReader.for_actor(self.actor)

        names = set(reader.devices().values_list("name", flat=True))

        self.assertEqual(names, {"reader-visible-device"})

    def test_racks_are_limited_to_what_the_actor_may_view(self):
        """Rack placement is planned from the same scoped view."""
        reader = NetBoxReader.for_actor(self.actor)

        names = set(reader.racks().values_list("name", flat=True))

        self.assertEqual(names, {"reader-visible-device-rack"})

    def test_an_action_the_actor_lacks_returns_nothing(self):
        """The actor may view these devices; it may not change them."""
        reader = NetBoxReader.for_actor(self.actor)

        self.assertEqual(reader.devices("change").count(), 0)
        self.assertEqual(reader.devices("view").count(), 1)

    def test_the_unrestricted_reader_is_asked_for_by_name(self):
        """The unscoped path exists for callers with no actor, and has to be chosen deliberately."""
        reader = NetBoxReader.unrestricted()

        self.assertEqual(reader.devices().count(), 2)
        self.assertIsNone(reader.actor)

    def test_a_superuser_sees_every_object(self):
        """Scoping must not hide anything from an actor whose permissions do not restrict it."""
        from django.contrib.auth import get_user_model

        superuser = get_user_model().objects.create_superuser(
            username="reader-super", email="reader-super@example.invalid", password="testpass"
        )

        reader = NetBoxReader.for_actor(superuser)

        self.assertEqual(reader.devices().count(), 2)
        self.assertEqual(reader.racks().count(), 2)

    def test_the_optional_boundary_scopes_an_actor_and_passes_none_through(self):
        """An explicit system caller remains the only unrestricted optional boundary."""
        self.assertEqual(NetBoxReader.for_optional_actor(self.actor).devices().count(), 1)
        self.assertEqual(NetBoxReader.for_optional_actor(None).devices().count(), 2)
        self.assertIs(NetBoxReader.for_optional_actor(self.actor).actor, self.actor)

    def test_for_actor_refuses_a_missing_actor(self):
        """`for_actor(None)` would be an unscoped read that reads like a scoped one."""
        with self.assertRaises(ValueError):
            NetBoxReader.for_actor(None)

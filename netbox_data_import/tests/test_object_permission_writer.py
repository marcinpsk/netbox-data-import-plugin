# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The one seam every permission-scoped import write goes through.

NetBox grants a bare `has_perm("app.add_thing")` when the user may act on any object of the type.
An ObjectPermission's constraints only apply to a saved instance, so a scoped write has to save
first and then ask again. These tests use real users and real ObjectPermission rows: a mocked
permission check would only restate the assumption under test.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from users.models import ObjectPermission

from netbox_data_import.models import DeviceTypeMapping, ImportProfile
from netbox_data_import.object_permissions import (
    ObjectPermissionDenied,
    delete_permission_scoped_objects,
    enforce_saved_object_permission,
    save_permission_scoped_object,
)


def _user_with(username, grants):
    """Create a user holding one ObjectPermission per (model, actions, constraints) grant."""
    user = get_user_model().objects.create_user(username=username, password="testpass")
    for model, actions, constraints in grants:
        permission = ObjectPermission.objects.create(
            name=f"{username} {model.__name__} {'-'.join(actions)}",
            actions=list(actions),
            constraints=constraints,
        )
        permission.object_types.add(ContentType.objects.get_for_model(model))
        permission.users.add(user)
    return user


class EnforceSavedObjectPermissionTest(TestCase):
    """The check has to work on a NetBox model and on a plain plugin model alike."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Scope Profile")
        self.other = ImportProfile.objects.create(name="Other Scope Profile")

    def test_a_constrained_netbox_model_is_scoped(self):
        """The NetBox models were already covered; this pins that behaviour before the change."""
        from dcim.models import Manufacturer

        allowed = Manufacturer.objects.create(name="Allowed", slug="allowed")
        refused = Manufacturer.objects.create(name="Refused", slug="refused")
        user = _user_with("scope-mfg", [(Manufacturer, ["view"], {"slug": "allowed"})])

        enforce_saved_object_permission(allowed, user, "view")
        with self.assertRaises(ObjectPermissionDenied):
            enforce_saved_object_permission(refused, user, "view")

    def test_a_constrained_plain_plugin_model_is_scoped(self):
        """DeviceTypeMapping is a plain Model, so its manager has no restrict()."""
        self.assertFalse(hasattr(DeviceTypeMapping.objects, "restrict"))
        mine = DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        theirs = DeviceTypeMapping.objects.create(profile=self.other, source_make="A", source_model="B")
        user = _user_with("scope-map", [(DeviceTypeMapping, ["view"], {"profile": self.profile.pk})])

        enforce_saved_object_permission(mine, user, "view")
        with self.assertRaises(ObjectPermissionDenied):
            enforce_saved_object_permission(theirs, user, "view")

    def test_no_user_is_not_a_scope_check(self):
        """Background imports run without a request user and keep their own authorization path."""
        mapping = DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        enforce_saved_object_permission(mapping, None, "view")


class SavePermissionScopedObjectTest(TestCase):
    """Create, update, keep and reject, each inside the caller's object scope."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Writer Profile")
        self.other = ImportProfile.objects.create(name="Writer Other Profile")

    def _lookup(self, profile=None):
        return {"profile": profile or self.profile, "source_make": "Acme", "source_model": "Widget"}

    def test_a_create_inside_the_scope_is_saved(self):
        user = _user_with("writer-add", [(DeviceTypeMapping, ["add"], {"profile": self.profile.pk})])

        result = save_permission_scoped_object(
            user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "acme"}
        )

        self.assertTrue(result.created)
        self.assertEqual(result.instance.netbox_manufacturer_slug, "acme")

    def test_a_create_outside_the_scope_writes_nothing(self):
        """The bare add permission passes; only the saved instance reveals the constraint."""
        user = _user_with("writer-add-out", [(DeviceTypeMapping, ["add"], {"profile": self.profile.pk})])

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(user, DeviceTypeMapping, self._lookup(self.other), {})

        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.other).exists())

    def test_an_update_needs_the_change_permission_not_add(self):
        user = _user_with("writer-add-only", [(DeviceTypeMapping, ["add"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}
            )

        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "before")

    def test_change_alone_updates_an_existing_row(self):
        """A user who may change but not add still has to be able to edit what exists."""
        user = _user_with("writer-change", [(DeviceTypeMapping, ["change"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        result = save_permission_scoped_object(
            user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}
        )

        self.assertFalse(result.created)
        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "after")

    def test_an_update_cannot_move_a_row_out_of_the_scope(self):
        """The check after the save is what catches this; the one before it cannot."""
        user = _user_with("writer-move", [(DeviceTypeMapping, ["change"], {"netbox_manufacturer_slug": "inside"})])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="inside")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "outside"}
            )

        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "inside")

    def test_keep_returns_the_existing_row_untouched(self):
        user = _user_with("writer-keep", [(DeviceTypeMapping, ["view"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        result = save_permission_scoped_object(
            user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}, on_existing="keep"
        )

        self.assertFalse(result.created)
        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "before")

    def test_keep_still_needs_the_view_permission(self):
        """Handing back someone else's row exposes it, so reuse is scoped too."""
        user = _user_with("writer-keep-out", [(DeviceTypeMapping, ["view"], {"profile": self.other.pk})])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(user, DeviceTypeMapping, self._lookup(), {}, on_existing="keep")

    def test_reject_refuses_an_existing_row(self):
        user = _user_with("writer-reject", [(DeviceTypeMapping, ["add", "change"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}, on_existing="reject"
            )

        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "before")

    def test_an_overlength_value_is_refused_before_the_database_sees_it(self):
        user = _user_with("writer-long", [(DeviceTypeMapping, ["add"], None)])

        with self.assertRaises(ValidationError):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "s" * 300}
            )

        self.assertFalse(DeviceTypeMapping.objects.filter(**self._lookup()).exists())


class DeletePermissionScopedObjectsTest(TestCase):
    """A refused row leaves the whole set intact."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Delete Profile")
        self.other = ImportProfile.objects.create(name="Delete Other Profile")

    def test_every_row_in_scope_is_deleted(self):
        user = _user_with("delete-all", [(DeviceTypeMapping, ["delete"], None)])
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="C", source_model="D")

        deleted = delete_permission_scoped_objects(user, DeviceTypeMapping.objects.filter(profile=self.profile))

        self.assertEqual(deleted, 2)
        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.profile).exists())

    def test_one_refused_row_leaves_the_whole_set(self):
        """Checking every row before deleting any is what makes this all-or-nothing."""
        user = _user_with("delete-part", [(DeviceTypeMapping, ["delete"], {"source_make": "A"})])
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="C", source_model="D")

        with self.assertRaises(ObjectPermissionDenied):
            delete_permission_scoped_objects(user, DeviceTypeMapping.objects.filter(profile=self.profile))

        self.assertEqual(DeviceTypeMapping.objects.filter(profile=self.profile).count(), 2)

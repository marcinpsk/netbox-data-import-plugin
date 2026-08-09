# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for import identity and preview-to-write safety."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.engine import (
    ImportContext,
    ImportResult,
    _DeviceBindingConflict,
    _assign_ip_to_device,
    _bind_device_source,
    _derived_slug_conflicts,
    _ensure_device_type,
    _ensure_manufacturer,
    _find_existing_device,
    _name_placement_conflict_row,
    _pass1_ensure_types,
    _rack_position_error_row,
    _suggest_unique_device_name,
    reapply_saved_resolutions,
    run_import,
)
from netbox_data_import.forms import ImportSetupForm
from netbox_data_import.models import ClassRoleMapping, DeviceExistingMatch, ImportProfile, SourceResolution
from netbox_data_import.views import _import_intents, _save_permission_scoped_object, _serialize_rows


class IdentitySafetyTest(TestCase):
    """Exercise identity decisions through the real engine, views, and database."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        cls.user = get_user_model().objects.create_superuser(
            username="identity-safety-user",
            email="identity-safety@example.invalid",
            password="testpass",
        )
        cls.site = Site.objects.create(name="Identity Safety Site", slug="identity-safety-site")
        cls.manufacturer = Manufacturer.objects.create(name="Identity Vendor", slug="identity-vendor")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer,
            model="Identity Model",
            slug="identity-vendor-identity-model",
            u_height=1,
        )
        cls.role = DeviceRole.objects.create(name="Identity Role", slug="identity-role")
        cls.rack_a = Rack.objects.create(site=cls.site, name="IDENTITY-RACK-A", u_height=42)
        cls.rack_b = Rack.objects.create(site=cls.site, name="IDENTITY-RACK-B", u_height=42)
        cls.profile = ImportProfile.objects.create(
            name="Identity Safety Profile",
            sheet_name="Data",
            update_existing=True,
            create_missing_device_types=False,
        )
        ClassRoleMapping.objects.create(
            profile=cls.profile,
            source_class="Server",
            creates_rack=False,
            role_slug=cls.role.slug,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _device_row(self, row_number, source_id, name, rack, position):
        return {
            "_row_number": row_number,
            "source_id": source_id,
            "device_name": name,
            "device_class": "Server",
            "make": self.manufacturer.name,
            "model": self.device_type.model,
            "u_height": "1",
            "rack_name": rack.name,
            "u_position": str(position),
            "face": "front",
            "serial": "",
            "asset_tag": "",
            "status": "active",
        }

    def _rack_profile(self, name="Rack Safety Profile"):
        profile = ImportProfile.objects.create(
            name=name,
            sheet_name="Data",
            update_existing=True,
        )
        ClassRoleMapping.objects.create(
            profile=profile,
            source_class="Cabinet",
            creates_rack=True,
        )
        return profile

    def _rack_row(self, row_number, source_id, rack_name, u_height="42"):
        return {
            "_row_number": row_number,
            "source_id": source_id,
            "device_name": rack_name,
            "rack_name": rack_name,
            "device_class": "Cabinet",
            "u_height": u_height,
            "serial": "",
        }

    def _set_import_session(self, rows, result=None, location=None, tenant=None):
        if result is None:
            result = run_import(
                rows,
                self.profile,
                {"site": self.site, "location": location, "tenant": tenant},
                dry_run=True,
            )
        session = self.client.session
        session["import_rows"] = _serialize_rows(rows)
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": location.pk if location else None,
            "tenant_id": tenant.pk if tenant else None,
            "filename": "identity-safety.xlsx",
        }
        session["import_result"] = result.to_session_dict() if hasattr(result, "to_session_dict") else result
        session.save()

    def _grant_object_permission(self, user, name, model, actions, constraints=None):
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.create(name=name, actions=actions, constraints=constraints)
        permission.object_types.add(ContentType.objects.get_for_model(model))
        permission.users.add(user)
        return permission

    def test_duplicate_names_are_identity_conflicts_with_unique_suggestions(self):
        rows = [
            self._device_row(2, "SRC-A", "shared-label", self.rack_a, 1),
            self._device_row(3, "SRC-B", "shared-label", self.rack_b, 2),
        ]

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [row for row in result.rows if row.object_type == "device"]

        self.assertEqual([row.action for row in device_rows], ["error", "error"])
        self.assertTrue(all(row.extra_data.get("identity_conflict") == "duplicate_name" for row in device_rows))
        suggestions = [row.extra_data.get("suggested_name") for row in device_rows]
        self.assertTrue(all(suggestions))
        self.assertEqual(len(set(suggestions)), 2)

    def test_duplicate_name_suggestion_can_be_saved_for_one_source_row(self):
        rows = [
            self._device_row(2, "SRC-A", "shared-label", self.rack_a, 1),
            self._device_row(3, "SRC-B", "shared-label", self.rack_b, 2),
        ]
        preview = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        suggestion = next(row for row in preview.rows if row.row_number == 3 and row.object_type == "device")
        self._set_import_session(rows, preview)

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": "SRC-B",
                "row_number": 3,
                "new_name": suggestion.extra_data["suggested_name"],
            },
        )

        self.assertEqual(response.status_code, 302)
        resolution = SourceResolution.objects.get(
            profile=self.profile,
            source_id="SRC-B",
            source_column="device_name",
        )
        self.assertEqual(resolution.resolved_fields, {"device_name": suggestion.extra_data["suggested_name"]})
        resolved_rows = reapply_saved_resolutions(rows, self.profile)
        result = run_import(resolved_rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [row for row in result.rows if row.object_type == "device"]
        self.assertEqual([row.action for row in device_rows], ["create", "create"])

    def test_single_row_sync_rechecks_full_batch_and_does_not_update_same_name_device(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="shared-label",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        rows = [
            self._device_row(2, "SRC-A", "shared-label", self.rack_a, 1),
            self._device_row(3, "SRC-B", "shared-label", self.rack_b, 2),
        ]
        stale_preview = {
            "rows": [
                {"row_number": 2, "action": "create", "object_type": "device"},
                {"row_number": 3, "action": "create", "object_type": "device"},
            ]
        }
        self._set_import_session(rows, stale_preview)

        response = self.client.post(reverse("plugins:netbox_data_import:sync_single_row"), {"row_number": 3})

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        existing.refresh_from_db()
        self.assertEqual(existing.rack, self.rack_a)
        self.assertEqual(existing.position, 1)

    def test_single_row_sync_rejects_changed_create_plan_after_preview(self):
        from dcim.models import Device

        rows = [self._device_row(2, "SYNC-STALE-PLAN", "previewed-device-name", self.rack_a, 1)]
        preview = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        self._set_import_session(rows, preview)

        session = self.client.session
        changed_rows = session["import_rows"]
        changed_rows[0]["device_name"] = "changed-device-name"
        session["import_rows"] = changed_rows
        session.save()

        response = self.client.post(reverse("plugins:netbox_data_import:sync_single_row"), {"row_number": 2})

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        self.assertIn("Review the refreshed preview", response.json()["error"])
        self.assertFalse(Device.objects.filter(name__in=["previewed-device-name", "changed-device-name"]).exists())

    def test_bulk_run_rejects_stale_create_that_now_matches_existing_device(self):
        from dcim.models import Device

        rows = [self._device_row(2, "SRC-STALE", "stale-create", self.rack_b, 2)]
        preview = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        self._set_import_session(rows, preview)
        existing = Device.objects.create(
            name="stale-create",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )

        response = self.client.post(reverse("plugins:netbox_data_import:import_run"))

        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        self.assertEqual(existing.rack, self.rack_a)
        self.assertEqual(existing.position, 1)

    def test_auto_match_does_not_link_duplicate_source_names_to_one_device(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="shared-label",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        rows = [
            self._device_row(2, "SRC-A", existing.name, self.rack_a, 1),
            self._device_row(3, "SRC-B", existing.name, self.rack_b, 2),
        ]
        self._set_import_session(rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:auto_match_devices"),
            {"profile_id": self.profile.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile).exists())

    def test_manual_link_rejects_target_already_bound_to_another_source(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="bound-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="SRC-A",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        rows = [self._device_row(3, "SRC-B", "other-source", self.rack_b, 2)]
        self._set_import_session(rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:match_existing_device"),
            {
                "profile_id": self.profile.pk,
                "source_id": "SRC-B",
                "netbox_device_id": existing.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="SRC-B").exists())

    def test_stored_source_metadata_is_used_as_identity_after_netbox_rename(self):
        from dcim.models import Device
        from extras.models import CustomField

        device_content_type = ContentType.objects.get_for_model(Device)
        custom_field, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            custom_field.object_types.set([device_content_type])
        elif not custom_field.object_types.filter(pk=device_content_type.pk).exists():
            custom_field.object_types.add(device_content_type)
        existing = Device.objects.create(
            name="renamed-in-netbox",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            custom_field_data={
                "data_import_source": {
                    "profile_id": self.profile.pk,
                    "profile_name": self.profile.name,
                    "source_id": "SRC-STABLE",
                }
            },
        )
        rows = [self._device_row(2, "SRC-STABLE", "source-name", self.rack_a, 1)]

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_row = next(row for row in result.rows if row.object_type == "device")

        self.assertEqual(device_row.action, "update")
        self.assertEqual(device_row.extra_data.get("netbox_device_id"), existing.pk)
        self.assertIn("source", device_row.detail.lower())

        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        self.assertTrue(
            DeviceExistingMatch.objects.filter(
                profile=self.profile,
                source_id="SRC-STABLE",
                netbox_device_id=existing.pk,
            ).exists()
        )

    def test_created_device_gets_persistent_source_binding(self):
        from dcim.models import Device

        rows = [self._device_row(2, "SRC-CREATE", "created-with-binding", self.rack_a, 1)]

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=False)

        device_row = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_row.action, "create")
        device = Device.objects.get(name="created-with-binding", site=self.site)
        self.assertTrue(
            DeviceExistingMatch.objects.filter(
                profile=self.profile,
                source_id="SRC-CREATE",
                netbox_device_id=device.pk,
            ).exists()
        )

    def test_database_rejects_two_source_bindings_to_one_device(self):
        from dcim.models import Device
        from django.db import IntegrityError, transaction

        device = Device.objects.create(
            name="unique-binding-target",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="BIND-A",
            netbox_device_id=device.pk,
            device_name=device.name,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            DeviceExistingMatch.objects.create(
                profile=self.profile,
                source_id="BIND-B",
                netbox_device_id=device.pk,
                device_name=device.name,
            )

    def test_engine_binding_cannot_overwrite_a_concurrent_relink(self):
        from dcim.models import Device
        from django.db import IntegrityError, transaction

        first = Device.objects.create(
            name="binding-race-first",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        second = Device.objects.create(
            name="binding-race-second",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        binding = DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="BINDING-RACE",
            netbox_device_id=second.pk,
            device_name=second.name,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            _bind_device_source(self.profile, binding.source_id, first)

        binding.refresh_from_db()
        self.assertEqual(binding.netbox_device_id, second.pk)

    def test_duplicate_source_ids_are_rejected_before_any_device_write(self):
        rows = [
            self._device_row(2, "SRC-DUP", "device-a", self.rack_a, 1),
            self._device_row(3, "SRC-DUP", "device-b", self.rack_b, 2),
        ]

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [row for row in result.rows if row.object_type == "device"]

        self.assertEqual([row.action for row in device_rows], ["error", "error"])
        self.assertTrue(all(row.extra_data.get("identity_conflict") == "duplicate_source_id" for row in device_rows))

    def test_rack_with_same_name_in_other_location_is_not_selected_for_update(self):
        from dcim.models import Location, Rack

        location_a = Location.objects.create(name="Identity Location A", slug="identity-location-a", site=self.site)
        location_b = Location.objects.create(name="Identity Location B", slug="identity-location-b", site=self.site)
        existing = Rack.objects.create(site=self.site, location=location_a, name="SHARED-RACK", u_height=42)
        rack_profile = ImportProfile.objects.create(
            name="Rack Identity Profile",
            sheet_name="Data",
            update_existing=True,
        )
        ClassRoleMapping.objects.create(
            profile=rack_profile,
            source_class="Cabinet",
            creates_rack=True,
        )
        rows = [
            {
                "_row_number": 2,
                "source_id": "RACK-SRC",
                "device_name": "SHARED-RACK",
                "rack_name": "SHARED-RACK",
                "device_class": "Cabinet",
                "u_height": "42",
                "serial": "",
            }
        ]

        result = run_import(rows, rack_profile, {"site": self.site, "location": location_b}, dry_run=True)
        rack_row = next(row for row in result.rows if row.object_type == "rack")

        self.assertEqual(rack_row.action, "create")
        existing.refresh_from_db()
        self.assertEqual(existing.location, location_a)

    def test_hidden_rack_still_participates_in_case_insensitive_ambiguity(self):
        from dcim.models import Rack

        visible = Rack.objects.create(site=self.site, name="SCOPED-RACK", u_height=42)
        Rack.objects.create(site=self.site, name="scoped-rack", u_height=42)
        user = get_user_model().objects.create_user(username="scoped-rack-ambiguity-user", password="testpass")
        self._grant_object_permission(
            user,
            "View one ambiguous rack",
            Rack,
            ["view"],
            {"pk": visible.pk},
        )
        row = self._device_row(2, "SCOPED-RACK-AMBIGUITY", "scoped-rack-device", visible, 1)

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True, user=user)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "ambiguous_rack")

    def test_derived_slug_collisions_are_rejected_for_each_source_row(self):
        collision_profile = ImportProfile.objects.create(
            name="Slug Collision Profile",
            sheet_name="Data",
            update_existing=True,
            create_missing_device_types=True,
        )
        ClassRoleMapping.objects.create(
            profile=collision_profile,
            source_class="Server",
            creates_rack=False,
            role_slug=self.role.slug,
        )
        rows = [
            {
                **self._device_row(2, "SLUG-A", "slug-device-a", self.rack_a, 1),
                "make": "Slug Vendor",
                "model": "Shared Model",
            },
            {
                **self._device_row(3, "SLUG-B", "slug-device-b", self.rack_b, 2),
                "make": "Slug-Vendor",
                "model": "Shared Model",
            },
        ]

        result = run_import(rows, collision_profile, {"site": self.site}, dry_run=True)
        device_rows = [row for row in result.rows if row.object_type == "device"]

        self.assertEqual([row.action for row in device_rows], ["error", "error"])
        self.assertTrue(all(row.extra_data.get("identity_conflict") == "derived_slug_collision" for row in device_rows))

    def test_derived_manufacturer_slug_cannot_merge_with_different_existing_name(self):
        from dcim.models import Manufacturer

        Manufacturer.objects.create(name="Existing Slug Vendor", slug="slug-vendor")
        collision_profile = ImportProfile.objects.create(
            name="Existing Manufacturer Slug Profile",
            sheet_name="Data",
            create_missing_device_types=True,
        )
        ClassRoleMapping.objects.create(
            profile=collision_profile,
            source_class="Server",
            creates_rack=False,
            role_slug=self.role.slug,
        )
        row = {
            **self._device_row(2, "MFG-SLUG", "manufacturer-slug-device", self.rack_a, 1),
            "make": "Slug Vendor",
            "model": "Unique Model",
        }

        result = run_import([row], collision_profile, {"site": self.site}, dry_run=True)
        device_row = next(row for row in result.rows if row.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "derived_slug_collision")

    def test_derived_device_type_slug_cannot_merge_with_different_existing_model(self):
        from dcim.models import DeviceType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Type Collision Vendor", slug="type-collision-vendor")
        DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Model-A",
            slug="type-collision-vendor-model-a",
            u_height=1,
        )
        collision_profile = ImportProfile.objects.create(
            name="Existing Device Type Slug Profile",
            sheet_name="Data",
            create_missing_device_types=True,
        )
        ClassRoleMapping.objects.create(
            profile=collision_profile,
            source_class="Server",
            creates_rack=False,
            role_slug=self.role.slug,
        )
        row = {
            **self._device_row(2, "DT-SLUG", "device-type-slug-device", self.rack_a, 1),
            "make": manufacturer.name,
            "model": "Model A",
        }

        result = run_import([row], collision_profile, {"site": self.site}, dry_run=True)
        device_row = next(row for row in result.rows if row.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "derived_slug_collision")

    def test_update_preview_lists_every_field_that_the_writer_changes(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer
        from tenancy.models import Tenant

        old_manufacturer = Manufacturer.objects.create(name="Old Vendor", slug="old-vendor")
        old_type = DeviceType.objects.create(
            manufacturer=old_manufacturer,
            model="Old Model",
            slug="old-vendor-old-model",
            u_height=1,
        )
        old_role = DeviceRole.objects.create(name="Old Role", slug="old-role")
        tenant = Tenant.objects.create(name="Identity Tenant", slug="identity-tenant")
        existing = Device.objects.create(
            name="field-diff-device",
            site=self.site,
            device_type=old_type,
            role=old_role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="DIFF-SRC",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        row = self._device_row(2, "DIFF-SRC", "field-diff-device", self.rack_b, 2)

        result = run_import(
            [row],
            self.profile,
            {"site": self.site, "tenant": tenant},
            dry_run=True,
        )
        device_row = next(row for row in result.rows if row.object_type == "device")
        field_diff = device_row.extra_data["field_diff"]

        self.assertTrue({"rack_name", "device_type", "role", "tenant"}.issubset(field_diff))

    def test_name_only_match_with_different_placement_requires_unique_name(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="placement-conflict",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        row = self._device_row(2, "PLACEMENT-SRC", existing.name, self.rack_b, 2)

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "name_placement_conflict")
        self.assertTrue(device_row.extra_data.get("suggested_name"))
        self.assertIn(self.rack_a.name, device_row.detail)
        self.assertIn(self.rack_b.name, device_row.detail)
        self._set_import_session([row], result)
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertContains(response, "Use name")
        self.assertContains(response, device_row.extra_data["suggested_name"])

    def test_unracked_name_match_in_another_location_requires_unique_name(self):
        from dcim.models import Device, Location

        location_a = Location.objects.create(name="Unracked Location A", slug="unracked-location-a", site=self.site)
        location_b = Location.objects.create(name="Unracked Location B", slug="unracked-location-b", site=self.site)
        existing = Device.objects.create(
            name="unracked-location-conflict",
            site=self.site,
            location=location_a,
            device_type=self.device_type,
            role=self.role,
        )
        row = self._device_row(2, "UNRACKED-LOCATION", existing.name, self.rack_a, 1)
        row.update({"rack_name": "", "u_position": "", "face": ""})

        preview = run_import(
            [row],
            self.profile,
            {"site": self.site, "location": location_b},
            dry_run=True,
        )
        preview_row = next(item for item in preview.rows if item.object_type == "device")

        self.assertEqual(preview_row.action, "error")
        self.assertEqual(preview_row.extra_data.get("identity_conflict"), "name_placement_conflict")
        self.assertTrue(preview_row.extra_data.get("suggested_name"))

        result = run_import(
            [row],
            self.profile,
            {"site": self.site, "location": location_b},
            dry_run=False,
        )
        write_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(write_row.action, "error")
        existing.refresh_from_db()
        self.assertEqual(existing.location, location_a)

    def test_unracked_name_match_in_same_location_can_update(self):
        from dcim.models import Device, Location

        location = Location.objects.create(name="Shared Unracked Location", slug="shared-unracked", site=self.site)
        existing = Device.objects.create(
            name="unracked-same-location",
            site=self.site,
            location=location,
            device_type=self.device_type,
            role=self.role,
        )
        row = self._device_row(2, "UNRACKED-SAME", existing.name, self.rack_a, 1)
        row.update({"rack_name": "", "u_position": "", "face": ""})

        preview = run_import(
            [row],
            self.profile,
            {"site": self.site, "location": location},
            dry_run=True,
        )
        preview_row = next(item for item in preview.rows if item.object_type == "device")

        self.assertEqual(preview_row.action, "update")
        self.assertNotIn("identity_conflict", preview_row.extra_data)

    def test_auto_match_does_not_bypass_name_placement_conflict(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="auto-placement-conflict",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        row = self._device_row(2, "AUTO-PLACEMENT", existing.name, self.rack_b, 2)
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:auto_match_devices"),
            {"profile_id": self.profile.pk},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 placement conflict(s)")
        self.assertNotContains(response, "1 ambiguous (multiple devices)")
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AUTO-PLACEMENT").exists())

    def test_name_match_is_scoped_to_tenant(self):
        from dcim.models import Device
        from tenancy.models import Tenant

        tenant_a = Tenant.objects.create(name="Identity Tenant A", slug="identity-tenant-a")
        tenant_b = Tenant.objects.create(name="Identity Tenant B", slug="identity-tenant-b")
        existing = Device.objects.create(
            name="tenant-scoped-name",
            site=self.site,
            tenant=tenant_a,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        row = self._device_row(2, "TENANT-SRC", existing.name, self.rack_b, 2)

        result = run_import([row], self.profile, {"site": self.site, "tenant": tenant_b}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "create")
        self.assertNotIn("netbox_device_id", device_row.extra_data)

    def test_strong_match_outside_active_site_is_an_error(self):
        from dcim.models import Device, Site

        other_site = Site.objects.create(name="Other Identity Site", slug="other-identity-site")
        existing = Device.objects.create(
            name="cross-site-device",
            site=other_site,
            device_type=self.device_type,
            role=self.role,
            serial="CROSS-SITE-SERIAL",
        )
        row = self._device_row(2, "CROSS-SITE-SRC", "source-cross-site", self.rack_a, 1)
        row["serial"] = existing.serial

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "cross_site_match")
        self.assertIn(other_site.name, device_row.detail)

    def test_stored_source_metadata_outside_active_site_is_an_error(self):
        from dcim.models import Device, Site
        from extras.models import CustomField

        content_type = ContentType.objects.get_for_model(Device)
        custom_field, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            custom_field.object_types.set([content_type])
        elif not custom_field.object_types.filter(pk=content_type.pk).exists():
            custom_field.object_types.add(content_type)
        other_site = Site.objects.create(name="Metadata Identity Site", slug="metadata-identity-site")
        existing = Device.objects.create(
            name="metadata-cross-site-device",
            site=other_site,
            device_type=self.device_type,
            role=self.role,
            custom_field_data={
                "data_import_source": {
                    "profile_id": self.profile.pk,
                    "profile_name": self.profile.name,
                    "source_id": "METADATA-CROSS-SITE",
                }
            },
        )
        row = self._device_row(2, "METADATA-CROSS-SITE", "metadata-source-name", self.rack_a, 1)

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "cross_site_match")
        self.assertEqual(device_row.extra_data.get("netbox_device_id"), existing.pk)

    def test_rack_location_is_visible_in_placement_diff(self):
        from dcim.models import Device, Location, Rack

        location_a = Location.objects.create(name="Placement Location A", slug="placement-location-a", site=self.site)
        location_b = Location.objects.create(name="Placement Location B", slug="placement-location-b", site=self.site)
        rack_a = Rack.objects.create(site=self.site, location=location_a, name="SHARED-PLACEMENT-RACK", u_height=42)
        rack_b = Rack.objects.create(site=self.site, location=location_b, name="SHARED-PLACEMENT-RACK", u_height=42)
        existing = Device.objects.create(
            name="location-move-device",
            site=self.site,
            location=location_a,
            device_type=self.device_type,
            role=self.role,
            rack=rack_a,
            position=1,
            face="front",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="LOCATION-SRC",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        row = self._device_row(2, "LOCATION-SRC", existing.name, rack_b, 1)

        result = run_import(
            [row],
            self.profile,
            {"site": self.site, "location": location_b},
            dry_run=True,
        )
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "update")
        rack_diff = device_row.extra_data["field_diff"]["rack_name"]
        self.assertIn(location_a.name, rack_diff["netbox"])
        self.assertIn(location_b.name, rack_diff["file"])

    def test_missing_rack_blocks_update_instead_of_unmounting_device(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="missing-rack-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="MISSING-RACK-SRC",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        row = self._device_row(2, "MISSING-RACK-SRC", existing.name, self.rack_b, 2)
        row["rack_name"] = "RACK-DOES-NOT-EXIST"

        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        preview_row = next(item for item in preview.rows if item.object_type == "device")
        executed = run_import([row], self.profile, {"site": self.site}, dry_run=False)
        executed_row = next(item for item in executed.rows if item.object_type == "device")

        self.assertEqual(preview_row.action, "error")
        self.assertEqual(preview_row.extra_data.get("identity_conflict"), "rack_not_found")
        self.assertEqual(executed_row.action, "error")
        existing.refresh_from_db()
        self.assertEqual(existing.rack, self.rack_a)
        self.assertEqual(existing.position, 1)

    def test_existing_multi_u_rack_occupancy_blocks_preview(self):
        from dcim.models import Device, DeviceType

        two_u_type = DeviceType.objects.create(
            manufacturer=self.manufacturer,
            model="Identity Two Unit Model",
            slug="identity-vendor-identity-two-unit-model",
            u_height=2,
        )
        Device.objects.create(
            name="rack-space-owner",
            site=self.site,
            device_type=two_u_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        row = self._device_row(2, "OCCUPIED-SRC", "rack-space-candidate", self.rack_a, 2)

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "rack_position_occupied")

    def test_run_refreshes_skip_that_would_become_an_update(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="stale-skip-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
            serial="OLD-SERIAL",
        )
        row = self._device_row(2, "STALE-SKIP-SRC", existing.name, self.rack_a, 1)
        row["serial"] = "NEW-SERIAL"
        self.profile.update_existing = False
        self.profile.save(update_fields=["update_existing"])
        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        self._set_import_session([row], preview)
        self.profile.update_existing = True
        self.profile.save(update_fields=["update_existing"])

        response = self.client.post(reverse("plugins:netbox_data_import:import_run"))

        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        self.assertEqual(existing.serial, "OLD-SERIAL")
        refreshed = self.client.session["import_result"]
        self.assertEqual(refreshed["rows"][0]["action"], "update")

    def test_run_refreshes_update_when_existing_placement_changed(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="stale-placement-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="STALE-PLACEMENT-SRC",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        row = self._device_row(2, "STALE-PLACEMENT-SRC", existing.name, self.rack_b, 2)
        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        self._set_import_session([row], preview)
        existing.rack = self.rack_b
        existing.position = 3
        existing.save(update_fields=["rack", "position"])

        response = self.client.post(reverse("plugins:netbox_data_import:import_run"))

        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        self.assertEqual(existing.rack, self.rack_b)
        self.assertEqual(existing.position, 3)

    def test_blank_face_and_airflow_do_not_clear_existing_values(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="preserve-optional-placement",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
            airflow="front-to-rear",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="PRESERVE-SRC",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        row = self._device_row(2, "PRESERVE-SRC", existing.name, self.rack_a, 1)
        row["face"] = ""
        row["airflow"] = ""

        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        preview_row = next(item for item in preview.rows if item.object_type == "device")
        run_import([row], self.profile, {"site": self.site}, dry_run=False)

        self.assertNotIn("face", preview_row.extra_data["field_diff"])
        self.assertNotIn("airflow", preview_row.extra_data["field_diff"])
        existing.refresh_from_db()
        self.assertEqual(existing.face, "front")
        self.assertEqual(existing.airflow, "front-to-rear")

    def test_duplicate_names_use_case_insensitive_identity(self):
        rows = [
            self._device_row(2, "CASE-A", "Case-Device", self.rack_a, 1),
            self._device_row(3, "CASE-B", "case-device", self.rack_b, 2),
        ]

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([item.action for item in device_rows], ["error", "error"])
        self.assertTrue(all(item.extra_data.get("identity_conflict") == "duplicate_name" for item in device_rows))

    def test_duplicate_name_resolution_rejects_case_insensitive_collision(self):
        rows = [
            self._device_row(2, "RESOLVE-CASE-A", "Case-Target", self.rack_a, 1),
            self._device_row(3, "RESOLVE-CASE-B", "duplicate", self.rack_b, 2),
        ]
        self._set_import_session(rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": "RESOLVE-CASE-B",
                "row_number": 3,
                "new_name": "case-target",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(profile=self.profile, source_id="RESOLVE-CASE-B").exists())

    def test_ignored_class_does_not_poison_duplicate_name_detection(self):
        ignored_mapping = ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Ignored Class",
            creates_rack=False,
            ignore=True,
        )
        active = self._device_row(2, "ACTIVE-SRC", "shared-with-ignored", self.rack_a, 1)
        ignored = self._device_row(3, "IGNORED-SRC", "shared-with-ignored", self.rack_b, 2)
        ignored["device_class"] = ignored_mapping.source_class

        result = run_import([active, ignored], self.profile, {"site": self.site}, dry_run=True)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([item.action for item in device_rows], ["create", "ignore"])

    def test_duplicate_strong_identifiers_are_rejected(self):
        for field_name, value, conflict_kind in (
            ("serial", "DUPLICATE-SERIAL", "duplicate_serial"),
            ("asset_tag", "DUPLICATE-ASSET", "duplicate_asset_tag"),
        ):
            with self.subTest(field_name=field_name):
                rows = [
                    self._device_row(2, f"{field_name}-A", f"{field_name}-device-a", self.rack_a, 1),
                    self._device_row(3, f"{field_name}-B", f"{field_name}-device-b", self.rack_b, 2),
                ]
                for row in rows:
                    row[field_name] = value

                result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
                device_rows = [item for item in result.rows if item.object_type == "device"]

                self.assertEqual([item.action for item in device_rows], ["error", "error"])
                self.assertTrue(all(item.extra_data.get("identity_conflict") == conflict_kind for item in device_rows))

    def test_none_like_source_ids_are_not_persisted_as_bindings(self):
        rows = [
            self._device_row(2, "N/A", "none-like-source-a", self.rack_a, 1),
            self._device_row(3, "nan", "none-like-source-b", self.rack_b, 2),
        ]

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([item.action for item in device_rows], ["create", "create"])
        self.assertEqual([item.source_id for item in device_rows], ["", ""])
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile).exists())

    def test_write_time_manufacturer_slug_check_rejects_different_identity(self):
        from dcim.models import Manufacturer

        Manufacturer.objects.create(name="Existing Write Identity", slug="write-race-slug")
        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=False,
            result=ImportResult(),
        )
        row = self._device_row(2, "WRITE-RACE-SRC", "write-race-device", self.rack_a, 1)

        _ensure_manufacturer("write-race-slug", "Different Write Identity", set(), ctx, row, Manufacturer)

        self.assertIn(row["_row_number"], ctx.slug_conflicts_by_row)
        self.assertEqual(Manufacturer.objects.get(slug="write-race-slug").name, "Existing Write Identity")

    def test_name_only_match_with_different_face_requires_unique_name(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="face-conflict-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        row = self._device_row(2, "FACE-CONFLICT", existing.name, self.rack_a, 1)
        row["face"] = "rear"

        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        preview_row = next(item for item in preview.rows if item.object_type == "device")
        self.assertEqual(preview_row.action, "error")
        self.assertEqual(preview_row.extra_data.get("identity_conflict"), "name_placement_conflict")
        self.assertIn("front", preview_row.detail)
        self.assertIn("rear", preview_row.detail)

        result = run_import([row], self.profile, {"site": self.site}, dry_run=False)
        write_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(write_row.action, "error")
        existing.refresh_from_db()
        self.assertEqual(existing.face, "front")

    def test_preview_reserves_resolved_device_type_height(self):
        self.device_type.u_height = 3
        self.device_type.save(update_fields=["u_height"])
        rows = [
            self._device_row(2, "HEIGHT-A", "height-device-a", self.rack_a, 1),
            self._device_row(3, "HEIGHT-B", "height-device-b", self.rack_a, 2),
        ]

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([item.action for item in device_rows], ["create", "error"])
        self.assertEqual(device_rows[1].extra_data.get("identity_conflict"), "rack_position_occupied")

    def test_preview_checks_existing_occupancy_for_pending_device_type(self):
        from dcim.models import Device

        Device.objects.create(
            name="pending-type-blocker",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=10,
            face="front",
        )
        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        row = self._device_row(2, "PENDING-TYPE", "pending-type-device", self.rack_a, 10)
        row.update({"make": "Pending Vendor", "model": "Pending Model", "u_height": "2"})

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "rack_position_occupied")

    def test_pending_device_type_claim_uses_netbox_full_depth_default(self):
        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        rows = [
            self._device_row(2, "PENDING-FULL-A", "pending-full-depth-a", self.rack_a, 12),
            self._device_row(3, "PENDING-FULL-B", "pending-full-depth-b", self.rack_a, 12),
        ]
        for row in rows:
            row.update({"make": "Pending Full Vendor", "model": "Pending Full Model"})
        rows[1]["face"] = "rear"

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([item.action for item in device_rows], ["create", "error"])
        self.assertEqual(device_rows[1].extra_data.get("identity_conflict"), "rack_position_occupied")

    def test_full_depth_device_claim_conflicts_across_faces(self):
        from dcim.models import DeviceType

        full_depth_type = DeviceType.objects.create(
            manufacturer=self.manufacturer,
            model="Full Depth Model",
            slug="identity-vendor-full-depth-model",
            u_height=1,
            is_full_depth=True,
        )
        rows = [
            self._device_row(2, "FULL-A", "full-depth-a", self.rack_a, 5),
            self._device_row(3, "FULL-B", "full-depth-b", self.rack_a, 5),
        ]
        for row in rows:
            row["model"] = full_depth_type.model
        rows[1]["face"] = "rear"

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([item.action for item in device_rows], ["create", "error"])
        self.assertEqual(device_rows[1].extra_data.get("identity_conflict"), "rack_position_occupied")

    def test_rack_shrink_below_mounted_device_is_rejected(self):
        from dcim.models import Device

        Device.objects.create(
            name="high-mounted-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=40,
            face="front",
        )
        profile = self._rack_profile("Rack Shrink Profile")
        row = self._rack_row(2, "RACK-SHRINK", self.rack_a.name, "20")

        preview = run_import([row], profile, {"site": self.site}, dry_run=True)
        preview_row = next(item for item in preview.rows if item.object_type == "rack")
        self.assertEqual(preview_row.action, "error")

        result = run_import([row], profile, {"site": self.site}, dry_run=False)
        write_row = next(item for item in result.rows if item.object_type == "rack")
        self.assertEqual(write_row.action, "error")
        self.rack_a.refresh_from_db()
        self.assertEqual(self.rack_a.u_height, 42)

    def test_ambiguous_root_rack_blocks_device_placement(self):
        from dcim.models import Rack

        Rack.objects.create(site=self.site, name=self.rack_a.name, u_height=42)
        row = self._device_row(2, "AMBIGUOUS-RACK", "ambiguous-rack-device", self.rack_a, 1)

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "ambiguous_rack")

    def test_ambiguous_root_rack_row_returns_error_instead_of_crashing(self):
        from dcim.models import Rack

        Rack.objects.create(site=self.site, name=self.rack_a.name, u_height=42)
        profile = self._rack_profile("Ambiguous Rack Profile")
        row = self._rack_row(2, "AMBIGUOUS-RACK-ROW", self.rack_a.name)

        result = run_import([row], profile, {"site": self.site}, dry_run=True)
        rack_row = next(item for item in result.rows if item.object_type == "rack")

        self.assertEqual(rack_row.action, "error")
        self.assertEqual(rack_row.extra_data.get("identity_conflict"), "ambiguous_rack")

    def test_ambiguous_stored_source_metadata_blocks_import(self):
        from dcim.models import Device
        from extras.models import CustomField

        content_type = ContentType.objects.get_for_model(Device)
        custom_field, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            custom_field.object_types.set([content_type])
        elif not custom_field.object_types.filter(pk=content_type.pk).exists():
            custom_field.object_types.add(content_type)
        metadata = {
            "data_import_source": {
                "profile_id": self.profile.pk,
                "profile_name": self.profile.name,
                "source_id": "AMBIGUOUS-METADATA",
            }
        }
        for suffix in ("a", "b"):
            Device.objects.create(
                name=f"metadata-device-{suffix}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                custom_field_data=metadata,
            )
        row = self._device_row(2, "AMBIGUOUS-METADATA", "renamed-source-device", self.rack_a, 1)

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "ambiguous_source_id")

    def test_individually_ignored_device_does_not_preview_type_side_effects(self):
        from netbox_data_import.models import IgnoredDevice

        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        IgnoredDevice.objects.create(profile=self.profile, source_id="IGNORED-TYPE")
        row = self._device_row(2, "IGNORED-TYPE", "ignored-type-device", self.rack_a, 1)
        row.update({"make": "Ignored Vendor", "model": "Ignored Model"})

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)

        self.assertEqual([(item.object_type, item.action) for item in result.rows], [("device", "ignore")])

    def test_individually_ignored_rack_is_not_written(self):
        from dcim.models import Rack
        from netbox_data_import.models import IgnoredDevice

        profile = self._rack_profile("Ignored Rack Profile")
        IgnoredDevice.objects.create(profile=profile, source_id="IGNORED-RACK")
        row = self._rack_row(2, "IGNORED-RACK", "IGNORED-RACK-NAME")

        result = run_import([row], profile, {"site": self.site}, dry_run=False)

        self.assertFalse(Rack.objects.filter(site=self.site, name="IGNORED-RACK-NAME").exists())
        rack_row = next(item for item in result.rows if item.object_type == "rack")
        self.assertEqual(rack_row.action, "ignore")

    def test_ignored_row_does_not_create_slug_conflict_for_active_row(self):
        from netbox_data_import.models import IgnoredDevice

        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        IgnoredDevice.objects.create(profile=self.profile, source_id="IGNORED-SLUG")
        ignored = self._device_row(2, "IGNORED-SLUG", "ignored-slug-device", self.rack_a, 1)
        ignored.update({"make": "Slug Vendor", "model": "Model A"})
        active = self._device_row(3, "ACTIVE-SLUG", "active-slug-device", self.rack_a, 2)
        active.update({"make": "Slug-Vendor", "model": "Model A"})

        result = run_import([ignored, active], self.profile, {"site": self.site}, dry_run=True)
        active_row = next(item for item in result.rows if item.row_number == 3 and item.object_type == "device")

        self.assertEqual(active_row.action, "create")

    def test_none_like_rack_source_id_is_normalized_before_storage(self):
        from dcim.models import Rack

        profile = self._rack_profile("Rack Source ID Profile")
        row = self._rack_row(2, "N/A", "NORMALIZED-SOURCE-RACK")

        result = run_import([row], profile, {"site": self.site}, dry_run=False)

        self.assertEqual(next(item for item in result.rows if item.object_type == "rack").action, "create")
        rack = Rack.objects.get(site=self.site, name="NORMALIZED-SOURCE-RACK")
        self.assertEqual(rack.custom_field_data["data_import_source"]["source_id"], "")

    def test_blank_source_id_duplicate_does_not_offer_unusable_name_resolution(self):
        rows = [
            self._device_row(2, "", "blank-source-name", self.rack_a, 1),
            self._device_row(3, "N/A", "blank-source-name", self.rack_b, 2),
        ]
        preview = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        self._set_import_session(rows, preview)

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertNotContains(response, "Use name")

    def test_execute_rejects_device_state_changed_after_preview(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="locked-state-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
            serial="OLD-SERIAL",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="LOCKED-STATE",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        row = self._device_row(2, "LOCKED-STATE", existing.name, self.rack_b, 2)
        row["serial"] = "SOURCE-SERIAL"
        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        intents = _import_intents(preview)
        existing.serial = "CONCURRENT-SERIAL"
        existing.save(update_fields=["serial"])

        result = run_import(
            [row],
            self.profile,
            {"site": self.site},
            dry_run=False,
            expected_intents=intents,
        )

        device_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(device_row.action, "error")
        self.assertTrue(device_row.extra_data.get("identity_state_changed"))
        existing.refresh_from_db()
        self.assertEqual(existing.serial, "CONCURRENT-SERIAL")

    def test_execute_rejects_rack_state_changed_after_preview(self):
        profile = self._rack_profile("Rack State Guard Profile")
        self.rack_a.serial = "OLD-RACK-SERIAL"
        self.rack_a.save(update_fields=["serial"])
        row = self._rack_row(2, "RACK-STATE", self.rack_a.name)
        row["serial"] = "SOURCE-RACK-SERIAL"
        preview = run_import([row], profile, {"site": self.site}, dry_run=True)
        intents = _import_intents(preview)
        self.rack_a.serial = "CONCURRENT-RACK-SERIAL"
        self.rack_a.save(update_fields=["serial"])

        result = run_import(
            [row],
            profile,
            {"site": self.site},
            dry_run=False,
            expected_intents=intents,
        )

        rack_row = next(item for item in result.rows if item.object_type == "rack")
        self.assertEqual(rack_row.action, "error")
        self.assertTrue(rack_row.extra_data.get("identity_state_changed"))
        self.rack_a.refresh_from_db()
        self.assertEqual(self.rack_a.serial, "CONCURRENT-RACK-SERIAL")

    def test_write_time_device_type_check_applies_to_every_shared_slug_row(self):
        from dcim.models import DeviceType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Shared Slug Vendor", slug="shared-slug-vendor")
        DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Existing Shared Model",
            slug="shared-model",
            u_height=1,
        )
        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=False,
            result=ImportResult(),
        )
        rows = [
            self._device_row(2, "SHARED-SLUG-A", "shared-slug-a", self.rack_a, 1),
            self._device_row(3, "SHARED-SLUG-B", "shared-slug-b", self.rack_a, 2),
        ]
        seen = set()
        for row in rows:
            _ensure_device_type(
                manufacturer.slug,
                "shared-model",
                manufacturer.name,
                "Different Shared Model",
                1,
                seen,
                ctx,
                row,
                Manufacturer,
                DeviceType,
            )

        self.assertEqual(set(ctx.slug_conflicts_by_row), {2, 3})

    def test_duplicate_rack_rows_are_rejected_before_update(self):
        profile = self._rack_profile("Duplicate Rack Row Profile")
        rows = [
            self._rack_row(2, "RACK-DUP-A", self.rack_a.name, "40"),
            self._rack_row(3, "RACK-DUP-B", self.rack_a.name, "30"),
        ]

        result = run_import(rows, profile, {"site": self.site}, dry_run=True)
        rack_rows = [item for item in result.rows if item.object_type == "rack"]

        self.assertEqual([item.action for item in rack_rows], ["error", "error"])
        self.assertTrue(all(item.extra_data.get("identity_conflict") == "duplicate_rack" for item in rack_rows))

    def test_below_rack_row_does_not_make_active_identifiers_ambiguous(self):
        below_rack = self._device_row(2, "BELOW-RACK", "eligible-device", self.rack_a, 0)
        below_rack["serial"] = "SHARED-ELIGIBILITY-SERIAL"
        active = self._device_row(3, "ACTIVE-RACK", "eligible-device", self.rack_a, 1)
        active["serial"] = "SHARED-ELIGIBILITY-SERIAL"

        result = run_import([below_rack, active], self.profile, {"site": self.site}, dry_run=True)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([(item.row_number, item.action) for item in device_rows], [(2, "skip"), (3, "create")])

    def test_asset_tag_matching_is_case_insensitive(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="asset-tag-existing",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            asset_tag="TAG-CASE-1",
        )
        row = self._device_row(2, "ASSET-CASE", "asset-tag-source-name", self.rack_a, 1)
        row["asset_tag"] = "tag-case-1"

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "update")
        self.assertEqual(device_row.extra_data.get("netbox_device_id"), existing.pk)

    def test_case_insensitive_asset_tag_ambiguity_blocks_import(self):
        from dcim.models import Device

        for name, asset_tag in (("asset-ambiguous-a", "TAG-AMBIGUOUS"), ("asset-ambiguous-b", "tag-ambiguous")):
            Device.objects.create(
                name=name,
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                asset_tag=asset_tag,
            )
        row = self._device_row(2, "ASSET-AMBIGUOUS", "asset-ambiguous-source", self.rack_a, 1)
        row["asset_tag"] = "Tag-Ambiguous"

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "ambiguous_asset_tag")

    def test_asset_tag_fallback_name_participates_in_duplicate_detection(self):
        fallback = self._device_row(2, "FALLBACK-NAME-A", "", self.rack_a, 1)
        fallback["asset_tag"] = "EFFECTIVE-NAME"
        explicit = self._device_row(3, "FALLBACK-NAME-B", "EFFECTIVE-NAME", self.rack_b, 2)
        explicit["asset_tag"] = "OTHER-ASSET"

        result = run_import([fallback, explicit], self.profile, {"site": self.site}, dry_run=True)
        device_rows = [item for item in result.rows if item.object_type == "device"]

        self.assertEqual([item.action for item in device_rows], ["error", "error"])
        self.assertTrue(all(item.extra_data.get("identity_conflict") == "duplicate_name" for item in device_rows))

    def test_ambiguous_serial_does_not_fall_through_to_name_match(self):
        from dcim.models import Device

        Device.objects.create(
            name="serial-name-match",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="AMBIGUOUS-SERIAL",
        )
        Device.objects.create(
            name="serial-other-match",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="AMBIGUOUS-SERIAL",
        )
        row = self._device_row(2, "SERIAL-SOURCE", "serial-name-match", self.rack_a, 1)
        row["serial"] = "AMBIGUOUS-SERIAL"

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "ambiguous_serial")

    def test_hidden_device_still_participates_in_strong_identity_ambiguity(self):
        from dcim.models import Device
        from tenancy.models import Tenant

        visible_tenant = Tenant.objects.create(name="Visible Identity Tenant", slug="visible-identity-tenant")
        hidden_tenant = Tenant.objects.create(name="Hidden Identity Tenant", slug="hidden-identity-tenant")
        Device.objects.create(
            name="visible-serial-device",
            site=self.site,
            tenant=visible_tenant,
            device_type=self.device_type,
            role=self.role,
            serial="SCOPED-AMBIGUOUS-SERIAL",
        )
        Device.objects.create(
            name="hidden-serial-device",
            site=self.site,
            tenant=hidden_tenant,
            device_type=self.device_type,
            role=self.role,
            serial="SCOPED-AMBIGUOUS-SERIAL",
        )
        user = get_user_model().objects.create_user(username="scoped-identity-user", password="testpass")
        self._grant_object_permission(
            user,
            "View visible identity devices",
            Device,
            ["view", "change"],
            {"tenant_id": visible_tenant.pk},
        )
        row = self._device_row(2, "SCOPED-IDENTITY", "visible-serial-device", self.rack_a, 1)
        row.update(
            {
                "rack_name": "",
                "u_position": "",
                "face": "",
                "serial": "SCOPED-AMBIGUOUS-SERIAL",
            }
        )

        result = run_import(
            [row],
            self.profile,
            {"site": self.site, "tenant": visible_tenant},
            dry_run=True,
            user=user,
        )
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "ambiguous_serial")

    def test_import_setup_limits_profiles_to_change_scope(self):
        denied_profile = ImportProfile.objects.create(name="Denied Setup Profile", sheet_name="Data")
        user = get_user_model().objects.create_user(username="scoped-setup-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change allowed setup profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )

        form = ImportSetupForm(user=user)

        self.assertQuerySetEqual(form.fields["profile"].queryset, [self.profile])
        self.assertNotIn(denied_profile, form.fields["profile"].queryset)

    def test_import_preview_rejects_profile_outside_change_scope(self):
        denied_profile = ImportProfile.objects.create(name="Denied Preview Profile", sheet_name="Data")
        user = get_user_model().objects.create_user(username="scoped-preview-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change allowed preview profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        row = self._device_row(2, "DENIED-PREVIEW", "denied-preview-device", self.rack_a, 1)
        self.client.force_login(user)
        self._set_import_session([row])
        session = self.client.session
        session["import_context"]["profile_id"] = denied_profile.pk
        session.save()

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

    def test_single_row_sync_rejects_profile_outside_change_scope(self):
        from dcim.models import Device

        denied_profile = ImportProfile.objects.create(name="Denied Sync Profile", sheet_name="Data")
        ClassRoleMapping.objects.create(
            profile=denied_profile,
            source_class="Server",
            creates_rack=False,
            role_slug=self.role.slug,
        )
        user = get_user_model().objects.create_user(username="scoped-sync-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change allowed sync profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(user, "Create sync devices", Device, ["add"])
        row = self._device_row(2, "DENIED-SYNC", "denied-sync-device", self.rack_a, 1)
        row.update({"rack_name": "", "u_position": "", "face": ""})
        self.client.force_login(user)
        self._set_import_session([row])
        session = self.client.session
        session["import_context"]["profile_id"] = denied_profile.pk
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_single_row"),
            {"row_number": 2},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Import profile not found")
        self.assertFalse(Device.objects.filter(name="denied-sync-device").exists())

    def test_device_create_enforces_constrained_add_permission(self):
        from dcim.models import Device, Site

        denied_site = Site.objects.create(name="Denied Create Site", slug="denied-create-site")
        user = get_user_model().objects.create_user(username="scoped-device-create-user", password="testpass")
        self._grant_object_permission(
            user,
            "Create devices at allowed site",
            Device,
            ["add"],
            {"site_id": self.site.pk},
        )
        row = self._device_row(2, "DENIED-CREATE", "denied-create-device", self.rack_a, 1)
        row.update({"rack_name": "", "u_position": "", "face": ""})

        result = run_import([row], self.profile, {"site": denied_site}, dry_run=False, user=user)

        device_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.detail, "Permission denied: dcim.add_device")
        self.assertFalse(Device.objects.filter(name="denied-create-device").exists())

    def test_device_update_enforces_permission_after_imported_fields(self):
        from dcim.models import Device
        from tenancy.models import Tenant

        allowed_tenant = Tenant.objects.create(name="Allowed Update Tenant", slug="allowed-update-tenant")
        denied_tenant = Tenant.objects.create(name="Denied Update Tenant", slug="denied-update-tenant")
        device = Device.objects.create(
            name="post-save-device",
            site=self.site,
            tenant=allowed_tenant,
            device_type=self.device_type,
            role=self.role,
            serial="ORIGINAL-POST-SAVE-SERIAL",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="POST-SAVE-DEVICE",
            netbox_device_id=device.pk,
            device_name=device.name,
        )
        user = get_user_model().objects.create_user(username="scoped-device-update-user", password="testpass")
        self._grant_object_permission(
            user,
            "Update devices in allowed tenant",
            Device,
            ["view", "change"],
            {"tenant_id": allowed_tenant.pk},
        )
        row = self._device_row(2, "POST-SAVE-DEVICE", device.name, self.rack_a, 1)
        row.update({"rack_name": "", "u_position": "", "face": "", "serial": "CHANGED-POST-SAVE-SERIAL"})

        result = run_import(
            [row],
            self.profile,
            {"site": self.site, "tenant": denied_tenant},
            dry_run=False,
            user=user,
        )

        device_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.detail, "Permission denied: dcim.change_device")
        device.refresh_from_db()
        self.assertEqual(device.tenant, allowed_tenant)
        self.assertEqual(device.serial, "ORIGINAL-POST-SAVE-SERIAL")

    def test_rack_create_enforces_constrained_add_permission(self):
        from dcim.models import Rack, Site

        denied_site = Site.objects.create(name="Denied Rack Site", slug="denied-rack-site")
        profile = self._rack_profile("Scoped Rack Create Profile")
        user = get_user_model().objects.create_user(username="scoped-rack-create-user", password="testpass")
        self._grant_object_permission(
            user,
            "Create racks at allowed site",
            Rack,
            ["add"],
            {"site_id": self.site.pk},
        )
        row = self._rack_row(2, "DENIED-RACK-CREATE", "DENIED-RACK-CREATE")

        result = run_import([row], profile, {"site": denied_site}, dry_run=False, user=user)

        rack_row = next(item for item in result.rows if item.object_type == "rack")
        self.assertEqual(rack_row.action, "error")
        self.assertEqual(rack_row.detail, "Permission denied: dcim.add_rack")
        self.assertFalse(Rack.objects.filter(site=denied_site, name="DENIED-RACK-CREATE").exists())

    def test_rack_update_enforces_permission_after_imported_fields(self):
        from dcim.models import Rack
        from tenancy.models import Tenant

        allowed_tenant = Tenant.objects.create(name="Allowed Rack Tenant", slug="allowed-rack-tenant")
        denied_tenant = Tenant.objects.create(name="Denied Rack Tenant", slug="denied-rack-tenant")
        rack = Rack.objects.create(site=self.site, name="POST-SAVE-RACK", tenant=allowed_tenant, u_height=42)
        profile = self._rack_profile("Scoped Rack Update Profile")
        user = get_user_model().objects.create_user(username="scoped-rack-update-user", password="testpass")
        self._grant_object_permission(
            user,
            "Update racks in allowed tenant",
            Rack,
            ["view", "change"],
            {"tenant_id": allowed_tenant.pk},
        )
        row = self._rack_row(2, "POST-SAVE-RACK", rack.name, "20")

        result = run_import(
            [row],
            profile,
            {"site": self.site, "tenant": denied_tenant},
            dry_run=False,
            user=user,
        )

        rack_row = next(item for item in result.rows if item.object_type == "rack")
        self.assertEqual(rack_row.action, "error")
        self.assertEqual(rack_row.detail, "Permission denied: dcim.change_rack")
        rack.refresh_from_db()
        self.assertEqual(rack.tenant, allowed_tenant)
        self.assertEqual(rack.u_height, 42)

    def test_engine_cannot_update_device_outside_object_permission_scope(self):
        from dcim.models import Device
        from tenancy.models import Tenant

        allowed_tenant = Tenant.objects.create(name="Allowed Identity Tenant", slug="allowed-identity-tenant")
        denied_tenant = Tenant.objects.create(name="Denied Identity Tenant", slug="denied-identity-tenant")
        denied = Device.objects.create(
            name="denied-engine-device",
            site=self.site,
            tenant=denied_tenant,
            device_type=self.device_type,
            role=self.role,
            serial="DENIED-ENGINE-SERIAL",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="DENIED-ENGINE",
            netbox_device_id=denied.pk,
            device_name=denied.name,
        )
        user = get_user_model().objects.create_user(username="scoped-engine-user", password="testpass")
        self._grant_object_permission(
            user,
            "Scoped device access for engine",
            Device,
            ["view", "change"],
            {"tenant_id": allowed_tenant.pk},
        )
        row = self._device_row(2, "DENIED-ENGINE", "source-denied-device", self.rack_a, 1)
        row["serial"] = "SOURCE-WOULD-OVERWRITE"

        result = run_import(
            [row],
            self.profile,
            {"site": self.site, "tenant": allowed_tenant},
            dry_run=False,
            user=user,
        )

        device_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(device_row.action, "error")
        denied.refresh_from_db()
        self.assertEqual(denied.serial, "DENIED-ENGINE-SERIAL")
        self.assertEqual(denied.tenant, denied_tenant)

    def test_engine_cannot_update_rack_outside_object_permission_scope(self):
        from dcim.models import Rack

        profile = self._rack_profile("Scoped Rack Engine Profile")
        user = get_user_model().objects.create_user(username="scoped-rack-user", password="testpass")
        self._grant_object_permission(
            user,
            "Scoped rack access for engine",
            Rack,
            ["view", "change"],
            {"pk": self.rack_b.pk},
        )
        row = self._rack_row(2, "DENIED-RACK-ENGINE", self.rack_a.name, "20")

        result = run_import([row], profile, {"site": self.site}, dry_run=False, user=user)

        rack_row = next(item for item in result.rows if item.object_type == "rack")
        self.assertEqual(rack_row.action, "error")
        self.rack_a.refresh_from_db()
        self.assertEqual(self.rack_a.u_height, 42)

    def test_manual_link_cannot_select_device_outside_object_permission_scope(self):
        from dcim.models import Device
        from tenancy.models import Tenant

        allowed_tenant = Tenant.objects.create(name="Allowed Manual Tenant", slug="allowed-manual-tenant")
        denied_tenant = Tenant.objects.create(name="Denied Manual Tenant", slug="denied-manual-tenant")
        denied = Device.objects.create(
            name="denied-manual-device",
            site=self.site,
            tenant=denied_tenant,
            device_type=self.device_type,
            role=self.role,
        )
        user = get_user_model().objects.create_user(username="scoped-manual-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change active manual profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(user, "Add manual device matches", DeviceExistingMatch, ["add"])
        self._grant_object_permission(
            user,
            "View allowed manual devices",
            Device,
            ["view"],
            {"tenant_id": allowed_tenant.pk},
        )
        row = self._device_row(2, "DENIED-MANUAL", "manual-source-device", self.rack_a, 1)
        self.client.force_login(user)
        self._set_import_session([row], tenant=allowed_tenant)

        response = self.client.post(
            reverse("plugins:netbox_data_import:match_existing_device"),
            {
                "profile_id": self.profile.pk,
                "source_id": "DENIED-MANUAL",
                "netbox_device_id": denied.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="DENIED-MANUAL").exists())

    def test_manual_relink_requires_change_permission_for_existing_binding(self):
        from dcim.models import Device

        original = Device.objects.create(
            name="original-manual-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        replacement = Device.objects.create(
            name="replacement-manual-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        binding = DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="RELINK-MANUAL",
            netbox_device_id=original.pk,
            device_name=original.name,
        )
        user = get_user_model().objects.create_user(username="manual-relink-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change manual relink profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(user, "Add manual relink bindings", DeviceExistingMatch, ["add"])
        self._grant_object_permission(user, "View manual relink devices", Device, ["view"])
        row = self._device_row(2, "RELINK-MANUAL", "manual-relink-source", self.rack_a, 1)
        self.client.force_login(user)
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:match_existing_device"),
            {
                "profile_id": self.profile.pk,
                "source_id": "RELINK-MANUAL",
                "netbox_device_id": replacement.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        binding.refresh_from_db()
        self.assertEqual(binding.netbox_device_id, original.pk)

    def test_unlink_enforces_binding_delete_permission_scope(self):
        from dcim.models import Device

        device = Device.objects.create(
            name="unlink-scope-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        denied_binding = DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="UNLINK-DENIED",
            netbox_device_id=device.pk,
            device_name=device.name,
        )
        allowed_profile = ImportProfile.objects.create(name="Allowed Unlink Profile", sheet_name="Data")
        allowed_binding = DeviceExistingMatch.objects.create(
            profile=allowed_profile,
            source_id="UNLINK-ALLOWED",
            netbox_device_id=device.pk,
            device_name=device.name,
        )
        user = get_user_model().objects.create_user(username="unlink-scope-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change unlink profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(
            user,
            "Delete another binding",
            DeviceExistingMatch,
            ["delete"],
            {"pk": allowed_binding.pk},
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("plugins:netbox_data_import:unlink_device"),
            {"profile_id": self.profile.pk, "source_id": "UNLINK-DENIED"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(DeviceExistingMatch.objects.filter(pk=denied_binding.pk).exists())

    def test_duplicate_name_resolution_update_requires_change_permission(self):
        existing = SourceResolution.objects.create(
            profile=self.profile,
            source_id="RESOLUTION-AUTH",
            source_column="device_name",
            original_value="duplicate-name",
            resolved_fields={"device_name": "existing-resolution-name"},
        )
        user = get_user_model().objects.create_user(username="resolution-update-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change resolution profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(user, "Add name resolutions", SourceResolution, ["add"])
        row = self._device_row(2, "RESOLUTION-AUTH", "duplicate-name", self.rack_a, 1)
        self.client.force_login(user)
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "row_number": 2,
                "source_id": "RESOLUTION-AUTH",
                "new_name": "replacement-resolution-name",
            },
        )

        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        self.assertEqual(existing.resolved_fields, {"device_name": "existing-resolution-name"})

    def test_field_resolution_update_requires_change_permission(self):
        existing = SourceResolution.objects.create(
            profile=self.profile,
            source_id="FIELD-RESOLUTION-AUTH",
            source_column="Name",
            original_value="original-field-value",
            resolved_fields={"device_name": "existing-field-value"},
        )
        user = get_user_model().objects.create_user(username="field-resolution-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change field resolution profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(user, "Add field resolutions", SourceResolution, ["add"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "FIELD-RESOLUTION-AUTH",
                "source_column": "Name",
                "original_value": "replacement-original",
                "resolved_fields": '{"device_name": "replacement-field-value"}',
            },
        )

        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        self.assertEqual(existing.resolved_fields, {"device_name": "existing-field-value"})

    def test_auto_match_cannot_scan_device_outside_object_permission_scope(self):
        from dcim.models import Device
        from tenancy.models import Tenant

        allowed_tenant = Tenant.objects.create(name="Allowed Auto Tenant", slug="allowed-auto-tenant")
        denied_tenant = Tenant.objects.create(name="Denied Auto Tenant", slug="denied-auto-tenant")
        denied = Device.objects.create(
            name="denied-auto-device",
            site=self.site,
            tenant=denied_tenant,
            device_type=self.device_type,
            role=self.role,
            serial="DENIED-AUTO-SERIAL",
        )
        user = get_user_model().objects.create_user(username="scoped-auto-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change active auto profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(user, "Add auto device matches", DeviceExistingMatch, ["add"])
        self._grant_object_permission(
            user,
            "View allowed auto devices",
            Device,
            ["view"],
            {"tenant_id": allowed_tenant.pk},
        )
        row = self._device_row(2, "DENIED-AUTO", "auto-source-device", self.rack_a, 1)
        row["serial"] = denied.serial
        self.client.force_login(user)
        self._set_import_session([row], tenant=allowed_tenant)

        response = self.client.post(
            reverse("plugins:netbox_data_import:auto_match_devices"),
            {"profile_id": self.profile.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="DENIED-AUTO").exists())

    def test_auto_match_counts_hidden_devices_in_strong_identity_ambiguity(self):
        from dcim.models import Device
        from tenancy.models import Tenant

        visible_tenant = Tenant.objects.create(name="Visible Auto Tenant", slug="visible-auto-tenant")
        hidden_tenant = Tenant.objects.create(name="Hidden Auto Tenant", slug="hidden-auto-tenant")
        Device.objects.create(
            name="visible-auto-ambiguous",
            site=self.site,
            tenant=visible_tenant,
            device_type=self.device_type,
            role=self.role,
            serial="AUTO-SCOPED-AMBIGUOUS",
        )
        Device.objects.create(
            name="hidden-auto-ambiguous",
            site=self.site,
            tenant=hidden_tenant,
            device_type=self.device_type,
            role=self.role,
            serial="AUTO-SCOPED-AMBIGUOUS",
        )
        user = get_user_model().objects.create_user(username="scoped-auto-ambiguity-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change auto ambiguity profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(user, "Add auto ambiguity matches", DeviceExistingMatch, ["add"])
        self._grant_object_permission(
            user,
            "View visible auto ambiguity devices",
            Device,
            ["view"],
            {"tenant_id": visible_tenant.pk},
        )
        row = self._device_row(2, "AUTO-SCOPED-SOURCE", "visible-auto-ambiguous", self.rack_a, 1)
        row["serial"] = "AUTO-SCOPED-AMBIGUOUS"
        self.client.force_login(user)
        self._set_import_session([row], tenant=visible_tenant)

        response = self.client.post(
            reverse("plugins:netbox_data_import:auto_match_devices"),
            {"profile_id": self.profile.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AUTO-SCOPED-SOURCE").exists()
        )

    def test_auto_match_enforces_constrained_binding_add_permission(self):
        from dcim.models import Device

        allowed_profile = ImportProfile.objects.create(name="Allowed Binding Profile", sheet_name="Data")
        existing = Device.objects.create(
            name="constrained-binding-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="CONSTRAINED-BINDING-SERIAL",
        )
        user = get_user_model().objects.create_user(username="constrained-binding-user", password="testpass")
        self._grant_object_permission(
            user,
            "Change active binding profile",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self._grant_object_permission(
            user,
            "Add bindings to another profile",
            DeviceExistingMatch,
            ["add"],
            {"profile_id": allowed_profile.pk},
        )
        self._grant_object_permission(user, "View constrained binding devices", Device, ["view"])
        row = self._device_row(2, "CONSTRAINED-BINDING", existing.name, self.rack_a, 1)
        row.update(
            {
                "rack_name": "",
                "u_position": "",
                "face": "",
                "serial": existing.serial,
            }
        )
        self.client.force_login(user)
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:auto_match_devices"),
            {"profile_id": self.profile.pk},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 skipped (permission denied or concurrent change)")
        self.assertNotContains(response, "1 ambiguous (multiple devices)")
        self.assertFalse(
            DeviceExistingMatch.objects.filter(profile=self.profile, source_id="CONSTRAINED-BINDING").exists()
        )

    def test_auto_match_does_not_bind_unique_strong_identity_from_another_site(self):
        from dcim.models import Device, Site

        other_site = Site.objects.create(name="Other Auto Site", slug="other-auto-site")
        Device.objects.create(
            name="other-site-auto-device",
            site=other_site,
            device_type=self.device_type,
            role=self.role,
            serial="AUTO-CROSS-SITE-SERIAL",
        )
        row = self._device_row(2, "AUTO-CROSS-SITE", "source-auto-device", self.rack_a, 1)
        row["serial"] = "AUTO-CROSS-SITE-SERIAL"
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:auto_match_devices"),
            {"profile_id": self.profile.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AUTO-CROSS-SITE").exists())

    def test_non_overlapping_rows_cannot_claim_the_same_matched_device(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="blank-claim-target",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
            serial="BLANK-CLAIM-SERIAL",
        )
        first = self._device_row(2, "", existing.name, self.rack_a, 1)
        second = self._device_row(3, "SECOND-CLAIM", "other-source-name", self.rack_a, 2)
        second["serial"] = existing.serial

        result = run_import([first, second], self.profile, {"site": self.site}, dry_run=True)
        second_result = next(row for row in result.rows if row.row_number == 3 and row.object_type == "device")

        self.assertEqual(second_result.action, "error")
        self.assertEqual(second_result.extra_data.get("identity_conflict"), "device_already_bound")
        self.assertIn("row 2", second_result.detail)
        self.assertNotIn("source ID ''", second_result.detail)

    def test_case_insensitive_rack_names_share_preview_position_claims(self):
        first = self._device_row(2, "CASE-RACK-A", "case-rack-device-a", self.rack_a, 1)
        second = self._device_row(3, "CASE-RACK-B", "case-rack-device-b", self.rack_a, 1)
        second["rack_name"] = self.rack_a.name.lower()

        result = run_import([first, second], self.profile, {"site": self.site}, dry_run=True)
        device_rows = [row for row in result.rows if row.object_type == "device"]

        self.assertEqual(device_rows[0].action, "create")
        self.assertEqual(device_rows[1].action, "error")
        self.assertEqual(device_rows[1].extra_data.get("identity_conflict"), "rack_position_occupied")

    def test_duplicate_name_resolution_preserves_the_preview_view(self):
        rows = [
            self._device_row(2, "NEXT-A", "next-shared", self.rack_a, 1),
            self._device_row(3, "NEXT-B", "next-shared", self.rack_b, 2),
        ]
        preview = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        suggestion = next(row for row in preview.rows if row.row_number == 3 and row.object_type == "device")
        self._set_import_session(rows, preview)
        next_url = f"{reverse('plugins:netbox_data_import:import_preview')}?view=racks"

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": "NEXT-B",
                "row_number": 3,
                "new_name": suggestion.extra_data["suggested_name"],
                "next": next_url,
            },
        )

        self.assertRedirects(response, next_url, fetch_redirect_response=False)

    def test_duplicate_name_resolution_checks_asset_tag_fallback_names(self):
        target = self._device_row(2, "RESOLVE-TARGET", "resolve-target", self.rack_a, 1)
        fallback = self._device_row(3, "RESOLVE-FALLBACK", "", self.rack_b, 2)
        fallback["asset_tag"] = "RESERVED-FALLBACK-NAME"
        self._set_import_session([target, fallback])

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": "RESOLVE-TARGET",
                "row_number": 2,
                "new_name": fallback["asset_tag"],
            },
            follow=True,
        )

        self.assertContains(response, "already used by another source row")
        self.assertFalse(
            SourceResolution.objects.filter(
                profile=self.profile,
                source_id="RESOLVE-TARGET",
                source_column="device_name",
            ).exists()
        )

    def test_duplicate_name_resolution_normalizes_null_original_value(self):
        row = self._device_row(2, "RESOLVE-NULL", None, self.rack_a, 1)
        row["asset_tag"] = "RESOLVE-NULL-ASSET"
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": row["source_id"],
                "row_number": row["_row_number"],
                "new_name": "resolve-null-name",
            },
        )

        self.assertEqual(response.status_code, 302)
        resolution = SourceResolution.objects.get(
            profile=self.profile,
            source_id=row["source_id"],
            source_column="device_name",
        )
        self.assertEqual(resolution.original_value, "")

    def test_auto_match_skips_rows_without_a_class_mapping(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="unmapped-auto-target",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="UNMAPPED-AUTO-SERIAL",
        )
        row = self._device_row(2, "UNMAPPED-AUTO", "unmapped-source", self.rack_a, 1)
        row.update({"device_class": "No Mapping", "serial": existing.serial})
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:auto_match_devices"),
            {"profile_id": self.profile.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="UNMAPPED-AUTO").exists())

    def test_device_lookup_preloads_fields_used_by_preview_diff(self):
        from dcim.models import Device
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Query Tenant", slug="query-tenant")
        existing = Device.objects.create(
            name="query-device",
            site=self.site,
            tenant=tenant,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            serial="QUERY-SERIAL",
        )

        with CaptureQueriesContext(connection) as queries:
            matched, method = _find_existing_device(
                self.profile,
                "",
                self.site,
                "source-query-name",
                existing.serial,
                "",
                Device,
                tenant=tenant,
            )
            self.assertEqual(method, "serial")
            self.assertEqual(matched.device_type.manufacturer.pk, self.manufacturer.pk)
            self.assertEqual(matched.role.pk, self.role.pk)
            self.assertEqual(matched.tenant.pk, tenant.pk)
            self.assertEqual(matched.rack.pk, self.rack_a.pk)
            self.assertIsNone(matched.location)

        self.assertEqual(len(queries), 1)

    def test_unchanged_name_placement_returns_before_formatting_labels(self):
        from dcim.models import Device
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        Device.objects.create(
            name="unchanged-placement",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack_a,
            position=1,
            face="front",
        )
        matched = Device.objects.get(name="unchanged-placement")
        row = self._device_row(2, "UNCHANGED-PLACEMENT", matched.name, self.rack_a, 1)
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=True,
            result=ImportResult(),
        )

        with CaptureQueriesContext(connection) as queries:
            conflict = _name_placement_conflict_row(
                row,
                ctx,
                matched,
                row["source_id"],
                row["device_name"],
                row["rack_name"],
                1,
                "front",
                "",
                "",
            )

        self.assertIsNone(conflict)
        self.assertEqual(len(queries), 1)

    def test_blank_import_context_clears_existing_location_and_tenant(self):
        from dcim.models import Device, Location
        from tenancy.models import Tenant

        location = Location.objects.create(name="Previous Location", slug="previous-location", site=self.site)
        tenant = Tenant.objects.create(name="Previous Tenant", slug="previous-tenant")
        existing = Device.objects.create(
            name="clear-import-scope",
            site=self.site,
            location=location,
            tenant=tenant,
            device_type=self.device_type,
            role=self.role,
            serial="CLEAR-SCOPE-SERIAL",
        )
        row = self._device_row(2, "CLEAR-SCOPE", existing.name, self.rack_a, 1)
        row["serial"] = existing.serial

        preview = run_import([row], self.profile, {"site": self.site, "location": None, "tenant": None}, dry_run=True)
        device_preview = next(item for item in preview.rows if item.object_type == "device")
        self.assertEqual(device_preview.extra_data["field_diff"]["location"]["file"], "")
        self.assertEqual(device_preview.extra_data["field_diff"]["tenant"]["file"], "")

        run_import([row], self.profile, {"site": self.site, "location": None, "tenant": None}, dry_run=False)

        existing.refresh_from_db()
        self.assertIsNone(existing.location)
        self.assertIsNone(existing.tenant)

    def test_slug_preflight_does_not_issue_per_row_manufacturer_exists_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        rows = [
            self._device_row(2, "SLUG-QUERY-A", "slug-query-a", self.rack_a, 1),
            self._device_row(3, "SLUG-QUERY-B", "slug-query-b", self.rack_a, 2),
        ]
        class_role_map = {"Server": ClassRoleMapping.objects.get(profile=self.profile, source_class="Server")}

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(_derived_slug_conflicts(rows, self.profile, class_role_map), {})

        repeated_exists_queries = [
            query["sql"]
            for query in queries
            if "netbox_data_import_manufacturermapping" in query["sql"] and 'SELECT 1 AS "a"' in query["sql"]
        ]
        self.assertEqual(repeated_exists_queries, [])

    def test_binding_database_conflict_keeps_its_identity_classification(self):
        row = self._device_row(2, "BINDING-RACE", "binding-race-device", self.rack_a, 1)

        result = _rack_position_error_row(
            row,
            row["source_id"],
            row["device_name"],
            row["make"],
            row["model"],
            row["asset_tag"],
            row["rack_name"],
            1,
            _DeviceBindingConflict("The source binding changed during import"),
            "update",
        )

        self.assertEqual(result.action, "error")
        self.assertEqual(result.extra_data.get("identity_conflict"), "device_already_bound")
        self.assertIn("source binding changed", result.detail)

    def test_execute_rejects_each_ambiguous_strong_identity(self):
        from dcim.models import Device, Site
        from extras.models import CustomField

        for suffix in ("a", "b"):
            Device.objects.create(
                name=f"execute-serial-{suffix}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                serial="EXECUTE-AMBIGUOUS-SERIAL",
            )
            Device.objects.create(
                name=f"execute-asset-{suffix}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                asset_tag="Execute-Ambiguous-Tag" if suffix == "a" else "execute-ambiguous-tag",
            )

        content_type = ContentType.objects.get_for_model(Device)
        custom_field, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            custom_field.object_types.set([content_type])
        elif not custom_field.object_types.filter(pk=content_type.pk).exists():
            custom_field.object_types.add(content_type)
        metadata = {
            "data_import_source": {
                "profile_id": self.profile.pk,
                "profile_name": self.profile.name,
                "source_id": "EXECUTE-AMBIGUOUS-METADATA",
            }
        }
        for suffix in ("a", "b"):
            Device.objects.create(
                name=f"execute-metadata-{suffix}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                custom_field_data=metadata,
            )

        other_site = Site.objects.create(name="Execute Other Site", slug="execute-other-site")
        Device.objects.create(
            name="execute-cross-site-target",
            site=other_site,
            device_type=self.device_type,
            role=self.role,
            serial="EXECUTE-CROSS-SITE",
        )
        rows = [
            self._device_row(2, "EXECUTE-SERIAL", "execute-serial-source", self.rack_a, 1),
            self._device_row(3, "EXECUTE-ASSET", "execute-asset-source", self.rack_a, 2),
            self._device_row(4, "EXECUTE-AMBIGUOUS-METADATA", "execute-metadata-source", self.rack_a, 3),
            self._device_row(5, "EXECUTE-CROSS", "execute-cross-source", self.rack_a, 4),
        ]
        rows[0]["serial"] = "EXECUTE-AMBIGUOUS-SERIAL"
        rows[1]["asset_tag"] = "EXECUTE-AMBIGUOUS-TAG"
        rows[3]["serial"] = "EXECUTE-CROSS-SITE"

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        conflicts = {
            row.source_id: row.extra_data.get("identity_conflict") for row in result.rows if row.object_type == "device"
        }

        self.assertEqual(conflicts["EXECUTE-SERIAL"], "ambiguous_serial")
        self.assertEqual(conflicts["EXECUTE-ASSET"], "ambiguous_asset_tag")
        self.assertEqual(conflicts["EXECUTE-AMBIGUOUS-METADATA"], "ambiguous_source_id")
        self.assertEqual(conflicts["EXECUTE-CROSS"], "cross_site_match")

    def test_duplicate_names_fail_closed_on_strong_identity_conflicts(self):
        from dcim.models import Device
        from extras.models import CustomField

        for suffix in ("a", "b"):
            Device.objects.create(
                name=f"duplicate-serial-target-{suffix}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                serial="DUPLICATE-NAME-SERIAL",
            )
            Device.objects.create(
                name=f"duplicate-asset-target-{suffix}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                asset_tag="Duplicate-Name-Tag" if suffix == "a" else "duplicate-name-tag",
            )

        content_type = ContentType.objects.get_for_model(Device)
        custom_field, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            custom_field.object_types.set([content_type])
        elif not custom_field.object_types.filter(pk=content_type.pk).exists():
            custom_field.object_types.add(content_type)
        metadata = {
            "data_import_source": {
                "profile_id": self.profile.pk,
                "profile_name": self.profile.name,
                "source_id": "DUPLICATE-NAME-METADATA",
            }
        }
        for suffix in ("a", "b"):
            Device.objects.create(
                name=f"duplicate-metadata-target-{suffix}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
                custom_field_data=metadata,
            )
        bound_device = Device.objects.create(
            name="duplicate-bound-target",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="DUPLICATE-NAME-BOUND",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="BOUND-ELSEWHERE",
            netbox_device_id=bound_device.pk,
            device_name=bound_device.name,
        )

        rows = []
        cases = (
            ("DUP-SERIAL", "duplicate-serial-name", "serial", "DUPLICATE-NAME-SERIAL"),
            ("DUP-ASSET", "duplicate-asset-name", "asset_tag", "DUPLICATE-NAME-TAG"),
            ("DUPLICATE-NAME-METADATA", "duplicate-metadata-name", None, None),
            ("DUP-BOUND", "duplicate-bound-name", "serial", "DUPLICATE-NAME-BOUND"),
        )
        for index, (source_id, name, field, value) in enumerate(cases, start=0):
            strong_row = self._device_row(2 + index * 2, source_id, name, self.rack_a, 1 + index * 2)
            if field:
                strong_row[field] = value
            rows.extend(
                [
                    strong_row,
                    self._device_row(3 + index * 2, f"{source_id}-OTHER", name, self.rack_a, 2 + index * 2),
                ]
            )

        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        conflicts = {
            row.source_id: row.extra_data.get("identity_conflict") for row in result.rows if row.object_type == "device"
        }

        self.assertEqual(conflicts["DUP-SERIAL"], "ambiguous_serial")
        self.assertEqual(conflicts["DUP-ASSET"], "ambiguous_asset_tag")
        self.assertEqual(conflicts["DUPLICATE-NAME-METADATA"], "ambiguous_source_id")
        self.assertEqual(conflicts["DUP-BOUND"], "device_already_bound")

    def test_binding_helper_rejects_a_device_bound_to_another_source(self):
        from dcim.models import Device

        device = Device.objects.create(
            name="helper-bound-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="HELPER-BOUND-A",
            netbox_device_id=device.pk,
            device_name=device.name,
        )

        with self.assertRaisesMessage(_DeviceBindingConflict, "HELPER-BOUND-A"):
            _bind_device_source(self.profile, "HELPER-BOUND-B", device)

    def test_unique_name_suggestion_uses_a_sanitized_source_and_counter(self):
        row = self._device_row(2, "Source / Two", "reserved-name", self.rack_a, 1)
        base = f"reserved-name-{self.rack_a.name}-U1"
        candidate = f"{base}-Source-Two"
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=True,
            result=ImportResult(),
            reserved_device_names={base.lower(), candidate.lower(), f"{candidate.lower()}-2"},
        )

        suggestion = _suggest_unique_device_name(row, ctx)

        self.assertEqual(suggestion, f"{candidate}-3")
        self.assertIn(suggestion.lower(), ctx.reserved_device_names)

    def test_execute_create_refuses_a_stale_preview_intent(self):
        row = self._device_row(2, "STALE-CREATE", "stale-create-device", self.rack_a, 1)
        expected_intents = {(2, "device"): {"action": "update", "object_id": 999999}}

        result = run_import(
            [row],
            self.profile,
            {"site": self.site},
            dry_run=False,
            expected_intents=expected_intents,
        )
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertTrue(device_row.extra_data.get("identity_state_changed"))

    def test_execute_update_rejects_a_device_bound_to_another_source(self):
        from dcim.models import Device

        existing = Device.objects.create(
            name="execute-bound-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="EXECUTE-BOUND-SERIAL",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="EXECUTE-BOUND-A",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        row = self._device_row(2, "EXECUTE-BOUND-B", "execute-bound-source", self.rack_a, 1)
        row["serial"] = existing.serial

        result = run_import([row], self.profile, {"site": self.site}, dry_run=False)
        device_row = next(item for item in result.rows if item.object_type == "device")

        self.assertEqual(device_row.action, "error")
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "device_already_bound")

    def test_zero_u_preview_and_update_clear_position_and_face(self):
        from dcim.models import Device, DeviceType

        zero_u_type = DeviceType.objects.create(
            manufacturer=self.manufacturer,
            model="Identity Zero U",
            slug="identity-vendor-identity-zero-u",
            u_height=0,
        )
        existing = Device.objects.create(
            name="zero-u-existing",
            site=self.site,
            device_type=zero_u_type,
            role=self.role,
            serial="ZERO-U-UPDATE",
            rack=self.rack_a,
        )
        create_row = self._device_row(2, "ZERO-U-CREATE", "zero-u-create", self.rack_a, 5)
        create_row["model"] = zero_u_type.model
        update_row = self._device_row(3, "ZERO-U-UPDATE", existing.name, self.rack_a, 6)
        update_row.update({"model": zero_u_type.model, "serial": existing.serial})

        preview = run_import([create_row], self.profile, {"site": self.site}, dry_run=True)
        preview_row = next(item for item in preview.rows if item.object_type == "device")
        self.assertIn("zero-U", preview_row.detail)
        self.assertIsNone(preview_row.extra_data["u_position"])
        self.assertEqual(preview_row.extra_data["face"], "")

        result = run_import([update_row], self.profile, {"site": self.site}, dry_run=False)
        update_result = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(update_result.action, "update")
        existing.refresh_from_db()
        self.assertIsNone(existing.position)
        self.assertIsNone(existing.face)

    def test_rack_rows_with_one_source_id_are_rejected_before_writes(self):
        profile = self._rack_profile("Duplicate Rack Source Profile")
        rows = [
            self._rack_row(2, "DUPLICATE-RACK-SOURCE", "SOURCE-RACK-A"),
            self._rack_row(3, "DUPLICATE-RACK-SOURCE", "SOURCE-RACK-B"),
        ]

        result = run_import(rows, profile, {"site": self.site}, dry_run=True)
        rack_rows = [item for item in result.rows if item.object_type == "rack"]

        self.assertEqual(len(rack_rows), 2)
        self.assertTrue(all(item.extra_data.get("identity_conflict") == "duplicate_source_id" for item in rack_rows))

    def test_rack_create_refuses_a_stale_preview_intent(self):
        profile = self._rack_profile("Stale Rack Create Profile")
        row = self._rack_row(2, "STALE-RACK", "STALE-RACK")

        result = run_import(
            [row],
            profile,
            {"site": self.site},
            dry_run=False,
            expected_intents={(2, "rack"): {"action": "update", "object_id": 999999}},
        )
        rack_row = next(item for item in result.rows if item.object_type == "rack")

        self.assertEqual(rack_row.action, "error")
        self.assertTrue(rack_row.extra_data.get("identity_state_changed"))

    def test_rack_update_retains_the_selected_location_scope(self):
        from dcim.models import Location, Rack

        profile = self._rack_profile("Rack Location Update Profile")
        location = Location.objects.create(name="Rack Target Location", slug="rack-target-location", site=self.site)
        rack = Rack.objects.create(site=self.site, location=location, name="RACK-LOCATION-UPDATE", u_height=42)
        row = self._rack_row(2, "RACK-LOCATION-UPDATE", rack.name)

        result = run_import([row], profile, {"site": self.site, "location": location}, dry_run=False)
        rack_result = next(item for item in result.rows if item.object_type == "rack")

        self.assertEqual(rack_result.action, "update")
        rack.refresh_from_db()
        self.assertEqual(rack.location, location)

    def test_generic_device_write_error_is_not_reported_as_a_position_conflict(self):
        from django.core.exceptions import ValidationError

        row = self._device_row(2, "GENERIC-VALIDATION", "generic-validation", self.rack_a, 1)
        validation_result = _rack_position_error_row(
            row,
            row["source_id"],
            row["device_name"],
            row["make"],
            row["model"],
            row["asset_tag"],
            row["rack_name"],
            1,
            ValidationError(["A device field is invalid"]),
            "create",
        )

        self.assertEqual(validation_result.extra_data.get("identity_conflict"), "device_validation_failed")
        self.assertIn("A device field is invalid", validation_result.detail)

    def test_existing_ip_owned_by_another_device_is_not_reassigned(self):
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        target = Device.objects.create(
            name="ip-target-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        target_interface = Interface.objects.create(device=target, name="eth0", type="1000base-t")
        IPAddress.objects.create(address="198.18.0.1/24", assigned_object=target_interface)
        owner = Device.objects.create(
            name="ip-owner-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        owner_interface = Interface.objects.create(device=owner, name="eth0", type="1000base-t")
        IPAddress.objects.create(address="198.18.0.2/24", assigned_object=owner_interface)

        assigned = _assign_ip_to_device(target, "primary_ip4", "198.18.0.2/24")

        self.assertFalse(assigned)
        target.refresh_from_db()
        self.assertIsNone(target.primary_ip4)

    def test_duplicate_name_resolution_rejects_invalid_request_boundaries(self):
        from dcim.models import Device

        row = self._device_row(2, "RESOLUTION-BOUNDARY", "resolution-boundary", self.rack_a, 1)
        self._set_import_session([row])
        url = reverse("plugins:netbox_data_import:resolve_duplicate_name")
        base_data = {
            "profile_id": self.profile.pk,
            "source_id": row["source_id"],
            "row_number": row["_row_number"],
            "new_name": "resolved-boundary-name",
        }

        invalid_requests = (
            {**base_data, "profile_id": 999999},
            {**base_data, "row_number": "not-a-row"},
            {**base_data, "source_id": ""},
            {**base_data, "new_name": ""},
        )
        for data in invalid_requests:
            with self.subTest(data=data):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302)

        existing = Device.objects.create(
            name="existing-resolution-name",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        response = self.client.post(url, {**base_data, "new_name": existing.name})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            SourceResolution.objects.filter(
                profile=self.profile,
                source_id=row["source_id"],
                source_column="device_name",
            ).exists()
        )

    def test_duplicate_name_resolution_requires_resolution_add_permission(self):
        limited_user = get_user_model().objects.create_user(
            username="resolution-boundary-user",
            email="resolution-boundary@example.invalid",
            password="testpass",
        )
        self._grant_object_permission(
            limited_user,
            "Change profile without adding resolutions",
            ImportProfile,
            ["change"],
            {"pk": self.profile.pk},
        )
        self.client.force_login(limited_user)
        row = self._device_row(2, "RESOLUTION-PERMISSION", "resolution-permission", self.rack_a, 1)
        self._set_import_session([row])

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": row["source_id"],
                "row_number": row["_row_number"],
                "new_name": "resolution-permission-unique",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(profile=self.profile, source_id=row["source_id"]).exists())

    def test_permission_scoped_create_only_helper_does_not_replace_an_existing_binding(self):
        from dcim.models import Device

        first_device = Device.objects.create(
            name="create-only-first",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        second_device = Device.objects.create(
            name="create-only-second",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        binding = DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="CREATE-ONLY",
            netbox_device_id=first_device.pk,
            device_name=first_device.name,
        )

        allowed = _save_permission_scoped_object(
            self.user,
            DeviceExistingMatch,
            {"profile": self.profile, "source_id": binding.source_id},
            {"netbox_device_id": second_device.pk, "device_name": second_device.name},
            allow_update=False,
        )

        self.assertFalse(allowed)
        binding.refresh_from_db()
        self.assertEqual(binding.netbox_device_id, first_device.pk)

    def test_auto_match_reports_invalid_and_conflicting_eligible_rows(self):
        from dcim.models import Device, Site
        from tenancy.models import Tenant

        url = reverse("plugins:netbox_data_import:auto_match_devices")

        session = self.client.session
        session["import_rows"] = []
        session["import_context"] = {"profile_id": self.profile.pk, "site_id": 999999}
        session.save()
        self.assertEqual(self.client.post(url, {"profile_id": self.profile.pk}).status_code, 302)

        duplicate_rows = [
            self._device_row(2, "AUTO-DUPLICATE", "auto-duplicate-a", self.rack_a, 1),
            self._device_row(3, "AUTO-DUPLICATE", "auto-duplicate-b", self.rack_a, 2),
        ]
        self._set_import_session(duplicate_rows)
        self.assertEqual(self.client.post(url, {"profile_id": self.profile.pk}).status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AUTO-DUPLICATE").exists())

        bound = Device.objects.create(
            name="auto-bound-target",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="AUTO-BOUND-TARGET",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="AUTO-BOUND-A",
            netbox_device_id=bound.pk,
            device_name=bound.name,
        )
        bound_row = self._device_row(2, "AUTO-BOUND-B", "auto-bound-source", self.rack_a, 1)
        bound_row["serial"] = bound.serial
        self._set_import_session([bound_row])
        self.assertEqual(self.client.post(url, {"profile_id": self.profile.pk}).status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AUTO-BOUND-B").exists())

        invalid_position = Device.objects.create(
            name="auto-invalid-position",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        invalid_position_row = self._device_row(2, "AUTO-INVALID-POSITION", invalid_position.name, self.rack_a, 1)
        invalid_position_row.update({"rack_name": "", "u_position": "not-a-position", "face": ""})
        self._set_import_session([invalid_position_row])
        self.assertEqual(self.client.post(url, {"profile_id": self.profile.pk}).status_code, 302)
        self.assertTrue(
            DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AUTO-INVALID-POSITION").exists()
        )

        tenant = Tenant.objects.create(name="Auto Probable Tenant", slug="auto-probable-tenant")
        Device.objects.create(
            name="auto-probable-device",
            site=self.site,
            tenant=tenant,
            device_type=self.device_type,
            role=self.role,
        )
        probable_row = self._device_row(2, "AUTO-PROBABLE", "prefix - probable-device", self.rack_a, 1)
        self._set_import_session([probable_row], tenant=tenant)
        self.assertEqual(self.client.post(url, {"profile_id": self.profile.pk}).status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile, source_id="AUTO-PROBABLE").exists())

        self.assertFalse(Site.objects.filter(pk=999999).exists())

    def test_quick_mapping_views_validate_inputs_and_refresh_session_rows(self):
        class_url = reverse("plugins:netbox_data_import:quick_add_class_mapping")
        invalid_action = self.client.post(
            class_url,
            {
                "profile_id": self.profile.pk,
                "source_class": "Boundary Class",
                "mapping_action": "invalid",
            },
        )
        missing_role = self.client.post(
            class_url,
            {
                "profile_id": self.profile.pk,
                "source_class": "Boundary Class",
                "mapping_action": "role",
                "role_slug": "",
            },
        )
        self.assertEqual(invalid_action.status_code, 302)
        self.assertEqual(missing_role.status_code, 302)
        self.assertFalse(ClassRoleMapping.objects.filter(profile=self.profile, source_class="Boundary Class").exists())

        session = self.client.session
        session["import_rows"] = [{"_row_number": 2, "Serial Alias": "SERIAL-FROM-ALIAS"}]
        session.save()
        mapping_response = self.client.post(
            reverse("plugins:netbox_data_import:quick_add_column_mapping"),
            {
                "profile_id": self.profile.pk,
                "source_column": "Serial Alias",
                "target_field": "serial",
            },
        )

        self.assertEqual(mapping_response.status_code, 302)
        self.assertEqual(self.client.session["import_rows"][0]["serial"], "SERIAL-FROM-ALIAS")

    def test_manual_match_rejects_ambiguous_source_and_cross_site_target(self):
        from dcim.models import Device, Site

        url = reverse("plugins:netbox_data_import:match_existing_device")
        target = Device.objects.create(
            name="manual-boundary-target",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        duplicate_rows = [
            self._device_row(2, "MANUAL-DUPLICATE", "manual-duplicate-a", self.rack_a, 1),
            self._device_row(3, "MANUAL-DUPLICATE", "manual-duplicate-b", self.rack_a, 2),
        ]
        self._set_import_session(duplicate_rows)
        duplicate_response = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": "MANUAL-DUPLICATE",
                "netbox_device_id": target.pk,
            },
        )

        other_site = Site.objects.create(name="Manual Other Site", slug="manual-other-site")
        other_target = Device.objects.create(
            name="manual-cross-site-target",
            site=other_site,
            device_type=self.device_type,
            role=self.role,
        )
        row = self._device_row(2, "MANUAL-CROSS-SITE", "manual-cross-site", self.rack_a, 1)
        self._set_import_session([row])
        cross_site_response = self.client.post(
            url,
            {
                "profile_id": self.profile.pk,
                "source_id": row["source_id"],
                "netbox_device_id": other_target.pk,
            },
        )

        self.assertEqual(duplicate_response.status_code, 302)
        self.assertEqual(cross_site_response.status_code, 302)
        self.assertFalse(
            DeviceExistingMatch.objects.filter(
                profile=self.profile,
                source_id__in=["MANUAL-DUPLICATE", "MANUAL-CROSS-SITE"],
            ).exists()
        )

    def test_quick_role_creation_requires_the_dcim_add_permission(self):
        limited_user = get_user_model().objects.create_user(
            username="quick-role-boundary-user",
            email="quick-role-boundary@example.invalid",
            password="testpass",
        )
        self._grant_object_permission(
            limited_user,
            "View profiles without adding roles",
            ImportProfile,
            ["view"],
        )
        self.client.force_login(limited_user)

        response = self.client.post(
            reverse("plugins:netbox_data_import:quick_create_role"),
            {"name": "Boundary Role", "slug": "boundary-role"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("dcim.add_devicerole", response.json()["error"])

    def test_single_row_sync_validates_row_number_and_active_site(self):
        row = self._device_row(2, "SYNC-BOUNDARY", "sync-boundary-device", self.rack_a, 1)
        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        self._set_import_session([row], preview)
        url = reverse("plugins:netbox_data_import:sync_single_row")

        invalid_number = self.client.post(url, {"row_number": "not-a-number"})

        session = self.client.session
        context = session["import_context"]
        context["site_id"] = 999999
        session["import_context"] = context
        session.save()
        missing_site = self.client.post(url, {"row_number": 2})

        self.assertEqual(invalid_number.status_code, 400)
        self.assertEqual(missing_site.status_code, 400)
        self.assertEqual(invalid_number.json()["error"], "Invalid row number")
        self.assertEqual(missing_site.json()["error"], "Site not found")

    def test_hidden_strong_identity_is_rejected_in_preview_write_and_duplicate_name_preflight(self):
        from dcim.models import Device

        hidden = Device.objects.create(
            name="hidden-strong-identity",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            serial="HIDDEN-STRONG-IDENTITY",
        )
        user = get_user_model().objects.create_user(username="hidden-strong-user", password="testpass")
        row = self._device_row(2, "HIDDEN-STRONG", "hidden-strong-source", self.rack_a, 1)
        row.update({"rack_name": "", "u_position": "", "face": "", "serial": hidden.serial})

        preview = run_import([row], self.profile, {"site": self.site}, dry_run=True, user=user)
        preview_row = next(item for item in preview.rows if item.object_type == "device")
        write = run_import([row], self.profile, {"site": self.site}, dry_run=False, user=user)
        write_row = next(item for item in write.rows if item.object_type == "device")

        other_row = self._device_row(3, "HIDDEN-STRONG-OTHER", row["device_name"], self.rack_a, 2)
        other_row.update({"rack_name": "", "u_position": "", "face": ""})
        duplicate_rows = [row, other_row]
        duplicate_preview = run_import(
            duplicate_rows,
            self.profile,
            {"site": self.site},
            dry_run=True,
            user=user,
        )
        duplicate_row = next(
            item
            for item in duplicate_preview.rows
            if item.object_type == "device" and item.source_id == row["source_id"]
        )

        self.assertEqual(preview_row.detail, "Permission denied: dcim.view_device")
        self.assertEqual(write_row.detail, "Permission denied: dcim.view_device")
        self.assertEqual(duplicate_row.detail, "Permission denied: dcim.view_device")

    def test_write_time_manufacturer_identity_change_stops_type_creation(self):
        from dcim.models import DeviceType, Manufacturer

        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        Manufacturer.objects.create(name="Different Race Vendor", slug="race-vendor")
        row = self._device_row(2, "RACE-VENDOR", "race-vendor-device", self.rack_a, 1)
        row.update({"make": "Race Vendor", "model": "Race Model"})
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=False,
            result=ImportResult(),
        )

        _ensure_device_type(
            "race-vendor",
            "race-model",
            row["make"],
            row["model"],
            1,
            set(),
            ctx,
            row,
            Manufacturer,
            DeviceType,
        )

        self.assertIn(row["_row_number"], ctx.slug_conflicts_by_row)
        self.assertFalse(DeviceType.objects.filter(manufacturer__slug="race-vendor", slug="race-model").exists())

        Manufacturer.objects.create(name="Different Pass Vendor", slug="pass-race-vendor")
        pass_row = self._device_row(3, "PASS-RACE", "pass-race-device", self.rack_a, 2)
        pass_row.update({"make": "Pass Race Vendor", "model": "Pass Race Model"})
        pass_ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=False,
            result=ImportResult(),
        )
        class_role_map = {
            "Server": ClassRoleMapping.objects.get(profile=self.profile, source_class="Server"),
        }

        _pass1_ensure_types([pass_row], pass_ctx, class_role_map)

        self.assertIn(pass_row["_row_number"], pass_ctx.slug_conflicts_by_row)
        self.assertFalse(
            DeviceType.objects.filter(
                manufacturer__slug="pass-race-vendor",
                slug="pass-race-vendor-pass-race-model",
            ).exists()
        )

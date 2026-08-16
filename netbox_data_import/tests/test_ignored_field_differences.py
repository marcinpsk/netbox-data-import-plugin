# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for reviewed device field differences."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.engine import run_import
from netbox_data_import.models import (
    ClassRoleMapping,
    DeviceExistingMatch,
    IgnoredFieldDifference,
    ImportProfile,
)
from netbox_data_import.views import _serialize_rows


class IgnoredFieldDifferencePreviewTest(TestCase):
    """Test Ignored Field Differences through the preview HTTP interface."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        user = get_user_model().objects.create_superuser(
            username="field-review-user",
            email="field-review@example.invalid",
            password="testpass",
        )
        self.user = user
        self.client = Client()
        self.client.force_login(user)
        self.site = Site.objects.create(name="Field Review Site", slug="field-review-site")
        manufacturer = Manufacturer.objects.create(
            name="Field Review Manufacturer",
            slug="field-review-manufacturer",
        )
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Field Review Model",
            slug="field-review-manufacturer-field-review-model",
            u_height=1,
        )
        self.role = DeviceRole.objects.create(name="Field Review Role", slug="field-review-role")
        self.rack = Rack.objects.create(name="Field Review Rack", site=self.site, u_height=42)
        self.device = Device.objects.create(
            name="field-review-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack,
            position=5,
            face="front",
            serial="FIELD-REVIEW-SERIAL",
            status="active",
        )
        self.profile = ImportProfile.objects.create(
            name="Field Review Profile",
            update_existing=True,
            create_missing_device_types=False,
        )
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            role_slug=self.role.slug,
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="FIELD-REVIEW-ROW",
            netbox_device_id=self.device.pk,
            device_name=self.device.name,
        )
        self.rows = [
            {
                "_row_number": 1,
                "source_id": "FIELD-REVIEW-ROW",
                "device_name": self.device.name,
                "device_class": "Server",
                "rack_name": self.rack.name,
                "make": manufacturer.name,
                "model": self.device_type.model,
                "u_height": 1,
                "u_position": 7,
                "face": "front",
                "status": "active",
                "serial": self.device.serial,
                "asset_tag": "",
            }
        ]
        preview = run_import(self.rows, self.profile, {"site": self.site}, dry_run=True, user=user)
        device_row = next(row for row in preview.rows if row.object_type == "device")
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "7"})

        session = self.client.session
        session["import_rows"] = _serialize_rows(self.rows)
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "field-review.xlsx",
        }
        session["import_result"] = preview.to_session_dict()
        session["import_preview_pending"] = True
        session.save()

    def _save_rows(self, rows):
        """Replace the active preview's source rows."""
        session = self.client.session
        session["import_rows"] = _serialize_rows(rows)
        session.save()

    def _preview_device_row(self):
        """Return the current device preview row."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        return response, next(row for row in response.context["result"].rows if row.object_type == "device")

    def test_user_can_ignore_the_exact_current_field_difference(self):
        """A reviewed value pair moves from Fields Differ to Fields Ignored."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        review = IgnoredFieldDifference.objects.get(
            profile=self.profile,
            source_id="FIELD-REVIEW-ROW",
            netbox_device_id=self.device.pk,
            target_field="u_position",
        )
        self.assertEqual(review.file_snapshot, {"canonical": "7", "display": "7"})
        self.assertEqual(review.netbox_snapshot, {"canonical": "5", "display": "5"})
        preview_response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        device_row = next(row for row in preview_response.context["result"].rows if row.object_type == "device")
        self.assertNotIn("u_position", device_row.extra_data["field_diff"])
        self.assertEqual(
            device_row.extra_data["field_ignored"]["u_position"],
            {"netbox": "5", "file": "7"},
        )
        self.assertContains(preview_response, "1 field(s) ignored")
        self.assertContains(preview_response, 'data-diff-target="diff-1"')
        self.assertContains(preview_response, 'aria-controls="diff-1"')

    def test_informational_differences_can_be_ignored(self):
        """Device name and U height differences can move to the ignored section."""
        self.rows[0]["device_name"] = "field-review-source-name"
        self.rows[0]["u_height"] = 2
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_height",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        preview_response, device_row = self._preview_device_row()
        self.assertEqual(
            device_row.extra_data["field_ignored"]["u_height"],
            {"netbox": "1", "file": "2"},
        )
        self.assertTrue(device_row.extra_data["field_non_writable"]["u_height"])
        self.assertIn("device_name", device_row.extra_data["field_diff"])
        self.assertContains(preview_response, "1 field(s) ignored")
        self.assertContains(preview_response, "(not written)")

        self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "device_name",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        _response, device_row = self._preview_device_row()
        self.assertEqual(set(device_row.extra_data["field_ignored"]), {"device_name", "u_height"})
        self.assertNotIn("device_name", device_row.extra_data.get("field_diff", {}))
        self.assertNotIn("u_height", device_row.extra_data.get("field_diff", {}))

    def test_ignore_prevents_only_that_field_from_full_import(self):
        """A full import preserves an ignored field while writing another field."""
        self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.rows[0]["status"] = "offline"
        self._save_rows(self.rows)

        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_row = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, 5)
        self.assertEqual(self.device.status, "offline")

    def test_ignored_fractional_position_is_preserved_during_full_import(self):
        """An ignored fractional position is not truncated while another field writes."""
        self.device.position = Decimal("5.5")
        self.device.save(update_fields=["position"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        review = IgnoredFieldDifference.objects.get(
            profile=self.profile,
            source_id="FIELD-REVIEW-ROW",
            netbox_device_id=self.device.pk,
            target_field="u_position",
        )
        self.assertEqual(review.netbox_snapshot, {"canonical": "5.5", "display": "5.5"})

        self.rows[0]["status"] = "offline"
        self._save_rows(self.rows)
        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_row = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, Decimal("5.5"))
        self.assertEqual(self.device.status, "offline")

    def test_ignored_half_u_file_position_resurfaces_after_the_file_changes(self):
        """A half-U source position stays exact in the saved review snapshot."""
        self.rows[0]["u_position"] = 7.5
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        review = IgnoredFieldDifference.objects.get(
            profile=self.profile,
            source_id="FIELD-REVIEW-ROW",
            netbox_device_id=self.device.pk,
            target_field="u_position",
        )
        self.assertEqual(review.file_snapshot, {"canonical": "7.5", "display": "7.5"})

        self.rows[0]["u_position"] = 7
        self._save_rows(self.rows)
        _response, device_row = self._preview_device_row()

        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "7"})
        self.assertNotIn("u_position", device_row.extra_data.get("field_ignored", {}))

        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_result = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_result.action, "update", device_result.to_dict())
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, 7)

    def test_ignored_device_type_does_not_clear_an_unignored_position(self):
        """A zero-U source type cannot clear a position after its type is ignored."""
        from dcim.models import DeviceType, Manufacturer

        zero_manufacturer = Manufacturer.objects.create(
            name="Field Review Zero Manufacturer",
            slug="field-review-zero-manufacturer",
        )
        zero_type = DeviceType.objects.create(
            manufacturer=zero_manufacturer,
            model="Zero Model",
            slug="field-review-zero-manufacturer-zero-model",
            u_height=0,
        )
        self.rows[0].update(make=zero_manufacturer.name, model=zero_type.model, status="offline")
        self._save_rows(self.rows)

        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("device_type", device_row.extra_data["field_diff"])
        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "7"})

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "device_type",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        _response, device_row = self._preview_device_row()
        self.assertEqual(
            device_row.extra_data["field_ignored"]["device_type"]["netbox"],
            "Field Review Manufacturer / Field Review Model",
        )
        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "7"})

        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_row = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.device.refresh_from_db()
        self.assertEqual(self.device.device_type_id, self.device_type.pk)
        self.assertEqual(self.device.position, 7)
        self.assertEqual(self.device.status, "offline")

    def test_zero_source_u_height_uses_the_same_review_snapshot_in_preview_and_write(self):
        """A zero source U-height is not clamped before exact review comparison."""
        from dcim.models import DeviceType

        zero_u_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="Field Review Zero U Model",
            slug="field-review-manufacturer-field-review-zero-u-mode",
            u_height=0,
        )
        self.device.device_type = zero_u_type
        self.device.rack = None
        self.device.position = None
        self.device.face = ""
        self.device.save(update_fields=["device_type", "rack", "position", "face"])
        self.rows[0].update(
            make=self.device_type.manufacturer.name,
            model=zero_u_type.model,
            rack_name="",
            u_height=0,
            u_position=None,
            face="",
        )
        self._save_rows(self.rows)

        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertNotIn("u_height", device_row.extra_data.get("field_diff", {}))

        self.rows[0]["u_height"] = 1
        self._save_rows(self.rows)
        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.extra_data["field_diff"]["u_height"], {"netbox": "0", "file": "1"})

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_height",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

        self.rows[0]["u_height"] = 2
        self._save_rows(self.rows)
        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.extra_data["field_diff"]["u_height"], {"netbox": "0", "file": "2"})

    def test_field_review_hint_follows_a_case_insensitive_name_match(self):
        """A field review keeps a name-matched device after its source name changes."""
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").delete()
        self.rows[0].update(device_name=self.device.name.upper(), serial="")
        self._save_rows(self.rows)

        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "error", device_row.to_dict())
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)
        self.assertIn("u_position", device_row.extra_data["field_diff"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertTrue(
            DeviceExistingMatch.objects.filter(
                profile=self.profile,
                source_id="FIELD-REVIEW-ROW",
                netbox_device_id=self.device.pk,
            ).exists()
        )

        self.rows[0]["device_name"] = "field-review-device-renamed"
        self._save_rows(self.rows)
        _response, device_row = self._preview_device_row()

        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)
        self.assertIn("device_name", device_row.extra_data["field_diff"])
        self.assertIn("u_position", device_row.extra_data["field_ignored"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)
        self.assertIn("device_name", device_row.extra_data["field_diff"])
        self.assertIn("u_position", device_row.extra_data["field_diff"])

    def test_ignored_missing_device_type_keeps_the_current_relation(self):
        """An active type review remains safe when the source type is deleted."""
        from dcim.models import DeviceType, Manufacturer

        source_manufacturer = Manufacturer.objects.create(
            name="Field Review Deleted Type Manufacturer",
            slug="field-review-deleted-type-manufacturer",
        )
        source_type = DeviceType.objects.create(
            manufacturer=source_manufacturer,
            model="Deleted Type Model",
            slug="field-review-deleted-type-manufacturer-deleted-type-model",
            u_height=1,
        )
        self.profile.create_missing_device_types = True
        self.profile.save(update_fields=["create_missing_device_types"])
        self.rows[0].update(make=source_manufacturer.name, model=source_type.model)
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "device_type",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        source_type.delete()

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(
            device_row.extra_data["field_ignored"]["device_type"],
            {
                "netbox": "Field Review Manufacturer / Field Review Model",
                "file": "Field Review Deleted Type Manufacturer / Deleted Type Model",
            },
        )
        self.assertNotIn("device_type", device_row.extra_data.get("field_diff", {}))
        self.assertContains(preview_response, "1 field(s) ignored")
        self.assertFalse(preview_response.context["result"].has_errors)
        self.assertFalse(any(row.object_type == "device_type" for row in preview_response.context["result"].rows))

        self.rows[0]["status"] = "offline"
        self._save_rows(self.rows)
        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_result = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_result.action, "update", device_result.to_dict())
        self.assertFalse(result.has_errors)
        self.assertFalse(
            DeviceType.objects.filter(
                manufacturer=source_manufacturer,
                slug=source_type.slug,
            ).exists()
        )
        self.device.refresh_from_db()
        self.assertEqual(self.device.device_type_id, self.device_type.pk)
        self.assertEqual(self.device.status, "offline")

    def test_missing_device_type_keeps_review_metadata_for_ignore(self):
        """A missing source type remains reviewable on a matched-device error row."""
        from dcim.models import DeviceType, Manufacturer

        source_manufacturer = Manufacturer.objects.create(
            name="Field Review Missing Type Manufacturer",
            slug="field-review-missing-type-manufacturer",
        )
        source_type = DeviceType.objects.create(
            manufacturer=source_manufacturer,
            model="Missing Type Model",
            slug="field-review-missing-type-manufacturer-missing-type-model",
            u_height=1,
        )
        self.rows[0].update(make=source_manufacturer.name, model=source_type.model)
        self._save_rows(self.rows)
        source_type.delete()

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "error", device_row.to_dict())
        self.assertIn("device_type", device_row.extra_data["field_diff"])
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)
        self.assertContains(preview_response, "Ignore this exact value difference")

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "device_type",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("device_type", device_row.extra_data["field_ignored"])
        self.assertFalse(preview_response.context["result"].has_errors)

    def test_ignored_device_type_survives_a_late_slug_collision(self):
        """An active type review bypasses only a later conflicting source slug."""
        from dcim.models import DeviceType, Manufacturer

        source_manufacturer = Manufacturer.objects.create(
            name="Field Review Slug Manufacturer",
            slug="field-review-slug-manufacturer",
        )
        source_type = DeviceType.objects.create(
            manufacturer=source_manufacturer,
            model="Field Review Source Model",
            slug="field-review-slug-manufacturer-field-review-source",
            u_height=1,
        )
        self.rows[0].update(make=source_manufacturer.name, model=source_type.model)
        self._save_rows(self.rows)

        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("device_type", device_row.extra_data["field_diff"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "device_type",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

        source_type.delete()
        DeviceType.objects.create(
            manufacturer=source_manufacturer,
            model="Field Review Conflicting Model",
            slug="field-review-slug-manufacturer-field-review-source",
            u_height=1,
        )

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("device_type", device_row.extra_data["field_ignored"])
        self.assertNotEqual(device_row.extra_data.get("identity_conflict"), "derived_slug_collision")
        self.assertFalse(preview_response.context["result"].has_errors)

    def test_initial_device_type_slug_collision_keeps_review_metadata(self):
        """An initial type collision still exposes the matched row for review."""
        from dcim.models import DeviceType, Manufacturer

        source_manufacturer = Manufacturer.objects.create(
            name="Field Review Initial Collision Manufacturer",
            slug="field-review-initial-collision-manufacturer",
        )
        DeviceType.objects.create(
            manufacturer=source_manufacturer,
            model="Field Review Existing Model",
            slug="field-review-initial-collision-manufacturer-field-",
            u_height=1,
        )
        self.rows[0].update(
            make=source_manufacturer.name,
            model="Field Review Source Model",
        )
        self._save_rows(self.rows)

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "error", device_row.to_dict())
        self.assertEqual(device_row.extra_data.get("identity_conflict"), "derived_slug_collision")
        self.assertIn("device_type", device_row.extra_data["field_diff"])
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)
        self.assertContains(preview_response, "Ignore this exact value difference")

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "device_type",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("device_type", device_row.extra_data["field_ignored"])
        self.assertFalse(preview_response.context["result"].has_errors)

    def test_ignored_missing_device_role_keeps_the_current_relation(self):
        """An active role review remains safe when the source role is deleted."""
        from dcim.models import DeviceRole

        source_role = DeviceRole.objects.create(
            name="Field Review Deleted Role",
            slug="field-review-deleted-role",
        )
        mapping = self.profile.class_role_mappings.get(source_class="Server")
        mapping.role_slug = source_role.slug
        mapping.save(update_fields=["role_slug"])
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "role",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        source_role.delete()

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(
            device_row.extra_data["field_ignored"]["role"],
            {"netbox": self.role.slug, "file": "field-review-deleted-role"},
        )
        self.assertNotIn("role", device_row.extra_data.get("field_diff", {}))
        self.assertContains(preview_response, "1 field(s) ignored")
        self.assertFalse(preview_response.context["result"].has_errors)

        self.rows[0]["status"] = "offline"
        self._save_rows(self.rows)
        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_result = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_result.action, "update", device_result.to_dict())
        self.assertFalse(result.has_errors)
        self.assertFalse(DeviceRole.objects.filter(slug=source_role.slug).exists())
        self.device.refresh_from_db()
        self.assertEqual(self.device.role_id, self.role.pk)
        self.assertEqual(self.device.status, "offline")

    def test_missing_device_role_keeps_review_metadata_for_ignore(self):
        """A missing source role remains reviewable on a matched-device error row."""
        from dcim.models import DeviceRole

        source_role = DeviceRole.objects.create(
            name="Field Review Missing Role",
            slug="field-review-missing-role",
        )
        mapping = self.profile.class_role_mappings.get(source_class="Server")
        mapping.role_slug = source_role.slug
        mapping.save(update_fields=["role_slug"])
        self._save_rows(self.rows)
        source_role.delete()

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("role", device_row.extra_data["field_diff"])
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)
        self.assertContains(preview_response, "Ignore this exact value difference")

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "role",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("role", device_row.extra_data["field_ignored"])
        self.assertFalse(preview_response.context["result"].has_errors)

    def test_zero_u_with_an_ignored_position_fails_consistently_in_preview_and_full_import(self):
        """A zero-U proposal reports an ignored-position conflict before any write."""
        from dcim.models import DeviceType, Manufacturer

        zero_manufacturer = Manufacturer.objects.create(
            name="Field Review Conflict Manufacturer",
            slug="field-review-conflict-manufacturer",
        )
        zero_type = DeviceType.objects.create(
            manufacturer=zero_manufacturer,
            model="Conflict Model",
            slug="field-review-conflict-manufacturer-conflict-model",
            u_height=0,
        )
        self.rows[0].update(make=zero_manufacturer.name, model=zero_type.model, status="offline")
        self._save_rows(self.rows)

        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        preview_response, preview_row = self._preview_device_row()
        self.assertEqual(preview_row.action, "error", preview_row.to_dict())
        self.assertIn("zero-U", preview_row.detail)
        self.assertEqual(preview_row.extra_data["field_ignored"]["u_position"], {"netbox": "5", "file": "7"})
        self.assertContains(preview_response, "1 field(s) ignored")
        self.assertContains(preview_response, "Unignore")

        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        _response, preview_row = self._preview_device_row()
        self.assertEqual(preview_row.action, "update", preview_row.to_dict())
        self.assertNotIn("u_position", preview_row.extra_data.get("field_ignored", {}))
        self.assertIn("u_position", preview_row.extra_data["field_diff"])

        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_row = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.device.refresh_from_db()
        self.assertEqual(self.device.device_type_id, zero_type.pk)
        self.assertIsNone(self.device.position)
        self.assertEqual(self.device.status, "offline")

    def test_occupied_position_still_exposes_review_metadata_and_ignore_action(self):
        """An occupied proposed U-position can be reviewed from an error preview row."""
        from dcim.models import Device

        Device.objects.create(
            name="field-review-occupied-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack,
            position=7,
            face="front",
            status="active",
        )

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "error", device_row.to_dict())
        self.assertEqual(device_row.extra_data["identity_conflict"], "rack_position_occupied")
        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "7"})
        self.assertContains(preview_response, "Ignore this exact value difference")

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(device_row.extra_data["field_ignored"]["u_position"], {"netbox": "5", "file": "7"})
        self.assertNotIn("u_position", device_row.extra_data["field_diff"])

    def test_ignored_identity_fields_do_not_trigger_raw_duplicate_validation(self):
        """Ignored serial and asset tag values use their effective current values for duplicates."""
        from dcim.models import Device

        second_device = Device.objects.create(
            name="field-review-second-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack,
            position=10,
            face="front",
            serial="FIELD-REVIEW-SECOND-SERIAL",
            status="active",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="FIELD-REVIEW-SECOND-ROW",
            netbox_device_id=second_device.pk,
            device_name=second_device.name,
        )
        shared_serial = "FIELD-REVIEW-SHARED-SERIAL"
        shared_asset_tag = "FIELD-REVIEW-SHARED-ASSET"
        self.rows[0].update(serial=shared_serial, asset_tag=shared_asset_tag)
        self._save_rows(self.rows)

        for target_field in ("serial", "asset_tag"):
            response = self.client.post(
                reverse("plugins:netbox_data_import:ignore_field_difference"),
                {
                    "profile_id": self.profile.pk,
                    "row_number": 1,
                    "target_field": target_field,
                    "next": reverse("plugins:netbox_data_import:import_preview"),
                },
            )
            self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

        second_row = dict(
            self.rows[0],
            _row_number=2,
            source_id="FIELD-REVIEW-SECOND-ROW",
            device_name=second_device.name,
            u_position=10,
        )
        result = run_import(
            [self.rows[0], second_row],
            self.profile,
            {"site": self.site},
            dry_run=False,
            user=self.user,
        )

        device_rows = [row for row in result.rows if row.object_type == "device"]
        self.assertEqual(
            [row.action for row in device_rows], ["update", "update"], [row.to_dict() for row in device_rows]
        )
        self.device.refresh_from_db()
        second_device.refresh_from_db()
        self.assertEqual(self.device.serial, "FIELD-REVIEW-SERIAL")
        self.assertFalse(self.device.asset_tag)
        self.assertEqual(second_device.serial, shared_serial)
        self.assertEqual(second_device.asset_tag, shared_asset_tag)

    def test_ignored_identity_field_is_not_a_duplicate_write(self):
        """An ignored identity source value does not collide with one unignored write."""
        from dcim.models import Device

        second_device = Device.objects.create(
            name="field-review-duplicate-write-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack,
            position=10,
            face="front",
            serial="FIELD-REVIEW-SECOND-SERIAL",
            status="active",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="FIELD-REVIEW-DUPLICATE-WRITE-ROW",
            netbox_device_id=second_device.pk,
            device_name=second_device.name,
        )
        ignored_source_serial = "FIELD-REVIEW-IGNORED-SERIAL"
        self.rows[0]["serial"] = ignored_source_serial
        self._save_rows(self.rows)
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "serial",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

        second_row = dict(
            self.rows[0],
            _row_number=2,
            source_id="FIELD-REVIEW-DUPLICATE-WRITE-ROW",
            device_name=second_device.name,
            serial=self.device.serial,
            asset_tag="",
            u_position=10,
        )
        result = run_import(
            [self.rows[0], second_row],
            self.profile,
            {"site": self.site},
            dry_run=False,
            user=self.user,
        )

        device_rows = [row for row in result.rows if row.object_type == "device"]
        self.assertEqual(
            [row.action for row in device_rows], ["update", "update"], [row.to_dict() for row in device_rows]
        )
        self.device.refresh_from_db()
        second_device.refresh_from_db()
        self.assertEqual(self.device.serial, "FIELD-REVIEW-SERIAL")
        self.assertEqual(second_device.serial, "FIELD-REVIEW-SERIAL")

    def test_changed_file_value_resurfaces_an_ignored_difference(self):
        """A changed source value makes the ignored field differ again."""
        self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.rows[0]["u_position"] = 8
        self._save_rows(self.rows)

        _response, device_row = self._preview_device_row()

        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "8"})
        self.assertNotIn("u_position", device_row.extra_data.get("field_ignored", {}))

    def test_changed_netbox_value_resurfaces_an_ignored_difference(self):
        """A changed NetBox value makes the ignored field differ again."""
        self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.device.position = 6
        self.device.save(update_fields=["position"])

        _response, device_row = self._preview_device_row()

        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "6", "file": "7"})
        self.assertNotIn("u_position", device_row.extra_data.get("field_ignored", {}))

    def test_unignore_restores_the_current_difference(self):
        """Unignore removes the current review and shows its difference again."""
        self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        _response, device_row = self._preview_device_row()

        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "7"})
        self.assertNotIn("u_position", device_row.extra_data.get("field_ignored", {}))
        self.assertFalse(
            IgnoredFieldDifference.objects.filter(
                profile=self.profile,
                source_id="FIELD-REVIEW-ROW",
                netbox_device_id=self.device.pk,
                target_field="u_position",
            ).exists()
        )

    def test_unlink_removes_dependent_field_reviews(self):
        """Unlink removes active and stale field reviews before releasing the source row."""
        from dcim.models import Device

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

        stale_device = Device.objects.create(
            name="field-review-stale-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="FIELD-REVIEW-ROW",
            netbox_device_id=stale_device.pk,
            target_field="status",
            file_snapshot={"canonical": "offline", "display": "offline"},
            netbox_snapshot={"canonical": "active", "display": "active"},
        )

        response = self.client.post(
            reverse("plugins:netbox_data_import:unlink_device"),
            {
                "profile_id": self.profile.pk,
                "source_id": "FIELD-REVIEW-ROW",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertFalse(
            DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").exists()
        )
        self.assertFalse(
            IgnoredFieldDifference.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").exists()
        )
        messages = [str(message) for message in response.wsgi_request._messages]
        self.assertTrue(any("field review" in message.lower() for message in messages), messages)

        self.rows[0].update(device_name="field-review-device-after-unlink", serial="")
        self._save_rows(self.rows)
        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "create", device_row.to_dict())
        self.assertNotIn("netbox_device_id", device_row.extra_data)

    def test_rematched_device_does_not_reuse_the_old_device_review(self):
        """A review stays bound to its original matched Device identity."""
        from dcim.models import Device

        self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        replacement = Device.objects.create(
            name="field-review-rematched-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack,
            position=3,
            face="front",
            status="active",
        )
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").update(
            netbox_device_id=replacement.pk,
            device_name=replacement.name,
        )

        _response, device_row = self._preview_device_row()

        self.assertEqual(device_row.extra_data["field_diff"]["u_position"], {"netbox": "3", "file": "7"})
        self.assertNotIn("u_position", device_row.extra_data.get("field_ignored", {}))

    def test_posted_noncurrent_field_is_rejected(self):
        """A POST cannot create a review for a field absent from the fresh preview."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "status",
                "device_id": self.device.pk,
                "file_value": "forged",
                "netbox_value": "forged",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertFalse(
            IgnoredFieldDifference.objects.filter(
                profile=self.profile,
                source_id="FIELD-REVIEW-ROW",
                target_field="status",
            ).exists()
        )

    def test_blank_source_id_cannot_create_a_field_review(self):
        """A review requires the source row identity used by its persistence key."""
        blank_source_rows = [dict(self.rows[0], source_id="")]
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").delete()
        self._save_rows(blank_source_rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertFalse(
            IgnoredFieldDifference.objects.filter(
                profile=self.profile, source_id="", target_field="u_position"
            ).exists()
        )
        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("u_position", device_row.extra_data["field_diff"])
        self.assertNotContains(preview_response, 'title="Ignore this exact value difference"')

    def test_posted_device_and_snapshot_values_are_ignored(self):
        """A valid target still uses the fresh matched Device and snapshots."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk + 1000,
                "row_number": 1,
                "target_field": "u_position",
                "device_id": self.device.pk + 1000,
                "file_value": "forged-file-value",
                "netbox_value": "forged-netbox-value",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        review = IgnoredFieldDifference.objects.get(
            profile=self.profile,
            source_id="FIELD-REVIEW-ROW",
            target_field="u_position",
        )
        self.assertEqual(review.netbox_device_id, self.device.pk)
        self.assertEqual(review.file_snapshot, {"canonical": "7", "display": "7"})
        self.assertEqual(review.netbox_snapshot, {"canonical": "5", "display": "5"})

    def test_ignored_rack_difference_preserves_the_matched_device_rack(self):
        """An ignored rack difference uses the matched rack during the full import."""
        from dcim.models import Location, Rack

        current_location = Location.objects.create(
            name="Field Review Current Location",
            slug="field-review-current-location",
            site=self.site,
        )
        import_location = Location.objects.create(
            name="Field Review Import Location",
            slug="field-review-import-location",
            site=self.site,
        )
        self.rack.location = current_location
        self.rack.save(update_fields=["location"])
        self.device.location = current_location
        self.device.save(update_fields=["location"])
        Rack.objects.create(
            name=self.rack.name,
            site=self.site,
            location=import_location,
            u_height=42,
        )

        session = self.client.session
        session["import_context"]["location_id"] = import_location.pk
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "rack_name",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "location",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertTrue(
            IgnoredFieldDifference.objects.filter(
                profile=self.profile,
                source_id="FIELD-REVIEW-ROW",
                netbox_device_id=self.device.pk,
                target_field="location",
            ).exists()
        )
        result = run_import(
            self.rows,
            self.profile,
            {"site": self.site, "location": import_location},
            dry_run=False,
            user=self.user,
        )

        device_result = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_result.action, "update", device_result.to_dict())
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack, self.rack)
        self.assertEqual(self.device.location, current_location)

    def test_ignored_missing_source_rack_keeps_review_metadata_and_current_rack(self):
        """An ignored rack does not resolve a deleted source rack again."""
        from dcim.models import Rack

        source_rack = Rack.objects.create(
            name="Field Review Deleted Source Rack",
            site=self.site,
            u_height=42,
        )
        self.rows[0]["rack_name"] = source_rack.name
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "rack_name",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        source_rack.delete()

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(
            device_row.extra_data["field_ignored"]["rack_name"],
            {
                "netbox": self.rack.name,
                "file": "Field Review Deleted Source Rack",
            },
        )
        self.assertNotIn("rack_name", device_row.extra_data.get("field_diff", {}))
        self.assertContains(preview_response, "1 field(s) ignored")

        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        device_result = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_result.action, "update", device_result.to_dict())
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack_id, self.rack.pk)

    def test_missing_source_rack_keeps_review_metadata_for_ignore(self):
        """A missing source rack remains reviewable on a matched-device error row."""
        self.rows[0]["rack_name"] = "Field Review Never Existing Rack"
        self._save_rows(self.rows)

        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "error", device_row.to_dict())
        self.assertIn("rack_name", device_row.extra_data["field_diff"])
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)
        self.assertContains(preview_response, "Ignore this exact value difference")

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "rack_name",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertIn("rack_name", device_row.extra_data["field_ignored"])
        self.assertEqual(device_row.rack_name, self.rack.name)

    def test_ignored_location_does_not_redirect_an_unignored_rack(self):
        """Ignoring location alone must not resolve the source rack in the old location."""
        from dcim.models import Location, Rack

        current_location = Location.objects.create(
            name="Field Review Partial Current Location",
            slug="field-review-partial-current-location",
            site=self.site,
        )
        import_location = Location.objects.create(
            name="Field Review Partial Import Location",
            slug="field-review-partial-import-location",
            site=self.site,
        )
        self.rack.location = current_location
        self.rack.save(update_fields=["location"])
        self.device.location = current_location
        self.device.save(update_fields=["location"])
        Rack.objects.create(name=self.rack.name, site=self.site, location=import_location, u_height=42)

        session = self.client.session
        session["import_context"]["location_id"] = import_location.pk
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "location",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        preview_response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "error", device_row.to_dict())
        self.assertEqual(device_row.extra_data["identity_conflict"], "rack_location_conflict")
        self.assertIn("rack_name", device_row.extra_data["field_diff"])
        self.assertIn("location", device_row.extra_data["field_ignored"])
        self.assertContains(preview_response, "Ignore this exact value difference")

        result = run_import(
            self.rows,
            self.profile,
            {"site": self.site, "location": import_location},
            dry_run=False,
            user=self.user,
        )

        device_result = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_result.action, "error", device_result.to_dict())
        self.assertIn("location", device_result.detail)
        self.device.refresh_from_db()
        self.assertEqual(self.device.location, current_location)
        self.assertEqual(self.device.rack, self.rack)

    def test_ignored_rack_without_location_rejects_partial_placement(self):
        """Ignoring rack alone must reject a device location that points elsewhere."""
        from dcim.models import Location, Rack

        current_location = Location.objects.create(
            name="Field Review Rack Only Current Location",
            slug="field-review-rack-only-current-location",
            site=self.site,
        )
        import_location = Location.objects.create(
            name="Field Review Rack Only Import Location",
            slug="field-review-rack-only-import-location",
            site=self.site,
        )
        self.rack.location = current_location
        self.rack.save(update_fields=["location"])
        self.device.location = current_location
        self.device.save(update_fields=["location"])
        Rack.objects.create(name=self.rack.name, site=self.site, location=import_location, u_height=42)

        session = self.client.session
        session["import_context"]["location_id"] = import_location.pk
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "rack_name",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        _response, device_row = self._preview_device_row()
        self.assertEqual(device_row.action, "error", device_row.to_dict())
        self.assertEqual(device_row.extra_data["identity_conflict"], "rack_location_conflict")
        self.assertIn("rack_name", device_row.extra_data["field_ignored"])
        self.assertIn("location", device_row.extra_data["field_diff"])

        result = run_import(
            self.rows,
            self.profile,
            {"site": self.site, "location": import_location},
            dry_run=False,
            user=self.user,
        )

        device_result = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_result.action, "error", device_result.to_dict())
        self.assertIn("location", device_result.detail)
        self.device.refresh_from_db()
        self.assertEqual(self.device.location, current_location)
        self.assertEqual(self.device.rack, self.rack)

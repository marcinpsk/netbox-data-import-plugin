# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Preview row actions consume target-neutral Import Plans."""

from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import (
    ClassRoleMapping,
    DeviceExistingMatch,
    IgnoredFieldDifference,
    ImportProfile,
)
from netbox_data_import.preview_row_actions import record_recalculated_preview
from netbox_data_import.tests.helpers import plan_source_rows, run_on_separate_connection, user_with_object_permission


class TargetNeutralFieldReviewTest(TransactionTestCase):
    """Field-review actions validate the exact unit stored in the Import Plan."""

    def setUp(self):
        """Create one bound Device with a previewed placement difference."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.actor = get_user_model().objects.create_superuser(
            username="review-action-operator",
            email="review-action@example.invalid",
            password="testpass",
        )
        self.client = Client()
        self.client.force_login(self.actor)
        self.site = Site.objects.create(name="Review Action Site", slug="review-action-site")
        manufacturer = Manufacturer.objects.create(name="Review Action Make", slug="review-action-make")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Review Action Model",
            slug="review-action-make-review-action-model",
            u_height=1,
        )
        self.role = DeviceRole.objects.create(name="Review Action Role", slug="review-action-role")
        self.rack = Rack.objects.create(name="Review Action Rack", site=self.site, u_height=42)
        self.device = Device.objects.create(
            name="review-action-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack,
            position=5,
            face="front",
            serial="REVIEW-ACTION-SERIAL",
            status="active",
        )
        self.profile = ImportProfile.objects.create(
            name="Review Action Profile",
            adapter_config={"update_existing": True, "create_missing_device_types": False},
        )
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            role_slug=self.role.slug,
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="REVIEW-ACTION-ROW",
            netbox_device_id=self.device.pk,
            device_name=self.device.name,
        )
        self.rows = [
            {
                "_row_number": 1,
                "source_id": "REVIEW-ACTION-ROW",
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
        self._materialize()

    def _materialize(self, *, expect_ignored=False):
        """Store a new-interface workspace as the active browser preview."""
        workspace = plan_source_rows(self.rows, self.profile, self.site, actor=self.actor)
        device_unit = next(unit for unit in workspace.units if unit.object_type == "device")
        self.assertEqual(device_unit.action, "update", device_unit)
        review_bucket = "field_ignored" if expect_ignored else "field_diff"
        self.assertEqual(device_unit.extra_data[review_bucket]["u_position"], {"netbox": "5", "file": "7"})
        session = self.client.session
        record_recalculated_preview(session, workspace.plan)
        session["import_rows"] = workspace.source_rows
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
        }
        session["import_preview_pending"] = True
        session.save()

    def _post(self, view_name, target_field="u_position"):
        """Post one JSON row action against the current preview revision."""
        return self.client.post(
            reverse(f"plugins:netbox_data_import:{view_name}"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": target_field,
                "preview_revision": self.client.session["import_preview_revision"],
            },
            HTTP_ACCEPT="application/json",
        )

    def _sync_field(self, field="u_position"):
        """Post one inline field sync against the current preview revision."""
        return self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            {
                "row_number": 1,
                "field": field,
                "preview_revision": self.client.session["import_preview_revision"],
            },
            HTTP_ACCEPT="application/json",
        )

    def _sync_placement(self):
        """Post one inline placement sync against the current preview revision."""
        return self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            {
                "row_number": 1,
                "preview_revision": self.client.session["import_preview_revision"],
            },
            HTTP_ACCEPT="application/json",
        )

    def _device_unit_data(self):
        """Return the mutable serialized Device unit in the active session."""
        session = self.client.session
        unit = next(item for item in session["import_plan"]["units"] if item["identity"].startswith("device:"))
        return session, unit

    def _ignore_and_replan(self):
        """Save one review and materialize the resulting ignored state."""
        response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 200, response.content)
        self._materialize(expect_ignored=True)

    def test_ignore_and_unignore_round_trip(self):
        """A saved review moves through two fresh plans without legacy result rows."""
        ignored = self._post("ignore_field_difference")

        self.assertEqual(ignored.status_code, 200)
        self.assertTrue(ignored.json()["ok"])
        self.assertTrue(self.client.session["import_preview_dirty"])
        self.assertTrue(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

        self._materialize(expect_ignored=True)
        restored = self._post("unignore_field_difference")

        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["ok"])
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_ignore_rejects_absent_and_malformed_preview_rows(self):
        """An absent preview, malformed row number, or unknown field cannot authorize a review."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        self.assertEqual(self._post("ignore_field_difference").status_code, 409)

        self._materialize()
        self.assertEqual(
            self.client.post(
                reverse("plugins:netbox_data_import:ignore_field_difference"),
                {"row_number": "invalid", "target_field": "u_position"},
                HTTP_ACCEPT="application/json",
            ).status_code,
            409,
        )
        self.assertEqual(self._post("ignore_field_difference", "unknown").status_code, 409)

    def test_ignore_rejects_a_cached_plan_that_cannot_be_deserialized(self):
        """A stale cached schema follows the normal unavailable-preview response path."""
        session = self.client.session
        session["import_plan"]["schema_version"] = 999
        session.save()

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_ignore_rejects_a_difference_removed_from_the_plan(self):
        """The action refuses a field that the accepted plan no longer offers."""
        session, unit = self._device_unit_data()
        unit["display"]["extra_data"]["field_diff"].pop("u_position")
        session.save()

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer present", response.json()["error"])

    def test_ignore_rejects_missing_snapshots_and_a_deleted_device(self):
        """A review requires both authoritative snapshots and its visible Device."""
        session, unit = self._device_unit_data()
        unit["display"]["extra_data"]["field_review_snapshots"].pop("u_position")
        session.save()
        self.assertEqual(self._post("ignore_field_difference").status_code, 409)

        self._materialize()
        self.device.delete()
        response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_ignore_rejects_a_changed_netbox_baseline(self):
        """A Device change after planning invalidates the review snapshot."""
        self.device.position = 6
        self.device.save(update_fields=["position"])

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertIn("value changed", response.json()["error"])

    def test_ignore_rejects_conflicting_device_bindings(self):
        """A field review cannot move a source binding or reuse another source's Device."""
        from dcim.models import Device

        replacement = Device.objects.create(
            name="review-action-replacement",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="REVIEW-ACTION-ROW").update(
            netbox_device_id=replacement.pk,
            device_name=replacement.name,
        )
        response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 409)
        self.assertIn("linked elsewhere", response.json()["error"])

        DeviceExistingMatch.objects.filter(profile=self.profile).delete()
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="OTHER-ROW",
            netbox_device_id=self.device.pk,
            device_name=self.device.name,
        )
        response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 409)
        self.assertIn("linked elsewhere", response.json()["error"])

    def test_ignore_sanitizes_a_real_object_permission_failure(self):
        """A constrained add permission rolls back and returns a bounded row-action error."""
        from dcim.models import Device

        actor = user_with_object_permission(
            "review-action-denied",
            [
                (ImportProfile, ("change",), {"pk": self.profile.pk}),
                (Device, ("view",), {"pk": self.device.pk}),
                (IgnoredFieldDifference, ("add",), {"source_id": "OTHER-ROW"}),
            ],
        )
        self.client.force_login(actor)
        self._materialize()

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_ignore_sanitizes_a_real_validation_failure(self):
        """An overlong source identity is rejected through the real policy write."""
        DeviceExistingMatch.objects.filter(profile=self.profile).delete()
        self.rows[0]["source_id"] = "X" * 201
        self._materialize()

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot exceed 200 characters", response.json()["error"])
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile).exists())
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_unignore_rejects_absent_stale_and_conflicting_records(self):
        """Unignore deletes only the exact review and binding shown in its plan."""
        self.assertEqual(self._post("unignore_field_difference").status_code, 409)

        self._ignore_and_replan()
        IgnoredFieldDifference.objects.filter(profile=self.profile).delete()
        self.assertEqual(self._post("unignore_field_difference").status_code, 409)

        self._materialize()
        self._ignore_and_replan()
        self.device.delete()
        response = self._post("unignore_field_difference")
        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_unignore_rejects_a_real_concurrent_binding_change(self):
        """Unignore preserves its record if the source binding moves before its locked read."""
        from django.db import connection
        from dcim.models import Device

        self._ignore_and_replan()
        replacement = Device.objects.create(
            name="review-action-unignore-replacement",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        binding_moved = []

        def move_binding_before_read(execute, sql, params, many, context):
            if not binding_moved and "SELECT" in sql and DeviceExistingMatch._meta.db_table in sql:
                binding_moved.append(True)

                def move_binding():
                    DeviceExistingMatch.objects.filter(
                        profile=self.profile,
                        source_id="REVIEW-ACTION-ROW",
                    ).update(netbox_device_id=replacement.pk, device_name=replacement.name)

                with run_on_separate_connection(move_binding):
                    # Finish the competing update before the locked read.
                    pass
            return execute(sql, params, many, context)

        with connection.execute_wrapper(move_binding_before_read):
            response = self._post("unignore_field_difference")

        self.assertEqual(binding_moved, [True])
        self.assertEqual(response.status_code, 409)
        self.assertIn("linked elsewhere", response.json()["error"])
        self.assertTrue(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_inline_field_sync_uses_the_plan_value_and_marks_it_stale(self):
        """An inline field write uses the plan snapshot, not a posted replacement value."""
        response = self._sync_field()

        self.assertEqual(response.status_code, 200, response.json())
        self.assertTrue(response.json()["ok"])
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, 7)
        self.assertTrue(self.client.session["import_preview_dirty"])

    def test_inline_field_sync_rejects_stale_plan_state(self):
        """An absent row, removed difference, missing snapshot, and changed Device are refused."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        self.assertEqual(self._sync_field().status_code, 409)

        self._materialize()
        session, unit = self._device_unit_data()
        unit["display"]["extra_data"]["field_diff"].pop("u_position")
        session.save()
        self.assertIn("no longer present", self._sync_field().json()["error"])

        self._materialize()
        session, unit = self._device_unit_data()
        unit["display"]["extra_data"]["field_review_snapshots"].pop("u_position")
        session.save()
        self.assertIn("no authoritative", self._sync_field().json()["error"])

        self._materialize()
        self.device.position = 6
        self.device.save(update_fields=["position"])
        self.assertIn("value changed", self._sync_field().json()["error"])

    def test_inline_position_sync_rejects_a_stale_rack(self):
        """Position sync refuses a Device that moved racks after the preview."""
        from dcim.models import Rack

        replacement = Rack.objects.create(name="Review Action Rack B", site=self.site, u_height=42)
        self.device.rack = replacement
        self.device.save(update_fields=["rack"])

        response = self._sync_field()

        self.assertEqual(response.status_code, 409, response.json())
        self.assertIn("placement changed", response.json()["error"])
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack, replacement)
        self.assertEqual(self.device.position, 5)

    def test_inline_face_sync_rejects_a_stale_rack(self):
        """Face sync refuses a Device that moved racks after the preview."""
        from dcim.models import Rack

        self.rows[0]["face"] = "rear"
        self._materialize()
        replacement = Rack.objects.create(name="Review Action Rack C", site=self.site, u_height=42)
        self.device.rack = replacement
        self.device.save(update_fields=["rack"])

        response = self._sync_field("face")

        self.assertEqual(response.status_code, 409, response.json())
        self.assertIn("placement changed", response.json()["error"])
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack, replacement)
        self.assertEqual(self.device.face, "front")

    def test_inline_placement_sync_uses_the_plan_and_rechecks_its_baseline(self):
        """Placement writes the accepted unit only while its NetBox snapshot is current."""
        response = self._sync_placement()
        self.assertEqual(response.status_code, 200, response.json())
        self.assertTrue(response.json()["ok"])
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, 7)

        self.device.position = 5
        self.device.save(update_fields=["position"])
        self._materialize()
        self.device.position = 6
        self.device.save(update_fields=["position"])
        response = self._sync_placement()
        self.assertEqual(response.status_code, 409)
        self.assertIn("placement changed", response.json()["error"])

    def test_inline_placement_sync_rejects_an_absent_preview_row(self):
        """A cleared preview cannot authorize placement from client-supplied data."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()

        response = self._sync_placement()

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_unlink_removes_the_binding_and_its_dependent_field_reviews(self):
        """A source link and all reviews scoped by it are removed together."""
        self._ignore_and_replan()

        response = self.client.post(
            reverse("plugins:netbox_data_import:unlink_device"),
            {"profile_id": self.profile.pk, "source_id": "REVIEW-ACTION-ROW"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile).exists())
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_unlink_checks_the_binding_and_each_dependent_review_permission(self):
        """Object-scoped denial preserves the whole source-to-device review state."""
        self._ignore_and_replan()
        endpoint = reverse("plugins:netbox_data_import:unlink_device")
        data = {"profile_id": self.profile.pk, "source_id": "REVIEW-ACTION-ROW"}
        binding_denied = user_with_object_permission(
            "review-action-binding-denied",
            [
                (ImportProfile, ("change",), None),
                (DeviceExistingMatch, ("delete",), {"source_id": "OTHER-ROW"}),
                (IgnoredFieldDifference, ("delete",), None),
            ],
        )
        binding_client = Client()
        binding_client.force_login(binding_denied)

        self.assertEqual(binding_client.post(endpoint, data).status_code, 302)
        self.assertTrue(DeviceExistingMatch.objects.filter(profile=self.profile).exists())

        review_denied = user_with_object_permission(
            "review-action-review-denied",
            [
                (ImportProfile, ("change",), None),
                (DeviceExistingMatch, ("delete",), None),
                (IgnoredFieldDifference, ("delete",), {"source_id": "OTHER-ROW"}),
            ],
        )
        review_client = Client()
        review_client.force_login(review_denied)

        self.assertEqual(review_client.post(endpoint, data).status_code, 302)
        self.assertTrue(DeviceExistingMatch.objects.filter(profile=self.profile).exists())
        self.assertTrue(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())


class UnplacedNameMatchPlacementSyncTest(TransactionTestCase):
    """A row refused for an unplaced name match must still let the operator place the Device."""

    def setUp(self):
        """Create one unplaced Device that a row matches by name and wants to place."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.actor = get_user_model().objects.create_superuser(
            username="unplaced-operator",
            email="unplaced@example.invalid",
            password="testpass",
        )
        self.client = Client()
        self.client.force_login(self.actor)
        self.site = Site.objects.create(name="Unplaced Site", slug="unplaced-site")
        manufacturer = Manufacturer.objects.create(name="Unplaced Make", slug="unplaced-make")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Unplaced Model",
            slug="unplaced-make-unplaced-model",
            u_height=1,
        )
        self.role = DeviceRole.objects.create(name="Unplaced Role", slug="unplaced-role")
        self.rack = Rack.objects.create(name="Unplaced Rack", site=self.site, u_height=42)
        # The stored Device carries no placement, which is what makes the row an unplaced name match.
        self.device = Device.objects.create(
            name="unplaced-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            status="active",
        )
        self.profile = ImportProfile.objects.create(
            name="Unplaced Profile",
            adapter_config={"update_existing": True, "create_missing_device_types": False},
        )
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            role_slug=self.role.slug,
        )
        self.rows = [
            {
                "_row_number": 1,
                "source_id": "UNPLACED-ROW",
                "device_name": self.device.name,
                "device_class": "Server",
                "rack_name": self.rack.name,
                "make": manufacturer.name,
                "model": self.device_type.model,
                "u_height": 1,
                "u_position": 2,
                "face": "front",
                "status": "active",
                "serial": "",
                "asset_tag": "",
            }
        ]
        workspace = plan_source_rows(self.rows, self.profile, self.site, actor=self.actor)
        self.device_unit = next(unit for unit in workspace.units if unit.object_type == "device")
        session = self.client.session
        record_recalculated_preview(session, workspace.plan)
        session["import_rows"] = workspace.source_rows
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
        }
        session["import_preview_pending"] = True
        session.save()

    def test_the_refused_row_still_carries_its_placement_baseline(self):
        """The unit states only a diagnostic, so the baseline has to reach the row another way."""
        self.assertEqual(self.device_unit.action, "error")
        self.assertEqual(self.device_unit.extra_data["identity_conflict"], "name_placement_conflict")
        self.assertEqual(self.device_unit.extra_data["netbox_device_id"], self.device.pk)

        baseline = self.device_unit.extra_data.get("_placement_state")

        self.assertEqual(baseline, {"rack_id": None, "position": "", "face": ""})

    def test_placement_sync_places_the_device_the_row_matched_by_name(self):
        """Nothing in NetBox changed, so the refusal must not claim that it did."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            {
                "row_number": 1,
                "preview_revision": self.client.session["import_preview_revision"],
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack_id, self.rack.pk)
        self.assertEqual(self.device.position, 2)
        self.assertEqual(self.device.face, "front")

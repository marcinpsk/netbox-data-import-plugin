# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for reviewed device field differences."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.device_field_review import DeviceFieldReviewer
from netbox_data_import.engine import run_import
from netbox_data_import.models import (
    ClassRoleMapping,
    DeviceExistingMatch,
    IgnoredFieldDifference,
    ImportProfile,
)
from netbox_data_import.tests.helpers import user_with_object_permission
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
            name="Field Review Profile", adapter_config={"update_existing": True, "create_missing_device_types": False}
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
        session["import_preview_revision"] = "current-preview-revision"
        session.save()

    def _save_rows(self, rows):
        """Replace source rows and materialize the preview a browser would show."""
        session = self.client.session
        session["import_rows"] = _serialize_rows(rows)
        session.save()
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))

    def _preview_device_row(self):
        """Return the current device preview row."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        return response, next(row for row in response.context["result"].rows if row.object_type == "device")

    def _json_action(self, **values):
        """Add the current materialized preview revision to one JSON action."""
        return {
            "preview_revision": self.client.session["import_preview_revision"],
            **values,
        }

    def _ignore_and_recalculate(self, target_field="u_position"):
        """Save one review and materialize its ignored state."""
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
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))

    def _cached_device_row(self, session):
        """Return the Device row stored in one materialized preview session."""
        return next(row for row in session["import_result"]["rows"] if row["object_type"] == "device")

    def _constrained_review_client(self):
        """A client that may bind the device but whose review permission excludes this field.

        That is the only way to reach the refusal: the view's own mixin already requires
        add_ignoredfielddifference, so a user without it never enters the transaction.
        """
        from dcim.models import Device

        grants = [
            (IgnoredFieldDifference, ["add"], {"target_field": "serial"}),
            (DeviceExistingMatch, ["add"], None),
            (Device, ["view"], None),
            (ImportProfile, ["view", "change"], None),
        ]
        user_with_object_permission("constrained-review-user", grants)

        client = Client()
        self.assertTrue(client.login(username="constrained-review-user", password="testpass"))
        session = client.session
        for key in ("import_rows", "import_context", "import_result", "import_preview_revision"):
            session[key] = self.client.session[key]
        session["import_preview_pending"] = True
        session.save()
        return client

    def test_a_refused_review_leaves_no_device_binding_behind(self):
        """The binding is written first, so refusing the review has to take it back."""
        DeviceExistingMatch.objects.filter(profile=self.profile).delete()
        client = self._constrained_review_client()

        response = client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())
        self.assertFalse(
            DeviceExistingMatch.objects.filter(profile=self.profile).exists(),
            "a refused field review must not leave the device binding behind",
        )

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
        self.assertContains(
            preview_response,
            'class="badge ndi-badge-ignored ndi-diff-toggle mt-1"',
        )

    def test_preview_offers_placement_sync_while_the_placement_differs(self):
        """The action stays available while a placement sync would write something."""
        response, row = self._preview_device_row()

        self.assertNotIn("placement_sync_writes_nothing", row.extra_data)
        self.assertContains(response, "ndi-sync-placement-btn")
        self.assertContains(response, "btn-outline-success ndi-sync-placement-btn")

    def test_preview_greys_out_a_placement_sync_that_would_write_nothing(self):
        """A matched placement keeps the button visible but inert, not green."""
        self.device.position = 7
        self.device.serial = "SERIAL-DIFFERS-SO-THE-ROW-STILL-EXPANDS"
        self.device.save(update_fields=["position", "serial"])

        response, row = self._preview_device_row()

        self.assertTrue(row.extra_data["placement_sync_writes_nothing"])
        self.assertContains(response, "This row sets no placement value NetBox does not already hold")
        self.assertNotContains(response, "ndi-sync-placement-btn")

    def test_preview_does_not_call_an_omitted_position_a_matching_placement(self):
        """The import clears a position the row omits, so the greyed button must not claim a match."""
        rows = [{**self.rows[0], "u_position": ""}]
        self._save_rows(rows)

        response, row = self._preview_device_row()

        self.assertTrue(row.extra_data["placement_sync_writes_nothing"])
        self.assertNotContains(response, "Placement already matches")
        self.assertEqual(row.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": ""})

        run_import(rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        self.device.refresh_from_db()
        self.assertIsNone(self.device.position)

    def test_preview_renders_field_differences_collapsed(self):
        """The row collapses through `hidden`, so it holds while the page is still loading."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertContains(response, '<tr id="diff-1" class="ndi-diff-row" hidden>')
        self.assertContains(response, "netbox_data_import/js/preview_row_controls.js")

    def test_ignore_defers_preview_recalculation_for_javascript_callers(self):
        """Ignore saves immediately and marks the displayed preview as stale."""
        previous_result = self.client.session["import_result"]

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["row_number"], 1)
        self.assertEqual(payload["preview_state"], "recalculation_required")
        self.assertNotIn("row_html", payload)
        session = self.client.session
        self.assertTrue(session["import_preview_dirty"])
        self.assertEqual(session["import_result"], previous_result)
        self.assertTrue(
            IgnoredFieldDifference.objects.filter(
                profile=self.profile,
                source_id="FIELD-REVIEW-ROW",
                netbox_device_id=self.device.pk,
                target_field="u_position",
            ).exists()
        )

    def test_ignore_rejects_a_changed_netbox_value_without_saving(self):
        """A stale preview cannot save a review for a different NetBox value."""
        self.device.position = 6
        self.device.save(update_fields=["position"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        self.assertIn("recalculate", response.json()["error"].lower())
        self.assertFalse(IgnoredFieldDifference.objects.exists())
        self.assertFalse(self.client.session.get("import_preview_dirty", False))

    def test_ignore_returns_json_when_the_difference_is_no_longer_present(self):
        """An asynchronous Ignore request receives the specific stale-row error."""
        session = self.client.session
        device_row = next(row for row in session["import_result"]["rows"] if row["object_type"] == "device")
        device_row["extra_data"]["field_diff"].pop("u_position")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "The selected field difference is no longer present. Refresh the preview."},
        )

    def test_ignore_returns_json_without_an_active_preview(self):
        """A row action rejects a request after its preview session is cleared."""
        action = self._json_action(
            profile_id=self.profile.pk,
            row_number=1,
            target_field="u_position",
        )
        session = self.client.session
        session.pop("import_preview_pending")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            action,
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_ignore_returns_json_after_the_profile_is_deleted(self):
        """A row action rejects a cached preview whose profile is unavailable."""
        action = self._json_action(
            profile_id=self.profile.pk,
            row_number=1,
            target_field="u_position",
        )
        self.profile.delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            action,
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_ignore_returns_json_when_the_cached_match_is_incomplete(self):
        """Ignore reports an incomplete cached match without returning HTML."""
        session = self.client.session
        device_row = next(row for row in session["import_result"]["rows"] if row["object_type"] == "device")
        device_row["extra_data"].pop("netbox_device_id")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no current matched device", response.json()["error"])

    def test_ignore_returns_json_when_the_matched_device_is_deleted(self):
        """Ignore rechecks that the previewed Device is still visible."""
        self.device.delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_ignore_returns_json_when_the_saved_binding_conflicts(self):
        """Ignore reports a changed source binding without returning HTML."""
        from dcim.models import Device

        replacement = Device.objects.create(
            name="field-review-conflicting-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").update(
            netbox_device_id=replacement.pk,
            device_name=replacement.name,
        )

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already linked", response.json()["error"])

    def test_ignore_rejects_a_device_linked_to_another_source(self):
        """A field review rejects a claimed Device without exposing the other source."""
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").delete()
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="OTHER-SOURCE-ROW",
            netbox_device_id=self.device.pk,
            device_name=self.device.name,
        )

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already linked", response.json()["error"])
        self.assertNotIn("OTHER-SOURCE-ROW", response.json()["error"])
        self.assertFalse(IgnoredFieldDifference.objects.exists())

    def test_ignore_rejects_a_stale_preview_revision(self):
        """A row action from an older browser preview cannot change state."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "preview_revision": "older-preview-revision",
                "row_number": 1,
                "target_field": "u_position",
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        self.assertIn("recalculate", response.json()["error"].lower())
        self.assertFalse(IgnoredFieldDifference.objects.exists())

    def test_ignore_rejects_an_invalid_row_number(self):
        """A malformed row identity cannot select a cached difference."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number="invalid",
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_ignore_rejects_an_unknown_target_field(self):
        """Only fields in the shared review registry can be ignored."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="unknown_field",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_placement_sync_uses_cached_intent_and_defers_recalculation(self):
        """Placement sync ignores forged values and marks the preview as stale."""
        previous_result = self.client.session["import_result"]
        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            self._json_action(
                device_id=self.device.pk,
                rack_name="forged-rack",
                u_position="11",
                face="rear",
                row_number=1,
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["row_number"], 1)
        self.assertEqual(payload["preview_state"], "recalculation_required")
        self.assertNotIn("row_html", payload)
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack_id, self.rack.pk)
        self.assertEqual(self.device.position, 7)
        self.assertEqual(self.device.face, "front")
        session = self.client.session
        self.assertTrue(session["import_preview_dirty"])
        self.assertEqual(session["import_result"], previous_result)

    def test_placement_sync_skips_the_position_of_a_zero_u_matched_device(self):
        """A matched zero-U device takes the rack alone: NetBox allows it no rack position."""
        from dcim.models import DeviceType

        zero_u_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="360-imV-CNTRLR",
            slug="360-imv-cntrlr",
            u_height=0,
        )
        self.device.device_type = zero_u_type
        self.device.rack = None
        self.device.position = None
        self.device.face = ""
        self.device.save(update_fields=["device_type", "rack", "position", "face"])
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            self._json_action(row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertIn("360-imV-CNTRLR", payload["message"])
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack_id, self.rack.pk)
        self.assertIsNone(self.device.position)
        self.assertFalse(self.device.face)

    def test_field_sync_uses_cached_intent_and_defers_recalculation(self):
        """Field sync writes the previewed value instead of posted client data."""
        previous_result = self.client.session["import_result"]

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(
                device_id=self.device.pk,
                field="u_position",
                value="11",
                row_number=1,
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["preview_state"], "recalculation_required")
        self.assertNotIn("row_html", payload)
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, 7)
        session = self.client.session
        self.assertTrue(session["import_preview_dirty"])
        self.assertEqual(session["import_result"], previous_result)

    def test_field_sync_applies_a_fractional_cached_position(self):
        """The field action preserves a valid half-U preview value."""
        self.rows[0]["u_position"] = "7.5"
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="u_position", row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"], response.json())
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, Decimal("7.5"))

    def test_placement_sync_applies_a_fractional_cached_position(self):
        """The placement action preserves a valid half-U preview value."""
        self.rows[0]["u_position"] = "7.5"
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            self._json_action(row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"], response.json())
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, Decimal("7.5"))

    def test_field_sync_rejects_a_serial_the_writer_would_truncate(self):
        """Sync refuses an overlong value instead of writing a different one than the preview showed."""
        overlong_serial = "S" * 60
        self.rows[0]["serial"] = overlong_serial
        self._save_rows(self.rows)

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="serial", row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"], payload)
        self.assertIn("50", payload["error"])
        self.device.refresh_from_db()
        self.assertEqual(self.device.serial, "FIELD-REVIEW-SERIAL")

    def test_field_sync_rejects_a_request_without_an_active_preview(self):
        """A field action cannot use client values after preview state is cleared."""
        action = self._json_action(field="u_position", row_number=1)
        session = self.client.session
        session.pop("import_preview_pending")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            action,
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_field_sync_rejects_an_invalid_row_number(self):
        """A malformed cached row identity cannot authorize a field write."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="u_position", row_number="invalid"),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_placement_sync_rejects_a_request_without_an_active_preview(self):
        """Placement cannot use posted values after preview state is cleared."""
        action = self._json_action(row_number=1)
        session = self.client.session
        session.pop("import_preview_pending")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            action,
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_field_sync_rejects_a_cached_row_without_a_device(self):
        """A cached row without a matched Device cannot authorize a field write."""
        session = self.client.session
        self._cached_device_row(session)["extra_data"].pop("netbox_device_id")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="u_position", row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_field_sync_rejects_a_deleted_matched_device(self):
        """A cached Device ID is rechecked before a field write."""
        self.device.delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="u_position", row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

    def test_field_sync_rejects_a_difference_removed_from_the_cached_row(self):
        """A field write requires the exact difference shown to the operator."""
        session = self.client.session
        self._cached_device_row(session)["extra_data"]["field_diff"].pop("u_position")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="u_position", row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer present", response.json()["error"])

    def test_field_sync_rejects_a_cached_difference_without_snapshots(self):
        """A field write requires the authoritative values saved with the preview."""
        session = self.client.session
        self._cached_device_row(session)["extra_data"]["field_review_snapshots"].pop("u_position")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="u_position", row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no authoritative", response.json()["error"])

    def test_field_sync_rejects_a_changed_netbox_value(self):
        """A changed baseline requires a new preview before a field write."""
        self.device.position = 6
        self.device.save(update_fields=["position"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            self._json_action(field="u_position", row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("changed", response.json()["error"])

    def test_placement_sync_rejects_a_cached_row_without_identity_state(self):
        """Placement needs the complete NetBox baseline stored by the preview."""
        session = self.client.session
        self._cached_device_row(session)["extra_data"].pop("_identity_state")
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            self._json_action(row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("placement changed", response.json()["error"])

    def test_placement_sync_rejects_a_changed_netbox_placement(self):
        """Placement cannot overwrite a Device changed after preview."""
        self.device.position = 6
        self.device.save(update_fields=["position"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            self._json_action(row_number=1),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("placement changed", response.json()["error"])

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
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))

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
        self.profile.adapter_config["create_missing_device_types"] = True
        self.profile.save(update_fields=["adapter_config"])
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

    def test_multiple_stale_review_devices_block_preview_and_execution(self):
        """A source row cannot choose between reviews bound to different Devices."""
        from dcim.models import Device

        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").delete()
        second_device = Device.objects.create(
            name="second-reviewed-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        for device in (self.device, second_device):
            IgnoredFieldDifference.objects.create(
                profile=self.profile,
                source_id="FIELD-REVIEW-ROW",
                netbox_device_id=device.pk,
                target_field="status",
                file_snapshot={"canonical": "offline", "display": "offline"},
                netbox_snapshot={"canonical": "active", "display": "active"},
            )
        self.rows[0].update(device_name="unmatched-reviewed-device", serial="")

        preview = run_import(self.rows, self.profile, {"site": self.site}, dry_run=True, user=self.user)
        execution = run_import(self.rows, self.profile, {"site": self.site}, dry_run=False, user=self.user)

        for result in (preview, execution):
            device_row = next(row for row in result.rows if row.object_type == "device")
            self.assertEqual(device_row.action, "error", device_row.to_dict())
            self.assertEqual(device_row.extra_data["identity_conflict"], "ambiguous_field_review")

    def test_one_stale_review_device_restores_the_matched_identity(self):
        """One field review keeps its Device match after source identity changes."""
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").delete()
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="FIELD-REVIEW-ROW",
            netbox_device_id=self.device.pk,
            target_field="status",
            file_snapshot={"canonical": "offline", "display": "offline"},
            netbox_snapshot={"canonical": "active", "display": "active"},
        )
        self.rows[0].update(device_name="renamed-reviewed-device", serial="")

        result = run_import(self.rows, self.profile, {"site": self.site}, dry_run=True, user=self.user)

        device_row = next(row for row in result.rows if row.object_type == "device")
        self.assertEqual(device_row.action, "update", device_row.to_dict())
        self.assertEqual(device_row.extra_data["netbox_device_id"], self.device.pk)

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
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))
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

    def test_field_registry_normalizes_unexpected_values_at_its_boundary(self):
        """The registry keeps snapshots stable for malformed source values."""
        malformed_position = object()

        field_diff = DeviceFieldReviewer.field_diff(
            self.device,
            {
                "u_position": malformed_position,
                "device_type": "legacy-type-value",
            },
        )
        informational = DeviceFieldReviewer.field_diff(
            self.device,
            {"device_name": "different-device-name"},
            include_informational=True,
        )

        self.assertEqual(field_diff["u_position"]["file"], str(malformed_position))
        self.assertEqual(field_diff["device_type"]["file"], "legacy-type-value")
        self.assertIn("device_name", informational)
        self.assertIsNone(DeviceFieldReviewer.current_snapshot(self.device, "unsupported"))

    def test_unignore_defers_preview_recalculation_for_javascript_callers(self):
        """Unignore saves immediately and leaves the materialized preview unchanged."""
        self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {
                "profile_id": self.profile.pk,
                "row_number": 1,
                "target_field": "u_position",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        previous_result = self.client.session["import_result"]

        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["preview_state"], "recalculation_required")
        self.assertNotIn("row_html", payload)
        session = self.client.session
        self.assertTrue(session["import_preview_dirty"])
        self.assertEqual(session["import_result"], previous_result)
        self.assertFalse(IgnoredFieldDifference.objects.exists())

    def test_unignore_rejects_an_unknown_target_field(self):
        """Unignore accepts only a current field from the shared registry."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="unknown_field",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_unignore_rejects_a_field_that_is_not_ignored(self):
        """Unignore cannot remove a review absent from the cached row."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_unignore_rejects_a_changed_source_binding(self):
        """Unignore does not preserve a review under a different Device binding."""
        from dcim.models import Device

        self._ignore_and_recalculate()
        replacement = Device.objects.create(
            name="unignore-binding-replacement",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="FIELD-REVIEW-ROW").update(
            netbox_device_id=replacement.pk,
            device_name=replacement.name,
        )

        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already linked", response.json()["error"])
        self.assertTrue(IgnoredFieldDifference.objects.exists())

    def test_unignore_returns_json_when_the_review_record_disappears(self):
        """Unignore reports a stale materialized review without returning HTML."""
        self._ignore_and_recalculate()
        IgnoredFieldDifference.objects.all().delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer current", response.json()["error"])

    def test_unignore_returns_json_when_the_matched_device_disappears(self):
        """Unignore reports a deleted matched Device without returning HTML."""
        self._ignore_and_recalculate()
        self.device.delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:unignore_field_difference"),
            self._json_action(
                profile_id=self.profile.pk,
                row_number=1,
                target_field="u_position",
            ),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.json()["error"])

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
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))

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
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))

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
        self.client.get(reverse("plugins:netbox_data_import:import_preview"))

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

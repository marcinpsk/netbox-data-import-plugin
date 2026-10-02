# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Preview row actions consume target-neutral Import Plans."""

import time
from contextlib import contextmanager
from threading import Event

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.import_engine import ImportEngine
from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceExistingMatch,
    IgnoredFieldDifference,
    ImportProfile,
)
from netbox_data_import.review_workspace import ReviewWorkspace
from netbox_data_import.tests.helpers import (
    apply_source_rows,
    plan_source_rows,
    preview_claim,
    run_on_separate_connection,
    seed_preview,
    store_plan,
    store_workbook_document,
    stored_plan,
    user_with_object_permission,
)

# The first data row of a stored workbook is row 2, under its header.
ROW = 2
WORKBOOK_FIELDS = (
    "source_id",
    "device_name",
    "device_class",
    "rack_name",
    "make",
    "model",
    "u_position",
    "face",
    "status",
    "serial",
    "asset_tag",
)


def map_workbook_fields(profile):
    """Map one workbook column to each canonical field the rows below carry."""
    for field in WORKBOOK_FIELDS:
        ColumnMapping.objects.create(profile=profile, source_column=field, target_field=field)


def preview_rows(client, profile, site, rows):
    """Store a workbook of these rows, plan it as the client's operator, and make it the preview."""
    actor = get_user_model().objects.get(pk=client.session["_auth_user_id"])
    document = store_workbook_document(
        profile,
        list(WORKBOOK_FIELDS),
        [[row.get(field, "") for field in WORKBOOK_FIELDS] for row in rows],
        actor,
        "r.xlsx",
    )
    context = {"site_id": site.pk, "location_id": None, "tenant_id": None}
    plan = ImportEngine.plan(profile, document, actor, context)
    seed_preview(client, profile=profile, document=document, plan=plan, context=context)
    return ReviewWorkspace(plan, actor)


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
            adapter_config={"sheet_name": "Data", "update_existing": True},
        )
        map_workbook_fields(self.profile)
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

    def _materialize(self, *, expect_ignored=False, client=None):
        """Store the rows as a workbook and make its plan the client's active preview."""
        workspace = preview_rows(client or self.client, self.profile, self.site, self.rows)
        self.assertEqual(self._bucket(workspace, expect_ignored)["u_position"], {"netbox": "5", "file": "7"})

    def _bucket(self, workspace, ignored):
        """Return the field differences or the ignored fields of the Device row."""
        device_unit = next(unit for unit in workspace.units if unit.object_type == "device")
        self.assertEqual(device_unit.action, "update", device_unit)
        self.assertEqual(device_unit.row_number, ROW)
        return device_unit.extra_data["field_ignored" if ignored else "field_diff"]

    def _planning_view_grants(self):
        """Return the view grants an operator needs to plan the rows against the import site."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        return [(model, ("view",), None) for model in (Site, Rack, Manufacturer, DeviceType, DeviceRole)]

    def _stored_workspace(self):
        """Return the plan the client's preview now holds."""
        from netbox_data_import.plan import ImportPlan

        return ReviewWorkspace(ImportPlan.from_dict(stored_plan(self.client)), self.actor)

    def test_planning_requires_an_actor_before_it_reads_rows(self):
        with self.assertRaisesRegex(TypeError, "actor"):
            plan_source_rows(self.rows, self.profile, self.site)

    def test_applying_requires_an_actor_before_it_reads_rows(self):
        with self.assertRaisesRegex(TypeError, "actor"):
            apply_source_rows(self.rows, self.profile, self.site)

    def test_the_row_reports_the_fields_netbox_already_holds(self):
        """The preview states what stays the same, so a sync is not read as rewriting every field."""
        workspace = plan_source_rows(self.rows, self.profile, self.site, actor=self.actor)
        device_unit = next(unit for unit in workspace.units if unit.object_type == "device")

        matching = device_unit.extra_data["field_matching"]

        self.assertEqual(matching["serial"], {"netbox": self.device.serial, "file": self.device.serial})
        self.assertEqual(matching["rack_name"], {"netbox": self.rack.name, "file": self.rack.name})
        self.assertEqual(matching["face"], {"netbox": "front", "file": "front"})
        # The one real difference stays out of the matching map.
        self.assertNotIn("u_position", matching)
        self.assertEqual(device_unit.extra_data["field_diff"]["u_position"], {"netbox": "5", "file": "7"})

    def _post(self, view_name, target_field="u_position"):
        """Post one JSON row action against the current preview revision."""
        return self.client.post(
            reverse(f"plugins:netbox_data_import:{view_name}"),
            {**preview_claim(self.client), "row_number": ROW, "target_field": target_field},
            HTTP_ACCEPT="application/json",
        )

    def _sync_field(self, field="u_position"):
        """Post one inline field sync against the current preview revision."""
        return self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            {**preview_claim(self.client), "row_number": ROW, "field": field},
            HTTP_ACCEPT="application/json",
        )

    def _sync_placement(self):
        """Post one inline placement sync against the current preview revision."""
        return self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            {**preview_claim(self.client), "row_number": ROW},
            HTTP_ACCEPT="application/json",
        )

    def _tamper_device_unit(self, change):
        """Change the serialized Device unit the preview stores, as a stale plan would carry it."""
        plan = stored_plan(self.client)
        change(next(item for item in plan["units"] if item["identity"].startswith("device:")))
        store_plan(self.client, plan)

    def _discard(self):
        """End the preview and return the claim the page held before."""
        claim = preview_claim(self.client)
        self.client.post(reverse("plugins:netbox_data_import:preview_discard"), claim)
        return claim

    def _ignore_and_replan(self):
        """Save one review; the command replans, so the stored plan shows it ignored."""
        response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._bucket(self._stored_workspace(), True)["u_position"], {"netbox": "5", "file": "7"})

    def test_ignore_and_unignore_round_trip(self):
        """A saved review moves through two fresh plans without legacy result rows."""
        ignored = self._post("ignore_field_difference")

        self.assertEqual(ignored.status_code, 200)
        self.assertTrue(ignored.json()["ok"])
        self.assertEqual(ignored.json()["preview_state"], "replanned")
        self.assertTrue(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())
        self.assertIn("u_position", self._bucket(self._stored_workspace(), True))

        restored = self._post("unignore_field_difference")

        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["ok"])
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())
        self.assertIn("u_position", self._bucket(self._stored_workspace(), False))

    def test_ignore_rejects_absent_and_malformed_preview_rows(self):
        """An absent preview, malformed row number, or unknown field cannot authorize a review."""
        ended = self._discard()
        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_field_difference"),
            {**ended, "row_number": ROW, "target_field": "u_position"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 409)

        self._materialize()
        self.assertEqual(
            self.client.post(
                reverse("plugins:netbox_data_import:ignore_field_difference"),
                {**preview_claim(self.client), "row_number": "invalid", "target_field": "u_position"},
                HTTP_ACCEPT="application/json",
            ).status_code,
            400,
        )
        self.assertEqual(self._post("ignore_field_difference", "unknown").status_code, 409)

    def test_ignore_rejects_a_cached_plan_that_cannot_be_deserialized(self):
        """A stale cached schema follows the normal unavailable-preview response path."""
        plan = stored_plan(self.client)
        plan["schema_version"] = 999
        store_plan(self.client, plan)

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "This preview cannot be read. Re-read the preview.")
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_ignore_rejects_a_difference_removed_from_the_plan(self):
        """The action refuses a field that the accepted plan no longer offers."""
        self._tamper_device_unit(lambda unit: unit["display"]["extra_data"]["field_diff"].pop("u_position"))

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer present", response.json()["error"])

    def test_ignore_rejects_missing_snapshots_and_a_deleted_device(self):
        """A review requires both authoritative snapshots and its visible Device."""
        self._tamper_device_unit(lambda unit: unit["display"]["extra_data"]["field_review_snapshots"].pop("u_position"))
        self.assertEqual(self._post("ignore_field_difference").status_code, 409)

        self._materialize()
        self.device.delete()
        response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"], "The selected field difference is no longer present. Re-read the preview."
        )

    def test_ignore_rejects_a_changed_netbox_baseline(self):
        """A Device change after planning invalidates the review snapshot."""
        self.device.position = 6
        self.device.save(update_fields=["position"])

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertIn("value changed", response.json()["error"])

    @contextmanager
    def _bindings_changed_at_the_locked_read(self, change):
        """Commit *change* on another connection just before the command locks its binding rows.

        The policy check has passed by then, so only the locked read can see the change.
        """
        from django.db import connection

        changed = []

        def change_before_the_locked_read(execute, sql, params, many, context):
            if not changed and "FOR UPDATE" in sql and DeviceExistingMatch._meta.db_table in sql:
                changed.append(True)
                with run_on_separate_connection(change):
                    pass
            return execute(sql, params, many, context)

        with connection.execute_wrapper(change_before_the_locked_read):
            yield
        self.assertEqual(changed, [True], "the binding rows were never locked")

    def test_ignore_rejects_conflicting_device_bindings(self):
        """A field review cannot move a source binding or reuse another source's Device."""
        from dcim.models import Device

        replacement = Device.objects.create(
            name="review-action-replacement",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )

        def move_the_binding():
            DeviceExistingMatch.objects.filter(profile=self.profile, source_id="REVIEW-ACTION-ROW").update(
                netbox_device_id=replacement.pk,
                device_name=replacement.name,
            )

        with self._bindings_changed_at_the_locked_read(move_the_binding):
            response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 409)
        self.assertIn("linked elsewhere", response.json()["error"])

        DeviceExistingMatch.objects.filter(profile=self.profile).delete()
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="OTHER-ROW",
            netbox_device_id=replacement.pk,
            device_name=replacement.name,
        )
        self.client.post(reverse("plugins:netbox_data_import:preview_reread"), preview_claim(self.client))

        def link_another_row():
            DeviceExistingMatch.objects.filter(profile=self.profile, source_id="OTHER-ROW").update(
                netbox_device_id=self.device.pk, device_name=self.device.name
            )

        with self._bindings_changed_at_the_locked_read(link_another_row):
            response = self._post("ignore_field_difference")
        self.assertEqual(response.status_code, 409)
        self.assertIn("linked elsewhere", response.json()["error"])
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_a_binding_changed_after_planning_is_a_moved_policy(self):
        """A binding written outside the preview moves the profile policy, so the command is refused."""
        DeviceExistingMatch.objects.filter(profile=self.profile).update(device_name="renamed-elsewhere")

        response = self._post("ignore_field_difference")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "preview_stale")
        self.assertIn("policy changed", response.json()["error"])
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_ignore_sanitizes_a_real_object_permission_failure(self):
        """A constrained add permission rolls back and returns a bounded row-action error."""
        from dcim.models import Device

        actor = user_with_object_permission(
            "review-action-denied",
            [
                (ImportProfile, ("change",), {"pk": self.profile.pk}),
                (Device, ("view",), {"pk": self.device.pk}),
                (IgnoredFieldDifference, ("add",), {"source_id": "OTHER-ROW"}),
                *self._planning_view_grants(),
            ],
        )
        self.client.force_login(actor)
        # Without dcim.change_device the row is refused, and its difference is still offered for review.
        preview_rows(self.client, self.profile, self.site, self.rows)

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
        self.assertEqual(
            response.json()["error"], "The selected field review is no longer current. Re-read the preview."
        )

    def test_unignore_rejects_a_real_concurrent_binding_change(self):
        """Unignore preserves its record if the source binding moves before its locked read."""
        from dcim.models import Device

        self._ignore_and_replan()
        replacement = Device.objects.create(
            name="review-action-unignore-replacement",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )

        def move_binding():
            DeviceExistingMatch.objects.filter(
                profile=self.profile,
                source_id="REVIEW-ACTION-ROW",
            ).update(netbox_device_id=replacement.pk, device_name=replacement.name)

        with self._bindings_changed_at_the_locked_read(move_binding):
            response = self._post("unignore_field_difference")

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
        self.assertEqual(response.json()["preview_state"], "replanned")
        device_unit = next(unit for unit in self._stored_workspace().units if unit.object_type == "device")
        self.assertNotIn("u_position", device_unit.extra_data.get("field_diff", {}))

    def test_inline_field_sync_rejects_stale_plan_state(self):
        """An absent row, removed difference, missing snapshot, and changed Device are refused."""
        ended = self._discard()
        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_device_field"),
            {**ended, "row_number": ROW, "field": "u_position"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 409)

        self._materialize()
        self._tamper_device_unit(lambda unit: unit["display"]["extra_data"]["field_diff"].pop("u_position"))
        self.assertIn("no longer present", self._sync_field().json()["error"])

        self._materialize()
        self._tamper_device_unit(lambda unit: unit["display"]["extra_data"]["field_review_snapshots"].pop("u_position"))
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

    def _wait_for_a_backend_blocked_on_a_lock(self, timeout=15):
        """Return whether another backend on this database is waiting for a lock."""
        from django.db import connection

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock'"
                )
                if cursor.fetchone()[0]:
                    return True
            time.sleep(0.05)
        return False

    def test_inline_placement_sync_cannot_overwrite_a_concurrent_move(self):
        """The unlocked baseline check is stale by the time the write runs, so recheck under the lock."""
        from dcim.models import Device

        moved = Event()
        release = Event()
        answered = {}

        def move_the_device_and_hold_the_lock():
            with transaction.atomic():
                locked = Device.objects.select_for_update().get(pk=self.device.pk)
                locked.position = 21
                locked.save(update_fields=["position"])
                moved.set()
                # Commit only once the request waits on this row, so its baseline read is already stale.
                release.wait(timeout=15)

        def sync_the_previewed_placement():
            answered["response"] = self._sync_placement()

        with run_on_separate_connection(move_the_device_and_hold_the_lock):
            self.assertTrue(moved.wait(timeout=15), "the concurrent move never started")
            with run_on_separate_connection(sync_the_previewed_placement):
                blocked = self._wait_for_a_backend_blocked_on_a_lock()
                release.set()
                self.assertTrue(blocked, "the sync never blocked on the moved device's row lock")

        response = answered["response"]
        self.assertEqual(response.status_code, 409, response.content[:300])
        self.assertIn("placement changed", response.json()["error"])
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, 21, "the sync overwrote a placement another writer had moved")

    def test_inline_placement_sync_rejects_an_absent_preview_row(self):
        """A cleared preview cannot authorize placement from client-supplied data."""
        ended = self._discard()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            {**ended, "row_number": ROW},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "preview_stale")
        self.device.refresh_from_db()
        self.assertEqual(self.device.position, 5)

    def test_unlink_removes_the_binding_and_its_dependent_field_reviews(self):
        """A source link and all reviews scoped by it are removed together."""
        self._ignore_and_replan()

        response = self.client.post(
            reverse("plugins:netbox_data_import:unlink_device"),
            {**preview_claim(self.client), "source_id": "REVIEW-ACTION-ROW"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile).exists())
        self.assertFalse(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())

    def test_unlink_checks_the_binding_and_each_dependent_review_permission(self):
        """Object-scoped denial preserves the whole source-to-device review state."""
        self._ignore_and_replan()
        endpoint = reverse("plugins:netbox_data_import:unlink_device")
        data = {"source_id": "REVIEW-ACTION-ROW"}
        binding_denied = user_with_object_permission(
            "review-action-binding-denied",
            [
                (ImportProfile, ("change",), None),
                (DeviceExistingMatch, ("delete",), {"source_id": "OTHER-ROW"}),
                (IgnoredFieldDifference, ("delete",), None),
                *self._planning_view_grants(),
            ],
        )
        binding_client = Client()
        binding_client.force_login(binding_denied)
        preview_rows(binding_client, self.profile, self.site, self.rows)

        self.assertEqual(binding_client.post(endpoint, {**preview_claim(binding_client), **data}).status_code, 302)
        self.assertTrue(DeviceExistingMatch.objects.filter(profile=self.profile).exists())

        review_denied = user_with_object_permission(
            "review-action-review-denied",
            [
                (ImportProfile, ("change",), None),
                (DeviceExistingMatch, ("delete",), None),
                (IgnoredFieldDifference, ("delete",), {"source_id": "OTHER-ROW"}),
                *self._planning_view_grants(),
            ],
        )
        review_client = Client()
        review_client.force_login(review_denied)
        preview_rows(review_client, self.profile, self.site, self.rows)

        self.assertEqual(review_client.post(endpoint, {**preview_claim(review_client), **data}).status_code, 302)
        self.assertTrue(DeviceExistingMatch.objects.filter(profile=self.profile).exists())
        self.assertTrue(IgnoredFieldDifference.objects.filter(profile=self.profile).exists())


class PlacementRackScopeTest(TestCase):
    """The placement write resolves a Rack by name, so it has to honour the operator's view scope."""

    def setUp(self):
        """Create one Rack the operator may see and one it may not."""
        from dcim.models import Rack, Site

        self.site = Site.objects.create(name="Rack Scope Site", slug="rack-scope-site")
        self.visible = Rack.objects.create(name="scope-visible", site=self.site, u_height=42)
        self.hidden = Rack.objects.create(name="scope-hidden", site=self.site, u_height=42)
        self.user = user_with_object_permission(
            "rack-scope-operator",
            [(Rack, ("view",), {"name": "scope-visible"})],
        )

    def _device(self):
        """Return an unsaved Device at the site, which is all the rack lookup reads."""
        from dcim.models import Device

        return Device(site=self.site)

    def test_the_rack_lookup_honours_the_actor_view_scope(self):
        """A Rack outside the operator's scope must not be bound, and its name must not leak."""
        from netbox_data_import.views import _lookup_rack_for_device

        found, error = _lookup_rack_for_device(self.user, self._device(), "scope-hidden")

        self.assertIsNone(found, "the lookup bound a Rack the operator cannot see")
        self.assertIn("not found", error)

    def test_the_rack_lookup_still_finds_a_rack_in_scope(self):
        """The scoping must not refuse the Rack the operator is allowed to use."""
        from netbox_data_import.views import _lookup_rack_for_device

        found, error = _lookup_rack_for_device(self.user, self._device(), "scope-visible")

        self.assertIsNone(error)
        self.assertEqual(found, self.visible)


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
            adapter_config={"sheet_name": "Data", "update_existing": True},
        )
        map_workbook_fields(self.profile)
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
        workspace = preview_rows(self.client, self.profile, self.site, self.rows)
        self.device_unit = next(unit for unit in workspace.units if unit.object_type == "device")

    def test_the_refused_row_still_carries_its_placement_baseline(self):
        """The unit states only a diagnostic, so the baseline has to reach the row another way."""
        self.assertEqual(self.device_unit.action, "error")
        self.assertEqual(self.device_unit.extra_data["identity_conflict"], "name_placement_conflict")
        self.assertEqual(self.device_unit.extra_data["netbox_device_id"], self.device.pk)

        baseline = self.device_unit.extra_data.get("_placement_state")

        self.assertEqual(baseline, {"location_id": None, "rack_id": None, "position": "", "face": ""})

    def test_placement_sync_refuses_a_device_that_moved_to_another_location(self):
        """Location is placement state, so a Device that moved has to fail the locked recheck."""
        from dcim.models import Device, Location, Rack

        other = Location.objects.create(name="Other Hall", slug="other-hall", site=self.site)
        decoy = Rack.objects.create(name=self.rack.name, site=self.site, location=other, u_height=42)
        self.device.location = other
        self.device.save(update_fields=["location"])

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            {**preview_claim(self.client), "row_number": ROW},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content[:300])
        self.device.refresh_from_db()
        self.assertIsNone(self.device.rack_id, "the sync placed the Device in another location's rack")
        self.assertFalse(Device.objects.filter(rack=decoy).exists())

    def test_placement_sync_places_the_device_the_row_matched_by_name(self):
        """Nothing in NetBox changed, so the refusal must not claim that it did."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_placement"),
            {**preview_claim(self.client), "row_number": ROW},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack_id, self.rack.pk)
        self.assertEqual(self.device.position, 2)
        self.assertEqual(self.device.face, "front")

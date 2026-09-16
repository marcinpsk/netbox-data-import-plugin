# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Row actions that settle a rack position two source rows both claim."""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile, SourceResolution
from netbox_data_import.preview_row_actions import PREVIEW_REVISION_SESSION_KEY
from netbox_data_import.tests.helpers import workbook_bytes
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin

HEADERS = ["Source ID", "Class", "Name", "Rack", "Make", "Model", "Position", "Face"]


def _workbook(rows) -> bytes:
    return workbook_bytes(HEADERS, rows)


class RackPositionConflictActionTest(IsolatedRQQueueTestMixin, TransactionTestCase):
    """Both rows in a rack-position collision must offer a way out."""

    def setUp(self):
        """Create the actor, profile, mappings, rack, and device type."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.actor = get_user_model().objects.create_superuser(
            username="conflict-operator",
            email="conflict@example.invalid",
            password="testpass",
        )
        self.client = Client()
        self.client.force_login(self.actor)
        self.site = Site.objects.create(name="Conflict Site", slug="conflict-site")
        self.rack = Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        manufacturer = Manufacturer.objects.create(name="Example", slug="example")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Model", slug="example-model", u_height=1
        )
        self.role = DeviceRole.objects.create(name="Server", slug="server")
        self.profile = ImportProfile.objects.create(
            name="Conflict Profile",
            adapter_config={"sheet_name": "Data", "update_existing": True, "source_id_column": "Source ID"},
        )
        for source_column, target_field in (
            ("Source ID", "source_id"),
            ("Class", "device_class"),
            ("Name", "device_name"),
            ("Rack", "rack_name"),
            ("Make", "make"),
            ("Model", "model"),
            ("Position", "u_position"),
            ("Face", "face"),
        ):
            ColumnMapping.objects.create(profile=self.profile, source_column=source_column, target_field=target_field)
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug="server")

    def _upload(self, rows):
        """Upload a workbook and land on the preview."""
        upload = SimpleUploadedFile(
            "conflict.xlsx",
            _workbook(rows),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
        )

    def _colliding_rows(self):
        """Two rows that both ask for U5 front in the same rack."""
        return [
            ["S-1", "Server", "srv-01", "rack-a", "Example", "Model", "5", "Front"],
            ["S-2", "Server", "srv-02", "rack-a", "Example", "Model", "5", "Front"],
        ]

    def _preview_rows(self):
        """Return the preview rows the template renders, with conflict comparisons attached."""
        from netbox_data_import.plan import ImportPlan
        from netbox_data_import.preview_row_actions import PREVIEW_PLAN_SESSION_KEY
        from netbox_data_import.review_workspace import ReviewWorkspace
        from netbox_data_import.views import _preview_rows_with_conflict_comparisons

        plan = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        workspace = ReviewWorkspace(plan)
        source_rows = [
            {"_row_number": 2, "source_id": "S-1", "u_position": 5, "face": "Front", "rack_name": "rack-a"},
            {"_row_number": 3, "source_id": "S-2", "u_position": 5, "face": "Front", "rack_name": "rack-a"},
        ]
        return _preview_rows_with_conflict_comparisons(workspace, source_rows, self.profile)

    def test_both_rows_in_the_collision_offer_a_way_out(self):
        """The refused row and the row that took the slot both need an action."""
        self._upload(self._colliding_rows())

        rows = {row.row_number: row for row in self._preview_rows() if row.object_type == "device"}
        refused = rows[3]
        self.assertEqual(refused.action, "error", refused.detail)
        self.assertIn("ignore_position", refused.extra_data["offered_actions"])
        self.assertIn("ignore_row", refused.extra_data["offered_actions"])

        comparison = {entry["row_number"]: entry for entry in refused.extra_data["conflict_rows"]}
        self.assertEqual(set(comparison), {2, 3})
        for row_number, entry in comparison.items():
            self.assertIn("ignore_position", entry["offered_actions"], f"row {row_number} offers no position action")
            self.assertIn("ignore_row", entry["offered_actions"], f"row {row_number} offers no ignore action")

    def test_ignoring_the_position_lets_the_other_row_keep_the_slot(self):
        """The row that gives up its position still imports, into the rack but unplaced."""
        self._upload(self._colliding_rows())

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_position"),
            {
                "profile_id": self.profile.pk,
                "source_id": "S-2",
                "row_number": 3,
                "preview_revision": self.client.session.get(PREVIEW_REVISION_SESSION_KEY),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertIn(response.status_code, (200, 302), getattr(response, "content", b"")[:300])

        saved = SourceResolution.objects.get(profile=self.profile, source_id="S-2", source_column="u_position")
        self.assertIsNone(saved.resolved_fields["u_position"])

        self._upload(self._colliding_rows())
        rows = {row.row_number: row for row in self._preview_rows() if row.object_type == "device"}
        self.assertNotEqual(rows[3].action, "error", rows[3].detail)
        self.assertEqual(rows[2].action, "create", rows[2].detail)

    def test_the_preview_page_renders_both_actions_for_both_rows(self):
        """The Resolve column must draw the buttons, not just carry the action names."""
        self._upload(self._colliding_rows())

        page = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        # Count the button labels, not the words: each title attribute repeats them.
        self.assertEqual(body.count("</i> Ignore position"), 2, "both rows in the collision need the position action")
        self.assertEqual(body.count("</i> Ignore row"), 2, "both rows in the collision need the ignore action")
        self.assertIn(reverse("plugins:netbox_data_import:ignore_position"), body)

    def test_ignoring_the_row_removes_it_from_the_import(self):
        """The operator can drop the colliding row outright instead of only its position."""
        from netbox_data_import.models import IgnoredDevice

        self._upload(self._colliding_rows())

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_device"),
            {
                "profile_id": self.profile.pk,
                "source_id": "S-2",
                "device_name": "srv-02",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(IgnoredDevice.objects.filter(profile=self.profile, source_id="S-2").exists())

        self._upload(self._colliding_rows())
        rows = {row.row_number: row for row in self._preview_rows() if row.object_type == "device"}
        self.assertEqual(rows[3].action, "ignore", rows[3].detail)
        self.assertEqual(rows[2].action, "create", rows[2].detail)

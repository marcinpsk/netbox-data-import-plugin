# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The server refuses a split that replaces a value the row carries, unless the operator acknowledged it."""

import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile, SourceResolution
from netbox_data_import.preview_row_actions import PREVIEW_PLAN_SESSION_KEY, PREVIEW_REVISION_SESSION_KEY
from netbox_data_import.tests.helpers import workbook_bytes

CAPITAL_SHARP = "STRAẞE"
SHARP = "Straße"
NAME = f"{SHARP} - host-900"


class SplitReplacementAcknowledgementTest(TestCase):
    """The row carries asset tag `STRAẞE` and serial `sn900`, and a split proposes other values."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        cls.actor = get_user_model().objects.create_superuser("split-ack", "split@example.invalid", "x")
        cls.site = Site.objects.create(name="Split Site", slug="split-site")
        DeviceRole.objects.create(name="Server", slug="server")
        manufacturer = Manufacturer.objects.create(name="Acme", slug="acme")
        DeviceType.objects.create(manufacturer=manufacturer, model="Widget", slug="acme-widget", u_height=1)
        cls.profile = ImportProfile.objects.create(name="Split Acknowledgement", adapter_config={"sheet_name": "Data"})
        for source_column, target_field in (
            ("Source ID", "source_id"),
            ("Class", "device_class"),
            ("Name", "device_name"),
            ("Asset", "asset_tag"),
            ("Serial", "serial"),
            ("Make", "make"),
            ("Model", "model"),
        ):
            ColumnMapping.objects.create(profile=cls.profile, source_column=source_column, target_field=target_field)
        ClassRoleMapping.objects.create(profile=cls.profile, source_class="Server", role_slug="server")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.actor)
        upload = SimpleUploadedFile(
            "split.xlsx",
            workbook_bytes(
                ["Source ID", "Class", "Name", "Asset", "Serial", "Make", "Model"],
                [["D-1", "Server", NAME, CAPITAL_SHARP, "sn900", "Acme", "Widget"]],
            ),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
        )
        self.assertEqual(setup.status_code, 302, setup.content[:300])
        preview = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(
            preview.context["split_field_values_by_source_id"]["D-1"]["asset_tag"], CAPITAL_SHARP, "fixture"
        )

    def save(self, resolved_fields, acknowledged=None):
        data = {
            "profile_id": self.profile.pk,
            "source_id": "D-1",
            "source_column": "device_name",
            "original_value": NAME,
            "resolved_fields": json.dumps(resolved_fields),
            "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
        }
        if acknowledged is not None:
            data["acknowledged_fields"] = json.dumps(acknowledged)
        return self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"), data, headers={"accept": "application/json"}
        )

    def saved(self):
        return SourceResolution.objects.filter(profile=self.profile, source_id="D-1").exists()

    def test_an_asset_tag_of_another_identity_needs_the_acknowledgement(self):
        refused = self.save({"asset_tag": SHARP, "device_name": "host-900"})

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertEqual(
            refused.json()["error"],
            f"The split replaces the Asset tag '{CAPITAL_SHARP}' with '{SHARP}'. Acknowledge the replacement to "
            "save it.",
        )
        self.assertFalse(self.saved())

        accepted = self.save({"asset_tag": SHARP, "device_name": "host-900"}, acknowledged=["asset_tag"])

        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertTrue(self.saved())

    def test_an_asset_tag_of_the_same_identity_needs_no_acknowledgement(self):
        accepted = self.save({"asset_tag": " straẞe ", "device_name": "host-900"})

        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertTrue(self.saved())

    def test_a_serial_that_differs_only_in_case_needs_the_acknowledgement(self):
        refused = self.save({"serial": "SN900", "device_name": "host-900"})

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertFalse(self.saved())
        self.assertEqual(self.save({"serial": "SN900"}, acknowledged=["serial"]).status_code, 200)

    def test_a_split_cannot_use_another_profiles_preview(self):
        other = ImportProfile.objects.create(name="Other Split Profile")
        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": other.pk,
                "source_id": "D-1",
                "source_column": "device_name",
                "resolved_fields": json.dumps({"asset_tag": CAPITAL_SHARP}),
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
            headers={"accept": "application/json"},
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "The selected profile is not the active import profile.")
        self.assertFalse(SourceResolution.objects.filter(profile=other).exists())

    def test_a_split_requires_one_source_row_in_an_active_preview(self):
        row = self.client.session[PREVIEW_PLAN_SESSION_KEY]["units"][0]
        for count in (0, 2):
            with self.subTest(count=count):
                session = self.client.session
                plan = session[PREVIEW_PLAN_SESSION_KEY]
                plan["units"] = [] if count == 0 else [row, {**row, "identity": row["identity"] + ":duplicate"}]
                session[PREVIEW_PLAN_SESSION_KEY] = plan
                session.save()
                response = self.save({"asset_tag": CAPITAL_SHARP})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["error"], "The source ID must identify one active import row.")
                self.assertFalse(self.saved())

    def test_a_preview_bound_split_requires_a_readable_plan(self):
        for plan in (None, {"units": "invalid"}):
            with self.subTest(plan=plan):
                session = self.client.session
                session[PREVIEW_PLAN_SESSION_KEY] = plan
                session.save()
                response = self.save({"asset_tag": CAPITAL_SHARP})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["error"], "The active Import Plan is no longer readable.")
                self.assertFalse(self.saved())

    def test_a_standalone_resolution_can_save_without_preview_values(self):
        session = self.client.session
        for key in tuple(session.keys()):
            if key.startswith("import_"):
                del session[key]
        session.save()
        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "D-1",
                "source_column": "device_name",
                "resolved_fields": json.dumps({"asset_tag": SHARP}),
            },
            headers={"accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.saved())

    def test_a_malformed_acknowledgement_is_refused(self):
        refused = self.save({"asset_tag": SHARP}, acknowledged={"asset_tag": True})

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertFalse(self.saved())

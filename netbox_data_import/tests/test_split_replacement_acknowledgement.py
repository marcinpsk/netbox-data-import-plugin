# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The server refuses a split that replaces a value the row carries, unless the operator acknowledged it."""

import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile, SourceResolution
from netbox_data_import.tests.helpers import preview_claim, upload_preview, workbook_bytes

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
        setup = upload_preview(self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})
        self.assertEqual(setup.status_code, 302, setup.content[:300])
        preview = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(
            preview.context["split_field_values_by_source_id"]["D-1"]["asset_tag"], CAPITAL_SHARP, "fixture"
        )

    def save(self, resolved_fields, acknowledged=None, claim=None):
        data = {
            **(claim or preview_claim(self.client)),
            "source_id": "D-1",
            "source_column": "device_name",
            "original_value": NAME,
            "resolved_fields": json.dumps(resolved_fields),
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

    def test_a_malformed_acknowledgement_is_refused(self):
        refused = self.save({"asset_tag": SHARP}, acknowledged={"asset_tag": True})

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertFalse(self.saved())

    def test_a_second_split_compares_against_the_value_the_first_split_saved(self):
        old_claim = preview_claim(self.client)
        first = self.save({"asset_tag": SHARP, "device_name": "host-900"}, acknowledged=["asset_tag"])
        self.assertEqual(first.status_code, 200, first.content)
        preview = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(preview.context["split_field_values_by_source_id"]["D-1"]["asset_tag"], SHARP, "fixture")

        refused = self.save({"asset_tag": CAPITAL_SHARP, "device_name": "host-900"})

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertEqual(
            refused.json()["error"],
            f"The split replaces the Asset tag '{SHARP}' with '{CAPITAL_SHARP}'. Acknowledge the replacement to "
            "save it.",
        )
        self.assertEqual(
            SourceResolution.objects.get(profile=self.profile, source_id="D-1").resolved_fields["asset_tag"], SHARP
        )

        stale = self.save({"asset_tag": CAPITAL_SHARP, "device_name": "host-900"}, claim=old_claim)

        self.assertEqual(stale.status_code, 409, stale.content)
        self.assertEqual(stale.json()["code"], "preview_stale")
        self.assertEqual(
            SourceResolution.objects.get(profile=self.profile, source_id="D-1").resolved_fields["asset_tag"], SHARP
        )

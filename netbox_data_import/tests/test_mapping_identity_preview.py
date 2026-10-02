# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Device Type and Manufacturer mappings that share one name identity, seen through the flat import preview."""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceTypeMapping,
    ImportProfile,
    ManufacturerMapping,
)
from netbox_data_import.review_workspace import _DIAGNOSTIC_MESSAGES
from netbox_data_import.tests.helpers import upload_preview, workbook_bytes

DOTLESS_I = "\u0131"


class MappingIdentityPreviewTest(TestCase):
    """`I` and the dotless i share one identity, so two mappings for them are one mapping or a conflict."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        cls.actor = get_user_model().objects.create_superuser("mapping-identity", "mapping@example.invalid", "x")
        cls.site = Site.objects.create(name="Mapping Site", slug="mapping-site")
        DeviceRole.objects.create(name="Server", slug="server")
        for slug in ("maker-i", "maker-dotless"):
            manufacturer = Manufacturer.objects.create(name=slug, slug=slug)
            for model in ("type-x", "type-y"):
                DeviceType.objects.create(manufacturer=manufacturer, model=model, slug=model, u_height=1)
        cls.profile = ImportProfile.objects.create(name="Mapping Identity", adapter_config={"sheet_name": "Data"})
        for source_column, target_field in (
            ("Source ID", "source_id"),
            ("Class", "device_class"),
            ("Name", "device_name"),
            ("Make", "make"),
            ("Model", "model"),
        ):
            ColumnMapping.objects.create(profile=cls.profile, source_column=source_column, target_field=target_field)
        ClassRoleMapping.objects.create(profile=cls.profile, source_class="Server", role_slug="server")

    def preview(self, *rows):
        """Upload one flat workbook and return the preview's rows by device name."""
        client = Client()
        client.force_login(self.actor)
        upload = SimpleUploadedFile(
            "mapping.xlsx",
            workbook_bytes(["Source ID", "Class", "Name", "Make", "Model"], [list(row) for row in rows]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        setup = upload_preview(client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})
        self.assertEqual(setup.status_code, 302, setup.content[:300])
        response = client.get(reverse("plugins:netbox_data_import:import_preview"))
        return {row.name: row for row in response.context["preview_rows"] if row.object_type == "device"}

    def map_type(self, make, model, manufacturer, device_type):
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make=make,
            source_model=model,
            netbox_manufacturer_slug=manufacturer,
            netbox_device_type_slug=device_type,
        )

    def test_two_device_type_mappings_with_two_targets_leave_every_spelling_blocked(self):
        self.map_type("I", "X", "maker-i", "type-x")
        self.map_type(DOTLESS_I, "X", "maker-dotless", "type-x")

        rows = self.preview(
            ("D-1", "Server", "host-1", DOTLESS_I, "x"),
            ("D-2", "Server", "host-2", DOTLESS_I, "X"),
            ("D-3", "Server", "host-3", "i", "X"),
        )

        wording = _DIAGNOSTIC_MESSAGES.get("device.device_type_mapping_ambiguous")
        self.assertEqual(
            {name: (row.disposition, row.detail) for name, row in rows.items()},
            dict.fromkeys(("host-1", "host-2", "host-3"), ("blocked", wording)),
        )

    def test_two_device_type_mappings_with_one_target_are_one_mapping(self):
        self.map_type("I", "X", "maker-i", "type-x")
        self.map_type(DOTLESS_I, "x", "maker-i", "type-x")

        rows = self.preview(("D-1", "Server", "host-1", DOTLESS_I, "X"))

        self.assertEqual(rows["host-1"].disposition, "actionable", rows["host-1"].detail)
        self.assertEqual(rows["host-1"].extra_data["device_type_id"], self.device_type("maker-i", "type-x"))

    def test_two_manufacturer_mappings_with_two_targets_leave_the_make_blocked(self):
        ManufacturerMapping.objects.create(profile=self.profile, source_make="I", netbox_manufacturer_slug="maker-i")
        ManufacturerMapping.objects.create(
            profile=self.profile, source_make=DOTLESS_I, netbox_manufacturer_slug="maker-dotless"
        )

        rows = self.preview(("D-1", "Server", "host-1", DOTLESS_I, "type-y"))

        self.assertEqual(
            (rows["host-1"].disposition, rows["host-1"].detail),
            ("blocked", _DIAGNOSTIC_MESSAGES.get("device.manufacturer_mapping_ambiguous")),
        )

    @staticmethod
    def device_type(manufacturer, slug):
        from dcim.models import DeviceType

        return DeviceType.objects.get(manufacturer__slug=manufacturer, slug=slug).pk

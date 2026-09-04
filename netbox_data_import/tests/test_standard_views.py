# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""Apply NetBox's standard UI and API test contracts to import profiles."""

from utilities.testing import APIViewTestCases, ViewTestCases

from netbox_data_import.adapter_forms import FlatWorkbookConfigForm
from netbox_data_import.models import ImportProfile

BASE_URL = "plugins:netbox_data_import:importprofile_{}"


class ImportProfileViewTestCase(ViewTestCases.PrimaryObjectViewTestCase):
    """Exercise standard detail, list, CRUD, changelog, and bulk profile views."""

    model = ImportProfile

    def _get_base_url(self):
        return BASE_URL

    def test_create_object_with_permission(self):
        """The profile view stores every submitted adapter setting."""
        super().test_create_object_with_permission()
        profile = ImportProfile.objects.get(name=self.form_data["name"])
        submitted_config = {
            name: self.form_data[name] for name in FlatWorkbookConfigForm.base_fields if name in self.form_data
        }
        self.assertEqual(profile.adapter_config, FlatWorkbookConfigForm.validate_config(submitted_config))

    @classmethod
    def setUpTestData(cls):
        ImportProfile.objects.bulk_create(
            [
                ImportProfile(name="Standard Profile 1"),
                ImportProfile(name="Standard Profile 2"),
                ImportProfile(name="Standard Profile 3"),
            ]
        )

        cls.form_data = {
            "name": "Standard Created Profile",
            "description": "Created by the NetBox view contract",
            "source_adapter": "flat_workbook",
            "sheet_name": "Inventory",
            "source_id_column": "Source ID",
            "custom_field_name": "",
            "update_existing": True,
            "preview_view_mode": "rows",
            "capture_extra_data": False,
            "primary_contact_lookup_field": "email",
        }
        cls.csv_data = (
            "name,description,source_adapter",
            "Standard Imported Profile 1,Imported 1,flat_workbook",
            "Standard Imported Profile 2,Imported 2,flat_workbook",
            "Standard Imported Profile 3,Imported 3,flat_workbook",
        )
        cls.csv_update_data = (
            "id,name,description",
            f"{ImportProfile.objects.first().pk},Standard Profile 1,Updated through bulk import",
        )
        cls.bulk_edit_data = {
            "description": "Bulk edited by the NetBox view contract",
        }


class ImportProfileAPIViewTestCase(APIViewTestCases.APIViewTestCase):
    """Exercise NetBox's standard REST and GraphQL contracts for import profiles."""

    model = ImportProfile
    brief_fields = ["description", "display", "id", "name", "url"]
    view_namespace = "plugins-api:netbox_data_import"

    @classmethod
    def setUpTestData(cls):
        ImportProfile.objects.bulk_create(
            [
                ImportProfile(name="Standard API Profile 1"),
                ImportProfile(name="Standard API Profile 2"),
                ImportProfile(name="Standard API Profile 3"),
            ]
        )
        # Submit a normalized configuration: the standard contract asserts the response echoes the request.
        flat_defaults = FlatWorkbookConfigForm.validate_config({})
        cls.create_data = [
            {
                "name": "Standard API Created 1",
                "adapter_config": {**flat_defaults, "sheet_name": "Inventory"},
            },
            {
                "name": "Standard API Created 2",
                "adapter_config": {**flat_defaults, "sheet_name": "Inventory", "update_existing": False},
            },
            {
                "name": "Standard API Created 3",
                "source_adapter": "flat_workbook",
                "adapter_config": {**flat_defaults, "capture_extra_data": True},
            },
        ]
        cls.bulk_update_data = {
            "description": "Bulk updated through the standard API contract",
            "adapter_config": {**flat_defaults, "update_existing": False},
        }

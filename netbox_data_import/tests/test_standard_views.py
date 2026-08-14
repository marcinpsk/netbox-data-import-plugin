# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""Apply NetBox's standard UI and API test contracts to import profiles."""

from utilities.testing import APIViewTestCases, ViewTestCases

from netbox_data_import.models import ImportProfile

BASE_URL = "plugins:netbox_data_import:importprofile_{}"


class ImportProfileViewTestCase(ViewTestCases.PrimaryObjectViewTestCase):
    """Exercise standard detail, list, CRUD, changelog, and bulk profile views."""

    model = ImportProfile

    def _get_base_url(self):
        return BASE_URL

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
            "sheet_name": "Inventory",
            "source_id_column": "Source ID",
            "custom_field_name": "",
            "update_existing": True,
            "create_missing_device_types": True,
            "preview_view_mode": "rows",
            "capture_extra_data": False,
            "primary_contact_lookup_field": "email",
        }
        cls.csv_data = (
            "name,description,sheet_name,source_id_column,update_existing,create_missing_device_types,"
            "preview_view_mode,capture_extra_data,primary_contact_lookup_field",
            "Standard Imported Profile 1,Imported 1,Inventory,Source ID,true,true,rows,false,email",
            "Standard Imported Profile 2,Imported 2,Inventory,Source ID,true,false,racks,true,name",
            "Standard Imported Profile 3,Imported 3,Inventory,Source ID,false,true,rows,false,email",
        )
        cls.csv_update_data = (
            "id,name,description",
            f"{ImportProfile.objects.first().pk},Standard Profile 1,Updated through bulk import",
        )
        cls.bulk_edit_data = {
            "description": "Bulk edited by the NetBox view contract",
            "update_existing": False,
            "create_missing_device_types": False,
            "capture_extra_data": True,
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
        cls.create_data = [
            {"name": "Standard API Created 1", "sheet_name": "Inventory"},
            {"name": "Standard API Created 2", "sheet_name": "Inventory", "update_existing": False},
            {"name": "Standard API Created 3", "sheet_name": "Inventory", "capture_extra_data": True},
        ]
        cls.bulk_update_data = {
            "description": "Bulk updated through the standard API contract",
            "update_existing": False,
        }

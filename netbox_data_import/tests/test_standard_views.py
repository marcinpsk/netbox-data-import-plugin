# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""Apply NetBox's standard UI and API test contracts to the plugin's own models."""

import json

from django.test import override_settings
from utilities.testing import APIViewTestCases, ViewTestCases

from netbox_data_import.adapter_forms import FlatWorkbookConfigForm
from netbox_data_import.models import ImportProfile, InferenceBackend

BASE_URL = "plugins:netbox_data_import:importprofile_{}"
INFERENCE_BASE_URL = "plugins:netbox_data_import:inferencebackend_{}"

# `clean()` validates `api_root` against this setting, so the contract needs it for every request.
INFERENCE_ALLOWLIST = ("https://backend.example.invalid:443",)
INFERENCE_REFERENCE = {
    "backend": "vault_kv_v2",
    "mount": "secret",
    "path": "inference/backend",
    "field": "api_key",
}


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
        # Lets NetBox run test_bulk_update_objects_validation_error, which needs a failing row.
        cls.bulk_update_invalid_data = {"source_adapter": "no_such_adapter"}


@override_settings(
    PLUGINS_CONFIG={"netbox_data_import": {"inference_backend_origin_allowlist": list(INFERENCE_ALLOWLIST)}}
)
class InferenceBackendViewTestCase(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """Exercise the UI detail, list, CRUD and changelog views for AI backends.

    The mixins are named one by one because `InferenceBackend` registers no bulk views. The REST
    endpoint credential contract is covered by the configuration surface tests.
    """

    model = InferenceBackend
    maxDiff = None
    # Postgres orders jsonb keys by length, so the tests below compare the parsed mapping instead.
    validation_excluded_fields = ["credential_reference"]

    def _get_base_url(self):
        return INFERENCE_BASE_URL

    def test_create_object_with_permission(self):
        """The create view stores the typed reference the form submitted."""
        super().test_create_object_with_permission()
        created = InferenceBackend.objects.get(backend_key=self.form_data["backend_key"])
        self.assertEqual(created.credential_reference, INFERENCE_REFERENCE)

    def test_edit_object_with_permission(self):
        """The edit view stores the typed reference the form submitted."""
        super().test_edit_object_with_permission()
        edited = InferenceBackend.objects.get(backend_key=self.form_data["backend_key"])
        self.assertEqual(edited.credential_reference, INFERENCE_REFERENCE)

    @classmethod
    def setUpTestData(cls):
        # Every fixture stays disabled: a partial unique index permits only one enabled row.
        InferenceBackend.objects.bulk_create(
            [
                InferenceBackend(
                    backend_key=f"standard-backend-{index}",
                    display_name=f"Standard backend {index}",
                    adapter_type="openai_compatible",
                    api_root=INFERENCE_ALLOWLIST[0],
                    model="row-model",
                    authentication="bearer",
                    response_mode="prompt_json",
                    credential_reference=INFERENCE_REFERENCE,
                    connect_timeout=5,
                    read_timeout=60,
                    enabled=False,
                )
                for index in (1, 2, 3)
            ]
        )

        cls.form_data = {
            "backend_key": "standard-created-backend",
            "display_name": "Standard created backend",
            "adapter_type": "openai_compatible",
            "api_root": INFERENCE_ALLOWLIST[0],
            "model": "created-model",
            "authentication": "bearer",
            "response_mode": "prompt_json",
            # A plain JSONField renders a textarea, so the contract submits the reference as JSON text.
            "credential_reference": json.dumps(INFERENCE_REFERENCE),
            "connect_timeout": 5,
            "read_timeout": 60,
            "enabled": False,
        }

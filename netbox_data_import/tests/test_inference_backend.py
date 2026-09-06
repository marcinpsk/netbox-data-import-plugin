# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Inference Backend row, its one-enabled rule, and the active-backend resolver (specification 8.2)."""

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase, override_settings

from netbox_data_import.inference_backend import (
    NoActiveInferenceBackend,
    resolve_active_backend,
)
from netbox_data_import.inference_credentials import InvalidCredentialReference
from netbox_data_import.inference_settings import FILE_FALLBACK_KEY, InvalidInferenceConfiguration
from netbox_data_import.models import InferenceBackend

ALLOWLIST = ["https://backend.example.invalid:443"]
REFERENCE = {"backend": "vault_kv_v2", "mount": "secret", "path": "inference/backend", "field": "api_key"}

FALLBACK = {
    "display_name": "File fallback",
    "adapter_type": "openai_compatible",
    "api_root": "https://backend.example.invalid:443",
    "model": "fallback-model",
    "authentication": "bearer",
    "response_mode": "prompt_json",
    "credential_reference": REFERENCE,
    "connect_timeout": 5,
    "read_timeout": 60,
}


def make_row(**overrides):
    """Create one Inference Backend row with sensible defaults."""
    values = {
        "backend_key": "primary",
        "display_name": "Primary backend",
        "adapter_type": "openai_compatible",
        "api_root": "https://backend.example.invalid:443",
        "model": "row-model",
        "authentication": "bearer",
        "response_mode": "prompt_json",
        "credential_reference": REFERENCE,
        "connect_timeout": 5,
        "read_timeout": 60,
        "enabled": False,
    }
    values.update(overrides)
    return InferenceBackend.objects.create(**values)


def plugin_settings(**overrides):
    """Return a PLUGINS_CONFIG entry for the plugin with the named keys replaced."""
    config = {"inference_backend_origin_allowlist": ALLOWLIST}
    config.update(overrides)
    return {"netbox_data_import": config}


class OneEnabledRowTest(TestCase):
    """At most one row is enabled, because the enabled row is the active backend."""

    def test_many_rows_may_exist_disabled(self):
        make_row(backend_key="a")
        make_row(backend_key="b")

        self.assertEqual(InferenceBackend.objects.count(), 2)

    def test_one_row_may_be_enabled(self):
        make_row(backend_key="a", enabled=True)

        self.assertEqual(InferenceBackend.objects.filter(enabled=True).count(), 1)

    def test_a_second_enabled_row_is_refused_by_the_database(self):
        make_row(backend_key="a", enabled=True)

        with self.assertRaises(IntegrityError):
            make_row(backend_key="b", enabled=True)

    def test_a_second_enabled_row_is_refused_by_validation(self):
        """The operator sees a message rather than an integrity error."""
        make_row(backend_key="a", enabled=True)
        second = InferenceBackend(
            backend_key="b",
            display_name="Second",
            api_root="https://backend.example.invalid:443",
            model="m",
            credential_reference=REFERENCE,
            enabled=True,
        )

        with self.assertRaises(ValidationError) as caught:
            second.full_clean()

        self.assertIn("enabled", caught.exception.message_dict)

    def test_the_backend_key_is_unique(self):
        make_row(backend_key="a")

        with self.assertRaises(IntegrityError):
            make_row(backend_key="a")


class RowValidationTest(TestCase):
    """A row validates against the same trust boundary and reference rules as the fallback."""

    @override_settings(PLUGINS_CONFIG=plugin_settings())
    def test_an_api_root_outside_the_allowlist_is_refused(self):
        row = InferenceBackend(
            backend_key="a",
            display_name="A",
            api_root="https://elsewhere.example.invalid:443",
            model="m",
            credential_reference=REFERENCE,
        )

        with self.assertRaises(ValidationError) as caught:
            row.full_clean()

        self.assertIn("api_root", caught.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=plugin_settings())
    def test_a_credential_reference_carrying_a_token_is_refused(self):
        row = InferenceBackend(
            backend_key="a",
            display_name="A",
            api_root="https://backend.example.invalid:443",
            model="m",
            credential_reference={**REFERENCE, "token": "s.leaked"},
        )

        with self.assertRaises(ValidationError) as caught:
            row.full_clean()

        self.assertIn("credential_reference", caught.exception.message_dict)

    @override_settings(PLUGINS_CONFIG=plugin_settings())
    def test_a_valid_row_passes(self):
        row = InferenceBackend(
            backend_key="a",
            display_name="A",
            api_root="https://backend.example.invalid:443",
            model="m",
            credential_reference=REFERENCE,
        )

        row.full_clean()


class ActiveBackendResolutionTest(TestCase):
    """The enabled row is the active backend; the file fallback acts only when no row is enabled."""

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend=FALLBACK))
    def test_the_enabled_row_wins_over_the_file_fallback(self):
        make_row(backend_key="primary", enabled=True, model="row-model")

        active = resolve_active_backend()

        self.assertEqual(active.backend_key, "primary")
        self.assertEqual(active.model, "row-model")
        self.assertEqual(active.source, "database")

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend=FALLBACK))
    def test_the_file_fallback_acts_when_no_row_is_enabled(self):
        make_row(backend_key="primary", enabled=False, model="row-model")

        active = resolve_active_backend()

        self.assertEqual(active.backend_key, FILE_FALLBACK_KEY)
        self.assertEqual(active.model, "fallback-model")
        self.assertEqual(active.source, "file-fallback")

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend=FALLBACK))
    def test_the_two_sources_are_never_merged_field_by_field(self):
        """A row missing nothing takes every field from itself, not one from the fallback."""
        make_row(backend_key="primary", enabled=True, model="row-model", display_name="Row display")

        active = resolve_active_backend()

        self.assertEqual(active.display_name, "Row display")
        self.assertNotEqual(active.model, FALLBACK["model"])

    @override_settings(PLUGINS_CONFIG=plugin_settings())
    def test_no_row_and_no_fallback_has_no_active_backend(self):
        with self.assertRaises(NoActiveInferenceBackend):
            resolve_active_backend()

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend={"display_name": "broken"}))
    def test_a_malformed_fallback_fails_rather_than_selecting_another_backend(self):
        """Section 8.2.1: a malformed mapping is invalid_configuration, never a silent fallback."""
        make_row(backend_key="primary", enabled=False)

        with self.assertRaises(InvalidInferenceConfiguration):
            resolve_active_backend()

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend=FALLBACK))
    def test_the_resolved_backend_carries_its_typed_credential_reference(self):
        active = resolve_active_backend()

        self.assertEqual(active.credential_reference.mount, "secret")
        self.assertEqual(active.credential_reference.field, "api_key")

    @override_settings(PLUGINS_CONFIG=plugin_settings())
    def test_a_row_whose_reference_is_malformed_is_refused_at_resolution(self):
        make_row(backend_key="primary", enabled=True, credential_reference={"backend": "vault_kv_v2"})

        with self.assertRaises(InvalidCredentialReference):
            resolve_active_backend()

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend_origin_allowlist=["https://other.invalid:443"]))
    def test_a_row_whose_api_root_left_the_allowlist_is_refused_at_resolution(self):
        """Spec 8.3 validates a row and a setting alike, and a saved row outlives its allowlist."""
        make_row(backend_key="primary", enabled=True)

        with self.assertRaises(InvalidInferenceConfiguration):
            resolve_active_backend()

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend=FALLBACK))
    def test_the_source_reaches_backend_metadata(self):
        """The worker records which source it used, so an operator can tell them apart."""
        make_row(backend_key="primary", enabled=True)

        self.assertEqual(resolve_active_backend().metadata()["backend_source"], "database")

    @override_settings(PLUGINS_CONFIG=plugin_settings(inference_backend=FALLBACK))
    def test_backend_metadata_carries_no_credential_material(self):
        metadata = resolve_active_backend().metadata()

        self.assertNotIn("credential_reference", metadata)
        self.assertEqual(metadata["backend_key"], FILE_FALLBACK_KEY)

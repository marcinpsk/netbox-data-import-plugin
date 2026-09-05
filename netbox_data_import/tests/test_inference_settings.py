# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The three Inference Backend plugin settings and the startup shape gate (specification 8.2.1)."""

from django.test import SimpleTestCase

from netbox_data_import.inference_settings import (
    FILE_FALLBACK_KEY,
    InvalidInferenceConfiguration,
    validate_plugin_settings,
)


def settings_with(**overrides):
    """Return a plugin settings mapping that passes, with the named keys replaced."""
    config = {
        "inference_backend_origin_allowlist": ["https://backend.example.invalid:443"],
        "vault": {
            "address": "https://vault.example.invalid:8200",
            "auth_method": "proxy",
            "connect_timeout": 5,
            "read_timeout": 60,
        },
    }
    config.update(overrides)
    return config


class OriginAllowlistTest(SimpleTestCase):
    """An allowlist entry names one exact origin: scheme, host and port."""

    def rejects(self, entry):
        """Return the message raised for one allowlist entry."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend_origin_allowlist=[entry]))
        return str(caught.exception)

    def test_an_exact_origin_is_accepted(self):
        validate_plugin_settings(
            settings_with(inference_backend_origin_allowlist=["https://backend.example.invalid:443"])
        )

    def test_a_bare_host_is_rejected(self):
        self.assertIn("scheme", self.rejects("backend.example.invalid"))

    def test_an_entry_without_a_port_is_rejected(self):
        self.assertIn("port", self.rejects("https://backend.example.invalid"))

    def test_an_entry_with_a_path_is_rejected(self):
        self.assertIn("path", self.rejects("https://backend.example.invalid:443/v1"))

    def test_a_wildcard_entry_is_rejected(self):
        self.assertIn("wildcard", self.rejects("https://*.example.invalid:443"))

    def test_a_non_list_allowlist_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings(settings_with(inference_backend_origin_allowlist="https://a.invalid:443"))


class VaultSettingTest(SimpleTestCase):
    """The deployment-owned vault mapping carries connection data and no secret material."""

    def rejects(self, mapping):
        """Return the message raised for one vault mapping."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(vault=mapping))
        return str(caught.exception)

    def test_the_proxy_auth_method_is_accepted(self):
        validate_plugin_settings(settings_with())

    def test_the_token_auth_method_is_accepted(self):
        validate_plugin_settings(
            settings_with(vault={"address": "https://vault.example.invalid:8200", "auth_method": "token"})
        )

    def test_any_other_auth_method_is_rejected(self):
        message = self.rejects({"address": "https://vault.example.invalid:8200", "auth_method": "approle"})

        self.assertIn("auth_method", message)
        self.assertIn("approle", message)

    def test_a_mount_key_is_rejected(self):
        """The KV v2 mount belongs to the credential reference, so it cannot live here."""
        message = self.rejects(
            {"address": "https://vault.example.invalid:8200", "auth_method": "proxy", "mount": "secret"}
        )

        self.assertIn("mount", message)

    def test_a_token_value_is_rejected(self):
        message = self.rejects(
            {"address": "https://vault.example.invalid:8200", "auth_method": "token", "token": "s.hunter2"}
        )

        self.assertIn("token", message)

    def test_an_approle_secret_is_rejected(self):
        message = self.rejects(
            {"address": "https://vault.example.invalid:8200", "auth_method": "proxy", "secret_id": "x"}
        )

        self.assertIn("secret_id", message)

    def test_a_tls_verification_override_is_rejected(self):
        message = self.rejects(
            {"address": "https://vault.example.invalid:8200", "auth_method": "proxy", "verify": False}
        )

        self.assertIn("verify", message)

    def test_a_missing_address_is_rejected(self):
        self.assertIn("address", self.rejects({"auth_method": "proxy"}))


class FileFallbackSettingTest(SimpleTestCase):
    """The whole-backend fallback carries the row fields minus the backend key and enabled."""

    def fallback(self, **overrides):
        """Return a valid file-fallback mapping with the named keys replaced."""
        mapping = {
            "display_name": "Fallback backend",
            "adapter_type": "openai_compatible",
            "api_root": "https://backend.example.invalid:443",
            "model": "gpt-4o-mini",
            "authentication": "bearer",
            "response_mode": "prompt_json",
            "credential_reference": {
                "backend": "vault_kv_v2",
                "mount": "secret",
                "path": "inference/backend",
                "field": "api_key",
            },
            "connect_timeout": 5,
            "read_timeout": 60,
        }
        mapping.update(overrides)
        for key, value in list(mapping.items()):
            if value is None:
                del mapping[key]
        return mapping

    def test_a_complete_fallback_is_accepted(self):
        validate_plugin_settings(settings_with(inference_backend=self.fallback()))

    def test_the_setting_is_optional(self):
        validate_plugin_settings(settings_with())

    def test_a_missing_field_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=self.fallback(model=None)))

        self.assertIn("model", str(caught.exception))

    def test_an_unknown_field_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=self.fallback(enabled=True)))

        self.assertIn("enabled", str(caught.exception))

    def test_a_backend_key_is_rejected(self):
        """The fallback's key is the fixed value, so the mapping cannot name its own."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=self.fallback(backend_key="mine")))

        self.assertIn("backend_key", str(caught.exception))

    def test_an_api_root_outside_the_allowlist_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(
                settings_with(inference_backend=self.fallback(api_root="https://elsewhere.example.invalid:443"))
            )

        self.assertIn("allowlist", str(caught.exception))

    def test_a_non_mapping_fallback_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings(settings_with(inference_backend=["not", "a", "mapping"]))

    def test_the_fixed_backend_key_is_stated(self):
        self.assertEqual(FILE_FALLBACK_KEY, "file-fallback")


class StartupContactTest(SimpleTestCase):
    """Shape validation is offline: startup never contacts Vault or the backend."""

    def test_validation_opens_no_socket(self):
        import socket

        opened = []
        original = socket.socket.connect

        def record(self, address):
            opened.append(address)
            raise AssertionError(f"startup validation contacted {address}")

        socket.socket.connect = record
        try:
            validate_plugin_settings(settings_with(inference_backend=FileFallbackSettingTest().fallback()))
        finally:
            socket.socket.connect = original

        self.assertEqual(opened, [])


class PluginConfigStartupGateTest(SimpleTestCase):
    """The plugin refuses to start on a malformed Inference Backend configuration."""

    def validate(self, config):
        """Run the plugin's own startup validation over one PLUGINS_CONFIG entry."""
        from netbox_data_import import NetBoxDataImportConfig

        NetBoxDataImportConfig.validate(config, "4.6.0")

    def test_a_valid_configuration_starts(self):
        self.validate(settings_with())

    def test_a_malformed_allowlist_entry_stops_startup(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured) as caught:
            self.validate({"inference_backend_origin_allowlist": ["backend.example.invalid"]})

        self.assertIn("port", str(caught.exception))

    def test_a_rejected_vault_auth_method_stops_startup(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            self.validate(settings_with(vault={"address": "https://v.invalid:8200", "auth_method": "approle"}))

    def test_the_allowlist_defaults_to_empty(self):
        """A deployment that names no allowlist reaches no origin, rather than every origin."""
        config: dict = {}

        self.validate(config)

        self.assertEqual(config["inference_backend_origin_allowlist"], [])

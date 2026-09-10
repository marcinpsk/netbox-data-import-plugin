# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The three Inference Backend plugin settings and the startup shape gate (specification 8.2.1)."""

from django.test import SimpleTestCase, override_settings

from netbox_data_import.inference_backend import proposal_candidate_limit
from netbox_data_import.inference_settings import (
    FILE_FALLBACK_KEY,
    PROPOSAL_CANDIDATE_LIMIT_DEFAULT,
    PROPOSAL_CANDIDATE_LIMIT_MAX,
    PROPOSAL_CANDIDATE_LIMIT_SETTING,
    InvalidInferenceConfiguration,
    validate_credential_reference,
    validate_plugin_settings,
    validate_proposal_candidate_limit,
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

    def test_a_non_string_address_is_rejected(self):
        """A truthy non-string reaches the request as `str(value)`, which is not a URL."""
        self.assertIn("address", self.rejects({"address": 8200, "auth_method": "proxy"}))

    def test_a_non_string_namespace_is_rejected(self):
        message = self.rejects({"address": "https://vault.example.invalid:8200", "namespace": ["team-a"]})

        self.assertIn("namespace", message)

    def test_an_empty_namespace_is_rejected(self):
        for namespace in ("", "   "):
            with self.subTest(namespace=namespace):
                message = self.rejects({"address": "https://vault.example.invalid:8200", "namespace": namespace})

                self.assertIn("namespace", message)

    def test_a_non_empty_namespace_is_accepted(self):
        validate_plugin_settings(
            settings_with(vault={"address": "https://vault.example.invalid:8200", "namespace": "team-a"})
        )

    def test_an_address_carrying_a_credential_is_rejected(self):
        """Userinfo in the address is secret material in a setting that must hold none."""
        message = self.rejects({"address": "https://user:token@vault.example.invalid:8200", "auth_method": "proxy"})

        self.assertIn("userinfo", message)

    def test_an_address_with_a_query_is_rejected(self):
        """The read appends /v1/<mount>/data/<path>, which a query would swallow."""
        self.assertIn("query", self.rejects({"address": "https://vault.example.invalid:8200?a=1"}))

    def test_an_address_with_a_fragment_is_rejected(self):
        self.assertIn("fragment", self.rejects({"address": "https://vault.example.invalid:8200#f"}))

    def test_an_address_ending_in_a_bare_delimiter_is_rejected(self):
        """The read appends /v1/<mount>/data/<path>, which a bare '?' or '#' puts in the wrong part."""
        for address in ("https://vault.example.invalid:8200?", "https://vault.example.invalid:8200#"):
            with self.subTest(address=address):
                self.assertTrue(self.rejects({"address": address}))

    def test_a_rejected_address_is_not_echoed_back(self):
        """The message reaches Job.data, and an address can carry a token in its userinfo."""
        message = self.rejects({"address": "https://user:s3cr3t-token@[bad"})

        self.assertNotIn("s3cr3t-token", message)
        self.assertNotIn("[bad", message)

    def test_an_address_without_a_scheme_is_rejected(self):
        self.assertIn("scheme", self.rejects({"address": "vault.example.invalid:8200"}))


def file_fallback(**overrides):
    """Return a valid file-fallback mapping with the named keys replaced, or dropped when None."""
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
    return {key: value for key, value in mapping.items() if value is not None}


class FileFallbackSettingTest(SimpleTestCase):
    """The whole-backend fallback carries the row fields minus the backend key and enabled."""

    def test_a_complete_fallback_is_accepted(self):
        validate_plugin_settings(settings_with(inference_backend=file_fallback()))

    def test_the_setting_is_optional(self):
        validate_plugin_settings(settings_with())

    def test_a_missing_field_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(model=None)))

        self.assertIn("model", str(caught.exception))

    def test_an_unknown_field_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(enabled=True)))

        self.assertIn("enabled", str(caught.exception))

    def test_a_backend_key_is_rejected(self):
        """The fallback's key is the fixed value, so the mapping cannot name its own."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(backend_key="mine")))

        self.assertIn("backend_key", str(caught.exception))

    def test_an_api_root_outside_the_allowlist_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(
                settings_with(inference_backend=file_fallback(api_root="https://elsewhere.example.invalid:443"))
            )

        self.assertIn("allowlist", str(caught.exception))

    def test_an_adapter_type_outside_the_row_choices_is_rejected(self):
        """The fallback is one whole backend row, so every field carries the row's constraint."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(adapter_type="anthropic")))

        self.assertIn("adapter_type", str(caught.exception))

    def test_a_response_mode_outside_the_row_choices_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(response_mode="freeform")))

        self.assertIn("response_mode", str(caught.exception))

    def test_an_empty_model_is_rejected(self):
        """The worker never chooses a model, so an empty one has no request to make."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(model="   ")))

        self.assertIn("model", str(caught.exception))

    def test_a_model_longer_than_the_column_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(model="m" * 201)))

        self.assertIn("model", str(caught.exception))

    def test_a_timeout_the_column_could_not_hold_is_rejected(self):
        """`True` is the one that matters: bool is an int, so it would read as a one second timeout.

        Zero and 2**31 are rejected on both sides: a zero timeout raises in the transport, and
        anything above the column maximum cannot be stored. The row carries the same rule.
        """
        for field in ("connect_timeout", "read_timeout"):
            for value in (-1, 0, "five", 1.5, True, 2**31):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(InvalidInferenceConfiguration) as caught:
                        validate_plugin_settings(settings_with(inference_backend=file_fallback(**{field: value})))

                    self.assertIn(field, str(caught.exception))

    def test_an_authentication_method_outside_the_row_choices_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(authentication="basic")))

        self.assertIn("authentication", str(caught.exception))

    def test_an_empty_display_name_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(display_name="  ")))

        self.assertIn("display_name", str(caught.exception))

    def test_an_api_root_longer_than_the_column_is_rejected(self):
        root = "https://backend.example.invalid:443/" + "p" * 500
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(inference_backend=file_fallback(api_root=root)))

        self.assertIn("api_root", str(caught.exception))

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
            validate_plugin_settings(settings_with(inference_backend=file_fallback()))
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


class VaultAddressSchemeTest(SimpleTestCase):
    """A Vault read carries a token, so its address follows the same scheme rule as an API root."""

    def vault(self, address):
        """Return a vault mapping with the given address."""
        return {"address": address, "auth_method": "proxy"}

    def test_https_is_accepted(self):
        validate_plugin_settings(settings_with(vault=self.vault("https://vault.example.invalid:8200")))

    def test_a_remote_http_address_is_rejected(self):
        """`_read` sends the token to this address, so cleartext would put it on the wire."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(vault=self.vault("http://vault.example.invalid:8200")))

        self.assertIn("https", str(caught.exception))

    def test_a_loopback_http_address_is_accepted(self):
        """The same exception the API root makes for a local endpoint, for the same reason."""
        validate_plugin_settings(settings_with(vault=self.vault("http://127.0.0.1:8200")))


class VaultTimeoutTest(SimpleTestCase):
    """The Vault backend hands these to requests, so an unusable value cannot reach it."""

    def vault(self, **overrides):
        """Return a vault mapping with the named keys replaced."""
        mapping = {"address": "https://vault.example.invalid:8200", "auth_method": "proxy"}
        mapping.update(overrides)
        return mapping

    def test_absent_timeouts_are_accepted(self):
        validate_plugin_settings(settings_with(vault=self.vault()))

    def test_a_whole_number_of_seconds_is_accepted(self):
        validate_plugin_settings(settings_with(vault=self.vault(connect_timeout=5, read_timeout=10)))

    def test_a_null_timeout_is_rejected(self):
        """requests reads None as 'no deadline', which is the one thing a timeout must never mean."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(vault=self.vault(connect_timeout=None)))

        self.assertIn("connect_timeout", str(caught.exception))

    def test_a_zero_timeout_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings(settings_with(vault=self.vault(read_timeout=0)))

    def test_a_string_timeout_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings(settings_with(vault=self.vault(read_timeout="10")))


class CredentialReferenceTypeTest(SimpleTestCase):
    """Every Vault reference field names text. A number only looks valid once something coerces it."""

    def reference(self, **overrides):
        """Return a valid credential reference with the named keys replaced."""
        mapping = {"backend": "vault_kv_v2", "mount": "secret", "path": "ai/backend", "field": "api_key"}
        mapping.update(overrides)
        return mapping

    def test_a_valid_reference_is_accepted(self):
        self.assertEqual(validate_credential_reference(self.reference())["field"], "api_key")

    def test_a_non_string_unknown_key_is_reported_not_raised(self):
        """The unknown-key message sorts and joins the keys, which a number turned into a TypeError."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_credential_reference({**self.reference(), 5: "x"})

        self.assertIn("Unknown", str(caught.exception))

    def test_a_wrong_backend_is_refused_without_quoting_what_was_given(self):
        """The message is persisted, so it names the expected value and never the supplied one."""
        secret = "sk-should-never-be-echoed"
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_credential_reference(self.reference(backend=secret))

        self.assertIn("backend", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))

    def test_a_numeric_mount_is_rejected(self):
        """`str(value)` would make 8200 a valid-looking mount that `quote()` then refuses."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_credential_reference(self.reference(mount=8200))

        self.assertIn("mount", str(caught.exception))

    def test_a_numeric_path_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_credential_reference(self.reference(path=42))

        self.assertIn("path", str(caught.exception))

    def test_a_numeric_field_is_rejected(self):
        """Only truthiness was checked, so a number reached the Vault lookup as a key name."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_credential_reference(self.reference(field=1))

        self.assertIn("field", str(caught.exception))


class VaultCaBundleTest(SimpleTestCase):
    """`ca_bundle` names a CA file. It can never turn certificate verification off."""

    def vault(self, **overrides):
        """Return a vault mapping with the named keys replaced."""
        mapping = {"address": "https://vault.example.invalid:8200", "auth_method": "proxy"}
        mapping.update(overrides)
        return mapping

    def test_a_path_is_accepted(self):
        validate_plugin_settings(settings_with(vault=self.vault(ca_bundle="/etc/ssl/certs/vault.pem")))

    def test_false_is_rejected(self):
        """requests reads verify=False as 'skip verification', which this setting must never mean."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_plugin_settings(settings_with(vault=self.vault(ca_bundle=False)))

        self.assertIn("ca_bundle", str(caught.exception))

    def test_true_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings(settings_with(vault=self.vault(ca_bundle=True)))

    def test_an_empty_path_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings(settings_with(vault=self.vault(ca_bundle="")))

    def test_a_non_string_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings(settings_with(vault=self.vault(ca_bundle=["/a/b.pem"])))


class ProposalCandidateLimitTest(SimpleTestCase):
    """The candidate bound is a deployment setting, validated at startup (section 7.3)."""

    def test_a_positive_integer_is_accepted(self):
        self.assertEqual(validate_proposal_candidate_limit(96), 96)

    def test_the_upper_bound_is_accepted(self):
        self.assertEqual(validate_proposal_candidate_limit(PROPOSAL_CANDIDATE_LIMIT_MAX), PROPOSAL_CANDIDATE_LIMIT_MAX)

    def test_a_value_past_the_range_is_rejected(self):
        """`10**100` must never reach a database slice, because the retrieval materializes it."""
        for value in (PROPOSAL_CANDIDATE_LIMIT_MAX + 1, 10**100):
            with self.subTest(value=value):
                with self.assertRaises(InvalidInferenceConfiguration):
                    validate_proposal_candidate_limit(value)

    def test_zero_and_negatives_are_rejected(self):
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(InvalidInferenceConfiguration):
                    validate_proposal_candidate_limit(value)

    def test_a_boolean_is_rejected(self):
        """`True` is numerically 1 and would silently admit a one-candidate set."""
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(InvalidInferenceConfiguration):
                    validate_proposal_candidate_limit(value)

    def test_a_float_a_string_and_none_are_rejected(self):
        for value in (64.0, "64", None):
            with self.subTest(value=value):
                with self.assertRaises(InvalidInferenceConfiguration):
                    validate_proposal_candidate_limit(value)

    def test_startup_validation_reaches_it_without_any_other_inference_setting(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_plugin_settings({PROPOSAL_CANDIDATE_LIMIT_SETTING: 0})

    def test_the_default_applies_only_when_the_key_is_omitted(self):
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {}}):
            self.assertEqual(proposal_candidate_limit(), PROPOSAL_CANDIDATE_LIMIT_DEFAULT)

        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {PROPOSAL_CANDIDATE_LIMIT_SETTING: 128}}):
            self.assertEqual(proposal_candidate_limit(), 128)

    def test_a_configured_value_is_validated_when_it_is_read(self):
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {PROPOSAL_CANDIDATE_LIMIT_SETTING: True}}):
            with self.assertRaises(InvalidInferenceConfiguration):
                proposal_candidate_limit()

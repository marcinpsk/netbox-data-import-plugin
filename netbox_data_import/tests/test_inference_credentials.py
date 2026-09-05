# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The credential boundary and its Vault KV v2 implementation (specification 8.5, 8.6).

Every case runs against a real HTTP server on the loopback interface, so the request the plugin
actually builds is the one under test: its path, its headers, and its body.
"""

import json
import threading

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import SimpleTestCase

from netbox_data_import.inference_credentials import (
    CredentialDenied,
    CredentialFailure,
    CredentialReference,
    CredentialUnavailable,
    InvalidCredentialReference,
    InvalidSecretMaterial,
    VaultKvV2CredentialBackend,
)

SECRET = "sk-do-not-leak-this-value"
VAULT_TOKEN = "s.token-do-not-leak"
REFERENCE = {"backend": "vault_kv_v2", "mount": "secret", "path": "inference/backend", "field": "api_key"}


class RecordingVault(BaseHTTPRequestHandler):
    """Answer one KV v2 read with whatever the enclosing test configured."""

    status = 200
    payload: object = {"data": {"data": {"api_key": SECRET}}}
    seen: list = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler names the hook.
        length = int(self.headers.get("Content-Length") or 0)
        type(self).seen.append(
            {
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": self.rfile.read(length).decode() if length else "",
                "method": "GET",
            }
        )
        body = self.payload if isinstance(self.payload, (bytes, str)) else json.dumps(self.payload)
        encoded = body.encode() if isinstance(body, str) else body
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        """Keep the test output quiet."""


@contextmanager
def serving(status=200, payload=None):
    """Run a Vault stand-in on the loopback interface and yield its settings and request log."""

    class Handler(RecordingVault):
        pass

    Handler.status = status
    Handler.payload = {"data": {"data": {"api_key": SECRET}}} if payload is None else payload
    Handler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            {
                "address": f"http://127.0.0.1:{server.server_address[1]}",
                "auth_method": "proxy",
                "connect_timeout": 2,
                "read_timeout": 2,
            },
            Handler.seen,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class CredentialReferenceTest(SimpleTestCase):
    """The typed reference carries the KV v2 mount and nothing about the connection."""

    def test_a_complete_reference_is_accepted(self):
        reference = CredentialReference.from_mapping(REFERENCE)

        self.assertEqual((reference.mount, reference.path, reference.field), ("secret", "inference/backend", "api_key"))

    def test_a_missing_field_is_rejected(self):
        with self.assertRaises(InvalidCredentialReference):
            CredentialReference.from_mapping({**REFERENCE, "field": ""})

    def test_a_vault_address_is_rejected(self):
        """Connection data belongs to the deployment-owned vault setting."""
        with self.assertRaises(InvalidCredentialReference):
            CredentialReference.from_mapping({**REFERENCE, "address": "https://vault.example.invalid:8200"})

    def test_a_token_is_rejected(self):
        with self.assertRaises(InvalidCredentialReference):
            CredentialReference.from_mapping({**REFERENCE, "token": VAULT_TOKEN})

    def test_another_backend_is_rejected(self):
        with self.assertRaises(InvalidCredentialReference):
            CredentialReference.from_mapping({**REFERENCE, "backend": "aws_secrets_manager"})

    def test_the_reference_repr_hides_nothing_because_it_holds_no_secret(self):
        """The reference is restricted metadata, not secret material."""
        reference = CredentialReference.from_mapping(REFERENCE)

        self.assertNotIn(SECRET, repr(reference))


class VaultReadTest(SimpleTestCase):
    """The Vault backend reads one field from one path over a real request."""

    def resolve(self, settings, reference=None):
        """Resolve the reference through a backend built on the given vault settings."""
        backend = VaultKvV2CredentialBackend(settings)
        return backend.resolve(CredentialReference.from_mapping(reference or REFERENCE))

    def test_the_configured_field_is_returned(self):
        with serving() as (settings, seen):
            self.assertEqual(self.resolve(settings), SECRET)

        self.assertEqual(len(seen), 1)

    def test_the_request_names_the_kv_v2_data_path(self):
        with serving() as (settings, seen):
            self.resolve(settings)

        self.assertEqual(seen[0]["path"], "/v1/secret/data/inference/backend")

    def test_the_request_carries_no_plugin_data(self):
        """Vault learns nothing about a Source Trace, a device, a contact or an operator."""
        with serving() as (settings, seen):
            self.resolve(settings)

        self.assertEqual(seen[0]["body"], "")
        self.assertEqual(seen[0]["method"], "GET")

    def test_the_proxy_auth_method_sends_no_token(self):
        with serving() as (settings, seen):
            self.resolve(settings)

        self.assertNotIn("x-vault-token", seen[0]["headers"])

    def test_the_token_auth_method_sends_the_environment_token(self):
        import os

        with serving() as (settings, seen):
            os.environ["VAULT_TOKEN"] = VAULT_TOKEN
            try:
                self.resolve({**settings, "auth_method": "token"})
            finally:
                del os.environ["VAULT_TOKEN"]

        self.assertEqual(seen[0]["headers"]["x-vault-token"], VAULT_TOKEN)

    def test_the_token_auth_method_without_a_token_fails_as_configuration(self):
        import os

        os.environ.pop("VAULT_TOKEN", None)
        with serving() as (settings, _seen):
            with self.assertRaises(CredentialFailure) as caught:
                self.resolve({**settings, "auth_method": "token"})

        self.assertEqual(caught.exception.category, "invalid_configuration")

    def test_a_namespace_is_sent_when_configured(self):
        with serving() as (settings, seen):
            self.resolve({**settings, "namespace": "team-a"})

        self.assertEqual(seen[0]["headers"]["x-vault-namespace"], "team-a")


class VaultFailureClassificationTest(SimpleTestCase):
    """Each documented condition maps to its typed failure class, and none carries the secret."""

    def resolve(self, settings, reference=None):
        """Resolve one reference and return whatever it raises."""
        backend = VaultKvV2CredentialBackend(settings)
        return backend.resolve(CredentialReference.from_mapping(reference or REFERENCE))

    def failure(self, status=200, payload=None):
        """Return the failure one Vault answer produces."""
        with serving(status=status, payload=payload) as (settings, _seen):
            with self.assertRaises(CredentialFailure) as caught:
                self.resolve(settings)
        return caught.exception

    def test_a_denied_read_is_credential_denied(self):
        failure = self.failure(status=403, payload={"errors": ["permission denied"]})

        self.assertIsInstance(failure, CredentialDenied)
        self.assertEqual(failure.category, "credential_denied")

    def test_an_unauthenticated_read_is_credential_denied(self):
        self.assertEqual(
            self.failure(status=401, payload={"errors": ["missing client token"]}).category, "credential_denied"
        )

    def test_a_missing_path_is_credential_unavailable(self):
        failure = self.failure(status=404, payload={"errors": []})

        self.assertIsInstance(failure, CredentialUnavailable)
        self.assertEqual(failure.category, "credential_unavailable")

    def test_a_server_error_is_credential_unavailable(self):
        self.assertEqual(self.failure(status=500, payload={"errors": ["sealed"]}).category, "credential_unavailable")

    def test_a_malformed_envelope_is_credential_unavailable(self):
        self.assertEqual(self.failure(payload="not json at all").category, "credential_unavailable")

    def test_an_absent_field_is_invalid_secret_material(self):
        failure = self.failure(payload={"data": {"data": {"other": SECRET}}})

        self.assertIsInstance(failure, InvalidSecretMaterial)
        self.assertEqual(failure.category, "invalid_secret_material")

    def test_an_empty_field_is_invalid_secret_material(self):
        self.assertEqual(self.failure(payload={"data": {"data": {"api_key": ""}}}).category, "invalid_secret_material")

    def test_a_non_string_field_is_invalid_secret_material(self):
        self.assertEqual(
            self.failure(payload={"data": {"data": {"api_key": 1234}}}).category, "invalid_secret_material"
        )

    def test_an_unreachable_vault_is_credential_unavailable(self):
        settings = {"address": "http://127.0.0.1:1", "auth_method": "proxy", "connect_timeout": 1, "read_timeout": 1}
        backend = VaultKvV2CredentialBackend(settings)

        with self.assertRaises(CredentialUnavailable):
            backend.resolve(CredentialReference.from_mapping(REFERENCE))

    def test_no_failure_message_carries_the_secret_or_the_response_body(self):
        """Section 8.6: no exception text may carry the secret or a Vault response body."""
        cases = (
            {"status": 403, "payload": {"errors": [f"denied for {SECRET}"]}},
            {"status": 500, "payload": {"errors": [SECRET]}},
            {"payload": {"data": {"data": {"api_key": ""}}}},
            {"payload": {"data": {"data": {"other": SECRET}}}},
            {"payload": f"garbage {SECRET}"},
        )

        for case in cases:
            with self.subTest(case=case):
                failure = self.failure(**case)

                self.assertNotIn(SECRET, str(failure))
                self.assertNotIn(SECRET, repr(failure))
                self.assertNotIn("denied for", str(failure))

    def test_a_successful_resolution_is_not_written_into_the_backend(self):
        """The plugin caches no key value, so rotation takes effect on the next resolution."""
        with serving() as (settings, _seen):
            backend = VaultKvV2CredentialBackend(settings)
            backend.resolve(CredentialReference.from_mapping(REFERENCE))

            self.assertNotIn(SECRET, repr(vars(backend)))

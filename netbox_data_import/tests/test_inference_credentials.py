# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The credential boundary and its Vault KV v2 implementation (specification 8.5, 8.6).

Every case runs against a real HTTP server on the loopback interface, so the request the plugin
actually builds is the one under test: its path, its headers, and its body.
"""

import json
import socket
import threading

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

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

    def do_GET(self):
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
def serving(status=200, payload=None, handler=RecordingVault):
    """Run a Vault stand-in on the loopback interface and yield its settings and request log."""

    class Handler(handler):
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


@contextmanager
def serving_rebinding():
    """Run approved and private Vault stand-ins that share one port."""

    class Approved(RecordingVault):
        pass

    class Private(RecordingVault):
        pass

    Approved.seen = []
    Private.seen = []
    approved_server = ThreadingHTTPServer(("127.0.0.1", 0), Approved)
    port = approved_server.server_address[1]
    private_server = ThreadingHTTPServer(("127.0.0.2", port), Private)
    servers = (approved_server, private_server)
    threads = tuple(threading.Thread(target=server.serve_forever, daemon=True) for server in servers)
    for thread in threads:
        thread.start()
    try:
        yield (
            {
                "address": f"http://localhost:{port}",
                "auth_method": "proxy",
                "connect_timeout": 2,
                "read_timeout": 2,
            },
            Approved.seen,
            Private.seen,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def rebinding_dns(original):
    """Return the approved address to a trust lookup and a private one to a normal lookup."""

    def getaddrinfo(host, port, *args, **kwargs):
        proto = kwargs.get("proto", args[2] if len(args) > 2 else 0)
        if host == "localhost":
            address = "127.0.0.1" if proto == socket.IPPROTO_TCP else "127.0.0.2"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
        return original(host, port, *args, **kwargs)

    return getaddrinfo


class CloseRecordingSession(requests.Session):
    """A real Requests session that records when its pools are closed."""

    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True
        super().close()


@contextmanager
def vault_token(value):
    """Set VAULT_TOKEN for the block, then put back whatever the process had, including nothing."""
    import os

    previous = os.environ.get("VAULT_TOKEN")
    if value is None:
        os.environ.pop("VAULT_TOKEN", None)
    else:
        os.environ["VAULT_TOKEN"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("VAULT_TOKEN", None)
        else:
            os.environ["VAULT_TOKEN"] = previous


class CredentialReferenceTest(SimpleTestCase):
    """The typed reference carries the KV v2 mount and nothing about the connection."""

    def test_a_complete_reference_is_accepted(self):
        reference = CredentialReference.from_mapping(REFERENCE)

        self.assertEqual((reference.mount, reference.path, reference.field), ("secret", "inference/backend", "api_key"))

    def test_a_missing_field_is_rejected(self):
        with self.assertRaises(InvalidCredentialReference):
            CredentialReference.from_mapping({**REFERENCE, "field": ""})

    def test_a_traversal_path_is_rejected(self):
        """The path is interpolated into a URL, so a dot segment could name another secret."""
        for value in ("../../other", "inference/../../other", "inference/./backend"):
            with self.subTest(path=value):
                with self.assertRaises(InvalidCredentialReference):
                    CredentialReference.from_mapping({**REFERENCE, "path": value})

    def test_url_syntax_in_the_reference_is_rejected(self):
        """A query or fragment delimiter changes which Vault request the read actually makes."""
        for key, value in (
            ("path", "inference?list=true"),
            ("path", "inference#fragment"),
            ("path", "inference%2f..%2fother"),
            ("mount", "secret/data"),
            ("mount", "secret?x=1"),
            ("path", "inference//backend"),
        ):
            with self.subTest(**{key: value}):
                with self.assertRaises(InvalidCredentialReference):
                    CredentialReference.from_mapping({**REFERENCE, key: value})

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

    def test_close_closes_only_an_owned_session(self):
        with serving() as (settings, _seen):
            owned = CloseRecordingSession()
            with patch("netbox_data_import.inference_credentials.requests.Session", autospec=True, return_value=owned):
                owned_backend = VaultKvV2CredentialBackend(settings)
            injected = CloseRecordingSession()
            injected_backend = VaultKvV2CredentialBackend(settings, session=injected)

            owned_backend.close()
            injected_backend.close()

        self.assertTrue(owned.closed)
        self.assertFalse(injected.closed)
        injected.close()

    def test_context_exit_closes_an_owned_session_after_success_and_failure(self):
        for status in (200, 403):
            with self.subTest(status=status), serving(status=status) as (settings, _seen):
                owned = CloseRecordingSession()
                with patch(
                    "netbox_data_import.inference_credentials.requests.Session", autospec=True, return_value=owned
                ):
                    backend = VaultKvV2CredentialBackend(settings)
                try:
                    with backend as store:
                        self.assertIs(store, backend)
                        store.resolve(CredentialReference.from_mapping(REFERENCE))
                except CredentialDenied:
                    self.assertEqual(status, 403)

                self.assertTrue(owned.closed)

    def test_the_configured_field_is_returned(self):
        with serving() as (settings, seen):
            self.assertEqual(self.resolve(settings), SECRET)

        self.assertEqual(len(seen), 1)

    def test_the_connection_uses_the_address_resolved_for_the_vault_origin(self):
        original = socket.getaddrinfo
        with serving_rebinding() as (settings, approved_seen, private_seen):
            with patch("socket.getaddrinfo", side_effect=rebinding_dns(original)):
                self.assertEqual(self.resolve(settings), SECRET)

        self.assertEqual(len(approved_seen), 1)
        self.assertEqual(approved_seen[0]["headers"]["host"], settings["address"].removeprefix("http://"))
        self.assertEqual(private_seen, [])

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
        with serving() as (settings, seen):
            with vault_token(VAULT_TOKEN):
                self.resolve({**settings, "auth_method": "token"})

        self.assertEqual(seen[0]["headers"]["x-vault-token"], VAULT_TOKEN)

    def test_the_token_auth_method_without_a_token_fails_as_configuration(self):
        with serving() as (settings, _seen):
            with vault_token(None), self.assertRaises(CredentialFailure) as caught:
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

    def test_a_three_hundred_answer_is_never_read_as_a_secret(self):
        """Only some 3xx codes were listed, so a KV-shaped 300 body was accepted as the secret."""
        failure = self.failure(status=300, payload={"data": {"data": {"api_key": "sk-not-a-secret"}}})

        self.assertIsInstance(failure, CredentialFailure)
        self.assertNotIn("sk-not-a-secret", str(failure))

    def test_a_not_modified_answer_is_never_read_as_a_secret(self):
        self.assertIsInstance(self.failure(status=304, payload=None), CredentialFailure)

    def test_a_kv_envelope_holding_no_mapping_is_credential_unavailable(self):
        """The parse guard ends before the field lookup, so a list reached it and raised TypeError."""
        failure = self.failure(status=200, payload={"data": {"data": ["api_key"]}})

        self.assertIsInstance(failure, CredentialUnavailable)
        self.assertEqual(failure.category, "credential_unavailable")

    def test_a_kv_envelope_holding_a_scalar_is_credential_unavailable(self):
        self.assertIsInstance(self.failure(status=200, payload={"data": {"data": 7}}), CredentialUnavailable)

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

    def test_an_unreachable_vault_does_not_quote_its_address(self):
        """`run_connection_test` stores this text in `Job.data`, which is readable in the UI.

        The address is deployment infrastructure, so the failure names the class of fault only.
        """
        # Loopback: a hostname here is answered by the environment's proxy instead of raising.
        settings = {
            "address": "http://127.0.0.1:9",
            "auth_method": "proxy",
            "connect_timeout": 1,
            "read_timeout": 1,
        }
        backend = VaultKvV2CredentialBackend(settings)

        with self.assertRaises(CredentialUnavailable) as caught:
            backend.resolve(CredentialReference.from_mapping(REFERENCE))

        message = str(caught.exception)
        self.assertNotIn("127.0.0.1", message)
        self.assertNotIn(":9", message)
        self.assertIn("credential store", message)

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


class VaultRedirectTest(SimpleTestCase):
    """A redirecting Vault names an address problem, not an unreadable secret."""

    class Redirecting(RecordingVault):
        """Answer with a redirect the client must not follow."""

        def do_GET(self):
            type(self).seen.append({"path": self.path, "headers": {}, "body": "", "method": "GET"})
            self.send_response(307)
            self.send_header("Location", "https://vault-active.example.invalid:8200/v1/secret/data/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            """Keep the test output quiet."""

    def test_a_redirect_is_reported_as_a_configuration_problem(self):
        with serving(handler=self.Redirecting) as (settings, _seen):
            backend = VaultKvV2CredentialBackend(settings)

            with self.assertRaises(CredentialFailure) as caught:
                backend.resolve(CredentialReference.from_mapping(REFERENCE))

        self.assertEqual(caught.exception.category, "invalid_configuration")
        self.assertIn("redirect", str(caught.exception))

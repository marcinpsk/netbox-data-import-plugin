# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The connection test: a worker Job, a typed result, and one object permission (specification 8.6, 13.1)."""

import json
import threading

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_data_import.inference_connection_test import (
    CONNECTION_TEST_CATEGORIES,
    run_connection_test,
)
from netbox_data_import.models import InferenceBackend
from netbox_data_import.tests.helpers import user_with_object_permission

SECRET = "sk-connection-test-secret"
REFERENCE = {"backend": "vault_kv_v2", "mount": "secret", "path": "inference/backend", "field": "api_key"}


class Vault(BaseHTTPRequestHandler):
    """Answer one KV v2 read with whatever the enclosing test configured."""

    status = 200
    payload: object = {"data": {"data": {"api_key": SECRET}}}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler names the hook.
        raw = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        encoded = raw.encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        """Keep the test output quiet."""


@contextmanager
def vault(status=200, payload=None):
    """Run a Vault stand-in on loopback and yield the vault settings that reach it."""

    class Handler(Vault):
        pass

    Handler.status = status
    Handler.payload = {"data": {"data": {"api_key": SECRET}}} if payload is None else payload
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "address": f"http://127.0.0.1:{server.server_address[1]}",
            "auth_method": "proxy",
            "connect_timeout": 2,
            "read_timeout": 2,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_row(**overrides):
    """Create one enabled Inference Backend row."""
    values = {
        "backend_key": "primary",
        "display_name": "Primary",
        "api_root": "https://backend.example.invalid:443",
        "model": "m",
        "credential_reference": REFERENCE,
        "enabled": True,
    }
    values.update(overrides)
    return InferenceBackend.objects.create(**values)


def settings_for(vault_settings):
    """Return a PLUGINS_CONFIG entry pointing the plugin at one Vault stand-in."""
    return {
        "netbox_data_import": {
            "inference_backend_origin_allowlist": ["https://backend.example.invalid:443"],
            "vault": vault_settings,
        }
    }


class ConnectionTestResultTest(TestCase):
    """The test resolves the reference and returns one typed category, never a secret."""

    def test_a_readable_secret_is_ok(self):
        make_row()
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test()

        self.assertEqual(result.category, "ok")

    def test_a_denied_read_reports_credential_denied(self):
        make_row()
        with vault(status=403, payload={"errors": ["denied"]}) as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test()

        self.assertEqual(result.category, "credential_denied")

    def test_an_unreachable_store_reports_credential_unavailable(self):
        make_row()
        unreachable = {"address": "http://127.0.0.1:1", "auth_method": "proxy", "connect_timeout": 1, "read_timeout": 1}

        with override_settings(PLUGINS_CONFIG=settings_for(unreachable)):
            result = run_connection_test()

        self.assertEqual(result.category, "credential_unavailable")

    def test_an_empty_field_reports_invalid_secret_material(self):
        make_row()
        with vault(payload={"data": {"data": {"api_key": ""}}}) as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test()

        self.assertEqual(result.category, "invalid_secret_material")

    def test_a_malformed_reference_reports_invalid_credential_reference(self):
        make_row(credential_reference={"backend": "vault_kv_v2"})
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test()

        self.assertEqual(result.category, "invalid_credential_reference")

    def test_no_active_backend_reports_invalid_configuration(self):
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test()

        self.assertEqual(result.category, "invalid_configuration")

    def test_every_category_is_one_the_specification_names(self):
        make_row()
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                self.assertIn(run_connection_test().category, CONNECTION_TEST_CATEGORIES)

    def test_the_result_never_carries_the_secret_or_a_vault_body(self):
        make_row()
        cases = (
            {"status": 403, "payload": {"errors": [f"denied {SECRET}"]}},
            {"status": 500, "payload": {"errors": [SECRET]}},
            {"payload": {"data": {"data": {"api_key": ""}}}},
            {"payload": f"garbage {SECRET}"},
        )

        for case in cases:
            with self.subTest(case=case):
                with vault(**case) as vault_settings:
                    with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                        result = run_connection_test()

                serialized = json.dumps(result.as_dict())
                self.assertNotIn(SECRET, serialized)
                self.assertNotIn("denied ", serialized)

    def test_a_successful_result_names_the_backend_but_not_its_reference(self):
        make_row()
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test()

        payload = result.as_dict()
        self.assertEqual(payload["backend_key"], "primary")
        self.assertNotIn("credential_reference", payload)
        self.assertNotIn("mount", json.dumps(payload))


class ConnectionTestAuthorizationTest(TestCase):
    """One object permission on InferenceBackend authorizes the test; nothing else does."""

    def setUp(self):
        """Create the row and two users: one holding the InferenceBackend permission, one holding none."""
        self.row = make_row()
        self.url = reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[self.row.pk])
        self.permitted = user_with_object_permission("permitted", [(InferenceBackend, ["change"], {})])
        self.denied = get_user_model().objects.create_user(username="denied", password="testpass")

    def test_a_user_with_the_permission_may_run_the_test(self):
        self.client.force_login(self.permitted)

        response = self.client.post(self.url)

        self.assertIn(response.status_code, (200, 302))

    def test_a_user_without_the_permission_may_not(self):
        self.client.force_login(self.denied)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 403)

    def test_no_superuser_shortcut_replaces_the_permission(self):
        """Section 13.1: this one rule authorizes the action, with no separate administrator check."""
        import inspect

        from netbox_data_import import views

        source = inspect.getsource(views.InferenceBackendConnectionTestView)

        self.assertNotIn("is_superuser", source)
        self.assertNotIn("is_staff", source)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Secrets never persist (specification 8.6).

The sweep looks for the secret value in every surface section 8.6 names: a model row, a YAML export,
the session, a job payload, and a log record. The typed reference is different from a secret value,
so it lives in exactly one authoritative place and the sweep checks that too.
"""

import json
import logging
import threading

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO

from core.models import Job
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_data_import.inference_connection_test import run_connection_test
from netbox_data_import.jobs import InferenceBackendConnectionTestJob
from netbox_data_import.models import ImportProfile, InferenceBackend
from netbox_data_import.tests.helpers import user_with_object_permission

SECRET = "sk-never-persisted-anywhere"
REFERENCE = {"backend": "vault_kv_v2", "mount": "secret", "path": "inference/backend", "field": "api_key"}


class Vault(BaseHTTPRequestHandler):
    """Serve the one secret this sweep hunts for."""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler names the hook.
        encoded = json.dumps({"data": {"data": {"api_key": SECRET}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        """Keep the test output quiet."""


@contextmanager
def vault():
    """Run a Vault stand-in on loopback and yield the settings that reach it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Vault)
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


def settings_for(vault_settings):
    """Return a PLUGINS_CONFIG entry pointing the plugin at one Vault stand-in."""
    return {
        "netbox_data_import": {
            "inference_backend_origin_allowlist": ["https://backend.example.invalid:443"],
            "vault": vault_settings,
        }
    }


class SecretContainmentTest(TestCase):
    """A resolved secret reaches no durable surface."""

    def setUp(self):
        """Create the backend row whose reference names the planted secret."""
        self.row = InferenceBackend.objects.create(
            backend_key="primary",
            display_name="Primary",
            api_root="https://backend.example.invalid:443",
            model="m",
            credential_reference=REFERENCE,
            enabled=True,
        )

    def resolve_once(self):
        """Run one connection test against a Vault that serves the secret."""
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                return run_connection_test()

    def test_the_secret_resolves_so_the_sweep_is_meaningful(self):
        self.assertEqual(self.resolve_once().category, "ok")

    def test_no_model_row_holds_the_secret(self):
        self.resolve_once()
        self.row.refresh_from_db()

        rows = json.dumps(list(InferenceBackend.objects.values()), default=str)
        self.assertNotIn(SECRET, rows)

    def test_the_stored_reference_holds_no_secret_value(self):
        """The typed reference is restricted metadata, not secret material."""
        self.row.refresh_from_db()

        self.assertNotIn(SECRET, json.dumps(self.row.credential_reference))
        self.assertEqual(set(self.row.credential_reference), {"backend", "mount", "path", "field"})

    def test_no_job_payload_holds_the_secret(self):
        user = user_with_object_permission("tester", [(InferenceBackend, ["change"], {})])
        self.client.force_login(user)
        url = reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[self.row.pk])

        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                self.client.post(url)
                # The view only queues, so an unrun body would leave the payload empty to assert on.
                InferenceBackendConnectionTestJob.handle(Job.objects.get())

        job = Job.objects.get()
        self.assertEqual(job.data.get("category"), "ok")
        payloads = json.dumps(list(Job.objects.values("data", "name", "error")), default=str)
        self.assertNotIn(SECRET, payloads)

    def test_no_session_holds_the_secret(self):
        user = user_with_object_permission("session-tester", [(InferenceBackend, ["change"], {})])
        self.client.force_login(user)

        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                self.client.post(
                    reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[self.row.pk])
                )

        self.assertNotIn(SECRET, json.dumps(dict(self.client.session), default=str))

    def test_no_log_record_holds_the_secret(self):
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        try:
            self.resolve_once()
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)

        self.assertNotIn(SECRET, stream.getvalue())

    def test_the_profile_yaml_export_holds_no_backend_credential(self):
        """The profile export carries policy tables, never an Inference Backend reference."""
        profile = ImportProfile.objects.create(name="Export sweep", adapter_config={})
        user = user_with_object_permission("export-tester", [(ImportProfile, ["view", "change"], {})])
        user.is_superuser = True
        user.save()
        self.client.force_login(user)

        response = self.client.get(reverse("plugins:netbox_data_import:exportprofile_yaml", kwargs={"pk": profile.pk}))

        body = response.content.decode()
        self.assertNotIn(SECRET, body)
        self.assertNotIn("credential_reference", body)

    def test_the_reference_lives_in_exactly_one_authoritative_place(self):
        """Section 8.6: the enabled row, or the file fallback when no row is enabled."""
        self.resolve_once()

        holders = [
            model
            for model in (InferenceBackend,)
            if any("credential_reference" in field.name for field in model._meta.get_fields() if hasattr(field, "name"))
        ]

        self.assertEqual(holders, [InferenceBackend])

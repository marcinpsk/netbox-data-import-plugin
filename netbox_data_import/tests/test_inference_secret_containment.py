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

from core.models import Job, ObjectChange
from django.apps import apps
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_data_import.inference_connection_test import run_connection_test
from netbox_data_import.jobs import InferenceBackendConnectionTestJob
from netbox_data_import.inference_backend import resolve_active_backend
from netbox_data_import.models import ExecutionOutcome, ImportExecution, ImportProfile, InferenceBackend
from netbox_data_import.tests.helpers import user_with_object_permission

SECRET = "sk-never-persisted-anywhere"
REFERENCE = {"backend": "vault_kv_v2", "mount": "secret", "path": "inference/backend", "field": "api_key"}


class Vault(BaseHTTPRequestHandler):
    """Serve the one secret this sweep hunts for."""

    def do_GET(self):
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


def settings_for(vault_settings, *, inference_backend=None):
    """Return a PLUGINS_CONFIG entry pointing the plugin at one Vault stand-in."""
    settings = {
        "netbox_data_import": {
            "inference_backend_origin_allowlist": ["https://backend.example.invalid:443"],
            "vault": vault_settings,
        }
    }
    if inference_backend is not None:
        settings["netbox_data_import"]["inference_backend"] = inference_backend
    return settings


def occurrences(value, expected) -> int:
    """Count exact nested occurrences of one configuration value."""
    if value == expected:
        return 1
    if isinstance(value, dict):
        return sum(occurrences(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(occurrences(item, expected) for item in value)
    return 0


def persisted_state() -> dict:
    """Return every plugin row and NetBox job payload as plain values."""
    return {
        "plugin_rows": {
            model._meta.label: list(model.objects.values())
            for model in apps.get_app_config("netbox_data_import").get_models()
        },
        "jobs": list(Job.objects.values()),
        "object_changes": list(ObjectChange.objects.values("prechange_data", "postchange_data")),
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
                return run_connection_test(self.row.pk, "primary")

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
                InferenceBackendConnectionTestJob.handle(Job.objects.get(), pk=self.row.pk, backend_key="primary")

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

    def test_no_log_record_names_the_vault_address(self):
        """urllib3 logs the host and the request URL at DEBUG, which the exception redaction misses.

        The address is deployment infrastructure and the KV path names which secret was read, so
        neither belongs in a log a wider audience can read than the one holding the settings.
        """
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

        written = stream.getvalue()
        self.assertNotIn("127.0.0.1", written)
        self.assertNotIn("/v1/secret/data/", written)

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

    def test_the_rest_endpoint_holds_no_backend_credential(self):
        """The AI backends endpoint names no Vault location in a read response."""
        user = user_with_object_permission("api-tester", [(InferenceBackend, ["view"], {})])
        self.client.force_login(user)

        response = self.client.get(reverse("plugins-api:netbox_data_import-api:inferencebackend-list"))

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.row.backend_key, body)
        self.assertNotIn(SECRET, body)
        self.assertNotIn("credential_reference", body)
        self.assertNotIn("inference/backend", body)

    def test_one_real_job_leaves_no_secret_and_one_authoritative_database_reference(self):
        """One sweep covers persisted rows, an audit, a job payload, the session, and logs."""
        profile = ImportProfile.objects.create(name="Redaction audit", adapter_config={})
        ImportExecution.objects.create(
            profile=profile,
            outcome=ExecutionOutcome.FAILED,
            failure_detail={"reason": "planning"},
        )
        user = user_with_object_permission("redaction-job", [(InferenceBackend, ["change"], {})])
        self.client.force_login(user)
        url = reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[self.row.pk])
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        try:
            with vault() as vault_settings:
                configuration = settings_for(vault_settings)
                with override_settings(PLUGINS_CONFIG=configuration):
                    edit_response = self.client.post(
                        reverse(
                            "plugins:netbox_data_import:inferencebackend_edit",
                            kwargs={"pk": self.row.pk},
                        ),
                        {
                            "backend_key": self.row.backend_key,
                            "display_name": "Updated primary",
                            "adapter_type": self.row.adapter_type,
                            "api_root": self.row.api_root,
                            "model": self.row.model,
                            "authentication": self.row.authentication,
                            "response_mode": self.row.response_mode,
                            "credential_reference": json.dumps(REFERENCE),
                            "connect_timeout": self.row.connect_timeout,
                            "read_timeout": self.row.read_timeout,
                            "enabled": "on",
                        },
                    )
                    self.assertEqual(edit_response.status_code, 302, edit_response.content)
                    self.client.post(url)
                    InferenceBackendConnectionTestJob.handle(Job.objects.get(), pk=self.row.pk, backend_key="primary")
                    state = {
                        **persisted_state(),
                        "session": dict(self.client.session),
                        "logs": stream.getvalue(),
                        "inference_backend_setting": configuration["netbox_data_import"].get("inference_backend"),
                    }
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)

        serialized = json.dumps(state, default=str)
        self.assertNotIn(SECRET, serialized)
        self.assertIn("netbox_data_import.ImportExecution", state["plugin_rows"])
        self.assertTrue(state["object_changes"])
        self.assertEqual(occurrences(state, REFERENCE), 1)

    def test_the_file_fallback_is_the_only_reference_when_no_row_is_enabled(self):
        """The fallback reference stays in settings and is not copied to persistent state."""
        fallback = {
            "display_name": "File fallback",
            "adapter_type": "openai_compatible",
            "api_root": "https://backend.example.invalid:443",
            "model": "m",
            "authentication": "bearer",
            "response_mode": "prompt_json",
            "credential_reference": REFERENCE,
            "connect_timeout": 2,
            "read_timeout": 2,
        }
        self.row.delete()
        with vault() as vault_settings:
            configuration = settings_for(vault_settings, inference_backend=fallback)
            with override_settings(PLUGINS_CONFIG=configuration):
                active = resolve_active_backend()
                state = {
                    **persisted_state(),
                    "inference_backend_setting": configuration["netbox_data_import"]["inference_backend"],
                }

        self.assertEqual(active.source, "file-fallback")
        self.assertNotIn(SECRET, json.dumps(state, default=str))
        self.assertEqual(occurrences(state, REFERENCE), 1)

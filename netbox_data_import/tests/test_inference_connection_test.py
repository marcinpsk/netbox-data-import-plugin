# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The foreground connection test and its one object permission (specification 8.6, 13.1)."""

import json
import pathlib
import socket

from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from netbox_data_import.inference_connection_test import (
    CONNECTION_TEST_CATEGORIES,
    run_connection_test,
)
from netbox_data_import.models import InferenceBackend
from netbox_data_import.tests.helpers import user_with_object_permission
from netbox_data_import.tests.inference_http import issue_server_certificate, serving_tls
from netbox_data_import.tests.test_inference_adapter import completion, serving as serving_backend

SECRET = "sk-connection-test-secret"
REFERENCE = {"backend": "vault_kv_v2", "mount": "secret", "path": "inference/backend", "field": "api_key"}


# The paths the Vault stand-in was asked for, so a test can assert which secret was read.
SEEN_PATHS: list[str] = []


class Vault(BaseHTTPRequestHandler):
    """Answer one KV v2 read with whatever the enclosing test configured."""

    status = 200
    payload: object = {"data": {"data": {"api_key": SECRET}}}

    def do_GET(self):
        SEEN_PATHS.append(self.path)
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
    SEEN_PATHS.clear()
    with TemporaryDirectory() as temporary:
        ca_path, certificate_path, key_path = issue_server_certificate(pathlib.Path(temporary), "localhost")
        with serving_tls(Handler, Handler.payload, certificate_path, key_path) as (port, _seen, _server_names):
            yield {
                "address": f"https://localhost:{port}",
                "auth_method": "proxy",
                "ca_bundle": str(ca_path),
                "connect_timeout": 2,
                "read_timeout": 2,
            }


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


def settings_for(vault_settings, *, origin_allowlist=None):
    """Return a PLUGINS_CONFIG entry pointing the plugin at one Vault stand-in."""
    return {
        "netbox_data_import": {
            "inference_backend_origin_allowlist": origin_allowlist or ["https://backend.example.invalid:443"],
            "vault": vault_settings,
        }
    }


class ConnectionTestResultTest(TestCase):
    """The test resolves the reference and returns one typed category, never a secret."""

    def test_a_readable_secret_is_ok(self):
        with serving_backend() as (root, _seen, allowlist):
            row = make_row(api_root=root)
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "ok")

    def test_a_successful_test_calls_chat_completions_and_returns_discovered_models(self):
        models_payload = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        with serving_backend(models_payload=models_payload) as (root, seen, allowlist):
            row = make_row(api_root=root, model="model-a")
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "ok")
        self.assertEqual(result.models, ("model-a", "model-b"))
        self.assertEqual([request["path"] for request in seen], ["/models", "/chat/completions"])
        self.assertEqual(json.loads(seen[1]["body"])["model"], "model-a")

    def test_unsupported_model_discovery_does_not_fail_a_working_completion(self):
        with serving_backend(models_status=404, models_payload={"detail": "not found"}) as (root, seen, allowlist):
            row = make_row(api_root=root)
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "ok")
        self.assertEqual(result.models, ())
        self.assertEqual([request["path"] for request in seen], ["/models", "/chat/completions"])

    def test_a_completion_failure_stays_typed_and_keeps_discovered_models(self):
        models_payload = {"data": [{"id": "working-model"}]}
        with serving_backend(status=401, payload={"detail": "denied"}, models_payload=models_payload) as (
            root,
            _seen,
            allowlist,
        ):
            row = make_row(api_root=root)
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "authentication_failure")
        self.assertEqual(result.models, ("working-model",))

    def test_a_rejected_request_with_discovered_models_explains_how_to_correct_the_model(self):
        models_payload = {"data": [{"id": "working-model"}]}
        with serving_backend(status=400, payload={"detail": "invalid model"}, models_payload=models_payload) as (
            root,
            _seen,
            allowlist,
        ):
            row = make_row(api_root=root, model="unknown-model")
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "invalid_configuration")
        self.assertEqual(result.models, ("working-model",))
        self.assertIn(
            "Select one of the available models below, save the backend, and run the test again.", result.detail
        )

    def test_a_completion_that_echoes_the_credential_returns_no_backend_text(self):
        with serving_backend(payload=completion(content=SECRET)) as (root, _seen, allowlist):
            row = make_row(api_root=root)
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "invalid_response")
        self.assertNotIn(SECRET, json.dumps(asdict(result)))

    def test_a_refused_or_empty_completion_does_not_pass_the_connection_test(self):
        payloads = (
            completion(content=None, message={"refusal": "cannot answer"}),
            completion(content="  "),
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                with serving_backend(payload=payload) as (root, _seen, allowlist):
                    backend_key = f"primary-{index}"
                    row = make_row(api_root=root, backend_key=backend_key, enabled=index == 0)
                    with vault() as vault_settings:
                        with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                            result = run_connection_test(row.pk, backend_key)

                self.assertEqual(result.category, "invalid_response")
                self.assertIn("did not return an answer", result.detail)

    def test_a_denied_read_reports_credential_denied(self):
        row = make_row()
        with vault(status=403, payload={"errors": ["denied"]}) as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "credential_denied")

    def test_an_unreachable_store_reports_credential_unavailable(self):
        row = make_row()
        with socket.socket() as bound_socket:
            bound_socket.bind(("127.0.0.1", 0))
            unreachable = {
                "address": f"https://127.0.0.1:{bound_socket.getsockname()[1]}",
                "auth_method": "proxy",
                "connect_timeout": 1,
                "read_timeout": 1,
            }

            with override_settings(PLUGINS_CONFIG=settings_for(unreachable)):
                result = run_connection_test(row.pk, "primary")

            self.assertEqual(result.category, "credential_unavailable")

    def test_a_missing_ca_bundle_reports_invalid_configuration(self):
        row = make_row()
        with TemporaryDirectory() as temporary:
            vault_settings = {
                "address": "https://127.0.0.1:1",
                "auth_method": "proxy",
                "ca_bundle": str(pathlib.Path(temporary) / "missing-ca.pem"),
                "connect_timeout": 1,
                "read_timeout": 1,
            }

            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "invalid_configuration")

    def test_an_empty_field_reports_invalid_secret_material(self):
        row = make_row()
        with vault(payload={"data": {"data": {"api_key": ""}}}) as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "invalid_secret_material")

    def test_a_malformed_reference_reports_invalid_credential_reference(self):
        row = make_row(credential_reference={"backend": "vault_kv_v2"})
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "invalid_credential_reference")

    def test_a_missing_row_reports_invalid_configuration(self):
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(0, "primary")

        self.assertEqual(result.category, "invalid_configuration")

    def test_every_category_is_one_the_specification_names(self):
        with serving_backend() as (root, _seen, allowlist):
            row = make_row(api_root=root)
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    self.assertIn(run_connection_test(row.pk, "primary").category, CONNECTION_TEST_CATEGORIES)

    def test_the_result_never_carries_the_secret_or_a_vault_body(self):
        row = make_row()
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
                        result = run_connection_test(row.pk, "primary")

                serialized = json.dumps(asdict(result))
                self.assertNotIn(SECRET, serialized)
                self.assertNotIn("denied ", serialized)

    def test_a_successful_result_names_the_backend_but_not_its_reference(self):
        with serving_backend() as (root, _seen, allowlist):
            row = make_row(api_root=root)
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "primary")

        payload = asdict(result)
        self.assertEqual(payload["backend_key"], "primary")
        self.assertNotIn("credential_reference", payload)
        self.assertNotIn("mount", json.dumps(payload))


class SelectedBackendTest(TestCase):
    """The view authorizes one row, so the foreground test must use that row and no other."""

    def test_the_named_backend_is_tested_rather_than_the_active_one(self):
        """A second enabled row must not answer for the row the operator selected.

        The two rows reference different Vault paths, so the assertion is which secret was read,
        not merely which key the result names.
        """
        with serving_backend() as (root, _seen, allowlist):
            row = make_row(
                backend_key="selected",
                display_name="Selected",
                enabled=False,
                api_root=root,
                credential_reference={**REFERENCE, "path": "inference/selected"},
            )
            make_row(
                backend_key="other-enabled",
                display_name="Other",
                enabled=True,
                credential_reference={**REFERENCE, "path": "inference/other"},
            )

            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "selected")

        self.assertEqual(result.category, "ok")
        self.assertEqual(result.backend_key, "selected")
        self.assertEqual([path for path in SEEN_PATHS if "inference/" in path], ["/v1/secret/data/inference/selected"])

    def test_a_deleted_row_does_not_reach_the_deployment_credential(self):
        """A missing authorized row is a refusal, even when its key names the file fallback."""
        row = make_row(backend_key="file-fallback", display_name="Mine", enabled=False)
        pk = row.pk
        row.delete()
        fallback = {
            "display_name": "Deployment fallback",
            "adapter_type": "openai_compatible",
            "api_root": "https://backend.example.invalid:443",
            "model": "m",
            "authentication": "bearer",
            "response_mode": "prompt_json",
            "credential_reference": {**REFERENCE, "path": "inference/deployment"},
            "connect_timeout": 5,
            "read_timeout": 60,
        }

        with vault() as vault_settings:
            config = settings_for(vault_settings)
            config["netbox_data_import"]["inference_backend"] = fallback
            with override_settings(PLUGINS_CONFIG=config):
                result = run_connection_test(pk, "file-fallback")

        self.assertEqual(result.category, "invalid_configuration")
        self.assertNotIn("/v1/secret/data/inference/deployment", SEEN_PATHS)

    def test_a_disabled_row_can_be_tested_before_it_is_enabled(self):
        """The operator tests a row to decide whether to enable it, so enabled is not the filter."""
        with serving_backend() as (root, _seen, allowlist):
            row = make_row(backend_key="selected", display_name="Selected", enabled=False, api_root=root)

            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    result = run_connection_test(row.pk, "selected")

        self.assertEqual(result.category, "ok")
        self.assertEqual(result.backend_key, "selected")

    def test_a_missing_row_id_is_invalid_configuration(self):
        make_row(backend_key="primary")

        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(0, "gone")

        self.assertEqual(result.category, "invalid_configuration")
        self.assertIn("gone", result.detail)


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

    def test_a_view_only_user_may_not_run_the_test(self):
        """Seeing a backend is not authority to make it call out; only `change` is."""
        viewer = user_with_object_permission("view-only", [(InferenceBackend, ["view"], {})])
        self.client.force_login(viewer)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 403)

    def test_no_superuser_shortcut_replaces_the_permission(self):
        """Section 13.1: this one rule authorizes the action, with no separate administrator check."""
        import inspect

        from netbox_data_import import views

        source = inspect.getsource(views.InferenceBackendConnectionTestView)

        self.assertNotIn("is_superuser", source)
        self.assertNotIn("is_staff", source)

    def test_a_constrained_user_cannot_test_a_backend_outside_its_scope(self):
        """A model-level check would let a scoped user reach any row, so the queryset is restricted."""
        other = InferenceBackend.objects.create(
            backend_key="other",
            display_name="Other",
            api_root="https://backend.example.invalid:443",
            model="m",
            credential_reference=REFERENCE,
        )
        scoped = user_with_object_permission("scoped", [(InferenceBackend, ["change"], {"backend_key": "primary"})])
        self.client.force_login(scoped)

        response = self.client.post(
            reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[other.pk])
        )

        self.assertEqual(response.status_code, 404)


class BackendDetailPageTest(TestCase):
    """Every redirect target in this feature has to render, or a save ends on a 500."""

    def setUp(self):
        """Create one row and a user allowed to view it."""
        self.row = make_row()
        self.viewer = user_with_object_permission("viewer", [(InferenceBackend, ["view", "change"], {})])
        self.client.force_login(self.viewer)

    def test_the_detail_page_renders(self):
        response = self.client.get(self.row.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "primary")

    def test_the_detail_page_shows_no_secret_bearing_field(self):
        """The reference is restricted metadata, so the page names its parts and no value."""
        response = self.client.get(self.row.get_absolute_url())

        self.assertContains(response, "inference/backend")
        self.assertNotContains(response, SECRET)

    def test_the_detail_page_has_no_model_picker_before_a_connection_test(self):
        response = self.client.get(self.row.get_absolute_url())

        self.assertNotContains(response, "Available models")

    def test_the_connection_test_runs_in_the_request_and_reports_the_result_on_the_backend_page(self):
        from core.models import Job

        models_payload = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        with serving_backend(models_payload=models_payload) as (root, seen, allowlist):
            self.row.api_root = root
            self.row.model = "model-a"
            self.row.save(update_fields=("api_root", "model"))
            with vault() as vault_settings:
                with override_settings(PLUGINS_CONFIG=settings_for(vault_settings, origin_allowlist=allowlist)):
                    response = self.client.post(
                        reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[self.row.pk]),
                        follow=True,
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(self.row.get_absolute_url(), 302)])
        self.assertContains(response, "Connection test succeeded")
        self.assertContains(response, "Available models")
        self.assertContains(response, '<option value="model-b">model-b</option>', html=True)
        self.assertEqual([request["path"] for request in seen], ["/models", "/chat/completions"])
        self.assertFalse(Job.objects.exists())
        self.assertNotContains(response, SECRET)

        edit_response = self.client.get(
            reverse("plugins:netbox_data_import:inferencebackend_edit", args=[self.row.pk]),
            {"model": "model-b"},
        )
        self.assertEqual(edit_response.context["form"]["model"].value(), "model-b")
        self.assertContains(edit_response, "Enter the exact model id")

        second_detail = self.client.get(self.row.get_absolute_url())
        self.assertNotContains(second_detail, "Available models")

    def test_a_failed_foreground_connection_test_reports_its_safe_category_and_detail(self):
        from core.models import Job

        with vault(status=403, payload={"errors": ["denied"]}) as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                response = self.client.post(
                    reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[self.row.pk]),
                    follow=True,
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(self.row.get_absolute_url(), 302)])
        self.assertContains(response, "Connection test failed (credential denied)")
        self.assertContains(response, "The credential store refused the read (HTTP 403)")
        self.assertFalse(Job.objects.exists())

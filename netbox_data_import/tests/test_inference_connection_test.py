# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The connection test: a worker Job, a typed result, and one object permission (specification 8.6, 13.1)."""

import json
import socket
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


# The paths the Vault stand-in was asked for, so a test can assert which secret was read.
SEEN_PATHS: list[str] = []


class Vault(BaseHTTPRequestHandler):
    """Answer one KV v2 read with whatever the enclosing test configured."""

    status = 200
    payload: object = {"data": {"data": {"api_key": SECRET}}}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler names the hook.
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
        row = make_row()
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(row.pk, "primary")

        self.assertEqual(result.category, "ok")

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
                "address": f"http://127.0.0.1:{bound_socket.getsockname()[1]}",
                "auth_method": "proxy",
                "connect_timeout": 1,
                "read_timeout": 1,
            }

            with override_settings(PLUGINS_CONFIG=settings_for(unreachable)):
                result = run_connection_test(row.pk, "primary")

            self.assertEqual(result.category, "credential_unavailable")

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
        row = make_row()
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
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

                serialized = json.dumps(result.as_dict())
                self.assertNotIn(SECRET, serialized)
                self.assertNotIn("denied ", serialized)

    def test_a_successful_result_names_the_backend_but_not_its_reference(self):
        row = make_row()
        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(row.pk, "primary")

        payload = result.as_dict()
        self.assertEqual(payload["backend_key"], "primary")
        self.assertNotIn("credential_reference", payload)
        self.assertNotIn("mount", json.dumps(payload))


class SelectedBackendTest(TestCase):
    """The view authorizes one row, so the worker has to test that row and no other."""

    def test_the_named_backend_is_tested_rather_than_the_active_one(self):
        """A second enabled row must not answer for the row the operator selected.

        The two rows reference different Vault paths, so the assertion is which secret was read,
        not merely which key the result names.
        """
        row = make_row(
            backend_key="selected",
            display_name="Selected",
            enabled=False,
            credential_reference={**REFERENCE, "path": "inference/selected"},
        )
        make_row(
            backend_key="other-enabled",
            display_name="Other",
            enabled=True,
            credential_reference={**REFERENCE, "path": "inference/other"},
        )

        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                result = run_connection_test(row.pk, "selected")

        self.assertEqual(result.backend_key, "selected")
        self.assertEqual([path for path in SEEN_PATHS if "inference/" in path], ["/v1/secret/data/inference/selected"])

    def test_a_deleted_row_does_not_reach_the_deployment_credential(self):
        """A missing authorized row is a refusal, even when its queued key names the file fallback."""
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

    def test_a_row_disabled_after_the_job_was_queued_is_still_the_one_tested(self):
        """The operator tests a row to decide whether to enable it, so enabled is not the filter."""
        row = make_row(backend_key="selected", display_name="Selected", enabled=False)

        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
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

    def test_the_view_hands_the_worker_the_pk_and_key_of_the_row_it_authorized(self):
        """The worker needs the authorized row identity and its operator-facing key."""
        row = make_row(backend_key="selected", display_name="Selected", enabled=False)
        permitted = user_with_object_permission("queuer", [(InferenceBackend, ["change"], {})])
        self.client.force_login(permitted)

        # NetBox enqueues through transaction.on_commit, so the real call is a captured partial.
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.client.post(reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[row.pk]))

        keywords = [getattr(callback, "keywords", {}) for callback in callbacks]
        self.assertIn(row.pk, [item.get("pk") for item in keywords])
        self.assertIn("selected", [item.get("backend_key") for item in keywords])


class ConnectionTestQueuedPathTest(TestCase):
    """The whole queued path: the view enqueues, and the worker runs what the view authorized."""

    def test_a_deleted_row_is_not_replaced_by_another_row_with_the_same_key(self):
        from core.models import Job

        from netbox_data_import.jobs import InferenceBackendConnectionTestJob

        row = make_row()
        self.client.force_login(user_with_object_permission("queuer", [(InferenceBackend, ["change"], {})]))
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(
                reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[row.pk])
            )
        self.assertEqual(response.status_code, 302)
        queued = next(keywords for callback in callbacks if (keywords := getattr(callback, "keywords", {})))
        # The worker loads the Job before the backend deletion cascades to its database row.
        job = Job.objects.get(name=InferenceBackendConnectionTestJob.Meta.name)
        row.delete()
        make_row(credential_reference={**REFERENCE, "path": "inference/replacement"})

        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                InferenceBackendConnectionTestJob.handle(
                    job, **{key: value for key, value in queued.items() if key != "job"}
                )

        job.refresh_from_db()
        self.assertEqual(SEEN_PATHS, [])
        self.assertEqual(job.data["category"], "invalid_configuration")
        self.assertEqual(job.data["backend_key"], "primary")
        self.assertIn("primary", job.data["detail"])

    def test_the_queued_job_tests_the_row_the_view_named_even_once_it_is_disabled(self):
        """Resolution happens on the worker later, so the row's state can change before it runs."""
        from core.models import Job

        from netbox_data_import.jobs import InferenceBackendConnectionTestJob

        row = make_row(
            backend_key="selected",
            display_name="Selected",
            enabled=True,
            credential_reference={**REFERENCE, "path": "inference/selected"},
        )
        # Only one row may be enabled, so this one waits to take over once `selected` steps down.
        other = make_row(
            backend_key="other-enabled",
            display_name="Other",
            enabled=False,
            credential_reference={**REFERENCE, "path": "inference/other"},
        )
        self.client.force_login(user_with_object_permission("queuer", [(InferenceBackend, ["change"], {})]))

        # NetBox pushes to the queue on commit; the Job row itself is written before that.
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.client.post(reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[row.pk]))

        queued = next(keywords for callback in callbacks if (keywords := getattr(callback, "keywords", {})))
        # The operator retires the tested row and promotes another before the worker picks the Job up.
        InferenceBackend.objects.filter(pk=row.pk).update(enabled=False)
        InferenceBackend.objects.filter(pk=other.pk).update(enabled=True)
        job = Job.objects.get(name=InferenceBackendConnectionTestJob.Meta.name)

        with vault() as vault_settings:
            with override_settings(PLUGINS_CONFIG=settings_for(vault_settings)):
                InferenceBackendConnectionTestJob.handle(job, pk=queued["pk"], backend_key=queued["backend_key"])

        job.refresh_from_db()
        self.assertEqual(job.data["backend_key"], "selected")
        self.assertIn(job.data["category"], CONNECTION_TEST_CATEGORIES)
        self.assertEqual([path for path in SEEN_PATHS if "inference/" in path], ["/v1/secret/data/inference/selected"])


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

    def test_the_connection_test_redirect_lands_on_a_page_that_renders(self):
        response = self.client.post(
            reverse("plugins:netbox_data_import:inferencebackend_connection_test", args=[self.row.pk]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

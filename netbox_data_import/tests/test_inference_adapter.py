# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The OpenAI-compatible adapter: one non-streaming Chat Completion, typed failures (specification 8.1, 8.4)."""

import ast
import json
import pathlib
import threading

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import SimpleTestCase

from netbox_data_import.inference_adapter import (
    AuthenticationFailure,
    BackendTimeout,
    InferenceRequest,
    InvalidBackendConfiguration,
    MalformedEnvelope,
    OpenAICompatibleAdapter,
    RateLimited,
    TransportFailure,
)

API_KEY = "sk-adapter-secret"


def completion(content='{"answer": 1}', finish_reason="stop", **extra):
    """Return one non-streaming Chat Completions envelope."""
    message = {"role": "assistant", "content": content}
    message.update(extra.pop("message", {}))
    return {
        "id": "cmpl-123",
        "model": "served-model",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        **extra,
    }


class RecordingBackend(BaseHTTPRequestHandler):
    """Answer one Chat Completions call with whatever the enclosing test configured."""

    status = 200
    payload: object = {}
    headers_out: dict = {}
    seen: list = []
    delay = 0.0

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler names the hook.
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append(
            {
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        if self.delay:
            import time

            time.sleep(self.delay)
        raw = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        encoded = raw.encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        for name, value in self.headers_out.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        """Keep the test output quiet."""


@contextmanager
def serving(status=200, payload=None, headers_out=None, delay=0.0):
    """Run a Chat Completions stand-in on loopback and yield its api_root and request log."""

    class Handler(RecordingBackend):
        pass

    Handler.status = status
    Handler.payload = completion() if payload is None else payload
    Handler.headers_out = headers_out or {}
    Handler.seen = []
    Handler.delay = delay
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", Handler.seen, [f"http://127.0.0.1:{port}"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def adapter_for(api_root, allowlist, **overrides):
    """Return an adapter pointed at the given root."""
    values = {
        "api_root": api_root,
        "model": "configured-model",
        "authentication": "bearer",
        "response_mode": "prompt_json",
        "allowlist": allowlist,
        "connect_timeout": 2,
        "read_timeout": 2,
    }
    values.update(overrides)
    return OpenAICompatibleAdapter(**values)


REQUEST = InferenceRequest(
    system_instruction="You map source ports to NetBox terminations.",
    user_payload_json='{"port": "Gi0/1"}',
    requested_response_mode="prompt_json",
)


class ChatCompletionRequestTest(SimpleTestCase):
    """The adapter sends one non-streaming Chat Completions call built from configuration."""

    def test_the_client_appends_chat_completions_to_the_api_root(self):
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(seen[0]["path"], "/chat/completions")

    def test_the_configured_model_is_sent_and_never_chosen_at_run_time(self):
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(json.loads(seen[0]["body"])["model"], "configured-model")

    def test_the_key_travels_as_a_bearer_token(self):
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(seen[0]["headers"]["authorization"], f"Bearer {API_KEY}")

    def test_streaming_is_never_requested(self):
        """Section 8.4 rejects streaming."""
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertIs(json.loads(seen[0]["body"])["stream"], False)

    def test_exactly_one_choice_is_requested(self):
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(json.loads(seen[0]["body"])["n"], 1)

    def test_no_tool_or_function_field_is_sent(self):
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        body = json.loads(seen[0]["body"])
        for rejected in ("tools", "tool_choice", "functions", "function_call", "stream_options"):
            self.assertNotIn(rejected, body)

    def test_the_system_instruction_and_payload_are_the_two_messages(self):
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        messages = json.loads(seen[0]["body"])["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], REQUEST.user_payload_json)

    def test_the_json_object_response_mode_is_sent_when_configured(self):
        asked = InferenceRequest(
            system_instruction=REQUEST.system_instruction,
            user_payload_json=REQUEST.user_payload_json,
            requested_response_mode="json_object",
        )

        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist, response_mode="json_object").complete(asked, api_key=API_KEY)

        self.assertEqual(json.loads(seen[0]["body"])["response_format"], {"type": "json_object"})

    def test_the_prompt_json_response_mode_sends_no_response_format(self):
        with serving() as (root, seen, allowlist):
            adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertNotIn("response_format", json.loads(seen[0]["body"]))


class CompletionParsingTest(SimpleTestCase):
    """A returned completion means the call reached the backend and the envelope parsed."""

    def complete(self, payload, **overrides):
        """Return the completion one envelope produces."""
        with serving(payload=payload) as (root, _seen, allowlist):
            return adapter_for(root, allowlist, **overrides).complete(REQUEST, api_key=API_KEY)

    def test_content_and_identifiers_are_returned(self):
        result = self.complete(completion(content='{"answer": 1}'))

        self.assertEqual(result.content_text, '{"answer": 1}')
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.backend_response_id, "cmpl-123")
        self.assertEqual(result.backend_model, "served-model")
        self.assertFalse(result.is_refusal)

    def test_stop_with_empty_content_is_a_refusal(self):
        result = self.complete(completion(content=""))

        self.assertTrue(result.is_refusal)

    def test_a_refusal_payload_instead_of_content_is_a_refusal(self):
        result = self.complete(completion(content=None, message={"refusal": "I cannot help with that."}))

        self.assertTrue(result.is_refusal)

    def test_a_length_finish_reason_raises_rather_than_returning_a_completion(self):
        with self.assertRaises(MalformedEnvelope):
            self.complete(completion(finish_reason="length"))

    def test_a_content_filter_finish_reason_raises(self):
        with self.assertRaises(MalformedEnvelope):
            self.complete(completion(finish_reason="content_filter"))

    def test_an_envelope_without_choices_raises(self):
        with self.assertRaises(MalformedEnvelope):
            self.complete({"id": "x", "choices": []})

    def test_a_body_that_is_not_json_raises(self):
        with self.assertRaises(MalformedEnvelope):
            self.complete("<html>gateway</html>")

    def test_several_choices_raise_because_the_adapter_rejects_them(self):
        envelope = completion()
        envelope["choices"].append({"index": 1, "finish_reason": "stop", "message": {"content": "second"}})

        with self.assertRaises(MalformedEnvelope):
            self.complete(envelope)


class FailureClassificationTest(SimpleTestCase):
    """Each documented backend condition maps to its typed failure class."""

    def failure(self, status, payload=None, headers_out=None):
        """Return the failure one backend answer produces."""
        with serving(status=status, payload=payload or {"error": {"message": "no"}}, headers_out=headers_out) as (
            root,
            _seen,
            allowlist,
        ):
            with self.assertRaises(Exception) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)
        return caught.exception

    def test_a_401_is_an_authentication_failure(self):
        self.assertIsInstance(self.failure(401), AuthenticationFailure)

    def test_a_403_is_an_authentication_failure(self):
        self.assertIsInstance(self.failure(403), AuthenticationFailure)

    def test_a_429_is_rate_limited(self):
        self.assertIsInstance(self.failure(429), RateLimited)

    def test_a_429_reports_its_retry_after(self):
        failure = self.failure(429, headers_out={"Retry-After": "17"})

        self.assertEqual(failure.retry_after, 17)

    def test_a_500_is_a_transport_failure(self):
        self.assertIsInstance(self.failure(500), TransportFailure)

    def test_an_unreachable_backend_is_a_transport_failure(self):
        adapter = adapter_for("http://127.0.0.1:1", ["http://127.0.0.1:1"])

        with self.assertRaises(TransportFailure):
            adapter.complete(REQUEST, api_key=API_KEY)

    def test_a_slow_backend_times_out(self):
        with serving(delay=3) as (root, _seen, allowlist):
            adapter = adapter_for(root, allowlist, read_timeout=1)

            with self.assertRaises(BackendTimeout):
                adapter.complete(REQUEST, api_key=API_KEY)

    def test_no_failure_message_carries_the_api_key(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                failure = self.failure(status, payload={"error": {"message": f"key {API_KEY} rejected"}})

                self.assertNotIn(API_KEY, str(failure))


class RedirectTest(SimpleTestCase):
    """A redirect to a disallowed target is not followed (specification 8.3)."""

    def test_a_redirect_is_refused_rather_than_followed(self):
        with serving(status=302, headers_out={"Location": "https://attacker.example.invalid/collect"}) as (
            root,
            seen,
            allowlist,
        ):
            with self.assertRaises(InvalidBackendConfiguration) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertIn("redirect", str(caught.exception))
        # Only the original call was made, so the key never reached the redirect target.
        self.assertEqual(len(seen), 1)


class AdapterIsolationTest(SimpleTestCase):
    """The adapter imports no NetBox, candidate, proposal or job model (ticket T7)."""

    FORBIDDEN = frozenset(
        {
            "circuits",
            "core",
            "dcim",
            "django",
            "extras",
            "ipam",
            "netbox",
            "tenancy",
            "utilities",
            "jobs",
            "models",
            "plan",
            "views",
            "import_engine",
            "target_modules",
            "cable_target",
            "netbox_reader",
        }
    )

    def test_the_adapter_module_imports_nothing_from_the_application(self):
        package = pathlib.Path(__file__).resolve().parents[1]
        source = (package / "inference_adapter.py").read_text()
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.removeprefix(f"{package.name}.").partition(".")[0])
            elif isinstance(node, ast.ImportFrom):
                roots.update(alias.name.partition(".")[0] for alias in node.names)

        self.assertEqual(sorted(roots & self.FORBIDDEN), [])


class RequestTimeTrustTest(SimpleTestCase):
    """The allowlist is enforced again at request time, not only at the form boundary."""

    def test_an_origin_outside_the_allowlist_is_refused_before_the_key_travels(self):
        """A row saved before the allowlist changed must not keep calling the old destination."""
        with serving() as (root, seen, _allowlist):
            adapter = adapter_for(root, allowlist=[])

            with self.assertRaises(InvalidBackendConfiguration) as caught:
                adapter.complete(REQUEST, api_key=API_KEY)

        # The precise phrase matters: the private-address rejection also says "allowlist".
        self.assertIn("is not on the inference_backend_origin_allowlist", str(caught.exception))
        self.assertEqual(seen, [])


class ResponseModeAgreementTest(SimpleTestCase):
    """The request states the mode it wants, so a backend configured for another one refuses."""

    def test_a_request_for_another_mode_is_refused(self):
        asked = InferenceRequest(system_instruction="s", user_payload_json="{}", requested_response_mode="json_object")

        with serving() as (root, seen, allowlist):
            adapter = adapter_for(root, allowlist, response_mode="prompt_json")

            with self.assertRaises(InvalidBackendConfiguration):
                adapter.complete(asked, api_key=API_KEY)

        self.assertEqual(seen, [])

    def test_the_json_schema_mode_is_refused_because_this_delivery_sends_no_schema(self):
        """Section 8.2 offers the mode, but no schema field exists to make a valid request."""
        asked = InferenceRequest(system_instruction="s", user_payload_json="{}", requested_response_mode="json_schema")

        with serving() as (root, seen, allowlist):
            adapter = adapter_for(root, allowlist, response_mode="json_schema")

            with self.assertRaises(InvalidBackendConfiguration) as caught:
                adapter.complete(asked, api_key=API_KEY)

        self.assertIn("schema", str(caught.exception))
        self.assertEqual(seen, [])


class NonStringContentTest(SimpleTestCase):
    """Content parts are common on OpenAI-compatible servers, and must stay inside the taxonomy."""

    def complete(self, payload):
        """Return whatever one envelope produces."""
        with serving(payload=payload) as (root, _seen, allowlist):
            return adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

    def test_a_content_parts_array_raises_a_typed_error(self):
        envelope = completion()
        envelope["choices"][0]["message"]["content"] = [{"type": "text", "text": "{}"}]

        with self.assertRaises(MalformedEnvelope):
            self.complete(envelope)

    def test_a_numeric_content_raises_a_typed_error(self):
        envelope = completion()
        envelope["choices"][0]["message"]["content"] = 7

        with self.assertRaises(MalformedEnvelope):
            self.complete(envelope)

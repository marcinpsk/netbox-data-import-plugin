# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The OpenAI-compatible adapter: one non-streaming Chat Completion, typed failures (specification 8.1, 8.4)."""

import ast
import json
import pathlib
import threading

import requests

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import SimpleTestCase

from netbox_data_import.inference_adapter import (
    BODY_ABSENT,
    BODY_EMPTY,
    BODY_INTERRUPTED,
    BODY_PRESENT,
    AuthenticationFailure,
    BackendTimeout,
    InferenceRequest,
    InvalidBackendConfiguration,
    MalformedEnvelope,
    OpenAICompatibleAdapter,
    RateLimited,
    TransportFailure,
    TRANSIENT_STATUSES,
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

    def do_POST(self):
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


@contextmanager
def serving_truncated():
    """Serve a Content-Length larger than the body sent, so the read is cut short."""

    class Truncating(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            # Promise more than is written, then close: the client sees an incomplete read.
            self.send_header("Content-Length", "4096")
            self.end_headers()
            self.wfile.write(b'{"diagnostic body prefix"')
            self.close_connection = True

        def log_message(self, *args):
            """Keep the test output quiet."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Truncating)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", [f"http://127.0.0.1:{port}"]
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

    def test_a_trailing_slash_in_the_api_root_is_refused_at_request_time(self):
        """The boundary rejects a trailing slash, so the client must not normalize one away.

        Normalizing in the constructor made the request-time recheck validate a value the form
        boundary refuses, which is the whole point of rechecking.
        """
        with serving() as (root, seen, allowlist):
            adapter = adapter_for(f"{root}/", allowlist)

            with self.assertRaises(InvalidBackendConfiguration) as caught:
                adapter.complete(REQUEST, api_key=API_KEY)

        self.assertIn("trailing slash", str(caught.exception))
        self.assertEqual(seen, [])

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


class RetryClassificationTest(SimpleTestCase):
    """Specification 13.3 separates a request the operator must repair from a backend that is busy."""

    def failure(self, status):
        with serving(status=status, payload={"error": {"message": "no"}}) as (root, _seen, allowlist):
            with self.assertRaises(Exception) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)
        return caught.exception

    def test_a_request_error_is_not_retryable(self):
        """400, 404 and 405 name a request the operator must repair, so a retry repeats the mistake."""
        for status in (400, 404, 405):
            with self.subTest(status=status):
                failure = self.failure(status)

                self.assertIsInstance(failure, InvalidBackendConfiguration)
                self.assertFalse(failure.retryable)

    def test_a_temporary_backend_failure_is_retryable(self):
        for status in TRANSIENT_STATUSES:
            with self.subTest(status=status):
                failure = self.failure(status)

                self.assertIsInstance(failure, TransportFailure)
                self.assertTrue(failure.retryable)

    def test_any_other_error_status_is_not_retryable(self):
        """13.3 names four transient statuses; everything else at 400 and above is the request."""
        for status in (406, 409, 413, 415, 422, 501):
            with self.subTest(status=status):
                failure = self.failure(status)

                self.assertIsInstance(failure, InvalidBackendConfiguration)
                self.assertFalse(failure.retryable)

    def test_a_credential_refusal_is_not_retryable(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.assertFalse(self.failure(status).retryable)

    def test_a_rate_limit_is_retryable(self):
        self.assertTrue(self.failure(429).retryable)

    def test_an_unreachable_backend_is_retryable(self):
        adapter = adapter_for("http://127.0.0.1:1", ["http://127.0.0.1:1"])

        with self.assertRaises(TransportFailure) as caught:
            adapter.complete(REQUEST, api_key=API_KEY)

        self.assertTrue(caught.exception.retryable)

    def test_an_unreadable_envelope_is_not_retryable(self):
        """A backend that answers unreadably answers the same way next time."""
        with serving(payload="not json at all") as (root, _seen, allowlist):
            with self.assertRaises(MalformedEnvelope) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertFalse(caught.exception.retryable)


class ResponseDiagnosticTest(SimpleTestCase):
    """A failed proposal stores the raw response, so the adapter has to carry one out."""

    def test_a_credential_refusal_carries_the_body_it_received(self):
        with serving(status=401, payload={"error": {"message": "token expired"}}) as (root, _seen, allowlist):
            with self.assertRaises(AuthenticationFailure) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(caught.exception.diagnostic.receipt, BODY_PRESENT)
        self.assertIn("token expired", caught.exception.diagnostic.text)
        self.assertEqual(caught.exception.diagnostic.status_code, 401)

    def test_a_temporary_failure_carries_the_body_it_received(self):
        with serving(status=503, payload={"error": {"message": "draining"}}) as (root, _seen, allowlist):
            with self.assertRaises(TransportFailure) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertIn("draining", caught.exception.diagnostic.text)

    def test_a_wrong_finish_reason_carries_the_body(self):
        """The proposal reads this to explain why no answer was produced."""
        with serving(payload=completion(content="half an ans", finish_reason="length")) as (root, _seen, allowlist):
            with self.assertRaises(MalformedEnvelope) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertIn("half an ans", caught.exception.diagnostic.text)

    def test_an_unreadable_body_is_carried_verbatim(self):
        with serving(payload="not json at all") as (root, _seen, allowlist):
            with self.assertRaises(MalformedEnvelope) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(caught.exception.diagnostic.text, "not json at all")

    def test_a_refusal_keeps_the_text_it_refused_with(self):
        """`is_refusal` is computed from it and the text was previously discarded."""
        envelope = completion(content=None, message={"refusal": "I will not answer that."})
        with serving(payload=envelope) as (root, _seen, allowlist):
            answer = adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertTrue(answer.is_refusal)
        self.assertIn("I will not answer that.", answer.diagnostic.text)

    def test_a_completion_carries_its_body(self):
        with serving() as (root, _seen, allowlist):
            answer = adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(answer.diagnostic.receipt, BODY_PRESENT)
        self.assertIn("served-model", answer.diagnostic.text)

    def test_an_empty_body_is_not_an_absent_body(self):
        """A worker must tell "the backend said nothing" from "nothing arrived"."""
        with serving(status=500, payload="") as (root, _seen, allowlist):
            with self.assertRaises(TransportFailure) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(caught.exception.diagnostic.receipt, BODY_EMPTY)
        self.assertEqual(caught.exception.diagnostic.text, "")

    def test_a_call_that_never_answered_reports_an_absent_body(self):
        adapter = adapter_for("http://127.0.0.1:1", ["http://127.0.0.1:1"])

        with self.assertRaises(TransportFailure) as caught:
            adapter.complete(REQUEST, api_key=API_KEY)

        self.assertEqual(caught.exception.diagnostic.receipt, BODY_ABSENT)
        self.assertIsNone(caught.exception.diagnostic.text)

    def test_an_escaped_key_is_redacted_too(self):
        """A JSON escape hides the key from a literal search but not from whoever decodes the body."""
        escaped = API_KEY.replace("-", "\\u002d")

        with serving(status=500, payload=f'{{"error": "key {escaped} rejected"}}') as (root, _seen, allowlist):
            with self.assertRaises(TransportFailure) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertTrue(caught.exception.diagnostic.redacted)
        self.assertNotIn(API_KEY, caught.exception.diagnostic.text)
        # The escaped form has to be gone too, or the key is one `json.loads` away.
        self.assertNotIn("u002d", caught.exception.diagnostic.text)

    def test_a_key_holding_json_syntax_is_redacted_too(self):
        """A quote or a backslash is re-escaped by the encoder, so a literal search misses it.

        A key holding a newline is not covered: `requests` raises `InvalidHeader` before the call, so
        no backend ever receives it.
        """
        for secret in ('sk-"quote', "sk-back\\slash", "sk-tab\there"):
            with self.subTest(secret=secret):
                with serving(status=500, payload={"error": {"message": f"rejected {secret}"}}) as (
                    root,
                    _seen,
                    allowlist,
                ):
                    with self.assertRaises(TransportFailure) as caught:
                        adapter_for(root, allowlist).complete(REQUEST, api_key=secret)

                self.assertTrue(caught.exception.diagnostic.redacted)
                self.assertNotIn(secret, caught.exception.diagnostic.text)

    def test_an_escaped_key_in_a_body_that_is_not_json_is_redacted(self):
        """A truncated body never parses, so detection cannot depend on decoding it."""
        escaped = API_KEY.replace("-", "\\u002d")

        with serving(status=500, payload=f'{{"error": "key {escaped} rejected"') as (root, _seen, allowlist):
            with self.assertRaises(TransportFailure) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertTrue(caught.exception.diagnostic.redacted)
        self.assertNotIn("u002d", caught.exception.diagnostic.text)

    def test_no_diagnostic_carries_the_api_key(self):
        """The body is a new persistence surface, so the containment rule reaches it too."""
        for status in (401, 500):
            with self.subTest(status=status):
                with serving(status=status, payload={"error": {"message": f"key {API_KEY} rejected"}}) as (
                    root,
                    _seen,
                    allowlist,
                ):
                    with self.assertRaises(Exception) as caught:
                        adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

                self.assertNotIn(API_KEY, caught.exception.diagnostic.text or "")
                self.assertTrue(caught.exception.diagnostic.redacted)


class InterruptedAndMalformedTransportTest(SimpleTestCase):
    """Two paths that lost the answer before it reached any classification."""

    def test_a_body_cut_short_is_reported_as_interrupted(self):
        """Requests raises with response=None, so the bytes cannot be recovered; say so."""
        with serving_truncated() as (root, allowlist):
            with self.assertRaises(TransportFailure) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertEqual(caught.exception.diagnostic.receipt, BODY_INTERRUPTED)
        self.assertTrue(caught.exception.retryable)

    def test_a_deeply_nested_body_stays_typed(self):
        """On 3.12 and 3.13 the decoder recurses and raises; 3.14 parses it and the envelope fails."""
        payload = "[" * 10000 + "0" + "]" * 10000

        with serving(payload=payload) as (root, _seen, allowlist):
            with self.assertRaises(MalformedEnvelope) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.diagnostic.receipt, BODY_PRESENT)
        self.assertTrue(caught.exception.diagnostic.text.startswith("[[["))

    def test_a_decode_failure_outside_the_value_error_tree_is_typed(self):
        """The interpreter under test parses the body above, so the raise it makes is reproduced here."""

        class Recursing(requests.Response):
            def json(self, **kwargs):
                raise RecursionError("maximum recursion depth exceeded")

        response = Recursing()
        response.status_code = 200
        response._content = b'{"deep": true}'

        with self.assertRaises(MalformedEnvelope) as caught:
            adapter_for("http://127.0.0.1:1", ["http://127.0.0.1:1"])._read(response, API_KEY)

        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.diagnostic.text, '{"deep": true}')

    def test_a_malformed_redirect_target_is_typed(self):
        """`requests` raises a bare ValueError while preparing it, which no caller can classify."""
        with serving(status=302, headers_out={"Location": "https://[invalid/"}) as (root, _seen, allowlist):
            with self.assertRaises(InvalidBackendConfiguration) as caught:
                adapter_for(root, allowlist).complete(REQUEST, api_key=API_KEY)

        self.assertFalse(caught.exception.retryable)

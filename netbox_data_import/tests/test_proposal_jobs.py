# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Exercise proposal jobs through real HTTP requests and persisted proposal rows."""

import inspect
import json
import threading
import time
import uuid

from contextlib import contextmanager
from http.server import ThreadingHTTPServer

from core.models import Job, ObjectType

from django.db import connections
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from netbox_data_import import inference_adapter, inference_credentials, proposal_jobs
from netbox_data_import.field_keys import SELECT_TERMINATION_TASK
from netbox_data_import.jobs import ResolutionProposalJob
from netbox_data_import.models import InferenceBackend, ProposalFailureReason, ProposalOutcome, ProposalStatus
from netbox_data_import.proposal_jobs import ADAPTER_FAILURE_REASONS, CREDENTIAL_FAILURE_REASONS, run_proposal
from netbox_data_import.resolution_proposals import cancel_proposal, claim_proposal, request_proposal
from netbox_data_import.tests.test_inference_adapter import RecordingBackend, completion, serving, serving_truncated
from netbox_data_import.tests.test_inference_connection_test import make_row
from netbox_data_import.tests.test_inference_credentials import SECRET, serving as serving_vault
from netbox_data_import.tests.test_resolution_proposals import ProposalFixture


def answer(outcome="candidate", candidate_id="candidate-0001", **changes):
    """Return response content under the proposal schema."""
    return json.dumps(
        {
            "schema_version": 1,
            "outcome": outcome,
            "candidate_id": candidate_id,
            "explanation": "Matching label.",
            **changes,
        }
    )


class ProposalRetryDelayTest(SimpleTestCase):
    def test_retry_delay_clamps_rate_limit(self):
        error = inference_adapter.RateLimited("Rate limited.", retry_after=86400)
        for attempt in (0, 1):
            with self.subTest(attempt=attempt):
                self.assertEqual(proposal_jobs._retry_delay(attempt, error), proposal_jobs.MAX_RETRY_AFTER_SECONDS)

    def test_retry_delay_honors_rate_limit_within_cap(self):
        error = inference_adapter.RateLimited("Rate limited.", retry_after=5)
        self.assertEqual(proposal_jobs._retry_delay(0, error), 5)

    def test_retry_delay_uses_exponential_backoff(self):
        for error in (
            inference_adapter.TransportFailure("Unavailable."),
            inference_adapter.RateLimited("Rate limited."),
        ):
            for attempt in (0, 1):
                with self.subTest(error=type(error).__name__, attempt=attempt):
                    delay = proposal_jobs._retry_delay(attempt, error)
                    self.assertGreaterEqual(delay, 2**attempt)
                    self.assertLess(delay, 2**attempt + 1)


class WorkerFixture:
    """Share row construction and deployment settings between transaction styles."""

    def frozen_proposal(self):
        snapshot = {
            "total": 1,
            "candidates": [
                {
                    "candidate_id": "candidate-0001",
                    "object_type": "dcim.interface",
                    "object_id": self.interface.pk,
                    "display_name": self.interface.name,
                }
            ],
        }
        return request_proposal(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
            source_evidence={"port": "Eth1/1"},
            resolved_device_type=self.device_type_ct,
            resolved_device_id=self.device.pk,
            prompt_version=1,
            response_schema_version=1,
            candidate_snapshot=snapshot,
            requested_by=self.operator,
        )

    @contextmanager
    def configured(self, api_root, allowlist, *, fallback=False, vault_status=200, secret=SECRET):
        with serving_vault(status=vault_status, payload={"data": {"data": {"api_key": secret}}}) as (
            vault_settings,
            vault_seen,
        ):
            config = {"inference_backend_origin_allowlist": allowlist, "vault": vault_settings}
            row = make_row(api_root=api_root, connect_timeout=2, read_timeout=2)
            if fallback:
                config["inference_backend"] = {
                    field: getattr(row, field)
                    for field in (
                        "display_name",
                        "adapter_type",
                        "api_root",
                        "model",
                        "authentication",
                        "response_mode",
                        "credential_reference",
                        "connect_timeout",
                        "read_timeout",
                    )
                }
                row.delete()
            try:
                with override_settings(PLUGINS_CONFIG={"netbox_data_import": config}):
                    yield vault_seen
            finally:
                if row.pk is not None:
                    row.delete()

    def assert_failure(self, proposal, reason, seen, attempts, status, text):
        proposal.refresh_from_db()
        self.assertEqual(len(seen), attempts)
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, reason)
        self.assertEqual(proposal.outcome, "")
        self.assertEqual(
            proposal.response_diagnostic,
            {
                "receipt": "present" if text else "empty",
                "text": text,
                "status_code": status,
                "redacted": False,
                "truncated": False,
            },
        )
        self.assertEqual(proposal.backend_metadata["backend_source"], "database")
        self.assertEqual(len(proposal.backend_metadata["attempts"]), attempts)


class ProposalWorkerTest(WorkerFixture, ProposalFixture):
    def test_every_credential_category_has_an_explicit_mapping(self):
        categories = {
            cls.category
            for _, cls in inspect.getmembers(inference_credentials, inspect.isclass)
            if issubclass(cls, inference_credentials.CredentialFailure)
        }
        self.assertEqual(set(CREDENTIAL_FAILURE_REASONS), categories)

    def omit_credential_mapping(self, category):
        reason = CREDENTIAL_FAILURE_REASONS.pop(category)
        self.addCleanup(CREDENTIAL_FAILURE_REASONS.__setitem__, category, reason)

    def test_unmapped_reference_failure_reaches_a_terminal_status(self):
        self.omit_credential_mapping("invalid_credential_reference")
        proposal = self.frozen_proposal()
        with serving() as (root, _seen, allowed), self.configured(root, allowed):
            InferenceBackend.objects.update(credential_reference={})
            run_proposal(proposal.pk)

        proposal.refresh_from_db()
        self.assertEqual(
            (proposal.status, proposal.failure_reason),
            (ProposalStatus.FAILED, ProposalFailureReason.CREDENTIAL_UNAVAILABLE),
        )

    def test_unmapped_store_failure_reaches_a_terminal_status(self):
        self.omit_credential_mapping("credential_denied")
        proposal = self.frozen_proposal()
        with serving() as (root, _seen, allowed), self.configured(root, allowed, vault_status=403):
            run_proposal(proposal.pk)

        proposal.refresh_from_db()
        self.assertEqual(
            (proposal.status, proposal.failure_reason),
            (ProposalStatus.FAILED, ProposalFailureReason.CREDENTIAL_UNAVAILABLE),
        )

    def test_unexpected_snapshot_error_fails_and_releases_the_active_slot(self):
        proposal = self.frozen_proposal()
        proposal.candidate_snapshot = {}
        proposal.save()
        with serving() as (root, _seen, allowed), self.configured(root, allowed):
            with self.assertRaises(KeyError):
                run_proposal(proposal.pk)

        proposal.refresh_from_db()
        self.assertEqual(
            (proposal.status, proposal.failure_reason),
            (ProposalStatus.FAILED, ProposalFailureReason.TEMPORARY_BACKEND_FAILURE),
        )
        self.assertEqual(self.frozen_proposal().status, ProposalStatus.QUEUED)

    def test_unexpected_selection_error_fails_and_releases_the_active_slot(self):
        proposal = self.frozen_proposal()
        proposal.candidate_snapshot["candidates"][0]["object_type"] = "dcim.missing"
        proposal.save()
        with serving(payload=completion(answer())) as (root, _seen, allowed), self.configured(root, allowed):
            with self.assertRaises(ObjectType.DoesNotExist):
                run_proposal(proposal.pk)

        proposal.refresh_from_db()
        self.assertEqual(
            (proposal.status, proposal.failure_reason),
            (ProposalStatus.FAILED, ProposalFailureReason.TEMPORARY_BACKEND_FAILURE),
        )
        self.assertEqual(self.frozen_proposal().status, ProposalStatus.QUEUED)

    def test_every_adapter_error_has_an_explicit_mapping(self):
        errors = {
            cls
            for _, cls in inspect.getmembers(inference_adapter, inspect.isclass)
            if cls is not inference_adapter.InferenceBackendError
            and issubclass(cls, inference_adapter.InferenceBackendError)
        }
        self.assertEqual(set(ADAPTER_FAILURE_REASONS), errors)

    def test_transient_failures_send_exactly_three_requests(self):
        for status in (500, 502, 503, 504, 429):
            with self.subTest(status=status), serving(status=status, payload="temporary") as (root, seen, allowed):
                proposal = self.frozen_proposal()
                with self.configured(root, allowed):
                    run_proposal(proposal.pk)
                reason = (
                    ProposalFailureReason.RATE_LIMIT
                    if status == 429
                    else ProposalFailureReason.TEMPORARY_BACKEND_FAILURE
                )
                self.assert_failure(proposal, reason, seen, 3, status, "temporary")
                proposal.delete()

    def test_an_oversized_failure_body_is_stored_bounded_and_marked(self):
        """The adapter's bound has to reach the column, which is where the body actually persists."""
        from netbox_data_import.inference_adapter import DIAGNOSTIC_TEXT_LIMIT

        payload = "x" * (DIAGNOSTIC_TEXT_LIMIT + 500)
        with serving(status=400, payload=payload) as (root, _seen, allowed):
            proposal = self.frozen_proposal()
            with self.configured(root, allowed):
                run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(len(proposal.response_diagnostic["text"]), DIAGNOSTIC_TEXT_LIMIT)
        self.assertTrue(proposal.response_diagnostic["truncated"])

    def test_non_transient_statuses_send_exactly_one_request(self):
        for status in (400, 404, 405, 408, 422, 501, 401, 403):
            with self.subTest(status=status), serving(status=status, payload="rejected") as (root, seen, allowed):
                proposal = self.frozen_proposal()
                with self.configured(root, allowed):
                    run_proposal(proposal.pk)
                reason = (
                    ProposalFailureReason.AUTHENTICATION_FAILURE
                    if status in (401, 403)
                    else ProposalFailureReason.INVALID_CONFIGURATION
                )
                self.assert_failure(proposal, reason, seen, 1, status, "rejected")
                proposal.delete()

    def test_cancelled_claim_sends_no_request(self):
        proposal = self.frozen_proposal()
        self.assertTrue(cancel_proposal(proposal.pk))
        with serving() as (root, seen, allowed), self.configured(root, allowed) as vault_seen:
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.CANCELLED)
        self.assertEqual(seen, [])
        self.assertEqual(vault_seen, [])

    def test_already_claimed_row_sends_no_request(self):
        proposal = self.frozen_proposal()
        self.assertTrue(claim_proposal(proposal.pk))
        with serving() as (root, seen, allowed), self.configured(root, allowed) as vault_seen:
            run_proposal(proposal.pk)
        self.assertEqual(seen, [])
        self.assertEqual(vault_seen, [])
        self.assertEqual(self.status_of(proposal), ProposalStatus.RUNNING)

    def check_completion_failure(self, payload, reason):
        proposal = self.frozen_proposal()
        with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed):
            run_proposal(proposal.pk)
        self.assert_failure(proposal, reason, seen, 1, 200, json.dumps(payload))

    def test_refusal_is_not_no_match(self):
        self.check_completion_failure(
            completion(answer(), message={"refusal": "Declined"}), ProposalFailureReason.BACKEND_REFUSAL
        )

    def test_empty_completion_is_not_no_match(self):
        self.check_completion_failure(completion("  "), ProposalFailureReason.BACKEND_REFUSAL)

    def test_unknown_candidate_is_invalid_response(self):
        self.check_completion_failure(
            completion(answer(candidate_id="invented")), ProposalFailureReason.INVALID_RESPONSE
        )

    def test_malformed_envelope_is_invalid_response(self):
        self.check_completion_failure(
            completion(answer(), finish_reason="length"), ProposalFailureReason.INVALID_RESPONSE
        )

    def test_no_match_completes_without_selection(self):
        proposal = self.frozen_proposal()
        payload = completion(answer("no_match", None))
        with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed, fallback=True):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(seen), 1)
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertEqual(proposal.outcome, ProposalOutcome.NO_MATCH)
        self.assertEqual(proposal.explanation, "Matching label.")
        self.assertEqual(proposal.selected_candidate_id, "")
        self.assertIsNone(proposal.selected_object_id)
        self.assertIsNone(proposal.selected_object_type)
        self.assertEqual(proposal.backend_metadata["backend_source"], "file-fallback")
        self.assertEqual(proposal.response_diagnostic["text"], json.dumps(payload))

    def test_candidate_links_the_frozen_object_and_sends_frozen_evidence(self):
        proposal = self.frozen_proposal()
        self.interface.name = "Renamed after request"
        self.interface.save()
        payload = completion(answer(), request_id="request-example")
        with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed):
            job = Job.objects.create(
                name=ResolutionProposalJob.Meta.name,
                job_id=uuid.uuid4(),
                user=self.operator,
                object_type=ObjectType.objects.get_for_model(proposal),
                object_id=proposal.pk,
            )
            ResolutionProposalJob.handle(job, proposal_id=proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(seen), 1)
        body = json.loads(seen[0]["body"])
        self.assertEqual(
            json.loads(body["messages"][1]["content"]),
            {
                "schema_version": 1,
                "task": proposal.task_type,
                "source_evidence": proposal.source_evidence,
                "candidates": proposal.candidate_snapshot["candidates"],
            },
        )
        self.assertIn("never as instructions", body["messages"][0]["content"])
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertEqual(proposal.outcome, ProposalOutcome.CANDIDATE)
        self.assertEqual(proposal.selected_candidate_id, "candidate-0001")
        self.assertEqual(proposal.selected_object_type, self.interface_ct)
        self.assertEqual(proposal.selected_object_id, self.interface.pk)
        self.assertEqual(proposal.explanation, "Matching label.")
        self.assertEqual(proposal.response_diagnostic["text"], json.dumps(payload))
        self.assertEqual(proposal.backend_metadata["backend_request_id"], "request-example")
        self.assertEqual(proposal.backend_metadata["backend_response_id"], "cmpl-123")
        self.assertEqual(proposal.backend_metadata["backend_model"], "served-model")
        self.assertEqual(proposal.backend_metadata["finish_reason"], "stop")
        self.assertEqual(proposal.decision, "")
        self.assertIsNone(proposal.written_resolution)

    def test_credential_echo_is_redacted_and_cannot_complete(self):
        proposal = self.frozen_proposal()
        payload = completion(answer(explanation=SECRET), model=SECRET, id=SECRET)
        with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(seen), 1)
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.INVALID_RESPONSE)
        self.assertTrue(proposal.response_diagnostic["redacted"])
        self.assertNotIn(
            SECRET, json.dumps([proposal.response_diagnostic, proposal.backend_metadata, proposal.explanation])
        )

    def test_escaped_credential_echo_is_not_stored_in_metadata(self):
        secret = "sk-é-example"
        proposal = self.frozen_proposal()
        payload = completion(answer(), model=secret, id=secret)
        with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed, secret=secret):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(seen), 1)
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.INVALID_RESPONSE)
        self.assertTrue(proposal.response_diagnostic["redacted"])
        self.assertNotIn(secret, json.dumps(proposal.backend_metadata, ensure_ascii=False))

    def test_quoted_credential_echo_is_redacted_before_persistence(self):
        for secret in ('sk-"example', r"sk-\example"):
            with self.subTest(secret=secret):
                proposal = self.frozen_proposal()
                payload = completion(answer(explanation=secret), model=secret, id=secret)
                with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed, secret=secret):
                    run_proposal(proposal.pk)
                proposal.refresh_from_db()
                self.assertEqual(len(seen), 1)
                self.assertTrue(proposal.response_diagnostic["redacted"])
                self.assertEqual(proposal.status, ProposalStatus.FAILED)
                self.assertEqual(proposal.failure_reason, ProposalFailureReason.INVALID_RESPONSE)
                self.assertNotEqual(proposal.backend_metadata.get("backend_model"), secret)
                self.assertNotEqual(proposal.backend_metadata.get("backend_response_id"), secret)
                self.assertEqual(proposal.explanation, "")

    def test_overwritten_credential_echo_is_redacted_before_persistence(self):
        secret = 'sk-"example'
        proposal = self.frozen_proposal()
        payload = json.dumps(completion(answer())).replace(
            '"model": "served-model"',
            f'"model": {json.dumps(secret)}, "model": "served-model"',
        )
        with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed, secret=secret):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(seen), 1)
        self.assertTrue(proposal.response_diagnostic["redacted"])
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.INVALID_RESPONSE)

    def test_credential_unavailable_retries_without_calling_inference(self):
        proposal = self.frozen_proposal()
        with serving() as (root, seen, allowed), self.configured(root, allowed, vault_status=503) as vault_seen:
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(vault_seen), 3)
        self.assertEqual(seen, [])
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.CREDENTIAL_UNAVAILABLE)
        self.assertEqual(proposal.response_diagnostic["receipt"], "absent")
        self.assertEqual(len(proposal.backend_metadata["attempts"]), 3)

    def test_credential_denial_fails_without_retry_or_inference(self):
        proposal = self.frozen_proposal()
        with serving() as (root, seen, allowed), self.configured(root, allowed, vault_status=403) as vault_seen:
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(vault_seen), 1)
        self.assertEqual(seen, [])
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.CREDENTIAL_DENIED)
        self.assertEqual(proposal.response_diagnostic["receipt"], "absent")

    def test_transient_recovery_completes_after_backoff(self):
        proposal = self.frozen_proposal()
        responses = [(503, "temporary", {}), (503, "temporary", {}), (200, completion(answer()), {})]
        with serving_sequence(responses) as (root, seen, allowed, arrived), self.configured(root, allowed):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(seen), 3)
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertEqual(proposal.selected_object_id, self.interface.pk)
        self.assertGreaterEqual(arrived[1] - arrived[0], 1)
        self.assertGreaterEqual(arrived[2] - arrived[1], 2)
        self.assertEqual(
            [item.get("reason") for item in proposal.backend_metadata["attempts"]],
            [
                ProposalFailureReason.TEMPORARY_BACKEND_FAILURE,
                ProposalFailureReason.TEMPORARY_BACKEND_FAILURE,
                None,
            ],
        )
        self.assertEqual(proposal.backend_metadata["attempts"][0]["diagnostic"]["text"], "temporary")
        self.assertEqual(proposal.response_diagnostic["text"], json.dumps(responses[-1][1]))

    def test_rate_limit_honors_retry_after(self):
        proposal = self.frozen_proposal()
        responses = [(429, "wait", {"Retry-After": "3"}), (200, completion(answer()), {})]
        with serving_sequence(responses) as (root, seen, allowed, arrived), self.configured(root, allowed):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(len(seen), 2)
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertGreaterEqual(arrived[1] - arrived[0], 3)

    def test_empty_failure_body_remains_distinct_from_absent(self):
        proposal = self.frozen_proposal()
        with serving(status=400, payload="") as (root, seen, allowed), self.configured(root, allowed):
            run_proposal(proposal.pk)
        self.assert_failure(proposal, ProposalFailureReason.INVALID_CONFIGURATION, seen, 1, 400, "")

    def test_interrupted_body_receipt_survives_failure(self):
        proposal = self.frozen_proposal()
        with serving_truncated() as (root, allowed), self.configured(root, allowed):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.TEMPORARY_BACKEND_FAILURE)
        self.assertEqual(
            proposal.response_diagnostic,
            {
                "receipt": "interrupted",
                "text": None,
                "status_code": None,
                "redacted": False,
                "truncated": False,
            },
        )
        self.assertEqual(len(proposal.backend_metadata["attempts"]), 3)

    def test_missing_backend_fails_with_absent_diagnostic(self):
        proposal = self.frozen_proposal()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {}}):
            run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.INVALID_CONFIGURATION)
        self.assertEqual(proposal.response_diagnostic["receipt"], "absent")


@contextmanager
def serving_sequence(responses):
    """Record arrival times while a real backend changes its answer between requests."""
    arrived = []

    class Handler(RecordingBackend):
        seen = []

        def do_POST(self):
            arrived.append(time.monotonic())
            self.status, self.payload, self.headers_out = responses[min(len(self.seen), len(responses) - 1)]
            super().do_POST()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield root, Handler.seen, [root], arrived
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def cancelling_backend(proposal_id, payload, status):
    """Cancel through a separate database connection after the backend receives the request."""
    cancelled = []

    class Handler(RecordingBackend):
        seen = []

        def do_POST(self):
            try:
                cancelled.append(cancel_proposal(proposal_id))
            finally:
                connections.close_all()
            super().do_POST()

    Handler.payload = payload
    Handler.status = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield root, Handler.seen, [root], cancelled
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class ProposalCancellationTest(WorkerFixture, TransactionTestCase):
    def setUp(self):
        ProposalFixture.setUpTestData.__func__(type(self))

    def check_late_response(self, payload, status):
        proposal = self.frozen_proposal()
        with cancelling_backend(proposal.pk, payload, status) as (root, seen, allowed, cancelled):
            with self.configured(root, allowed):
                run_proposal(proposal.pk)
        proposal.refresh_from_db()
        self.assertEqual(cancelled, [True])
        self.assertEqual(len(seen), 1)
        self.assertEqual(proposal.status, ProposalStatus.CANCELLED)
        self.assertEqual(proposal.outcome, "")
        self.assertEqual(proposal.failure_reason, "")
        self.assertIsNone(proposal.response_diagnostic)
        self.assertIsNone(proposal.backend_metadata)

    def test_late_completion_leaves_cancellation_untouched(self):
        self.check_late_response(completion(answer()), 200)

    def test_late_failure_leaves_cancellation_untouched(self):
        self.check_late_response("rejected", 400)

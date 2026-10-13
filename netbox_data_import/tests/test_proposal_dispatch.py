# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Proposal Jobs start only while the active Inference Backend has a free request slot."""

import json
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread
from time import monotonic, sleep

from core.choices import JobStatusChoices
from core.models import Job
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django_rq import get_queue
from netbox.constants import RQ_QUEUE_LOW

from netbox_data_import.forms import InferenceBackendForm
from netbox_data_import.inference_settings import InvalidInferenceConfiguration, validate_file_fallback
from netbox_data_import.jobs import ResolutionProposalJob
from netbox_data_import.models import ProposalFailureReason, ProposalOutcome, ProposalStatus, ResolutionProposal
from netbox_data_import.resolution_proposals import claim_proposal
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.plugins_config import override_plugins_config
from netbox_data_import.tests.test_cable_module import CableTopologyMixin
from netbox_data_import.tests.test_inference_adapter import completion, serving
from netbox_data_import.tests.test_inference_backend import ALLOWLIST, FALLBACK, REFERENCE
from netbox_data_import.tests.test_inference_connection_test import make_row
from netbox_data_import.tests.test_inference_credentials import serving as serving_vault
from netbox_data_import.tests.test_proposal_in_place import HTMX, InPlacePreviewMixin
from netbox_data_import.tests.test_proposal_jobs import answer
from netbox_data_import.tests.test_resolution_proposals import ProposalFixture


def backend_config(**fallback):
    """Return plugin settings whose file fallback is the active backend, with the named fields replaced."""
    return {"inference_backend": {**FALLBACK, **fallback}, "inference_backend_origin_allowlist": ALLOWLIST}


def proposal_jobs():
    return Job.objects.filter(name=ResolutionProposalJob.Meta.name)


class _DispatchViewMixin(InPlacePreviewMixin):
    """Three open terminations, asked about through the real views and dispatched through the real queue."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def ask_all(self, **fallback):
        with override_plugins_config(netbox_data_import=backend_config(**fallback)):
            response = self.post("trace_request_all_proposals")
        self.assertEqual(response.status_code, 302, response.content[:500])
        return self.rows()

    def rows(self):
        return list(ResolutionProposal.objects.order_by("created", "pk"))

    def poll(self, field_key, **fallback):
        """Read one card as a pending card polls it, which is the dispatcher's backstop."""
        with override_plugins_config(netbox_data_import=backend_config(**fallback)):
            response = self.client.get(
                reverse("plugins:netbox_data_import:trace_proposal"),
                {**self.claim(), "field_key": field_key},
                headers=HTMX,
            )
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.content.decode()

    def claim(self):
        from netbox_data_import.tests.helpers import preview_claim

        return preview_claim(self.client)

    @contextmanager
    def answering_backend(self, **fallback):
        """Serve a backend that answers no_match, behind a Vault that hands out its key."""
        with (
            serving(payload=completion(answer("no_match", None))) as (root, seen, allowed),
            serving_vault() as (vault, _vault_seen),
        ):
            config = {
                "inference_backend": {
                    **FALLBACK,
                    "api_root": root,
                    "connect_timeout": 2,
                    "read_timeout": 2,
                    **fallback,
                },
                "inference_backend_origin_allowlist": allowed,
                "vault": vault,
            }
            with override_plugins_config(netbox_data_import=config):
                yield seen

    def run_job(self, proposal):
        """Deliver the proposal's Job as a worker does: through native Job handling."""
        job = Job.objects.get(pk=proposal.job_id)
        with self.captureOnCommitCallbacks(execute=True):
            ResolutionProposalJob.handle(job, proposal_id=proposal.pk)


class ProposalDispatchTest(_DispatchViewMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    """Ask AI for all creates every attempt, and the request slots decide how many Jobs run."""

    def test_ask_ai_for_all_with_one_slot_starts_one_job_and_the_rest_wait_without_one(self):
        rows = self.ask_all()

        self.assertEqual(len(rows), 3)
        self.assertEqual({row.status for row in rows}, {ProposalStatus.QUEUED})
        jobs = list(proposal_jobs())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(rows[0].job_id, jobs[0].pk)
        self.assertEqual([row.job_id for row in rows[1:]], [None, None])
        self.assertEqual(get_queue(RQ_QUEUE_LOW).job_ids, [str(jobs[0].job_id)])

    def test_two_slots_start_two_jobs(self):
        rows = self.ask_all(max_concurrent_requests=2)

        self.assertEqual(proposal_jobs().count(), 2)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_each_finished_job_starts_exactly_the_next_waiting_proposal_in_request_order(self):
        rows = self.ask_all()

        with self.answering_backend() as seen:
            for index in range(3):
                self.run_job(self.rows()[index])
                rows = self.rows()
                self.assertEqual(rows[index].status, ProposalStatus.COMPLETED)
                self.assertEqual(rows[index].outcome, ProposalOutcome.NO_MATCH)
                started = min(index + 2, 3)
                self.assertEqual([row.job_id is not None for row in rows], [True] * started + [False] * (3 - started))
                self.assertEqual(proposal_jobs().count(), started)
        self.assertEqual(len(seen), 3)

    def test_a_cancelled_proposal_keeps_its_slot_until_its_running_job_ends(self):
        """The worker of a cancelled row can still be inside its backend call, which still occupies the server."""
        first, second, _third = self.ask_all()
        Job.objects.get(pk=first.job_id).start()
        self.assertTrue(claim_proposal(first.pk))
        cancelled = self.post("trace_cancel_proposal", proposal_id=first.pk)
        self.assertEqual(cancelled.status_code, 200, cancelled.content[:500])

        self.poll(second.field_key)

        self.assertIsNone(ResolutionProposal.objects.get(pk=second.pk).job_id)
        with self.answering_backend() as seen:
            self.run_job(first)
        self.assertEqual(seen, [])
        rows = self.rows()
        self.assertEqual(rows[0].status, ProposalStatus.CANCELLED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_a_cancelled_waiting_proposal_is_never_dispatched(self):
        first, second, _third = self.ask_all()

        cancelled = self.post("trace_cancel_proposal", proposal_id=second.pk)
        self.assertEqual(cancelled.status_code, 200, cancelled.content[:500])
        with self.answering_backend():
            self.run_job(first)

        rows = self.rows()
        self.assertEqual(rows[1].status, ProposalStatus.CANCELLED)
        self.assertEqual([row.job_id is not None for row in rows], [True, False, True])

    def test_a_lost_task_releases_its_slot_at_the_next_poll(self):
        first, second, _third = self.ask_all()
        job = Job.objects.get(pk=first.job_id)
        job.start()
        self.assertTrue(claim_proposal(first.pk))
        # The worker died, and its task went with it.
        get_queue(RQ_QUEUE_LOW).fetch_job(str(job.job_id)).delete()

        self.poll(second.field_key)

        rows = self.rows()
        self.assertEqual(
            (rows[0].status, rows[0].failure_reason), (ProposalStatus.FAILED, ProposalFailureReason.QUEUE_UNAVAILABLE)
        )
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_a_job_running_past_its_timeout_releases_its_slot_at_the_next_poll(self):
        from netbox_data_import.inference_backend import resolve_active_backend
        from netbox_data_import.proposal_dispatch import STALE_SLOT_GRACE
        from netbox_data_import.proposal_jobs import proposal_job_timeout

        first, second, _third = self.ask_all()
        job = Job.objects.get(pk=first.job_id)
        job.start()
        self.assertTrue(claim_proposal(first.pk))
        with override_plugins_config(netbox_data_import=backend_config()):
            timeout = timedelta(seconds=proposal_job_timeout(resolve_active_backend()))
        Job.objects.filter(pk=job.pk).update(started=timezone.now() - timeout)

        self.poll(second.field_key)

        self.assertIsNone(ResolutionProposal.objects.get(pk=second.pk).job_id)
        Job.objects.filter(pk=job.pk).update(started=timezone.now() - timeout - STALE_SLOT_GRACE - timedelta(seconds=5))

        self.poll(second.field_key)

        rows = self.rows()
        self.assertEqual(
            (rows[0].status, rows[0].failure_reason), (ProposalStatus.FAILED, ProposalFailureReason.TIMEOUT)
        )
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_the_job_timeout_covers_every_attempt_of_the_active_backend(self):
        from netbox_data_import.proposal_jobs import (
            JOB_TIMEOUT_MARGIN_SECONDS,
            MAX_ATTEMPTS,
            MAX_RETRY_AFTER_SECONDS,
        )

        rows = self.ask_all(connect_timeout=7, read_timeout=200)

        task = get_queue(RQ_QUEUE_LOW).fetch_job(str(Job.objects.get(pk=rows[0].job_id).job_id))
        # No vault mapping is configured, so each credential read has its 5 and 60 second defaults.
        self.assertEqual(
            task.timeout,
            MAX_ATTEMPTS * (7 + 200 + 5 + 60) + 2 * MAX_RETRY_AFTER_SECONDS + JOB_TIMEOUT_MARGIN_SECONDS,
        )
        self.assertEqual(MAX_ATTEMPTS, 3)

    def test_a_waiting_card_says_it_waits_for_a_free_backend_slot_and_keeps_polling(self):
        first, second, _third = self.ask_all()

        waiting = self.poll(second.field_key)
        running = self.poll(first.field_key)

        self.assertIn("Waiting for a free AI backend slot...", waiting)
        self.assertIn("Background job: not started yet", waiting)
        self.assertNotIn("No background job is recorded", waiting)
        self.assertRegex(waiting, r'<li\b[^>]*hx-trigger="every 3s, ndi:read"')
        self.assertIn("Waiting for the backend...", running)
        self.assertNotIn("free AI backend slot", running)

    def test_waiting_proposals_fail_when_no_backend_can_answer_them(self):
        _first, second, _third = self.ask_all()

        with override_plugins_config(netbox_data_import={}):
            response = self.client.get(
                reverse("plugins:netbox_data_import:trace_proposal"),
                {**self.claim(), "field_key": second.field_key},
                headers=HTMX,
            )

        self.assertEqual(response.status_code, 200, response.content[:500])
        rows = self.rows()
        self.assertEqual(rows[0].status, ProposalStatus.QUEUED)
        self.assertEqual(
            [(row.status, row.failure_reason) for row in rows[1:]],
            [(ProposalStatus.FAILED, ProposalFailureReason.INVALID_CONFIGURATION)] * 2,
        )


class DispatchRaceTest(IsolatedRQQueueTestMixin, TransactionTestCase):
    """Two dispatchers that read the same free slot must not both fill it."""

    def setUp(self):
        super().setUp()
        # TransactionTestCase truncates between tests, so the shared fixture is rebuilt per method.
        ProposalFixture.setUpTestData.__func__(type(self))
        self.enterContext(override_plugins_config(netbox_data_import=backend_config()))
        self.first = ProposalFixture.make_proposal(self, field_key=self.field_key)
        self.second = ProposalFixture.make_proposal(self, field_key=self.other_field_key)

    def wait_until_blocked_or(self, finished):
        """Sequence the threads only: wait until the other dispatcher waits for a lock, or ends."""
        query = "SELECT count(*) FROM pg_locks WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
        deadline = monotonic() + 10
        while monotonic() < deadline and not finished.is_set():
            with connection.cursor() as cursor:
                cursor.execute(query)
                if cursor.fetchone()[0]:
                    return
            sleep(0.05)

    def test_a_second_dispatcher_takes_no_slot_the_first_one_filled(self) -> None:
        from netbox_data_import.proposal_dispatch import dispatch_proposals

        other: list[int] = []
        errors: list[BaseException] = []
        finished = Event()

        def second_dispatcher():
            connections["default"].close()
            try:
                other.extend(dispatch_proposals())
            except BaseException as exc:  # noqa: BLE001 - the thread hands every failure to the test
                errors.append(exc)
            finally:
                connections["default"].close()
                finished.set()

        thread = Thread(target=second_dispatcher, daemon=True)
        with transaction.atomic():
            held = dispatch_proposals()
            thread.start()
            self.wait_until_blocked_or(finished)
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(held, [self.first.pk])
        self.assertEqual(other, [])
        self.assertEqual(proposal_jobs().count(), 1)
        self.assertIsNone(ResolutionProposal.objects.get(pk=self.second.pk).job_id)


class MaxConcurrentRequestsSettingTest(SimpleTestCase):
    """The file fallback takes the limit as an optional key, validated as the row column is."""

    def test_an_omitted_limit_is_one(self):
        self.assertEqual(validate_file_fallback(FALLBACK, ALLOWLIST)["max_concurrent_requests"], 1)

    def test_a_whole_number_of_at_least_one_is_kept(self):
        self.assertEqual(
            validate_file_fallback({**FALLBACK, "max_concurrent_requests": 4}, ALLOWLIST)["max_concurrent_requests"], 4
        )

    def test_a_limit_the_column_would_refuse_is_refused(self):
        for value in (0, -1, True, False, "2", 1.5, None):
            with self.subTest(value=value), self.assertRaises(InvalidInferenceConfiguration) as caught:
                validate_file_fallback({**FALLBACK, "max_concurrent_requests": value}, ALLOWLIST)
            self.assertIn("'inference_backend.max_concurrent_requests' must be a whole number", str(caught.exception))


class MaxConcurrentRequestsBackendTest(TestCase):
    """The row and the fallback both carry the limit into the resolved backend."""

    def form(self, value):
        data = {
            "backend_key": "limited",
            "display_name": "Limited",
            "adapter_type": "openai_compatible",
            "api_root": ALLOWLIST[0],
            "model": "limited-model",
            "authentication": "bearer",
            "response_mode": "prompt_json",
            "credential_reference": json.dumps(REFERENCE),
            "connect_timeout": 5,
            "read_timeout": 60,
            "max_concurrent_requests": value,
        }
        with override_plugins_config(netbox_data_import={"inference_backend_origin_allowlist": ALLOWLIST}):
            form = InferenceBackendForm(data=data)
            valid = form.is_valid()
        return form, valid

    def test_the_form_refuses_a_limit_below_one_or_not_a_number(self):
        for value in ("0", "-1", "true", "1.5", ""):
            with self.subTest(value=value):
                form, valid = self.form(value)
                self.assertFalse(valid)
                self.assertIn("max_concurrent_requests", form.errors)

    def test_the_form_saves_a_valid_limit(self):
        form, valid = self.form("3")
        self.assertTrue(valid, form.errors)
        self.assertEqual(form.save().max_concurrent_requests, 3)

    def test_the_model_refuses_a_zero_limit(self):
        with override_plugins_config(netbox_data_import={"inference_backend_origin_allowlist": ALLOWLIST}):
            row = make_row(api_root=ALLOWLIST[0], max_concurrent_requests=0)
            with self.assertRaises(ValidationError) as caught:
                row.full_clean()
        self.assertIn("max_concurrent_requests", caught.exception.message_dict)

    def test_the_resolved_backend_carries_the_limit_of_its_source(self):
        from netbox_data_import.inference_backend import resolve_active_backend

        with override_plugins_config(netbox_data_import=backend_config(max_concurrent_requests=2)):
            self.assertEqual(resolve_active_backend().max_concurrent_requests, 2)
            make_row(api_root=ALLOWLIST[0], max_concurrent_requests=5)
            self.assertEqual(resolve_active_backend().max_concurrent_requests, 5)

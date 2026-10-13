# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Proposal Jobs start only while the active Inference Backend has a free request slot."""

import json
import time
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread
from time import monotonic, sleep

from core.choices import JobStatusChoices
from core.models import Job
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django_pg_utils import advisory_lock
from django_rq import get_queue
from netbox.constants import RQ_QUEUE_LOW
from rq.executions import Execution
from rq.job import JobStatus

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


@contextmanager
def worker_owning(job):
    """Hold the Job's delivery lock on a second connection, as a worker inside its backend call does."""
    from netbox_data_import.jobs import _job_lock

    held, release = Event(), Event()
    errors: list[BaseException] = []

    def worker():
        try:
            with advisory_lock(_job_lock(job)):
                held.set()
                release.wait(timeout=30)
        except BaseException as exc:  # noqa: BLE001 - the thread hands every failure to the test
            errors.append(exc)
            held.set()
        finally:
            connections["default"].close()

    thread = Thread(target=worker, daemon=True)
    thread.start()
    if not held.wait(timeout=10):
        raise AssertionError("The worker did not get its Job lock.")
    try:
        yield
    finally:
        release.set()
        thread.join(timeout=10)
    if thread.is_alive() or errors:
        raise AssertionError(f"The worker lock holder did not finish cleanly: {errors}")


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

    def poll(self, field_key, *, next_interval=True, **fallback):
        """Read one card as a pending card polls it, which is the dispatcher's backstop."""
        from netbox_data_import.proposal_dispatch import POLL_DISPATCH_KEY

        if next_interval:
            cache.delete(POLL_DISPATCH_KEY)
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
        job = self.start(first)
        cancelled = self.post("trace_cancel_proposal", proposal_id=first.pk)
        self.assertEqual(cancelled.status_code, 200, cancelled.content[:500])

        with worker_owning(job):
            self.poll(second.field_key)

        self.assertIsNone(ResolutionProposal.objects.get(pk=second.pk).job_id)
        with self.answering_backend() as seen:
            self.run_job(first)
        self.assertEqual(seen, [])
        rows = self.rows()
        self.assertEqual(rows[0].status, ProposalStatus.CANCELLED)
        # A cancelled row is not abandoned, so its Job ends quietly.
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_COMPLETED)
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

    def start(self, proposal):
        """Start the proposal's Job and claim the proposal, as a worker does before its backend call."""
        job = Job.objects.get(pk=proposal.job_id)
        job.start()
        self.assertTrue(claim_proposal(proposal.pk))
        return job

    def test_a_worker_that_owns_its_job_keeps_the_slot_when_the_queue_loses_its_task(self):
        """A Redis restart can drop the task record while the worker is still inside its backend call."""
        first, second, _third = self.ask_all()
        job = self.start(first)
        get_queue(RQ_QUEUE_LOW).fetch_job(str(job.job_id)).delete()

        with worker_owning(job):
            self.poll(second.field_key)

            rows = self.rows()
            self.assertEqual((rows[0].status, rows[0].failure_reason), (ProposalStatus.RUNNING, ""))
            self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_RUNNING)
            self.assertEqual([row.job_id is not None for row in rows], [True, False, False])

        self.poll(second.field_key)

        rows = self.rows()
        self.assertEqual(
            (rows[0].status, rows[0].failure_reason), (ProposalStatus.FAILED, ProposalFailureReason.QUEUE_UNAVAILABLE)
        )
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_an_owned_job_keeps_its_slot_however_long_it_runs_and_whatever_the_timeouts_say(self):
        first, second, _third = self.ask_all(read_timeout=600)
        job = self.start(first)
        Job.objects.filter(pk=job.pk).update(started=timezone.now() - timedelta(days=1))

        with worker_owning(job):
            self.poll(second.field_key, connect_timeout=1, read_timeout=1)

        rows = self.rows()
        self.assertEqual((rows[0].status, rows[0].failure_reason), (ProposalStatus.RUNNING, ""))
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_RUNNING)
        self.assertEqual([row.job_id is not None for row in rows], [True, False, False])

    def test_a_running_job_keeps_its_slot_while_rq_runs_its_task_even_without_the_job_lock(self):
        """A dropped database session frees the Job lock while the worker's backend call goes on."""
        first, second, _third = self.ask_all()
        job = self.start(first)
        queue = get_queue(RQ_QUEUE_LOW)
        task = queue.fetch_job(str(job.job_id))
        # Start the task the way an RQ worker does, with an execution that is already past its heartbeat.
        queue.remove(task)
        with queue.connection.pipeline() as pipeline:
            task.prepare_for_execution("lost-session-worker", pipeline)
            Execution.create(task, ttl=1, pipeline=pipeline, worker_name="lost-session-worker")
            pipeline.execute()

        self.poll(second.field_key)

        rows = self.rows()
        self.assertEqual((rows[0].status, rows[0].failure_reason), (ProposalStatus.RUNNING, ""))
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_RUNNING)
        self.assertEqual([row.job_id is not None for row in rows], [True, False, False])

        # RQ's own cleanup marks the task of a dead worker failed once its heartbeat expires.
        queue.started_job_registry.cleanup(timestamp=time.time() + 60)
        self.assertEqual(task.get_status(refresh=True), JobStatus.FAILED)

        self.poll(second.field_key)

        rows = self.rows()
        self.assertEqual(
            (rows[0].status, rows[0].failure_reason), (ProposalStatus.FAILED, ProposalFailureReason.QUEUE_UNAVAILABLE)
        )
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_a_redelivered_job_fails_the_proposal_its_dead_delivery_claimed(self):
        """An operator requeues the task of a worker that died after its claim; the field must not stay running."""
        first, _second, _third = self.ask_all()
        job = self.start(first)

        with self.answering_backend() as seen:
            self.run_job(first)

        self.assertEqual(seen, [])
        rows = self.rows()
        self.assertEqual(
            (rows[0].status, rows[0].failure_reason), (ProposalStatus.FAILED, ProposalFailureReason.QUEUE_UNAVAILABLE)
        )
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_FAILED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_a_pending_job_releases_its_slot_only_once_its_task_is_lost(self):
        from netbox_data_import.jobs import QUEUE_PUSH_GRACE

        first, second, _third = self.ask_all()
        job = Job.objects.get(pk=first.job_id)

        self.poll(second.field_key)

        self.assertIsNone(ResolutionProposal.objects.get(pk=second.pk).job_id)
        get_queue(RQ_QUEUE_LOW).fetch_job(str(job.job_id)).delete()
        # A young pending Job may have no task yet, because the push follows the commit.
        self.poll(second.field_key)

        self.assertIsNone(ResolutionProposal.objects.get(pk=second.pk).job_id)
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - QUEUE_PUSH_GRACE - timedelta(seconds=5))

        self.poll(second.field_key)

        rows = self.rows()
        self.assertEqual(
            (rows[0].status, rows[0].failure_reason), (ProposalStatus.FAILED, ProposalFailureReason.QUEUE_UNAVAILABLE)
        )
        self.assertEqual(Job.objects.get(pk=job.pk).status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])

    def test_all_polls_inside_one_interval_dispatch_once(self):
        """Every pending card polls every 3 seconds, so the polls share one dispatch per interval."""
        from netbox_data_import.jobs import QUEUE_PUSH_GRACE

        first, second, third = self.ask_all()
        self.poll(third.field_key)
        job = Job.objects.get(pk=first.job_id)
        get_queue(RQ_QUEUE_LOW).fetch_job(str(job.job_id)).delete()
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - QUEUE_PUSH_GRACE - timedelta(seconds=5))

        self.poll(second.field_key, next_interval=False)

        self.assertEqual(ResolutionProposal.objects.get(pk=first.pk).status, ProposalStatus.QUEUED)
        self.assertIsNone(ResolutionProposal.objects.get(pk=second.pk).job_id)

        self.poll(second.field_key)

        rows = self.rows()
        self.assertEqual(rows[0].status, ProposalStatus.FAILED)
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


class OwnedDeliveryTest(_DispatchViewMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """The real proposal worker holds its Job lock through the run, so a poll cannot release its slot."""

    def setUp(self):
        self.build_topology()
        super().setUp()

    def test_a_poll_during_a_real_delivery_leaves_the_running_job_its_slot(self) -> None:
        first, second, _third = self.ask_all()
        task = get_queue(RQ_QUEUE_LOW).fetch_job(str(Job.objects.get(pk=first.job_id).job_id))
        arguments = task.kwargs
        claimed, release = Event(), Event()
        errors: list[BaseException] = []

        def pause_after_the_claim(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            # The worker started its Job and claimed the row, and has not called the backend yet.
            if sql.startswith('UPDATE "netbox_data_import_resolutionproposal" SET "status"') and params[0] == "running":
                claimed.set()
                release.wait(20)
            return result

        def deliver():
            try:
                with connection.execute_wrapper(pause_after_the_claim):
                    ResolutionProposalJob.handle(**arguments)
            except BaseException as exc:  # noqa: BLE001 - the thread hands every failure to the test
                errors.append(exc)
            finally:
                connection.close()

        with self.answering_backend() as seen:
            worker = Thread(target=deliver, daemon=True)
            worker.start()
            try:
                self.assertTrue(claimed.wait(20), "the worker never claimed its proposal")
                task.delete()
                response = self.client.get(
                    reverse("plugins:netbox_data_import:trace_proposal"),
                    {**self.claim(), "field_key": second.field_key},
                    headers=HTMX,
                )
                rows_while_running = self.rows()
            finally:
                release.set()
                worker.join(30)

        self.assertEqual(errors, [])
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual((rows_while_running[0].status, rows_while_running[0].failure_reason), ("running", ""))
        self.assertEqual([row.job_id is not None for row in rows_while_running], [True, False, False])
        self.assertEqual(len(seen), 1)
        rows = self.rows()
        self.assertEqual(rows[0].status, ProposalStatus.COMPLETED)
        self.assertEqual([row.job_id is not None for row in rows], [True, True, False])


class DeletedJobRaceTest(IsolatedRQQueueTestMixin, TransactionTestCase):
    """A Job deleted between the holder read and its refresh holds no slot and does not stop the dispatch."""

    def setUp(self):
        super().setUp()
        ProposalFixture.setUpTestData.__func__(type(self))
        self.enterContext(override_plugins_config(netbox_data_import=backend_config()))
        self.first = ProposalFixture.make_proposal(self, field_key=self.field_key)
        self.second = ProposalFixture.make_proposal(self, field_key=self.other_field_key)

    def test_a_job_deleted_after_its_lock_is_taken_frees_its_slot(self) -> None:
        from netbox_data_import.proposal_dispatch import dispatch_proposals
        from netbox_data_import.tests.helpers import run_on_separate_connection

        self.assertEqual(dispatch_proposals(), [self.first.pk])
        deleted = ResolutionProposal.objects.get(pk=self.first.pk).job_id

        def delete_the_job_after_the_lock(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "pg_try_advisory_xact_lock" in sql:
                with run_on_separate_connection(lambda: Job.objects.filter(pk=deleted).delete()):
                    pass
            return result

        with connection.execute_wrapper(delete_the_job_after_the_lock):
            dispatched = dispatch_proposals()

        self.assertFalse(Job.objects.filter(pk=deleted).exists())
        # The SET_NULL left the queued row without a Job, so it waits again and is the oldest one.
        self.assertEqual(dispatched, [self.first.pk])
        self.assertNotEqual(ResolutionProposal.objects.get(pk=self.first.pk).job_id, None)
        self.assertIsNone(ResolutionProposal.objects.get(pk=self.second.pk).job_id)


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

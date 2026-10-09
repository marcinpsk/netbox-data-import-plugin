# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A queued trace sync: its queue, what the workspace says about it, and how the operator cancels it."""

import threading
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import patch

from core.choices import JobStatusChoices
from core.management.commands.rqworker import DEFAULT_QUEUES
from core.models import Job
from dcim.models import Cable
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django_rq import get_queue, get_worker
from netbox.constants import RQ_QUEUE_DEFAULT, RQ_QUEUE_HIGH, RQ_QUEUE_LOW
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from rq.job import Job as RQJob, JobStatus
from rq.worker import Worker, WorkerStatus

from netbox_data_import.jobs import IMPORT_TASK_LOST, QUEUE_UNREADABLE, ImportJobRunner, ResolutionProposalJob
from netbox_data_import.models import ImportExecution, PreviewState
from netbox_data_import.preview_coordinator import (
    SYNC_CANCELLED,
    SYNC_FINISHED,
    SYNC_NOT_THIS_PREVIEW,
    SYNC_OTHER_ACTIVE,
    SYNC_QUEUED,
    SYNC_RUNNING,
    SYNC_STARTED,
)
from netbox_data_import.tests.helpers import (
    preview_claim,
    preview_coordinator,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.plugins_config import override_plugins_config
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path
from netbox_data_import.tests.test_inference_backend import ALLOWLIST, FALLBACK
from netbox_data_import.views import _trace_workspace_url

HTMX = {"HX-Request": "true"}
BACKEND = {"inference_backend": FALLBACK, "inference_backend_origin_allowlist": ALLOWLIST}


class _QueuedSyncMixin:
    """One actionable trace and two open terminations, so a page can ask AI and queue a sync."""

    def upload(self):
        """Leave the client on a planned preview of the two traces."""
        self.client.force_login(self.actor)
        upload = BytesIO(
            trace_workbook_bytes(
                path_blocks=[
                    direct_path(),
                    direct_path(
                        from_end=trace_termination("DEV-A", "", "absent-port", "Port"),
                        to_end=trace_termination("DEV-B", "", "absent-b", "Port"),
                    ),
                ]
            )
        )
        upload.name = "traces.xlsx"
        response = upload_preview(
            self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload}, follow=True
        )
        self.assertEqual(response.status_code, 200)

    def actionable_trace(self):
        """Return the identity of the trace the workspace offers to synchronize."""
        traces = self.client.get(reverse("plugins:netbox_data_import:trace_workspace")).context["traces"]
        return next(trace for trace in traces if any(a.key == "sync" and a.enabled for a in trace.actions)).identity

    def post(self, route, data, **extra):
        """Post one workspace command with the claim the page holds, and run its after-commit pushes."""
        capture = getattr(self, "captureOnCommitCallbacks", None)
        with capture(execute=True) if capture else nullcontext():
            return self.client.post(
                reverse(f"plugins:netbox_data_import:{route}"), {**preview_claim(self.client), **data}, **extra
            )

    def queue_sync(self):
        """Synchronize the actionable trace and return its Job, which no worker has started."""
        identity = self.actionable_trace()
        response = self.post("trace_sync", {"identity": identity})
        self.assertEqual(response.status_code, 302, response.content[:500])
        self.identity = identity
        return Job.objects.get(data__job_type=ImportJobRunner.job_type)

    def ask_all(self):
        """Ask AI about every open termination and return the proposal Jobs it queued."""
        with override_plugins_config(netbox_data_import=BACKEND):
            response = self.post("trace_request_all_proposals", {}, headers=HTMX)
        self.assertEqual(response.status_code, 302, response.content[:500])
        jobs = list(ResolutionProposalJob.get_jobs().order_by("pk"))
        self.assertEqual(len(jobs), 2)
        return jobs

    def cancel(self, job, **extra):
        """Post the cancel command for one Job, as the workspace form sends it."""
        return self.post("trace_sync_cancel", {"job_id": str(job.pk), "trace": self.identity}, **extra)

    def status_read(self, **params):
        """Read the sync status the workspace polls, with the claim the page holds."""
        return self.client.get(
            reverse("plugins:netbox_data_import:trace_sync_status"),
            {**preview_claim(self.client), "trace": self.identity, **params},
            headers=HTMX,
        )


class SyncQueuePriorityTest(_QueuedSyncMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    """A proposal waits in the low queue, so a sync queued after it does not wait for its inference."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def setUp(self):
        super().setUp()
        self.upload()

    def test_a_proposal_job_waits_in_the_low_queue(self):
        jobs = self.ask_all()

        for job in jobs:
            with self.subTest(job=job.pk):
                self.assertEqual(job.queue_name, RQ_QUEUE_LOW)
                self.assertIn(str(job.job_id), get_queue(RQ_QUEUE_LOW).job_ids)
        self.assertEqual(get_queue(RQ_QUEUE_DEFAULT).job_ids, [])

    def test_a_worker_takes_a_sync_before_the_proposals_queued_ahead_of_it(self):
        self.ask_all()
        sync = self.queue_sync()
        self.assertEqual(sync.queue_name, RQ_QUEUE_DEFAULT)

        # The queues a NetBox worker serves when none is named, in the order it serves them.
        taken, _queue = get_worker(*DEFAULT_QUEUES).dequeue_job_and_maintain_ttl(None)

        self.assertEqual(taken.id, str(sync.job_id))


class SyncStatusTest(_QueuedSyncMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    """The workspace says what the sync Job does now: waits in the queue, runs, or ended."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def setUp(self):
        super().setUp()
        self.upload()

    def workspace(self):
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), {"trace": self.identity})

    def push(self, queue_name):
        """Push one task that a worker would take, as another NetBox job pushes it after its commit."""
        with self.captureOnCommitCallbacks(execute=True):
            get_queue(queue_name).enqueue("builtins.len", [])

    def test_a_queued_sync_says_where_it_waits_in_its_queue(self):
        # A worker started for named queues may never serve high, so only the Job's own queue counts.
        self.push(RQ_QUEUE_HIGH)
        self.push(RQ_QUEUE_DEFAULT)
        self.push(RQ_QUEUE_DEFAULT)
        job = self.queue_sync()

        idle = self.workspace()
        self.assertContains(idle, SYNC_QUEUED)
        self.assertContains(idle, "Position in the default queue: 3.")
        self.assertContains(idle, "No worker serves the default queue.")

        worker = get_worker(*DEFAULT_QUEUES)
        worker.register_birth()
        worker.set_state(WorkerStatus.BUSY)
        busy = self.workspace()

        self.assertContains(busy, "Busy workers: 1 of 1")
        self.assertNotContains(busy, "No worker serves")
        self.assertNotContains(busy, SYNC_RUNNING)
        self.assertContains(busy, f'href="{job.get_absolute_url()}"')
        self.assertContains(busy, reverse("plugins:netbox_data_import:trace_sync_cancel"))
        self.assertContains(busy, 'hx-trigger="every 3s"')
        self.assertContains(busy, reverse("plugins:netbox_data_import:trace_sync_status"))

    def test_reading_the_status_leaves_the_worker_registry_alone(self):
        self.queue_sync()
        busy = get_worker(*DEFAULT_QUEUES, name="busy-worker")
        busy.register_birth()
        busy.set_state(WorkerStatus.BUSY)
        stale = get_worker(*DEFAULT_QUEUES, name="stale-worker")
        stale.register_birth()
        # A worker that stopped sending heartbeats: its hash expired, its registration stays.
        stale.connection.delete(stale.key)

        page = self.workspace()
        self.status_read()

        self.assertContains(page, "Busy workers: 1 of 1.")
        self.assertTrue(stale.connection.sismember("rq:workers", stale.key))
        self.assertTrue(stale.connection.sismember("rq:workers:default", stale.key))

    def test_a_queue_outage_keeps_the_job_state_and_says_the_queue_cannot_be_read(self):
        job = self.queue_sync()
        progress = reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk})
        outages = {
            "task": patch("netbox_data_import.jobs.import_queue_task", autospec=True, side_effect=RedisConnectionError),
            "position": patch.object(RQJob, "get_position", autospec=True, side_effect=RedisTimeoutError),
            "workers": patch.object(Worker, "all_keys", autospec=True, side_effect=RedisConnectionError),
        }
        for name, outage in outages.items():
            with self.subTest(outage=name), outage:
                page = self.workspace()
                poll = self.status_read()
                progress_page = self.client.get(progress)

                self.assertContains(page, SYNC_QUEUED)
                self.assertContains(page, QUEUE_UNREADABLE)
                self.assertNotContains(page, IMPORT_TASK_LOST)
                self.assertContains(page, 'hx-trigger="every 3s"')
                self.assertContains(poll, QUEUE_UNREADABLE)
                self.assertContains(progress_page, QUEUE_UNREADABLE)
                self.assertNotContains(progress_page, IMPORT_TASK_LOST)

        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_RUNNING, started=timezone.now())
        with patch("netbox_data_import.jobs.import_queue_task", autospec=True, side_effect=RedisConnectionError):
            running = self.workspace()
        self.assertContains(running, SYNC_RUNNING)
        self.assertContains(running, QUEUE_UNREADABLE)

    def test_a_running_sync_shows_its_phase_and_progress_and_offers_no_cancel(self):
        job = self.queue_sync()
        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_RUNNING, started=timezone.now())
        rq_job = get_queue(job.queue_name).fetch_job(str(job.job_id))
        rq_job.meta.update({"processed": 25, "total": 100, "phase": "importing"})
        rq_job.save_meta()

        page = self.workspace()

        self.assertContains(page, SYNC_RUNNING)
        self.assertContains(page, "Phase: importing")
        self.assertContains(page, "Steps: 25 of 100")
        self.assertNotContains(page, SYNC_QUEUED)
        self.assertNotContains(page, reverse("plugins:netbox_data_import:trace_sync_cancel"))
        self.assertContains(page, 'hx-trigger="every 3s"')

    def test_the_poll_answers_the_status_and_reloads_the_page_when_the_job_ends(self):
        job = self.queue_sync()

        queued = self.status_read()
        self.assertEqual(queued.status_code, 200)
        self.assertContains(queued, SYNC_QUEUED)
        self.assertContains(queued, 'hx-trigger="every 3s"')
        self.assertNotIn("HX-Refresh", queued.headers)

        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_COMPLETED, completed=timezone.now())
        ended = self.status_read()

        self.assertEqual(ended.headers.get("HX-Refresh"), "true")
        page = self.workspace()
        self.assertContains(page, SYNC_FINISHED)
        self.assertNotContains(page, 'hx-trigger="every 3s"')
        reread = page.content.decode().split('id="traceWorkspaceReread"', 1)[1].split(">", 1)[0]
        self.assertNotIn("disabled", reread)

    def test_a_sync_whose_job_was_deleted_asks_for_a_re_read(self):
        job = self.queue_sync()
        Job.objects.filter(pk=job.pk).delete()

        page = self.workspace()

        self.assertContains(page, SYNC_FINISHED)
        self.assertNotContains(page, 'hx-trigger="every 3s"')
        self.assertContains(page, "data-sync-reread")

    def test_the_poll_refuses_a_claim_the_page_no_longer_holds(self):
        self.queue_sync()
        stale = {**preview_claim(self.client), "preview_revision": "1"}

        response = self.client.get(
            reverse("plugins:netbox_data_import:trace_sync_status"), {**stale, "trace": self.identity}, headers=HTMX
        )

        self.assertEqual(response.status_code, 409)

    def test_a_refusal_names_the_queued_or_running_state_of_the_sync(self):
        job = self.queue_sync()

        queued = self.post("trace_sync", {"identity": self.identity})
        self.assertContains(queued, SYNC_QUEUED, status_code=409)
        self.assertNotContains(queued, SYNC_RUNNING, status_code=409)

        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_RUNNING, started=timezone.now())
        running = self.post("trace_sync", {"identity": self.identity})
        self.assertContains(running, SYNC_RUNNING, status_code=409)

    def test_the_progress_page_says_a_queued_sync_waits_for_a_worker(self):
        self.push(RQ_QUEUE_DEFAULT)
        job = self.queue_sync()

        for route in ("import_progress", "import_progress_status"):
            with self.subTest(route=route):
                page = self.client.get(reverse(f"plugins:netbox_data_import:{route}", kwargs={"pk": job.pk}))

                self.assertContains(page, "Position in the default queue: 2.")
                self.assertContains(page, "No worker serves the default queue.")


class SyncCancelTest(_QueuedSyncMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    """The operator cancels a sync that no worker has started, and the preview is ready again."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def setUp(self):
        super().setUp()
        self.upload()

    def test_cancel_ends_a_queued_sync_and_returns_the_preview_to_review(self):
        job = self.queue_sync()
        before = preview_coordinator(self.client)
        rq_job = get_queue(job.queue_name).fetch_job(str(job.job_id))
        arguments = rq_job.kwargs

        response = self.cancel(job, headers=HTMX)

        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assertEqual(response.url, _trace_workspace_url(self.identity))
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertIsNotNone(job.completed)
        self.assertIsNone(job.started)
        self.assertEqual(
            (job.data["phase"], job.data["message"], job.error), ("cancelled", SYNC_CANCELLED, SYNC_CANCELLED)
        )
        self.assertEqual(rq_job.get_status(refresh=True), JobStatus.CANCELED)
        self.assertNotIn(rq_job.id, get_queue(job.queue_name).job_ids)
        after = preview_coordinator(self.client)
        self.assertEqual((after.state, after.job_id), (PreviewState.READY, None))
        self.assertEqual(after.revision, before.revision + 1)
        self.assertEqual(after.preview_token, before.preview_token)
        page = self.client.get(response.url)
        self.assertNotContains(page, SYNC_QUEUED)
        self.assertTrue(next(a for a in page.context["selected_trace"].actions if a.key == "sync").enabled)

        # A delivery that RQ had already handed out writes nothing for a cancelled Job.
        ImportJobRunner.handle(**arguments)
        self.assertFalse(Cable.objects.exists())
        self.assertFalse(ImportExecution.objects.exists())
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)

    def test_the_progress_page_offers_cancel_and_then_says_the_sync_was_cancelled(self):
        job = self.queue_sync()
        progress = reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk})
        cancel_url = reverse("plugins:netbox_data_import:trace_sync_cancel")
        self.assertContains(self.client.get(progress), f'action="{cancel_url}"')

        response = self.post("trace_sync_cancel", {"job_id": str(job.pk)})

        self.assertEqual(response.url, reverse("plugins:netbox_data_import:trace_workspace"))
        page = self.client.get(progress)
        self.assertContains(page, SYNC_CANCELLED)
        self.assertNotContains(page, f'action="{cancel_url}"')
        self.assertNotContains(page, "A newer preview replaced this import's preview.")
        self.assertContains(page, f'href="{reverse("plugins:netbox_data_import:trace_workspace")}"')

    def test_cancel_refuses_a_sync_that_already_started(self):
        job = self.queue_sync()
        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_RUNNING, started=timezone.now())
        claim = preview_claim(self.client)

        refused = self.cancel(job)

        self.assertContains(refused, SYNC_STARTED, status_code=409)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_RUNNING)
        self.assertEqual(preview_claim(self.client), claim)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.SYNC_PENDING)
        self.assertIn(str(job.job_id), get_queue(job.queue_name).job_ids)

    def test_cancel_refuses_a_sync_that_already_ended(self):
        job = self.queue_sync()
        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_COMPLETED, completed=timezone.now())

        refused = self.cancel(job)

        self.assertContains(refused, SYNC_FINISHED, status_code=409)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)

    def test_cancel_refuses_a_job_this_preview_did_not_queue(self):
        job = self.queue_sync()
        other = get_user_model().objects.create_user("other-operator")
        foreign = ImportJobRunner.enqueue(
            name=ImportJobRunner.name,
            user=other,
            profile_id=self.profile.pk,
            source_document_id=job.data["source_document_id"],
            accepted_plan={},
            selection=[],
            idempotency_key="foreign",
        )
        foreign.data = dict(job.data)
        foreign.save(update_fields=["data"])

        refused = self.cancel(foreign)
        self.assertContains(refused, SYNC_NOT_THIS_PREVIEW, status_code=409)

        # The preview's own Job, moved to another user, is no longer this operator's to cancel.
        Job.objects.filter(pk=job.pk).update(user=other)
        refused = self.cancel(job)
        self.assertContains(refused, SYNC_NOT_THIS_PREVIEW, status_code=409)

        for each in (job, foreign):
            each.refresh_from_db()
            self.assertEqual(each.status, JobStatusChoices.STATUS_PENDING)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.SYNC_PENDING)

    def test_cancel_refuses_while_another_sync_of_the_source_is_active(self):
        job = self.queue_sync()
        rival = ImportJobRunner.enqueue(
            name=ImportJobRunner.name,
            user=self.actor,
            profile_id=self.profile.pk,
            source_document_id=job.data["source_document_id"],
            accepted_plan={},
            selection=[],
            idempotency_key="rival",
        )
        rival.data = dict(job.data)
        rival.save(update_fields=["data"])
        claim = preview_claim(self.client)

        refused = self.cancel(job)

        self.assertContains(refused, SYNC_OTHER_ACTIVE, status_code=409)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_PENDING)
        self.assertEqual(preview_claim(self.client), claim)

    def test_cancel_refuses_a_job_id_that_is_not_a_number(self):
        job = self.queue_sync()

        refused = self.post("trace_sync_cancel", {"job_id": "1e3", "trace": self.identity})

        self.assertContains(refused, SYNC_NOT_THIS_PREVIEW, status_code=409)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_PENDING)

    def test_a_queue_that_cannot_answer_keeps_the_committed_cancel(self):
        job = self.queue_sync()
        arguments = get_queue(job.queue_name).fetch_job(str(job.job_id)).kwargs

        # Redis is the boundary: the cancel after the commit cannot reach it.
        with patch.object(RQJob, "cancel", autospec=True, side_effect=RedisConnectionError):
            response = self.cancel(job)

        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)
        # The task stays queued, and its late delivery writes nothing.
        ImportJobRunner.handle(**arguments)
        self.assertFalse(Cable.objects.exists())
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)

    def test_cancel_needs_the_exact_claim(self):
        job = self.queue_sync()
        stale = {**preview_claim(self.client), "preview_revision": "1"}

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync_cancel"),
            {**stale, "job_id": str(job.pk), "trace": self.identity},
        )

        self.assertEqual(refused.status_code, 409)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_PENDING)


class SyncCancelRaceTest(_QueuedSyncMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """A worker that holds the Job lock owns the delivery, so a cancel must refuse rather than race it."""

    def setUp(self):
        super().setUp()
        self.build_topology()
        self.upload()

    def test_cancel_refuses_while_a_worker_holds_the_job_lock(self):
        job = self.queue_sync()
        arguments = get_queue(job.queue_name).fetch_job(str(job.job_id)).kwargs
        claim = preview_claim(self.client)
        locked, release = threading.Event(), threading.Event()
        errors: list[BaseException] = []

        def pause_after_the_lock(execute, sql, params, many, context):
            # The worker took the Job lock and has not yet read the Job row or started it.
            if "pg_advisory_lock" in sql:
                result = execute(sql, params, many, context)
                locked.set()
                release.wait(20)
                return result
            return execute(sql, params, many, context)

        def deliver():
            try:
                with connection.execute_wrapper(pause_after_the_lock):
                    ImportJobRunner.handle(**arguments)
            except BaseException as exc:  # noqa: BLE001 - the thread hands every failure to the test
                errors.append(exc)
            finally:
                connection.close()

        worker = threading.Thread(target=deliver, daemon=True)
        worker.start()
        try:
            self.assertTrue(locked.wait(20), "the worker never took the Job lock")
            refused = self.cancel(job)
            job.refresh_from_db()
            status_while_held = job.status
        finally:
            release.set()
            worker.join(30)

        self.assertContains(refused, SYNC_STARTED, status_code=409)
        self.assertEqual(status_while_held, JobStatusChoices.STATUS_PENDING)
        self.assertEqual(preview_claim(self.client), claim)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.SYNC_PENDING)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        # The refused cancel left the delivery alone, so the worker ran the sync to its end.
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(Cable.objects.count(), 1)

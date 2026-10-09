# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A queued trace sync: its queue, and what the workspace says about it."""

from contextlib import nullcontext
from io import BytesIO

from core.choices import JobStatusChoices
from core.management.commands.rqworker import DEFAULT_QUEUES
from core.models import Job
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_rq import get_queue, get_worker
from netbox.constants import RQ_QUEUE_DEFAULT, RQ_QUEUE_HIGH, RQ_QUEUE_LOW
from rq.worker import WorkerStatus

from netbox_data_import.jobs import ImportJobRunner, ResolutionProposalJob
from netbox_data_import.preview_coordinator import SYNC_FINISHED, SYNC_QUEUED, SYNC_RUNNING
from netbox_data_import.tests.helpers import (
    preview_claim,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.plugins_config import override_plugins_config
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path
from netbox_data_import.tests.test_inference_backend import ALLOWLIST, FALLBACK

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

    def test_a_queued_sync_says_it_waits_and_how_many_jobs_are_ahead_of_it(self):
        self.push(RQ_QUEUE_HIGH)
        self.push(RQ_QUEUE_DEFAULT)
        self.push(RQ_QUEUE_DEFAULT)
        job = self.queue_sync()

        idle = self.workspace()
        self.assertContains(idle, SYNC_QUEUED)
        self.assertContains(idle, "Jobs ahead of it: 3")
        self.assertContains(idle, "No worker serves the default queue.")

        worker = get_worker(*DEFAULT_QUEUES)
        worker.register_birth()
        worker.set_state(WorkerStatus.BUSY)
        busy = self.workspace()

        self.assertContains(busy, "Busy workers: 1 of 1")
        self.assertNotContains(busy, "No worker serves")
        self.assertNotContains(busy, SYNC_RUNNING)
        self.assertContains(busy, f'href="{job.get_absolute_url()}"')
        self.assertContains(busy, 'hx-trigger="every 3s"')
        self.assertContains(busy, reverse("plugins:netbox_data_import:trace_sync_status"))

    def test_a_running_sync_shows_its_phase_and_progress(self):
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

                self.assertContains(page, "Jobs ahead of it: 1")
                self.assertContains(page, "No worker serves the default queue.")

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A queued trace sync and the queue it waits in."""

from contextlib import nullcontext
from io import BytesIO

from core.management.commands.rqworker import DEFAULT_QUEUES
from core.models import Job
from django.test import TestCase
from django.urls import reverse
from django_rq import get_queue, get_worker
from netbox.constants import RQ_QUEUE_DEFAULT, RQ_QUEUE_LOW

from netbox_data_import.jobs import ImportJobRunner, ResolutionProposalJob
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

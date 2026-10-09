# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Native NetBox background jobs for data imports."""

import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, NoReturn

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection
from django.utils import timezone
from django_pg_utils import advisory_lock
from rq import get_current_job
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Job as RQJob, JobStatus
from rq.worker import Worker, WorkerStatus

from core.exceptions import JobFailed
from netbox.context_managers import event_tracking
from netbox.jobs import JobRunner, system_job
from utilities.request import NetBoxFakeRequest

from . import branching
from .adapters import SourceUnreadable, UnknownSourceAdapter
from .import_engine import (
    EngineConfigurationError,
    ImportEngine,
    PreconditionFailed,
    SelectionError,
    StalePlan,
    StaleSourceDocument,
    operator_failure_message,
)
from .models import (
    ExecutionOutcome,
    ImportExecution,
    ImportProfile,
    ProposalFailureReason,
    SourceDocument,
    validate_registered_adapter,
)
from .netbox_reader import PlanningTargetUnavailable
from .object_permissions import ObjectPermissionDenied
from .plan import PlanError


_PROGRESS_REPORT_INTERVAL = 25
logger = logging.getLogger(__name__)

IMPORT_TASK_LOST = "The import task is no longer queued or running. Re-read its preview to recover."
QUEUE_PUSH_GRACE = timedelta(minutes=1)


def _import_job_lock(job):
    """Name the session lock that serializes worker delivery with explicit recovery."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT hashtextextended(%s, 0)", [f"netbox-data-import-job:{job.job_id}"])
        return cursor.fetchone()[0]


def import_queue_task(job):
    """Fetch queue evidence without removing a missing task's ID from Redis."""
    import django_rq

    try:
        queue = django_rq.get_queue(job.queue_name)
    except KeyError:
        return None
    try:
        return RQJob.fetch(str(job.job_id), connection=queue.connection, serializer=queue.serializer)
    except NoSuchJobError:
        return None


def _task_lost(job, rq_job) -> bool:
    """Return whether an active Job's queue task can no longer run it."""
    from core.choices import JobStatusChoices

    if rq_job is None:
        # The Job commits before its queue push, so a young pending Job may have no task yet.
        return not (job.status == JobStatusChoices.STATUS_PENDING and job.created > timezone.now() - QUEUE_PUSH_GRACE)
    try:
        status = rq_job.get_status(refresh=True)
    except InvalidJobOperation:
        return True
    return status in (
        None,
        JobStatus.FINISHED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
        JobStatus.STOPPED,
    )


def import_job_abandoned(job) -> bool:
    """Read whether a native active import has lost its queue task, without changing either."""
    from core.choices import JobStatusChoices

    return job.status in JobStatusChoices.ENQUEUED_STATE_CHOICES and _task_lost(job, import_queue_task(job))


QUEUED, RUNNING, LOST, ENDED = "queued", "running", "lost", "ended"


@dataclass(frozen=True)
class ImportJobStatus:
    """What one import Job and its queue task say now, for a page that shows the Job."""

    job: Any
    state: str
    phase: str = ""
    processed: int = 0
    total: int = 0
    # Only for a queued Job whose task is in its queue: the jobs a worker takes first, and its queue's workers.
    ahead: int | None = None
    workers: int | None = None
    busy: int = 0

    @property
    def active(self) -> bool:
        """Return whether the Job still waits in its queue or runs."""
        return self.state in (QUEUED, RUNNING)


def import_job_status(job) -> ImportJobStatus:
    """Read one import Job, its queue task and its queue, without changing any of them."""
    from core.choices import JobStatusChoices

    data = job.data or {}
    status = ImportJobStatus(
        job,
        ENDED,
        phase=str(data.get("phase") or ""),
        processed=int(data.get("processed") or 0),
        total=int(data.get("total") or 0),
    )
    if job.status not in JobStatusChoices.ENQUEUED_STATE_CHOICES:
        return status
    rq_job = import_queue_task(job)
    if _task_lost(job, rq_job):
        return replace(status, state=LOST)
    if rq_job is None:
        # The queue push follows the commit, so the queue knows nothing of this Job yet.
        return replace(status, state=QUEUED)
    # The worker publishes row progress to the task, not to the Job row.
    status = replace(
        status,
        phase=str(rq_job.meta.get("phase") or status.phase),
        processed=int(rq_job.meta.get("processed") or status.processed),
        total=int(rq_job.meta.get("total") or status.total),
    )
    if job.status == JobStatusChoices.STATUS_RUNNING:
        return replace(status, state=RUNNING)
    return _with_queue_place(replace(status, state=QUEUED), rq_job)


def _with_queue_place(status: ImportJobStatus, rq_job) -> ImportJobStatus:
    """Add how many jobs a worker takes before a queued task, and how many workers serve its queue."""
    import django_rq
    from core.management.commands.rqworker import DEFAULT_QUEUES

    queue_name = status.job.queue_name
    workers = Worker.all(queue=django_rq.get_queue(queue_name))
    ahead = rq_job.get_position()
    if ahead is not None and queue_name in DEFAULT_QUEUES:
        # A NetBox worker takes every job of a queue before it takes one from the next queue.
        ahead += sum(django_rq.get_queue(name).count for name in DEFAULT_QUEUES[: DEFAULT_QUEUES.index(queue_name)])
    return replace(
        status,
        ahead=ahead,
        workers=len(workers),
        busy=sum(worker.get_state() == WorkerStatus.BUSY for worker in workers),
    )


def _try_import_job_lock(job) -> bool:
    """Take the Job lock until the transaction ends, or return False at once when a worker holds it."""
    if not connection.in_atomic_block:
        raise RuntimeError("The import Job lock requires the preview command transaction.")
    # Never wait for a worker while holding its profile. PostgreSQL holds this lock through commit or rollback.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [_import_job_lock(job)])
        return cursor.fetchone()[0]


def recover_abandoned_import_job(job) -> None:
    """Fail a lost task under the profile lock, unless a worker still owns its delivery."""
    from core.choices import JobStatusChoices
    from core.models import Job

    if _try_import_job_lock(job):
        job.refresh_from_db()
        if import_job_abandoned(job):
            Job.objects.filter(pk=job.pk, status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).update(
                status=JobStatusChoices.STATUS_ERRORED,
                completed=timezone.now(),
                error=IMPORT_TASK_LOST,
                data={**(job.data or {}), "phase": "failed", "message": IMPORT_TASK_LOST},
            )


class ImportJobRunner(JobRunner):
    """Validate and execute one import while publishing row progress to RQ."""

    job_type = "netbox_data_import.import"

    class Meta:
        name = "Data Import"

    @classmethod
    def handle(cls, job, *args, **kwargs):
        """Skip late or duplicate delivery without adding a transaction around the durable audit."""
        from core.choices import JobStatusChoices
        from core.models import Job

        with advisory_lock(_import_job_lock(job)):
            try:
                job.refresh_from_db()
            except Job.DoesNotExist:
                return
            if job.status in JobStatusChoices.ENQUEUED_STATE_CHOICES:
                super().handle(job, *args, **kwargs)

    def _save_data(self, **values):
        """Merge values into the native Job data."""
        data = {**(self.job.data or {}), **values}
        data.pop("accepted_plan", None)
        self.job.data = data
        self.job.save(update_fields=["data"])

    def _fail(self, message) -> NoReturn:
        """Record a recoverable failure and stop the native Job."""
        values = {"phase": "failed", "message": message}
        execution = ImportExecution.objects.filter(job=self.job).first()
        if execution is not None:
            values["import_execution_id"] = execution.pk
        self._save_data(**values)
        raise JobFailed

    @staticmethod
    def _publish_progress(processed, total):
        """Publish progress outside the database transaction through RQ metadata."""
        if processed not in (0, total) and processed % _PROGRESS_REPORT_INTERVAL:
            return
        rq_job = get_current_job()
        if rq_job is None:
            return
        rq_job.meta.update({"processed": processed, "total": total, "phase": "importing"})
        rq_job.save_meta()

    def _change_logging_request(self, user) -> NetBoxFakeRequest:
        """Return the request NetBox records this Job's changes and events under, with the Job UUID as its id."""
        return NetBoxFakeRequest(
            {
                "META": {},
                "COOKIES": {},
                "POST": {},
                "GET": {},
                "FILES": {},
                "user": user,
                "method": "POST",
                "path": "",
                "path_info": "",
                "id": self.job.job_id,
            }
        )

    def run(self, profile_id, source_document_id, accepted_plan, selection, idempotency_key):
        """Execute one accepted Import Plan as the Job's actor."""
        try:
            branching.fail_job_in_branch(self)
        except JobFailed as exc:
            self._save_data(phase="failed", message=str(exc))
            raise
        user = self.job.user
        if user is None:
            self._fail("The user who started this import is no longer available.")

        profile = ImportProfile.objects.restrict(user, "change").filter(pk=profile_id).first()
        if profile is None:
            self._fail("The import profile is no longer available.")
        try:
            validate_registered_adapter(profile)
        except ValidationError as exc:
            self._fail(operator_failure_message(exc))
        source_document = SourceDocument.objects.filter(pk=source_document_id, profile=profile).first()
        if source_document is None:
            self._fail("The stored source is no longer available. Upload it again.")

        self._save_data(phase="validating")
        progress = {"processed": 0, "total": 0}

        def publish_progress(processed, total):
            """Remember final progress and publish the bounded RQ updates."""
            progress.update(processed=processed, total=total)
            self._publish_progress(processed, total)

        # Only event_tracking: the other request processors (netbox-branching) must not run in a worker.
        with event_tracking(self._change_logging_request(user)):
            try:
                execution = ImportEngine.execute(
                    profile,
                    source_document,
                    accepted_plan,
                    selection,
                    idempotency_key,
                    user,
                    job=self.job,
                    progress_callback=publish_progress,
                )
            except ImportProfile.DoesNotExist:
                self._fail("The import profile is no longer available.")
            except DatabaseError as exc:
                logger.exception("Import execution failed with a database error")
                self._fail(operator_failure_message(exc))
            except (EngineConfigurationError, SourceUnreadable, UnknownSourceAdapter) as exc:
                logger.exception("Import execution failed before its source could be planned")
                self._fail(operator_failure_message(exc))
            except (
                ObjectPermissionDenied,
                PlanError,
                PlanningTargetUnavailable,
                PreconditionFailed,
                SelectionError,
                StalePlan,
                StaleSourceDocument,
                ValidationError,
            ) as exc:
                self._fail(operator_failure_message(exc))
            # Raising here leaves the block before event_tracking flushes the queued events.
            if execution.outcome != ExecutionOutcome.SUCCEEDED:
                reason = (execution.failure_detail or {}).get("reason") or execution.outcome or "unknown"
                self._fail(f"The accepted import execution did not succeed ({reason}).")
        self._save_data(
            phase="completed",
            processed=progress["processed"],
            total=progress["total"],
            import_execution_id=execution.pk,
        )


def retained_sync_jobs(user, profile_id, document_id):
    """Return the active per-trace sync Jobs of one source, whichever preview queued them.

    The Job rows are the record, so a request that lost a race to the coordinator still sees the sync.
    """
    from core.choices import JobStatusChoices

    return ImportJobRunner.get_jobs().filter(
        user=user,
        data__job_type=ImportJobRunner.job_type,
        data__keeps_preview=True,
        data__profile_id=profile_id,
        data__source_document_id=document_id,
        status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
    )


@system_job(interval=60 * 24)
class SourceDocumentRetentionJob(JobRunner):
    """Reclaim stored uploads no Import Execution references (section 9.1), and expire old previews."""

    class Meta:
        name = "Data Import source document retention"

    @staticmethod
    def purge() -> int:
        """Apply the retention rules and return the number of deleted documents."""
        return SourceDocument.purge_unreferenced()

    def run(self, *args, **kwargs):
        """Run one retention pass; a preview expires with the 30 days its Source Document is kept."""
        from .preview_coordinator import expire_previews

        branching.fail_job_in_branch(self)
        expire_previews()
        return self.purge()


class ResolutionProposalJob(JobRunner):
    """Run inference for one proposal id through the worker service."""

    job_type = "netbox_data_import.resolution_proposal"

    class Meta:
        name = "Resolution Proposal"

    def run(self, proposal_id):
        """Resolve backend configuration on the worker after claiming the proposal."""
        from .proposal_jobs import run_proposal
        from .resolution_proposals import fail_proposal

        try:
            branching.fail_job_in_branch(self)
        except JobFailed:
            # The row is main-only, so this write lands in main and frees the field for a new request.
            fail_proposal(proposal_id, reason=ProposalFailureReason.BRANCH_ACTIVE)
            raise
        return run_proposal(proposal_id)


__all__ = (
    "ImportJobRunner",
    "ResolutionProposalJob",
    "SourceDocumentRetentionJob",
)

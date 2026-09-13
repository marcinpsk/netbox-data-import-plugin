# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Native NetBox background jobs for data imports."""

import logging

from typing import NoReturn

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from rq import get_current_job

from core.exceptions import JobFailed
from netbox.jobs import JobRunner, system_job

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
from .models import ExecutionOutcome, ImportExecution, ImportProfile, SourceDocument, validate_registered_adapter
from .netbox_reader import PlanningTargetUnavailable
from .object_permissions import ObjectPermissionDenied
from .plan import PlanError


_PROGRESS_REPORT_INTERVAL = 25
logger = logging.getLogger(__name__)


class ImportJobRunner(JobRunner):
    """Validate and execute one import while publishing row progress to RQ."""

    job_type = "netbox_data_import.import"

    class Meta:
        name = "Data Import"

    def _save_data(self, **values):
        """Merge values into the native Job data."""
        self.job.data = {**(self.job.data or {}), **values}
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

    def run(self, profile_id, source_document_id, accepted_plan, selection, idempotency_key):
        """Execute one accepted Import Plan as the Job's actor."""
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
        except (
            EngineConfigurationError,
            ObjectPermissionDenied,
            PlanError,
            PlanningTargetUnavailable,
            PreconditionFailed,
            SelectionError,
            SourceUnreadable,
            StalePlan,
            StaleSourceDocument,
            UnknownSourceAdapter,
            ValidationError,
        ) as exc:
            self._fail(operator_failure_message(exc))
        if execution.outcome != ExecutionOutcome.SUCCEEDED:
            reason = (execution.failure_detail or {}).get("reason") or execution.outcome or "unknown"
            self._fail(f"The accepted import execution did not succeed ({reason}).")
        self._save_data(
            phase="completed",
            processed=progress["processed"],
            total=progress["total"],
            import_execution_id=execution.pk,
        )


@system_job(interval=60 * 24)
class SourceDocumentRetentionJob(JobRunner):
    """Reclaim stored uploads no Import Execution references (section 9.1)."""

    class Meta:
        name = "Data Import source document retention"

    @staticmethod
    def purge() -> int:
        """Apply the retention rules and return the number of deleted documents."""
        return SourceDocument.purge_unreferenced()

    def run(self, *args, **kwargs):
        """Run one retention pass."""
        return self.purge()


class InferenceBackendConnectionTestJob(JobRunner):
    """Resolve the named backend's credential on the worker, so the web process never holds one.

    The row ID selects the backend to test, enabled or not: an operator tests a backend to decide whether to
    enable it (specification 8.6).
    """

    job_type = "netbox_data_import.inference_connection_test"

    class Meta:
        name = "AI backend connection test"

    def run(self, pk, backend_key, *args, **kwargs):
        """Select by pk alone to prevent redirection to a recreated row; backend_key is display text only."""
        from .inference_connection_test import run_connection_test

        result = run_connection_test(pk, backend_key)
        self.job.data = {**(self.job.data or {}), **result.as_dict()}
        self.job.save(update_fields=["data"])
        return result.category


class ResolutionProposalJob(JobRunner):
    """Run inference for one proposal id through the worker service."""

    job_type = "netbox_data_import.resolution_proposal"

    class Meta:
        name = "Resolution Proposal"

    def run(self, proposal_id):
        """Resolve backend configuration on the worker after claiming the proposal."""
        from .proposal_jobs import run_proposal

        return run_proposal(proposal_id)


__all__ = (
    "ImportJobRunner",
    "InferenceBackendConnectionTestJob",
    "ResolutionProposalJob",
    "SourceDocumentRetentionJob",
)

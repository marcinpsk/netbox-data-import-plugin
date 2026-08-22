# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Native NetBox background jobs for data imports."""

from django.core.exceptions import ValidationError
from django.db import transaction
from rq import get_current_job

from core.exceptions import JobFailed
from netbox.jobs import JobRunner

from . import engine
from .models import ImportJob, ImportProfile, validate_registered_adapter


_PROGRESS_REPORT_INTERVAL = 25


class ImportJobRunner(JobRunner):
    """Validate and execute one import while publishing row progress to RQ."""

    job_type = "netbox_data_import.import"

    class Meta:
        name = "Data Import"

    def _save_data(self, **values):
        """Merge values into the native Job data."""
        self.job.data = {**(self.job.data or {}), **values}
        self.job.save(update_fields=["data"])

    def _fail(self, message, preview_result=None, context_data=None):
        """Record a recoverable failure and stop the native Job."""
        values = {"phase": "failed", "message": message}
        if preview_result is not None:
            values.update(
                preview_result=preview_result.to_session_dict(),
                context_data=context_data,
            )
        self._save_data(**values)
        raise JobFailed()

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

    def run(self, rows, context_data, stored_preview):
        """
        Validate and execute a queued data import after confirming its preview is unchanged.
        
        Parameters:
            rows: Import rows to process.
            context_data: Saved import context, including profile, target, and filename details.
            stored_preview: Preview generated before the job was queued.
        """
        from dcim.models import Location, Site
        from tenancy.models import Tenant

        from .views import _import_intents, _previewed_writes_changed

        user = self.job.user
        if user is None:
            self._fail("The user who started this import is no longer available.")

        profile = ImportProfile.objects.restrict(user, "change").filter(pk=context_data["profile_id"]).first()
        if profile is None:
            self._fail("The import profile is no longer available.")
        # A queued job can outlive the release that registered its adapter.
        try:
            validate_registered_adapter(profile)
        except ValidationError as exc:
            self._fail("; ".join(exc.messages))
        site = Site.objects.filter(pk=context_data["site_id"]).first()
        if site is None:
            self._fail("The target site is no longer available.")
        location_id = context_data.get("location_id")
        location = Location.objects.filter(pk=location_id).first() if location_id else None
        if location_id and location is None:
            self._fail("The target location is no longer available.")
        tenant_id = context_data.get("tenant_id")
        tenant = Tenant.objects.filter(pk=tenant_id).first() if tenant_id else None
        if tenant_id and tenant is None:
            self._fail("The target tenant is no longer available.")
        context = {"site": site, "location": location, "tenant": tenant}
        rows = engine.reapply_saved_resolutions(rows, profile)

        self._save_data(phase="validating")
        current_preview = engine.run_import(rows, profile, context, dry_run=True, user=user)
        if _previewed_writes_changed(stored_preview, current_preview):
            self._fail(
                "The import preview changed. Review the refreshed preview before importing.",
                current_preview,
                context_data,
            )

        identity_changed = False
        with transaction.atomic():
            result = engine.run_import(
                rows,
                profile,
                context,
                dry_run=False,
                user=user,
                expected_intents=_import_intents(current_preview),
                progress_callback=self._publish_progress,
            )
            identity_changed = any(row.extra_data.get("identity_state_changed") for row in result.rows)
            if identity_changed:
                transaction.set_rollback(True)
        if identity_changed:
            self._fail(
                "NetBox identity changed during import. No changes were saved. Review the refreshed preview.",
                current_preview,
                context_data,
            )

        history = ImportJob.objects.create(
            profile=profile,
            input_filename=context_data.get("filename", ""),
            dry_run=False,
            site_name=site.name,
            result_counts=result.counts,
            result_rows=[row.to_dict() for row in result.rows],
        )
        self._save_data(
            phase="completed",
            processed=len(rows),
            total=len(rows),
            import_job_id=history.pk,
            result=result.to_session_dict(),
        )


__all__ = ("ImportJobRunner",)

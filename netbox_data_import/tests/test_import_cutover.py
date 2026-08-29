# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The HTTP import workflow uses the target-neutral Import Engine contract."""

from io import BytesIO
import uuid
from unittest.mock import patch

import openpyxl
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from django.urls import reverse

from core.exceptions import JobFailed
from core.models import Job

from netbox_data_import.jobs import ImportJobRunner
from netbox_data_import.import_engine import PreconditionFailed
from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    ExecutionOutcome,
    ImportExecution,
    ImportProfile,
    SourceDocument,
)
from netbox_data_import.plan import ImportPlan
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin


def _workbook() -> bytes:
    """Return one small flat workbook for the HTTP boundary."""
    book = openpyxl.Workbook()
    sheet = book.active or book.create_sheet()
    sheet.title = "Data"
    sheet.append(["Source ID", "Class", "Name", "Rack", "Make", "Model"])
    sheet.append(["R-1", "Cabinet", "", "rack-a", "", ""])
    sheet.append(["D-1", "Server", "server-a", "rack-a", "Example", "Model"])
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class ImportCutoverHttpTest(IsolatedRQQueueTestMixin, TransactionTestCase):
    """Upload, review, and execution share one stored source and serialized plan."""

    def setUp(self):
        """Create the actor, profile, mappings, and import target."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        self.actor = get_user_model().objects.create_superuser(
            username="cutover-operator",
            email="cutover@example.invalid",
            password="testpass",
        )
        self.client = Client()
        self.client.force_login(self.actor)
        self.site = Site.objects.create(name="Cutover Site", slug="cutover-site")
        manufacturer = Manufacturer.objects.create(name="Example", slug="example")
        DeviceType.objects.create(manufacturer=manufacturer, model="Model", slug="example-model", u_height=1)
        DeviceRole.objects.create(name="Server", slug="server")
        self.profile = ImportProfile.objects.create(
            name="Cutover Profile",
            adapter_config={"sheet_name": "Data", "update_existing": True},
        )
        for source_column, target_field in (
            ("Source ID", "source_id"),
            ("Class", "device_class"),
            ("Name", "device_name"),
            ("Rack", "rack_name"),
            ("Make", "make"),
            ("Model", "model"),
        ):
            ColumnMapping.objects.create(
                profile=self.profile,
                source_column=source_column,
                target_field=target_field,
            )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug="server")

    def _upload(self):
        """Upload the standard workbook and return the setup response."""
        upload = SimpleUploadedFile(
            "cutover.xlsx",
            _workbook(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
        )

    def _job(self, *, status="pending", data=None, user=True, queue_name="default"):
        """Create one native data-import Job owned by this actor by default."""
        return Job.objects.create(
            name="Data Import",
            user=self.actor if user is True else user,
            status=status,
            job_id=uuid.uuid4(),
            queue_name=queue_name,
            data={"job_type": ImportJobRunner.job_type, **(data or {})},
        )

    def test_upload_stores_the_source_and_serialized_plan(self):
        """The session references audit input and a schema-versioned plan, never result rows."""
        response = self._upload()

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        document = SourceDocument.objects.get(profile=self.profile)
        session = self.client.session
        self.assertEqual(session["import_context"]["source_document_id"], document.pk)
        plan = ImportPlan.from_dict(session["import_plan"])
        self.assertEqual(plan.source_fingerprint, document.content_fingerprint)
        self.assertEqual(plan.actor, str(self.actor.pk))
        self.assertEqual(plan.planning_context["site_id"], self.site.pk)
        self.assertNotIn("import_result", session)

        preview = self.client.get(response["Location"])

        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "rack-a")
        self.assertContains(preview, "server-a")

    def test_final_execution_uses_the_accepted_plan_and_links_its_job(self):
        """The queued writer executes selected units and leaves one complete audit record."""
        from core.models import Job
        from dcim.models import Device, Rack

        self._upload()

        response = self.client.post(reverse("plugins:netbox_data_import:import_run"))

        job = Job.objects.get(data__job_type="netbox_data_import.import")
        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}),
            fetch_redirect_response=False,
        )
        self.run_rq_jobs()

        self.assertTrue(Rack.objects.filter(site=self.site, name="rack-a").exists())
        self.assertTrue(Device.objects.filter(site=self.site, name="server-a").exists())
        execution = ImportExecution.objects.get(profile=self.profile)
        self.assertEqual(execution.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertEqual(execution.actor, self.actor)
        self.assertEqual(execution.source_document.profile, self.profile)
        self.assertEqual(execution.job, job)
        self.assertTrue(execution.selected_units)
        self.assertNotIn("rows", job.data)
        self.assertNotIn("stored_preview", job.data)

        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "completed")
        self.assertEqual(job.data["processed"], job.data["total"])
        self.assertGreater(job.data["total"], len(execution.selected_units))

        status = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(status.status_code, 204)
        self.assertEqual(status.headers["HX-Redirect"], reverse("plugins:netbox_data_import:import_results"))

        results = self.client.get(reverse("plugins:netbox_data_import:import_results"))
        self.assertContains(results, "Import Complete")
        self.assertContains(results, "cutover.xlsx")

    def test_run_requires_an_active_clean_preview(self):
        """Missing, submitted, and dirty preview states never enqueue another Job."""
        run_url = reverse("plugins:netbox_data_import:import_run")

        response = self.client.post(run_url)
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

        self._upload()
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        response = self.client.post(run_url)
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

        job = self._job()
        session = self.client.session
        session["import_background_job_id"] = job.pk
        session.save()
        response = self.client.post(run_url)
        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}),
        )

        session = self.client.session
        session["import_preview_pending"] = True
        session["import_preview_dirty"] = True
        session.save()
        response = self.client.post(run_url)
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))

    def test_run_refuses_a_missing_source_and_a_corrupt_plan(self):
        """A queued write always refers to readable source bytes and a valid plan schema."""
        run_url = reverse("plugins:netbox_data_import:import_run")
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        document.delete()

        response = self.client.post(run_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertFalse(self.client.session["import_preview_pending"])

        self._upload()
        session = self.client.session
        session["import_plan"]["schema_version"] = 999
        session.save()

        response = self.client.post(run_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_run_refuses_plan_errors_and_a_plan_with_no_changes(self):
        """The final action requires an error-free selection with at least one write."""
        run_url = reverse("plugins:netbox_data_import:import_run")
        self._upload()
        session = self.client.session
        first = session["import_plan"]["units"][0]
        first["disposition"] = "invalid"
        first["changes"] = []
        first["diagnostics"] = [
            {"code": "rack.example", "severity": "error", "identities": [first["identity"]], "display": {}}
        ]
        session.save()

        response = self.client.post(run_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertFalse(Job.objects.filter(data__job_type=ImportJobRunner.job_type).exists())

        session = self.client.session
        for unit in session["import_plan"]["units"]:
            unit["disposition"] = "no-op"
            unit["changes"] = []
            unit["diagnostics"] = []
        session.save()

        response = self.client.post(run_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertFalse(Job.objects.filter(data__job_type=ImportJobRunner.job_type).exists())

    def test_progress_reads_live_rq_metadata_and_survives_a_removed_queue(self):
        """Polling uses uncommitted worker progress and falls back to native Job data."""
        from django_rq import get_queue

        self._upload()
        self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(data__job_type=ImportJobRunner.job_type)
        rq_job = get_queue(job.queue_name).fetch_job(str(job.job_id))
        rq_job.meta.update({"processed": 1, "total": 4, "phase": "importing"})
        rq_job.save_meta()

        status = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertContains(status, "Completed 1 of 4 plan steps")
        self.assertContains(status, 'aria-valuenow="25"')

        removed = self._job(
            data={"processed": 3, "total": 8},
            queue_name="removed-cutover-queue",
        )
        progress = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": removed.pk}))
        self.assertContains(progress, "Completed 3 of 8 plan steps")

    def test_failed_job_restores_its_plan_without_replacing_a_newer_preview(self):
        """A failed accepted plan is resumable only when another preview is not pending."""
        self._upload()
        accepted_plan = self.client.session["import_plan"]
        context_data = self.client.session["import_context"]
        document_id = context_data["source_document_id"]
        failed = self._job(
            status="failed",
            data={
                "accepted_plan": accepted_plan,
                "context_data": context_data,
                "source_document_id": document_id,
                "message": "The accepted plan changed.",
            },
        )

        blocked = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": failed.pk}))
        self.assertContains(blocked, "Finish the current preview before reviewing this failed import.")
        self.assertNotEqual(self.client.session.get("import_preview_source_job_id"), failed.pk)

        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        restored = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": failed.pk}))

        self.assertContains(restored, "Review preview")
        self.assertEqual(self.client.session["import_preview_source_job_id"], failed.pk)
        self.assertEqual(self.client.session["import_plan"], accepted_plan)

    def test_progress_restores_an_execution_beside_a_newer_preview(self):
        """An older result remains available without destroying an unsubmitted preview."""
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        execution = ImportExecution.objects.create(
            profile=self.profile,
            source_document=document,
            actor=self.actor,
            outcome=ExecutionOutcome.FAILED,
        )
        completed = self._job(status="completed", data={"import_execution_id": execution.pk})

        self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": completed.pk}))

        self.assertEqual(self.client.session["import_restored_execution_id"], execution.pk)
        results = self.client.get(reverse("plugins:netbox_data_import:import_results"))
        self.assertEqual(results.context["execution"], execution)

    def test_results_redirect_for_missing_or_foreign_execution(self):
        """A session cannot expose an absent audit row or another actor's result."""
        results_url = reverse("plugins:netbox_data_import:import_results")
        response = self.client.get(results_url)
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        other = get_user_model().objects.create_superuser(
            username="cutover-other",
            email="cutover-other@example.invalid",
            password="testpass",
        )
        execution = ImportExecution.objects.create(
            profile=self.profile,
            source_document=document,
            actor=other,
            outcome=ExecutionOutcome.FAILED,
        )
        session = self.client.session
        session["import_execution_id"] = execution.pk
        session.save()

        response = self.client.get(results_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

    def test_progress_is_owned_by_the_actor_and_rejects_unrelated_jobs(self):
        """Progress routes expose only this runner's Jobs owned by the current actor."""
        other = get_user_model().objects.create_superuser(
            username="cutover-progress-other",
            email="cutover-progress-other@example.invalid",
            password="testpass",
        )
        foreign = self._job(user=other)
        unrelated = Job.objects.create(
            name="Data Import",
            user=self.actor,
            status="pending",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={},
        )
        for job in (foreign, unrelated):
            response = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}))
            self.assertEqual(response.status_code, 404)

    def test_job_runner_reports_missing_dependencies_and_invalid_payloads(self):
        """Recoverable worker input failures finish the native Job with an operator message."""
        no_user = self._job(user=None)
        with self.assertRaises(JobFailed):
            ImportJobRunner(no_user).run(self.profile.pk, 1, {}, ["device:1"], "missing-user")
        no_user.refresh_from_db()
        self.assertIn("user", no_user.data["message"].lower())

        missing_profile = self._job()
        with self.assertRaises(JobFailed):
            ImportJobRunner(missing_profile).run(999999, 1, {}, ["device:1"], "missing-profile")
        missing_profile.refresh_from_db()
        self.assertIn("profile", missing_profile.data["message"].lower())

        missing_source = self._job()
        with self.assertRaises(JobFailed):
            ImportJobRunner(missing_source).run(self.profile.pk, 999999, {}, ["device:1"], "missing-source")
        missing_source.refresh_from_db()
        self.assertIn("stored source", missing_source.data["message"].lower())

        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        corrupt = self._job()
        with self.assertRaises(JobFailed):
            ImportJobRunner(corrupt).run(self.profile.pk, document.pk, {}, ["device:1"], "corrupt-plan")
        corrupt.refresh_from_db()
        self.assertEqual(corrupt.data["phase"], "failed")

        accepted = ImportPlan.from_dict(self.client.session["import_plan"])
        selected = accepted.units[0].identity
        retry_job = self._job()
        failed, created = ImportExecution.reserve(
            profile=self.profile,
            source_document=document,
            actor=self.actor,
            idempotency_key="failed-retry",
            plan_schema_version=accepted.schema_version,
            accepted_plan_fingerprint=accepted.fingerprint,
            selected_units=[selected],
        )
        self.assertTrue(created)
        failed.link_job(retry_job).mark_failed(reason="planning")

        with self.assertRaises(JobFailed):
            ImportJobRunner(retry_job).run(
                self.profile.pk,
                document.pk,
                accepted.to_dict(),
                [selected],
                "failed-retry",
            )
        retry_job.refresh_from_db()
        self.assertEqual(retry_job.data["import_execution_id"], failed.pk)
        self.assertIn("already failed", retry_job.data["message"])

    def test_progress_publication_is_throttled_and_tolerates_no_rq_context(self):
        """Large imports bound Redis writes, and synchronous calls have no RQ metadata."""

        class ProgressJob:
            def __init__(self):
                self.meta = {}
                self.saved = []

            def save_meta(self):
                self.saved.append(self.meta["processed"])

        progress_job = ProgressJob()
        with patch("netbox_data_import.jobs.get_current_job", return_value=progress_job):
            for processed in range(31):
                ImportJobRunner._publish_progress(processed, 30)
        self.assertEqual(progress_job.saved, [0, 25, 30])

        with patch("netbox_data_import.jobs.get_current_job", return_value=None):
            ImportJobRunner._publish_progress(0, 1)

    def test_single_row_sync_rejects_invalid_session_and_row_inputs(self):
        """Inline execution requires a readable plan, profile, source, and create unit."""
        sync_url = reverse("plugins:netbox_data_import:sync_single_row")
        self.assertEqual(self.client.post(sync_url, {"row_number": 2}).status_code, 400)

        self._upload()
        self.assertEqual(self.client.post(sync_url, {}).status_code, 400)
        self.assertEqual(self.client.post(sync_url, {"row_number": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post(sync_url, {"row_number": 999}).status_code, 400)

        session = self.client.session
        session["import_context"]["profile_id"] = 999999
        session.save()
        self.assertEqual(self.client.post(sync_url, {"row_number": 2}).status_code, 400)

        self._upload()
        session = self.client.session
        session["import_plan"]["schema_version"] = 999
        session.save()
        self.assertEqual(self.client.post(sync_url, {"row_number": 2}).status_code, 409)

        self._upload()
        SourceDocument.objects.get(pk=self.client.session["import_context"]["source_document_id"]).delete()
        self.assertEqual(self.client.post(sync_url, {"row_number": 2}).status_code, 400)

    def test_single_row_sync_classifies_expected_and_unexpected_engine_failures(self):
        """Inline execution exposes bounded errors from every coordinator failure class."""
        sync_url = reverse("plugins:netbox_data_import:sync_single_row")
        for failure, status in (
            (PreconditionFailed("changed"), 409),
            (ValidationError("invalid"), 400),
            (RuntimeError("unexpected"), 500),
        ):
            with self.subTest(failure=type(failure).__name__):
                self._upload()
                with patch("netbox_data_import.views.ImportEngine.execute", side_effect=failure):
                    response = self.client.post(sync_url, {"row_number": 2})
                self.assertEqual(response.status_code, status)

    def test_single_row_sync_refuses_a_unit_that_is_no_longer_a_create(self):
        """After one inline create, the same row cannot be submitted as another create."""
        sync_url = reverse("plugins:netbox_data_import:sync_single_row")
        self._upload()

        first = self.client.post(sync_url, {"row_number": 2})
        second = self.client.post(sync_url, {"row_number": 2})

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 400)
        self.assertIn("Only 'create' rows", second.json()["error"])

    def test_preview_discards_a_missing_source_and_a_corrupt_materialized_plan(self):
        """Session state cannot keep a preview whose stored input or plan schema is unreadable."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")
        self._upload()
        SourceDocument.objects.get(pk=self.client.session["import_context"]["source_document_id"]).delete()
        response = self.client.get(preview_url)
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

        self._upload()
        session = self.client.session
        session["import_plan"]["schema_version"] = 999
        session["import_preview_use_materialized_once"] = True
        session.save()
        response = self.client.get(preview_url)
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

    def test_preview_discards_a_target_that_became_unavailable(self):
        """Recalculation refuses the preview after its saved target disappears from scope."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")
        self._upload()
        self.client.get(preview_url)
        session = self.client.session
        session["import_context"]["site_id"] = 999999
        session.save()

        response = self.client.get(preview_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

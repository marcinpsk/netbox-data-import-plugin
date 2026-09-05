# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The HTTP import workflow uses the target-neutral Import Engine contract."""

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import Client, SimpleTestCase, TransactionTestCase
from django.urls import reverse

from core.exceptions import JobFailed
from core.models import Job

from netbox_data_import.import_engine import operator_failure_message
from netbox_data_import.jobs import ImportJobRunner
from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceTypeMapping,
    ExecutionOutcome,
    FailureReason,
    ImportExecution,
    ImportProfile,
    SourceDocument,
)
from netbox_data_import.plan import ImportPlan
from netbox_data_import.preview_row_actions import (
    PREVIEW_PLAN_SESSION_KEY,
    PREVIEW_REVISION_SESSION_KEY,
    PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY,
    retire_preview_revision,
)
from netbox_data_import.tests.helpers import run_on_separate_connection, user_with_object_permission, workbook_bytes
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin


def _workbook() -> bytes:
    """Return one small flat workbook for the HTTP boundary."""
    return workbook_bytes(
        ["Source ID", "Class", "Name", "Rack", "Make", "Model"],
        [
            ["R-1", "Cabinet", "", "rack-a", "", ""],
            ["D-1", "Server", "server-a", "rack-a", "Example", "Model"],
        ],
    )


class ImportJobRunnerMessageTest(SimpleTestCase):
    """Render worker failures without exposing Django exception internals."""

    def test_validation_messages_are_joined_for_the_operator(self):
        error = ValidationError(["First validation failure.", "Second validation failure."])

        self.assertEqual(
            operator_failure_message(error),
            "First validation failure.; Second validation failure.",
        )

    def test_database_details_are_not_shown_to_the_operator(self):
        """A database failure keeps statement and constraint details out of the Job record."""
        error = DatabaseError("duplicate key value violates constraint private_constraint")

        self.assertEqual(
            operator_failure_message(error),
            "The import could not be written. Check the NetBox logs and try again.",
        )


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

    def _sync_single_row(self, data=None):
        """Post an inline execution with the active preview revision when one exists."""
        payload = dict(data or {})
        if revision := self.client.session.get(PREVIEW_REVISION_SESSION_KEY):
            payload.setdefault("preview_revision", revision)
        return self.client.post(reverse("plugins:netbox_data_import:sync_single_row"), payload)

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
        plan = ImportPlan.from_dict(session[PREVIEW_PLAN_SESSION_KEY])
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
        session[PREVIEW_PLAN_SESSION_KEY]["schema_version"] = 999
        session.save()

        response = self.client.post(run_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_run_refuses_plan_errors_and_a_plan_with_no_changes(self):
        """The final action requires an error-free selection with at least one write."""
        run_url = reverse("plugins:netbox_data_import:import_run")
        self._upload()
        session = self.client.session
        first = session[PREVIEW_PLAN_SESSION_KEY]["units"][0]
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
        for unit in session[PREVIEW_PLAN_SESSION_KEY]["units"]:
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
        accepted_plan = self.client.session[PREVIEW_PLAN_SESSION_KEY]
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
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], accepted_plan)

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

    def test_results_accept_the_execution_view_permission(self):
        """The audit result has its own permission boundary, independent of profile access."""
        self._upload()
        actor = user_with_object_permission(
            "cutover-execution-viewer",
            [(ImportExecution, ("view",), None)],
        )
        execution = ImportExecution.objects.create(
            profile=self.profile,
            source_document=SourceDocument.objects.get(profile=self.profile),
            actor=actor,
            outcome=ExecutionOutcome.FAILED,
        )
        client = Client()
        client.force_login(actor)
        session = client.session
        session["import_execution_id"] = execution.pk
        session.save()

        response = client.get(reverse("plugins:netbox_data_import:import_results"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["execution"], execution)

    def test_results_reject_the_profile_view_permission(self):
        """Profile visibility alone must not expose an Import Execution audit record."""
        self._upload()
        actor = user_with_object_permission(
            "cutover-profile-viewer",
            [(ImportProfile, ("view",), None)],
        )
        execution = ImportExecution.objects.create(
            profile=self.profile,
            source_document=SourceDocument.objects.get(profile=self.profile),
            actor=actor,
            outcome=ExecutionOutcome.FAILED,
        )
        client = Client()
        client.force_login(actor)
        session = client.session
        session["import_execution_id"] = execution.pk
        session.save()

        response = client.get(reverse("plugins:netbox_data_import:import_results"))

        self.assertIn(response.status_code, (302, 403))

    def test_results_apply_the_execution_object_constraint(self):
        """A model-level grant does not expose an execution outside its object constraint."""
        self._upload()
        actor = user_with_object_permission(
            "cutover-constrained-execution-viewer",
            [(ImportExecution, ("view",), {"outcome": ExecutionOutcome.SUCCEEDED})],
        )
        execution = ImportExecution.objects.create(
            profile=self.profile,
            source_document=SourceDocument.objects.get(profile=self.profile),
            actor=actor,
            outcome=ExecutionOutcome.FAILED,
        )
        client = Client()
        client.force_login(actor)
        session = client.session
        session["import_execution_id"] = execution.pk
        session.save()

        response = client.get(reverse("plugins:netbox_data_import:import_results"))

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_setup"),
            fetch_redirect_response=False,
        )

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

        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
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
        self.assertIn("planning", retry_job.data["message"])

    def test_job_runner_reports_a_pending_duplicate_as_failure(self):
        """A duplicate delivery cannot report a still-running execution as complete."""
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        selected = accepted.units[0].identity
        job = self._job()
        pending, created = ImportExecution.reserve(
            profile=self.profile,
            source_document=document,
            actor=self.actor,
            idempotency_key="pending-retry",
            plan_schema_version=accepted.schema_version,
            accepted_plan_fingerprint=accepted.fingerprint,
            selected_units=[selected],
        )
        self.assertTrue(created)
        pending.link_job(job)

        with self.assertRaises(JobFailed):
            ImportJobRunner(job).run(
                self.profile.pk,
                document.pk,
                accepted.to_dict(),
                [selected],
                "pending-retry",
            )

        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "failed")
        self.assertEqual(job.data["import_execution_id"], pending.pk)
        self.assertIn("pending", job.data["message"])

    def test_job_runner_reports_a_profile_deleted_before_the_policy_lock(self):
        """A profile removed after the worker reads it still becomes a recoverable Job failure."""
        from django.db import connection

        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        selected = accepted.units[0].identity
        job = self._job()
        deleted = []

        def delete_the_profile_when_the_lock_runs(execute, sql, params, many, context):
            if not deleted and "FOR UPDATE" in sql and ImportProfile._meta.db_table in sql:
                deleted.append(True)

                def delete_it():
                    ImportProfile.objects.get(pk=self.profile.pk).delete()

                # Finish the concurrent change before execute can take the profile row lock.
                with run_on_separate_connection(delete_it):
                    pass
            return execute(sql, params, many, context)

        with connection.execute_wrapper(delete_the_profile_when_the_lock_runs):
            with self.assertRaises(JobFailed):
                ImportJobRunner(job).run(
                    self.profile.pk,
                    document.pk,
                    accepted.to_dict(),
                    [selected],
                    "deleted-before-lock",
                )

        self.assertEqual(deleted, [True], "the policy lock was never reached")
        job.refresh_from_db()
        self.assertIn("profile", job.data["message"].lower())

    def test_job_runner_reports_an_adapter_retired_before_the_policy_lock(self):
        """A profile changed after worker validation still leaves an operator-facing Job failure."""
        from django.db import connection

        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        selected = accepted.units[0].identity
        job = self._job()
        retired = []

        def retire_the_adapter_when_the_lock_runs(execute, sql, params, many, context):
            if not retired and "FOR UPDATE" in sql and ImportProfile._meta.db_table in sql:
                retired.append(True)

                def retire_it():
                    ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired_adapter")

                # Finish the concurrent change before execute can take the profile row lock.
                with run_on_separate_connection(retire_it):
                    pass
            return execute(sql, params, many, context)

        with connection.execute_wrapper(retire_the_adapter_when_the_lock_runs):
            with self.assertRaises(JobFailed):
                ImportJobRunner(job).run(
                    self.profile.pk,
                    document.pk,
                    accepted.to_dict(),
                    [selected],
                    "retired-before-lock",
                )

        self.assertEqual(retired, [True], "the policy lock was never reached")
        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "failed")
        self.assertIn("retired_adapter", job.data["message"])

    def test_job_runner_reports_a_missing_engine_policy_section(self):
        """A release with an incomplete catalog leaves a failed Job instead of a validating one."""
        from netbox_data_import import catalog

        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        selected = accepted.units[0].identity
        job = self._job()
        section = catalog._SECTIONS_BY_KEY.pop("source_resolutions")
        self.addCleanup(catalog._SECTIONS_BY_KEY.__setitem__, "source_resolutions", section)

        with self.assertRaises(JobFailed):
            ImportJobRunner(job).run(
                self.profile.pk,
                document.pk,
                accepted.to_dict(),
                [selected],
                "missing-policy-section",
            )

        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "failed")
        self.assertIn("source_resolutions", job.data["message"])

    def test_job_runner_reports_source_policy_that_changed_before_the_lock(self):
        """A source that the locked policy can no longer read leaves an operator-facing failure."""
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        selected = accepted.units[0].identity
        job = self._job()
        ImportProfile.objects.filter(pk=self.profile.pk).update(
            adapter_config={**self.profile.adapter_config, "sheet_name": "Missing"}
        )

        with self.assertRaises(JobFailed):
            ImportJobRunner(job).run(
                self.profile.pk,
                document.pk,
                accepted.to_dict(),
                [selected],
                "source-policy-changed",
            )

        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "failed")
        self.assertIn("Missing", job.data["message"])

    def test_job_runner_keeps_the_execution_id_when_the_target_disappears(self):
        """A target failure after reservation still links the failed audit row to its Job."""
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        selected = accepted.units[0].identity
        job = self._job()
        self.site.delete()

        with self.assertRaises(JobFailed):
            ImportJobRunner(job).run(
                self.profile.pk,
                document.pk,
                accepted.to_dict(),
                [selected],
                "target-disappeared",
            )

        execution = ImportExecution.objects.get(idempotency_key="target-disappeared")
        job.refresh_from_db()
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(job.data["phase"], "failed")
        self.assertEqual(job.data["import_execution_id"], execution.pk)

    def test_job_runner_does_not_classify_an_unexpected_lookup_failure(self):
        """A programming defect remains visible instead of looking like an operator repair."""
        from netbox_data_import import target_modules
        from netbox_data_import.plan import Disposition, PlannedChange, SynchronizationUnit

        class BrokenDeviceRuntime:
            @staticmethod
            def plan(*args, **kwargs):
                return (
                    SynchronizationUnit(
                        identity="test:unexpected-lookup",
                        disposition=Disposition.ACTIONABLE,
                        changes=(
                            PlannedChange(
                                identity="test:unexpected-lookup:apply",
                                target_module="device",
                                operation="update",
                                payload={},
                            ),
                        ),
                    ),
                )

            @staticmethod
            def apply(change, execution_context):
                del execution_context
                return change.payload["missing"]

        runtime = target_modules.MODULE_RUNTIMES["device"]
        target_modules.MODULE_RUNTIMES["device"] = BrokenDeviceRuntime
        self.addCleanup(target_modules.MODULE_RUNTIMES.__setitem__, "device", runtime)
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        job = self._job()

        with self.assertRaises(KeyError):
            ImportJobRunner(job).run(
                self.profile.pk,
                document.pk,
                accepted.to_dict(),
                ["test:unexpected-lookup"],
                "unexpected-lookup",
            )

        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "validating")

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
        self.assertEqual(self._sync_single_row({"row_number": 2}).status_code, 400)

        self._upload()
        self.assertEqual(self._sync_single_row().status_code, 400)
        self.assertEqual(self._sync_single_row({"row_number": "invalid"}).status_code, 400)
        self.assertEqual(self._sync_single_row({"row_number": 999}).status_code, 400)

        session = self.client.session
        session["import_context"]["profile_id"] = 999999
        session.save()
        self.assertEqual(self._sync_single_row({"row_number": 2}).status_code, 400)

        self._upload()
        session = self.client.session
        session[PREVIEW_PLAN_SESSION_KEY]["schema_version"] = 999
        session.save()
        self.assertEqual(self._sync_single_row({"row_number": 2}).status_code, 409)

        self._upload()
        SourceDocument.objects.get(pk=self.client.session["import_context"]["source_document_id"]).delete()
        self.assertEqual(self._sync_single_row({"row_number": 2}).status_code, 400)

    def test_single_row_sync_does_not_echo_a_database_error(self):
        """A database failure names no SQL to the operator and leaves its traceback in the log."""
        from dcim.models import Rack
        from django.db.models.signals import post_save

        self._upload()
        constraint = 'duplicate key value violates unique constraint "dcim_rack_name_site_id"'

        def refuse_rack(sender, instance, created, **kwargs):
            raise DatabaseError(constraint)

        post_save.connect(refuse_rack, sender=Rack, weak=False)
        self.addCleanup(post_save.disconnect, refuse_rack, sender=Rack)

        with self.assertLogs("netbox_data_import.views", level="ERROR"):
            response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(constraint, response.json()["error"])
        self.assertNotIn("dcim_rack", response.json()["error"])

    def test_single_row_sync_reports_a_refused_save_as_readable_text(self):
        """A NetBox validator's reason reads as its own text, not as the repr of a list."""
        from dcim.models import Rack
        from django.db.models.signals import pre_save

        self._upload()

        def refuse_rack(sender, instance, **kwargs):
            raise ValidationError("A NetBox validator refused this rack.")

        pre_save.connect(refuse_rack, sender=Rack, weak=False)
        self.addCleanup(pre_save.disconnect, refuse_rack, sender=Rack)

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "A NetBox validator refused this rack.")

    def test_single_row_sync_rejects_a_queued_or_dirty_preview(self):
        """Inline execution cannot use a plan after import starts or a review changes it."""
        from dcim.models import Rack

        for session_state in (
            {"import_preview_pending": False},
            {"import_preview_pending": True, "import_preview_dirty": True},
        ):
            with self.subTest(session_state=session_state):
                self._upload()
                session = self.client.session
                session.update(session_state)
                session.save()

                response = self._sync_single_row({"row_number": 2})

                self.assertEqual(response.status_code, 409)
                self.assertFalse(Rack.objects.filter(site=self.site, name="rack-a").exists())

        self._upload()
        session = self.client.session
        previous_revision = session[PREVIEW_REVISION_SESSION_KEY]
        retire_preview_revision(session)
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_single_row"),
            {"row_number": 2, "preview_revision": previous_revision},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(Rack.objects.filter(site=self.site, name="rack-a").exists())

    def test_single_row_sync_classifies_an_unexpected_engine_failure(self):
        """Inline execution returns a bounded response for an unexpected coordinator failure."""
        from netbox_data_import import target_modules

        self._upload()

        runtime = target_modules.MODULE_RUNTIMES["rack"]

        class BrokenRackRuntime:
            @staticmethod
            def plan(*args, **kwargs):
                return runtime.plan(*args, **kwargs)

            @staticmethod
            def apply(*args):
                raise RuntimeError("unexpected")

        target_modules.MODULE_RUNTIMES["rack"] = BrokenRackRuntime()
        self.addCleanup(target_modules.MODULE_RUNTIMES.__setitem__, "rack", runtime)

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 500)

    def test_single_row_sync_names_the_object_it_wrote(self):
        """The modal closes on success, so the page needs the write named to keep it on screen."""
        self._upload()

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 200, response.content)
        detail = response.json()["detail"]
        self.assertIn("rack-a", detail)
        self.assertIn("created", detail.lower())

    def test_single_row_sync_reports_real_stale_target_state(self):
        """A Rack that appears after planning invalidates the accepted unit."""
        from dcim.models import Rack

        self._upload()
        Rack.objects.create(site=self.site, name="rack-a")

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            ImportExecution.objects.latest("pk").failure_detail["reason"],
            FailureReason.STALE_PLAN,
        )

    def test_single_row_sync_reports_a_real_object_permission_failure(self):
        """A saved Rack outside the actor's object constraint returns a bounded 400."""
        from dcim.models import Rack, Site

        actor = user_with_object_permission(
            "cutover-restricted-writer",
            [
                (ImportProfile, ("change",), {"pk": self.profile.pk}),
                (Site, ("view",), {"pk": self.site.pk}),
                (Rack, ("add",), {"name": "allowed-rack"}),
            ],
        )
        self.client.force_login(actor)
        upload = self._upload()
        self.assertRedirects(
            upload,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Rack.objects.filter(site=self.site, name="rack-a").exists())
        self.assertEqual(
            ImportExecution.objects.latest("pk").failure_detail["reason"],
            FailureReason.PERMISSION,
        )

    def test_single_row_sync_marks_the_materialized_preview_stale(self):
        """A selective execution returns immediately and leaves recalculation to the operator."""
        from dcim.models import Rack

        self._upload()
        accepted_plan = self.client.session[PREVIEW_PLAN_SESSION_KEY]

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["preview_state"], "recalculation_required")
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], accepted_plan)
        self.assertTrue(self.client.session["import_preview_dirty"])
        self.assertTrue(Rack.objects.filter(site=self.site, name="rack-a").exists())

    def test_single_row_sync_refuses_a_second_sync_until_recalculation(self):
        """The first inline create makes the materialized preview too stale for another sync."""
        self._upload()

        first = self._sync_single_row({"row_number": 2})
        second = self._sync_single_row({"row_number": 2})

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 409)
        self.assertIn("Recalculate the preview", second.json()["error"])

    def test_device_type_mapping_leaves_the_materialized_preview_stale(self):
        """A quick mapping saves without rebuilding the active preview."""
        self._upload()
        accepted_plan = self.client.session[PREVIEW_PLAN_SESSION_KEY]

        response = self.client.post(
            reverse("plugins:netbox_data_import:quick_resolve_device_type"),
            {
                "profile_id": self.profile.pk,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                "source_make": "Source Make",
                "source_model": "Source Model",
                "netbox_mfg_slug": "example",
                "netbox_dt_slug": "example-model",
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["preview_state"], "recalculation_required")
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], accepted_plan)
        self.assertTrue(self.client.session["import_preview_dirty"])
        self.assertTrue(
            DeviceTypeMapping.objects.filter(
                profile=self.profile,
                source_make="Source Make",
                source_model="Source Model",
            ).exists()
        )

    def test_preview_discards_a_missing_source(self):
        """Session state cannot keep a preview whose stored input is unavailable."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")
        self._upload()
        SourceDocument.objects.get(pk=self.client.session["import_context"]["source_document_id"]).delete()
        response = self.client.get(preview_url)
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

    def test_preview_discards_malformed_candidate_values(self):
        """The renderer must reject malformed display data instead of raising an internal error."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")
        self._upload()
        session = self.client.session
        device_unit = next(
            unit
            for unit in session[PREVIEW_PLAN_SESSION_KEY]["units"]
            if unit["display"].get("object_type") == "device"
        )
        device_unit["display"].setdefault("extra_data", {})["candidate_values"] = ["invalid"]
        session[PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY] = True
        session.save()

        response = self.client.get(preview_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertNotIn(PREVIEW_PLAN_SESSION_KEY, self.client.session)

    def test_preview_discards_malformed_contact_candidate_values(self):
        """Contact suggestions require a source-column mapping, not any JSON value."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")
        self._upload()
        session = self.client.session
        device_unit = next(
            unit
            for unit in session[PREVIEW_PLAN_SESSION_KEY]["units"]
            if unit["display"].get("object_type") == "device"
        )
        device_unit["display"].setdefault("extra_data", {})["candidate_values"] = {"contact": ["invalid"]}
        session[PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY] = True
        session.save()

        response = self.client.get(preview_url)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertNotIn(PREVIEW_PLAN_SESSION_KEY, self.client.session)

    def test_preview_discards_a_materialized_plan_with_an_unknown_schema(self):
        """Session state cannot keep a materialized plan with an unreadable schema."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")
        self._upload()
        session = self.client.session
        session[PREVIEW_PLAN_SESSION_KEY]["schema_version"] = 999
        session[PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY] = True
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

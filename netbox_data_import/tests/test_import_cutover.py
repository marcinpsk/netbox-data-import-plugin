# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The HTTP import workflow uses the target-neutral Import Engine contract."""

import uuid

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
    DeviceExistingMatch,
    ColumnMapping,
    DeviceTypeMapping,
    ExecutionOutcome,
    FailureReason,
    ImportExecution,
    ImportProfile,
    PreviewCoordinator,
    PreviewState,
    SourceDocument,
)
from netbox_data_import.plan import ImportPlan
from netbox_data_import.preview_coordinator import SUBMITTED_PREVIEW, UNREADABLE_PREVIEW
from netbox_data_import.tests.helpers import (
    preview_claim,
    preview_coordinator,
    queued_webhooks,
    recorded_updates,
    run_on_separate_connection,
    store_plan,
    stored_plan,
    update_webhook_rule,
    upload_preview,
    user_with_object_permission,
    workbook_bytes,
)
from netbox_data_import.views import NO_RACK_FILTER_VALUE
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

    def test_an_unexpected_failure_keeps_internal_details_private(self):
        """The public formatter refuses arbitrary exception text."""
        error = RuntimeError("Internal storage failure in private_table")

        self.assertEqual(
            operator_failure_message(error),
            "An unexpected error occurred. See server logs.",
        )

    def test_import_plan_details_are_not_shown_to_the_operator(self):
        """An Import Plan error can name source data, so a Job record states one fixed sentence."""
        from netbox_data_import.import_engine import UNREADABLE_PLAN
        from netbox_data_import.plan import PlanInvalid

        error = PlanInvalid("The Import Plan has no Synchronization Unit 'device:name:private-name'.")

        self.assertEqual(operator_failure_message(error), UNREADABLE_PLAN)

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
        return upload_preview(self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})

    def test_the_preview_states_what_a_row_sync_would_change(self):
        """The sync confirmation reads this blob, so the rendered preview has to carry it."""
        from dcim.models import Device, DeviceRole, DeviceType, Rack

        device = Device.objects.create(
            name="server-a",
            site=self.site,
            device_type=DeviceType.objects.get(slug="example-model"),
            role=DeviceRole.objects.get(slug="server"),
            rack=Rack.objects.create(name="rack-b", site=self.site, u_height=42),
            status="active",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="D-1",
            netbox_device_id=device.pk,
            device_name=device.name,
        )
        self._upload()

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="ndi-sync-change-preview-by-row"', response.content)
        previews = response.context["sync_change_preview_by_row"]
        # Row numbers repeat across object types, so a row-only key lets one row replace another.
        self.assertTrue(all(key.startswith(("device:", "rack:")) for key in previews), sorted(previews))
        entries = {entry["field"]: entry for row in previews.values() for entry in row}
        self.assertEqual(entries["rack_name"]["state"], "change")
        self.assertEqual(entries["rack_name"]["netbox"], "rack-b")
        self.assertEqual(entries["rack_name"]["file"], "rack-a")
        # The name NetBox already holds is reported, not dropped, so the write is not read as total.
        self.assertEqual(entries["device_name"]["state"], "unchanged")

    def test_the_preview_offers_the_racks_its_rows_name(self):
        """The flat view filters by rack, so the page carries the racks and each row's own rack."""
        self._upload()

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rack_filter_options"], [{"value": "rack-a", "label": "rack-a"}])
        self.assertIn(b'data-rack-name="rack-a"', response.content)
        self.assertIn(b'id="previewRackFilter"', response.content)

    def test_the_no_rack_option_cannot_collide_with_a_rack_of_that_name(self):
        """A rack may legally carry the sentinel's own name, so the sentinel has to move aside."""
        upload = SimpleUploadedFile(
            "collide.xlsx",
            workbook_bytes(
                ["Source ID", "Class", "Name", "Rack", "Make", "Model"],
                [
                    ["R-1", "Cabinet", "", NO_RACK_FILTER_VALUE, "", ""],
                    ["D-1", "Server", "server-a", "", "Example", "Model"],
                ],
            ),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        upload_preview(self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertEqual(response.status_code, 200)
        options = response.context["rack_filter_options"]
        named = [option["value"] for option in options if option["label"] != "(No rack)"]
        self.assertIn(NO_RACK_FILTER_VALUE, named, "the workbook has to name a rack after the sentinel")
        self.assertNotIn(response.context["no_rack_filter_value"], named)
        self.assertEqual(len(options), len({option["value"] for option in options}))

    def test_a_rack_row_carries_its_own_name_as_its_rack(self):
        """A Rack row filters with its own rack, so selecting that rack cannot hide it."""
        upload = SimpleUploadedFile(
            "named-rack.xlsx",
            workbook_bytes(
                ["Source ID", "Class", "Name", "Rack", "Make", "Model"],
                # The rack names itself through the Name column and leaves Rack empty.
                [["R-9", "Cabinet", "RACK-X", "", "", ""]],
            ),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        upload_preview(self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertEqual(response.status_code, 200)
        rack_row = next(row for row in response.context["preview_rows"] if row.object_type == "rack")
        self.assertEqual(rack_row.name, "RACK-X")
        self.assertEqual(rack_row.rack_name, "RACK-X")
        self.assertEqual(response.context["rack_filter_options"], [{"value": "RACK-X", "label": "RACK-X"}])
        self.assertIn(b'data-rack-name="RACK-X"', response.content)

    def _claim(self):
        """Return the claim of the session's preview, or no claim before the setup page made one."""
        try:
            return preview_claim(self.client)
        except PreviewCoordinator.DoesNotExist:
            return {}

    def _sync_single_row(self, data=None, claim=None):
        """Post an inline execution with the current claim, or with the given one."""
        payload = {**(self._claim() if claim is None else claim), **(data or {})}
        return self.client.post(reverse("plugins:netbox_data_import:sync_single_row"), payload)

    def _run(self, claim=None):
        """Post the final import with the current claim, or with the given one."""
        return self.client.post(
            reverse("plugins:netbox_data_import:import_run"), self._claim() if claim is None else claim
        )

    def _preview_action(self, row_number):
        """Return the action the current preview plans for one source row."""
        from netbox_data_import.review_workspace import ReviewWorkspace

        workspace = ReviewWorkspace(ImportPlan.from_dict(stored_plan(self.client)), self.actor)
        return next(unit.action for unit in workspace.units if unit.row_number == row_number)

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
        """The coordinator references audit input and a schema-versioned plan; the session holds neither."""
        response = self._upload()

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        document = SourceDocument.objects.get(profile=self.profile)
        coordinator = preview_coordinator(self.client)
        self.assertEqual((coordinator.state, coordinator.source_document_id), (PreviewState.READY, document.pk))
        plan = ImportPlan.from_dict(coordinator.plan)
        self.assertEqual(plan.source_fingerprint, document.content_fingerprint)
        self.assertEqual(plan.actor, str(self.actor.pk))
        self.assertEqual(plan.planning_context["site_id"], self.site.pk)
        self.assertEqual([key for key, _value in self.client.session.items() if key.startswith("import_")], [])

        preview = self.client.get(response["Location"])

        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "rack-a")
        self.assertContains(preview, "server-a")

    def test_final_execution_uses_the_accepted_plan_and_links_its_job(self):
        """The queued writer executes selected units and leaves one complete audit record."""
        from core.models import Job
        from dcim.models import Device, Rack

        self._upload()

        response = self._run()

        job = Job.objects.get(data__job_type="netbox_data_import.import")
        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}),
            fetch_redirect_response=False,
        )
        coordinator = preview_coordinator(self.client)
        self.assertEqual((coordinator.state, coordinator.job_id), (PreviewState.SUBMITTED, job.pk))
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
        results_url = reverse("plugins:netbox_data_import:import_results", kwargs={"pk": execution.pk})
        self.assertEqual(status.status_code, 204)
        self.assertEqual(status.headers["HX-Redirect"], results_url)

        results = self.client.get(results_url)
        self.assertContains(results, "Import Complete")
        self.assertContains(results, "cutover.xlsx")

    def _existing_server(self):
        """Store server-a with a Device Type the workbook row replaces, and return both types."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack

        rack = Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        other_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.get(slug="example"), model="Other", slug="example-other", u_height=1
        )
        existing = Device.objects.create(
            name="server-a",
            site=self.site,
            rack=rack,
            device_type=other_type,
            role=DeviceRole.objects.get(slug="server"),
        )
        return existing, other_type, DeviceType.objects.get(slug="example-model")

    def test_the_worker_records_updates_under_its_job_and_runs_their_event_rules(self):
        """The queued import writes ObjectChanges as its Job, and an event rule gets a request it can copy."""
        from dcim.models import Device
        from django_rq import get_queue

        existing, before, after = self._existing_server()
        update_webhook_rule(Device)
        self._upload()
        self._run()
        job = Job.objects.get(data__job_type=ImportJobRunner.job_type)

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "completed", job.error)
        (change,) = recorded_updates(existing)
        self.assertEqual((change.user, change.request_id), (self.actor, job.job_id))
        self.assertEqual(
            (change.prechange_data["device_type"], change.postchange_data["device_type"]), (before.pk, after.pk)
        )
        queue = get_queue("default")
        ran = [
            queue.fetch_job(job_id)
            for registry in (queue.finished_job_registry, queue.failed_job_registry)
            for job_id in registry.get_job_ids()
        ]
        (sent,) = [item for item in ran if item.func_name == "extras.webhooks.send_webhook"]
        self.assertEqual((sent.kwargs["request"].id, sent.kwargs["request"].user), (job.job_id, self.actor))

    def test_run_requires_an_active_unsubmitted_preview(self):
        """A missing, stale, or submitted preview never enqueues another Job."""
        run_jobs = Job.objects.filter(data__job_type=ImportJobRunner.job_type)

        self.assertEqual(self._run().status_code, 409)
        self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertEqual(self._run().status_code, 409, "the empty setup preview has nothing to import")
        self.assertFalse(run_jobs.exists())

        self._upload()
        reviewed = preview_claim(self.client)
        self.assertEqual(self._run(reviewed).status_code, 302)
        self.assertEqual(run_jobs.count(), 1)

        stale = self._run(reviewed)
        submitted = self._run()

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(submitted.status_code, 409)
        self.assertContains(submitted, SUBMITTED_PREVIEW, status_code=409)
        self.assertEqual(run_jobs.count(), 1)

    def test_run_refuses_a_missing_source_and_a_corrupt_plan(self):
        """A queued write always refers to readable source bytes and a valid plan schema."""
        run_jobs = Job.objects.filter(data__job_type=ImportJobRunner.job_type)
        self._upload()
        SourceDocument.objects.get(profile=self.profile).delete()

        response = self._run()

        self.assertRedirects(
            response, reverse("plugins:netbox_data_import:import_setup"), fetch_redirect_response=False
        )
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)

        self._upload()
        store_plan(self.client, {**stored_plan(self.client), "schema_version": 999})

        response = self._run()

        self.assertContains(response, UNREADABLE_PREVIEW, status_code=409)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)
        self.assertFalse(run_jobs.exists())

    def test_run_refuses_plan_errors_and_a_plan_with_no_changes(self):
        """The final action requires an error-free selection with at least one write."""
        self._upload()
        plan = stored_plan(self.client)
        first = plan["units"][0]
        first["disposition"] = "invalid"
        first["changes"] = []
        first["diagnostics"] = [
            {"code": "rack.example", "severity": "error", "identities": [first["identity"]], "display": {}}
        ]
        store_plan(self.client, plan)

        response = self._run()

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertFalse(Job.objects.filter(data__job_type=ImportJobRunner.job_type).exists())

        plan = stored_plan(self.client)
        for unit in plan["units"]:
            unit["disposition"] = "no-op"
            unit["changes"] = []
            unit["diagnostics"] = []
        store_plan(self.client, plan)

        response = self._run()

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_preview"))
        self.assertFalse(Job.objects.filter(data__job_type=ImportJobRunner.job_type).exists())

    def test_progress_reads_live_rq_metadata_and_survives_a_removed_queue(self):
        """Polling uses uncommitted worker progress and falls back to native Job data."""
        from django_rq import get_queue

        self._upload()
        self._run()
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

    def test_a_missing_task_offers_explicit_restore_without_get_mutations(self):
        from datetime import timedelta
        from core.choices import JobStatusChoices
        from django.utils import timezone
        from django_rq import get_queue

        self._upload()
        self._run()
        job = Job.objects.get(data__job_type=ImportJobRunner.job_type)
        queue = get_queue(job.queue_name)
        rq_job = queue.fetch_job(str(job.job_id))
        # Remove only the task hash. A mutating queue fetch would also remove its queue ID.
        queue.connection.delete(rq_job.key)
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - timedelta(minutes=2))
        queue_before = {key: queue.connection.dump(key) for key in queue.connection.scan_iter()}
        before = preview_coordinator(self.client)

        progress = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}))

        self.assertContains(progress, "Review preview")
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_PENDING)
        self.assertEqual({key: queue.connection.dump(key) for key in queue.connection.scan_iter()}, queue_before)
        self.assertEqual(preview_coordinator(self.client).revision, before.revision)
        restored = self.client.post(
            reverse("plugins:netbox_data_import:import_restore", kwargs={"pk": job.pk}), preview_claim(self.client)
        )
        self.assertEqual(restored.status_code, 302, restored.content)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_ERRORED)
        self.assertIsNotNone(job.completed)
        after = preview_coordinator(self.client)
        self.assertEqual((after.state, after.job_id), (PreviewState.READY, None))
        self.assertEqual(after.revision, before.revision + 1)

    def test_a_deleted_final_job_offers_a_coordinated_reread(self):
        self._upload()
        self._run()
        Job.objects.get(data__job_type=ImportJobRunner.job_type).delete()
        before = preview_coordinator(self.client)

        page = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertContains(page, "Re-read the preview")
        self.assertEqual(preview_coordinator(self.client).revision, before.revision)
        restored = self.client.post(reverse("plugins:netbox_data_import:preview_reread"), preview_claim(self.client))
        self.assertEqual(restored.status_code, 302, restored.content)
        after = preview_coordinator(self.client)
        self.assertEqual((after.state, after.job_id), (PreviewState.READY, None))
        self.assertEqual(after.revision, before.revision + 1)

    def _failed_final_import(self):
        """Upload, queue the final import, and fail its Job before a worker runs it."""
        self._upload()
        self._run()
        job = Job.objects.get(data__job_type=ImportJobRunner.job_type)
        Job.objects.filter(pk=job.pk).update(status="failed")
        return job

    def test_failed_job_restores_its_plan_through_the_restore_command(self):
        """The progress page only offers the return; the POST with the claim replans the stored source."""
        job = self._failed_final_import()
        accepted_plan = stored_plan(self.client)
        restore_url = reverse("plugins:netbox_data_import:import_restore", kwargs={"pk": job.pk})
        before = preview_coordinator(self.client)

        progress = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}))

        self.assertContains(progress, restore_url)
        self.assertContains(progress, "Review preview")
        unchanged = preview_coordinator(self.client)
        self.assertEqual((unchanged.revision, unchanged.state), (before.revision, PreviewState.SUBMITTED))

        restored = self.client.post(restore_url, preview_claim(self.client))

        self.assertRedirects(
            restored, reverse("plugins:netbox_data_import:import_preview"), fetch_redirect_response=False
        )
        coordinator = preview_coordinator(self.client)
        self.assertEqual((coordinator.state, coordinator.job_id), (PreviewState.READY, None))
        self.assertGreater(coordinator.revision, before.revision)
        self.assertEqual(coordinator.plan["source_fingerprint"], accepted_plan["source_fingerprint"])
        self.assertEqual(self.client.get(reverse("plugins:netbox_data_import:import_preview")).status_code, 200)

    def test_failed_job_cannot_replace_a_newer_preview(self):
        """A newer upload detaches the failed import, and its restore is refused without a write."""
        job = self._failed_final_import()
        self._upload()
        newer = preview_coordinator(self.client)

        progress = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}))
        refused = self.client.post(
            reverse("plugins:netbox_data_import:import_restore", kwargs={"pk": job.pk}), preview_claim(self.client)
        )

        self.assertContains(progress, "A newer preview replaced this import's preview.")
        self.assertNotContains(progress, "Review preview")
        self.assertEqual(refused.status_code, 409)
        current = preview_coordinator(self.client)
        self.assertEqual(
            (current.preview_token, current.revision, current.state),
            (newer.preview_token, newer.revision, PreviewState.READY),
        )

    def test_failed_job_with_a_deleted_source_does_not_report_a_replaced_preview(self):
        """A missing source prevents restoration without changing which Job owns the preview."""
        job = self._failed_final_import()
        SourceDocument.objects.filter(pk=job.data["source_document_id"]).delete()
        before = preview_coordinator(self.client)

        for route in ("import_progress", "import_progress_status"):
            with self.subTest(route=route):
                progress = self.client.get(reverse(f"plugins:netbox_data_import:{route}", kwargs={"pk": job.pk}))

                self.assertContains(progress, "The import failed.")
                self.assertContains(progress, "Start a new import")
                self.assertNotContains(progress, "A newer preview replaced this import's preview.")
                self.assertNotContains(progress, "Review preview")
                self.assertFalse(progress.context["preview_replaced"])
                self.assertIsNone(progress.context["restore_claim"])
        after = preview_coordinator(self.client)
        self.assertEqual((after.state, after.job_id), (PreviewState.SUBMITTED, job.pk))
        self.assertEqual(after.revision, before.revision)

    def test_restore_refuses_its_own_job_while_it_is_still_queued(self):
        """Matching ownership cannot restore a Job whose real queue task is still pending."""
        self._upload()
        self._run()
        job = Job.objects.get(data__job_type=ImportJobRunner.job_type)
        before = preview_coordinator(self.client)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:import_restore", kwargs={"pk": job.pk}), preview_claim(self.client)
        )

        self.assertContains(refused, "This preview does not belong to that failed import.", status_code=409)
        job.refresh_from_db()
        self.assertEqual(job.status, "pending")
        after = preview_coordinator(self.client)
        self.assertEqual((after.state, after.job_id), (PreviewState.SUBMITTED, job.pk))
        self.assertEqual(after.revision, before.revision)

    def test_progress_links_an_execution_beside_a_newer_preview(self):
        """An older result stays reachable from its Job without touching an unsubmitted preview."""
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        execution = ImportExecution.objects.create(
            profile=self.profile,
            source_document=document,
            actor=self.actor,
            outcome=ExecutionOutcome.FAILED,
        )
        completed = self._job(status="completed", data={"import_execution_id": execution.pk})
        before = preview_coordinator(self.client)
        results_url = reverse("plugins:netbox_data_import:import_results", kwargs={"pk": execution.pk})

        progress = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": completed.pk}))

        self.assertContains(progress, results_url)
        results = self.client.get(results_url)
        self.assertEqual(results.context["execution"], execution)
        after = preview_coordinator(self.client)
        self.assertEqual((after.revision, after.state), (before.revision, PreviewState.READY))

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

        response = client.get(reverse("plugins:netbox_data_import:import_results", kwargs={"pk": execution.pk}))

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

        response = client.get(reverse("plugins:netbox_data_import:import_results", kwargs={"pk": execution.pk}))

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

        response = client.get(reverse("plugins:netbox_data_import:import_results", kwargs={"pk": execution.pk}))

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_setup"),
            fetch_redirect_response=False,
        )

    def test_results_redirect_for_missing_or_foreign_execution(self):
        """The results page cannot expose an absent audit row or another actor's result."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_results", kwargs={"pk": 999999}))
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
        response = self.client.get(reverse("plugins:netbox_data_import:import_results", kwargs={"pk": execution.pk}))

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

        missing_profile = self._job(data={"accepted_plan": {"policy": {"cable_type": "mmf-om4"}}})
        with self.assertRaises(JobFailed):
            ImportJobRunner(missing_profile).run(999999, 1, {}, ["device:1"], "missing-profile")
        missing_profile.refresh_from_db()
        self.assertIn("profile", missing_profile.data["message"].lower())
        self.assertNotIn("accepted_plan", missing_profile.data)

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

        accepted = ImportPlan.from_dict(stored_plan(self.client))
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
        accepted = ImportPlan.from_dict(stored_plan(self.client))
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
        accepted = ImportPlan.from_dict(stored_plan(self.client))
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
        accepted = ImportPlan.from_dict(stored_plan(self.client))
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
        accepted = ImportPlan.from_dict(stored_plan(self.client))
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
        self.assertEqual(job.data["message"], "An unexpected error occurred. See server logs.")
        self.assertNotIn("source_resolutions", job.data["message"])

    def test_job_runner_reports_source_policy_that_changed_before_the_lock(self):
        """A source that the locked policy can no longer read leaves an operator-facing failure."""
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(stored_plan(self.client))
        selected = accepted.units[0].identity
        job = self._job()
        ImportProfile.objects.filter(pk=self.profile.pk).update(
            adapter_config={**self.profile.adapter_config, "sheet_name": "Missing"}
        )

        with self.assertLogs("netbox_data_import.jobs", level="ERROR") as captured, self.assertRaises(JobFailed):
            ImportJobRunner(job).run(
                self.profile.pk,
                document.pk,
                accepted.to_dict(),
                [selected],
                "source-policy-changed",
            )

        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "failed")
        self.assertEqual(job.data["message"], "The source file cannot be read. Check the file and the import profile.")
        self.assertNotIn("Missing", job.data["message"])
        self.assertTrue(any("Missing" in record for record in captured.output))

    def test_job_runner_keeps_the_execution_id_when_the_target_disappears(self):
        """A target failure after reservation still links the failed audit row to its Job."""
        self._upload()
        document = SourceDocument.objects.get(profile=self.profile)
        accepted = ImportPlan.from_dict(stored_plan(self.client))
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
        accepted = ImportPlan.from_dict(stored_plan(self.client))
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

    def test_single_row_sync_rejects_invalid_claim_and_row_inputs(self):
        """Inline execution requires a claim on a readable plan, its profile and source, and a create unit."""
        no_preview = self._sync_single_row({"row_number": 2})
        self.assertEqual((no_preview.status_code, no_preview.json()["code"]), (409, "preview_stale"))

        self._upload()
        self.assertEqual(self._sync_single_row().status_code, 400)
        self.assertEqual(self._sync_single_row({"row_number": "invalid"}).status_code, 400)
        self.assertEqual(self._sync_single_row({"row_number": 999}).status_code, 400)
        other_profile = {**preview_claim(self.client), "preview_profile": "999999"}
        self.assertEqual(self._sync_single_row({"row_number": 2}, claim=other_profile).status_code, 409)

        self._upload()
        store_plan(self.client, {**stored_plan(self.client), "schema_version": 999})
        self.assertEqual(self._sync_single_row({"row_number": 2}).status_code, 409)

        self._upload()
        SourceDocument.objects.get(pk=preview_coordinator(self.client).source_document_id).delete()
        self.assertEqual(self._sync_single_row({"row_number": 2}).status_code, 409)
        self.assertFalse(ImportExecution.objects.exists())

    def test_an_unreadable_stored_plan_answers_one_fixed_sentence(self):
        """Code scanning taints every caught exception, so no Import Plan error text reaches a response."""
        from netbox_data_import.plan import PlanError

        corruptions = (("wrong schema version", {"schema_version": 999}), ("malformed units", {"units": "not units"}))
        for label, changes in corruptions:
            for view in ("preview", "run", "sync"):
                with self.subTest(label=label, view=view):
                    self._upload()
                    store_plan(self.client, {**stored_plan(self.client), **changes})
                    with self.assertRaises(PlanError) as raised:
                        ImportPlan.from_dict(stored_plan(self.client))
                    detail = str(raised.exception)

                    if view == "sync":
                        response = self._sync_single_row({"row_number": 2})
                        self.assertEqual(response.status_code, 409)
                        self.assertEqual(
                            response.json(), {"ok": False, "error": UNREADABLE_PREVIEW, "code": "preview_stale"}
                        )
                    elif view == "preview":
                        # A page load stays read-only and offers the re-read that recovers the plan.
                        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
                        self.assertContains(response, reverse("plugins:netbox_data_import:preview_reread"))
                        self.assertContains(response, "Re-read it from its stored source.")
                    else:
                        response = self._run()
                        self.assertContains(response, UNREADABLE_PREVIEW, status_code=409)
                    self.assertNotIn(detail, response.content.decode())

    def test_single_row_sync_executes_an_update_row(self):
        """Per-row sync runs the same engine step 3 runs, for a row that updates a device."""
        from dcim.models import Device, DeviceRole, DeviceType, Rack

        from dcim.models import Manufacturer

        rack = Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        # The row names the Example/Model type, so a device of another type is a real update.
        other_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.get(slug="example"), model="Other", slug="example-other", u_height=1
        )
        expected_type = DeviceType.objects.get(slug="example-model")
        existing = Device.objects.create(
            name="server-a",
            site=self.site,
            rack=rack,
            device_type=other_type,
            role=DeviceRole.objects.get(slug="server"),
        )

        self._upload()
        self.assertEqual(self._preview_action(3), "update", "the fixture does not produce an update row")

        response = self._sync_single_row({"row_number": 3})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertIn(b"updated in NetBox", response.content)

        existing.refresh_from_db()
        self.assertEqual(existing.device_type_id, expected_type.pk, "the update row did not reach NetBox")
        (change,) = recorded_updates(existing)
        self.assertEqual(change.prechange_data["device_type"], other_type.pk)

    def test_a_refused_single_row_sync_sends_no_event(self):
        """The engine rolls the row back, so the event NetBox queued for it must not be sent."""
        from dcim.models import Device
        from django.db.models.signals import post_save

        self._existing_server()
        update_webhook_rule(Device)
        self._upload()

        def refuse(sender, instance, **kwargs):
            raise ValidationError("The Device write is refused after it ran.")

        post_save.connect(refuse, sender=Device, weak=False)
        try:
            refused = self._sync_single_row({"row_number": 3})
        finally:
            post_save.disconnect(refuse, sender=Device)

        self.assertEqual(refused.status_code, 400, refused.content[:400])
        self.assertEqual(queued_webhooks(), [], "an event was sent for a rolled-back write")
        committed = self._sync_single_row({"row_number": 3})
        self.assertEqual(committed.status_code, 200, committed.content[:400])
        self.assertEqual(len(queued_webhooks()), 1)

    def test_single_row_sync_refuses_an_adapter_with_no_target_module(self):
        """A changed profile can require a Target Module that this release cannot run."""
        import dataclasses

        from netbox_data_import import catalog as catalog_module
        from netbox_data_import.catalog import TargetModuleKey

        self._upload()
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="trace_workbook")
        without_cable = tuple(
            dataclasses.replace(module, implemented=False) if module.key == TargetModuleKey.CABLE else module
            for module in catalog_module.TARGET_MODULES
        )

        with catalog_module.declared_modules_override(without_cable):
            response = self._sync_single_row({"row_number": 2})

        # No command can replan this profile, so the coordinator refuses it before any write.
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "This release cannot import from the 'trace_workbook' source adapter yet."},
        )

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
        self.assertEqual(
            ImportExecution.objects.latest("pk").failure_detail["reason"],
            FailureReason.DATABASE,
        )

    def test_single_row_sync_does_not_echo_why_the_stored_plan_is_unreadable(self):
        """A malformed stored plan answers one fixed sentence and logs the Python error."""
        self._upload()
        plan = stored_plan(self.client)
        del plan["units"]
        store_plan(self.client, plan)

        with self.assertLogs("netbox_data_import.plan", level="WARNING") as logs:
            response = self._sync_single_row({"row_number": 2})

        self.assertEqual((response.status_code, response.json()["error"]), (409, UNREADABLE_PREVIEW))
        self.assertIn("KeyError", "\n".join(logs.output))

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
        self.assertEqual(
            ImportExecution.objects.latest("pk").failure_detail["reason"],
            FailureReason.VALIDATION,
        )

    def test_single_row_sync_rejects_a_submitted_or_stale_preview(self):
        """Inline execution cannot use a plan after import starts or after another command replanned it."""
        from dcim.models import Rack

        self._upload()
        self._run()

        submitted = self._sync_single_row({"row_number": 2})

        self.assertEqual(submitted.status_code, 409)
        self.assertEqual(submitted.json()["error"], SUBMITTED_PREVIEW)
        self.assertFalse(Rack.objects.filter(site=self.site, name="rack-a").exists())

        self._upload()
        previous = preview_claim(self.client)
        reread = self.client.post(reverse("plugins:netbox_data_import:preview_reread"), previous)
        self.assertEqual(reread.status_code, 302)

        stale = self._sync_single_row({"row_number": 2}, claim=previous)

        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "preview_stale"))
        self.assertFalse(Rack.objects.filter(site=self.site, name="rack-a").exists())
        self.assertFalse(ImportExecution.objects.exists())

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

    def test_single_row_sync_answers_a_row_that_needs_an_unselected_row_with_one_sentence(self):
        """A Device whose new Rack is another row cannot run alone, and the answer names no plan identity."""
        from netbox_data_import.import_engine import UNMERGEABLE_SELECTION

        self._upload()
        plan = ImportPlan.from_dict(stored_plan(self.client))
        rack_change = next(
            change.identity for unit in plan.units for change in unit.changes if "rack" in change.identity
        )

        response = self._sync_single_row({"row_number": 3})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"ok": False, "error": UNMERGEABLE_SELECTION})
        self.assertNotIn(rack_change, response.content.decode())
        self.assertNotIn(rack_change, str(ImportExecution.objects.latest("pk").failure_detail))

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

    def test_single_row_sync_blocks_an_object_permission_failure_before_execution(self):
        """A Rack outside the actor's object constraint is blocked before a write starts."""
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
        self.assertFalse(ImportExecution.objects.exists())

    def test_single_row_sync_rechecks_permissions_on_the_wrapped_request_user(self):
        """The final HTTP write reads permissions again from the concrete request user."""
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Rack, Site
        from users.models import ObjectPermission

        from netbox_data_import import target_modules

        actor = user_with_object_permission(
            "cutover-revoked-writer",
            [
                (ImportProfile, ("change",), {"pk": self.profile.pk}),
                (Site, ("view",), {"pk": self.site.pk}),
                (Rack, ("view", "add"), None),
            ],
        )
        self.client.force_login(actor)
        upload = self._upload()
        self.assertRedirects(
            upload,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        runtime = target_modules.MODULE_RUNTIMES["rack"]

        class RevokingRackRuntime:
            @staticmethod
            def plan(*args, **kwargs):
                return runtime.plan(*args, **kwargs)

            @staticmethod
            def apply(*args, **kwargs):
                ObjectPermission.objects.filter(
                    users=actor,
                    object_types=ContentType.objects.get_for_model(Rack),
                ).delete()
                return runtime.apply(*args, **kwargs)

        target_modules.MODULE_RUNTIMES["rack"] = RevokingRackRuntime()
        self.addCleanup(target_modules.MODULE_RUNTIMES.__setitem__, "rack", runtime)

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()["error"], "Permission denied: this action is outside your NetBox object permissions."
        )
        self.assertNotIn("dcim.add_rack", response.json()["error"])
        self.assertFalse(Rack.objects.filter(site=self.site, name="rack-a").exists())
        self.assertEqual(
            ImportExecution.objects.latest("pk").failure_detail["reason"],
            FailureReason.PERMISSION,
        )

    def test_single_row_sync_replans_the_preview_in_the_same_command(self):
        """A selective execution stores the replanned preview and advances the revision."""
        from dcim.models import Rack

        self._upload()
        before = preview_coordinator(self.client)
        self.assertEqual(self._preview_action(2), "create")

        response = self._sync_single_row({"row_number": 2})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["preview_state"], "replanned")
        self.assertTrue(Rack.objects.filter(site=self.site, name="rack-a").exists())
        after = preview_coordinator(self.client)
        self.assertEqual(after.revision, before.revision + 1)
        self.assertNotEqual(after.plan, before.plan)
        self.assertNotEqual(self._preview_action(2), "create", "the stored plan still offers the written rack")

    def test_single_row_sync_refuses_a_second_sync_from_the_same_page(self):
        """The first inline create advances the revision, so the page's claim cannot run it again."""
        from dcim.models import Rack

        self._upload()
        page_claim = preview_claim(self.client)

        first = self._sync_single_row({"row_number": 2}, claim=page_claim)
        second = self._sync_single_row({"row_number": 2}, claim=page_claim)

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual((second.status_code, second.json()["code"]), (409, "preview_stale"))
        self.assertEqual(ImportExecution.objects.count(), 1)
        self.assertEqual(Rack.objects.filter(site=self.site, name="rack-a").count(), 1)

    def test_a_failed_replan_keeps_the_failed_audit_and_rolls_back_the_row(self):
        """The write and the replan share one savepoint, so a replan fault undoes the Device it wrote."""
        from dcim.models import Device, Rack
        from django.db import connection

        Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        self._upload()
        self.assertEqual(self._preview_action(3), "create", "the fixture does not produce a Device create")
        before = preview_coordinator(self.client)
        device_table, document_table = f'"{Device._meta.db_table}"', f'"{SourceDocument._meta.db_table}"'
        wrote_device, faulted = [], []

        def fail_the_replan(execute, sql, params, many, context):
            statement = sql.lstrip().upper()
            if statement.startswith(("INSERT", "UPDATE")) and device_table in sql:
                wrote_device.append(True)
            elif wrote_device and not faulted and statement.startswith("SELECT") and document_table in sql:
                faulted.append(True)
                raise DatabaseError("injected fault while the replan reads the stored source")
            return execute(sql, params, many, context)

        with self.assertLogs("netbox_data_import.views", level="ERROR"):
            with connection.execute_wrapper(fail_the_replan):
                response = self._sync_single_row({"row_number": 3})

        self.assertEqual((wrote_device[:1], faulted), ([True], [True]), "the fault never reached the replan")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(response.json()["ok"])
        self.assertFalse(Device.objects.filter(name="server-a").exists())
        execution = ImportExecution.objects.get(profile=self.profile)
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(execution.failure_detail["reason"], FailureReason.DATABASE)
        # No change failed and the written ones rolled back: the fault hit the replan, not a write.
        self.assertIsNone(execution.failure_detail["failed_change"])
        self.assertTrue(execution.failure_detail["rolled_back"])
        after = preview_coordinator(self.client)
        self.assertEqual((after.revision, after.plan), (before.revision, before.plan))

    def test_a_plan_size_refusal_keeps_the_failed_row_sync_audit(self):
        from dcim.models import Device, Rack
        from django.db import connection

        from netbox_data_import.tests.plugins_config import override_plugins_config

        Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        self._upload()
        before = preview_coordinator(self.client)
        writes = []

        def record_device_write(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("INSERT") and f'"{Device._meta.db_table}"' in sql:
                writes.append(True)
            return execute(sql, params, many, context)

        with override_plugins_config(netbox_data_import={"preview_max_plan_bytes": 16}):
            with connection.execute_wrapper(record_device_write):
                response = self._sync_single_row({"row_number": 3})

        self.assertEqual(writes, [True], "the refusal never reached a real Device write")
        self.assertEqual(response.status_code, 413, response.content)
        self.assertFalse(Device.objects.filter(name="server-a").exists())
        execution = ImportExecution.objects.get(profile=self.profile)
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertTrue(execution.failure_detail["rolled_back"])
        after = preview_coordinator(self.client)
        self.assertEqual((after.revision, after.plan), (before.revision, before.plan))

    def _assert_cached_device_review_hidden(self, *, diagnostic=False):
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Device, DeviceRole, DeviceType, Rack
        from users.models import ObjectPermission

        rack = Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        device = Device.objects.create(
            name="server-a",
            site=self.site,
            rack=rack,
            device_type=DeviceType.objects.get(slug="example-model"),
            role=DeviceRole.objects.get(slug="server"),
            serial="PRIVATE-SERIAL",
        )
        ColumnMapping.objects.create(profile=self.profile, source_column="Serial", target_field="serial")
        self.actor.is_superuser = False
        self.actor.save(update_fields=["is_superuser"])
        device_type = ContentType.objects.get_for_model(Device)
        grants = ObjectPermission.objects.create(name="Flat preview grants", actions=["view", "change", "add"])
        grants.object_types.set(ContentType.objects.exclude(pk=device_type.pk))
        grants.users.add(self.actor)
        device_grant = ObjectPermission.objects.create(name="Flat device grant", actions=["view", "change"])
        device_grant.object_types.add(device_type)
        device_grant.users.add(self.actor)
        if diagnostic:
            from netbox_data_import.models import DeviceExistingMatch

            DeviceExistingMatch.objects.create(
                profile=self.profile, source_id="D-OTHER", netbox_device_id=device.pk, device_name=device.name
            )
            device_grant.actions = ["view"]
            device_grant.save(update_fields=["actions"])
        upload = SimpleUploadedFile(
            "serial.xlsx",
            workbook_bytes(
                ["Source ID", "Class", "Name", "Rack", "Make", "Model", "Serial"],
                [["D-1", "Server", "server-a", "rack-a", "Example", "Model", "SOURCE-SERIAL"]],
            ),
        )
        setup = upload_preview(self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})
        self.assertEqual(setup.status_code, 302, setup.content)
        url = reverse("plugins:netbox_data_import:import_preview")
        self.assertContains(self.client.get(url), device.serial)
        before = preview_coordinator(self.client)
        if diagnostic:
            self.assertEqual(
                [item["code"] for item in before.plan["units"][0]["diagnostics"]],
                ["device.already_bound", "device.change_permission"],
            )
        device_grant.actions = ["change"]
        device_grant.save(update_fields=["actions"])

        hidden = self.client.get(url)

        self.assertNotContains(hidden, device.serial)
        self.assertNotContains(hidden, device.get_absolute_url())
        self.assertContains(hidden, "SOURCE-SERIAL")
        after = preview_coordinator(self.client)
        self.assertEqual((after.revision, after.plan), (before.revision, before.plan))

    def test_a_cached_device_review_hides_values_after_view_access_is_revoked(self):
        self._assert_cached_device_review_hidden()

    def test_a_diagnostic_device_review_hides_values_after_view_access_is_revoked(self):
        self._assert_cached_device_review_hidden(diagnostic=True)

    def test_a_cached_rack_row_hides_its_match_after_view_access_is_revoked(self):
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Rack
        from users.models import ObjectPermission

        rack = Rack.objects.create(name="rack-a", site=self.site, u_height=42)
        self.actor.is_superuser = False
        self.actor.save(update_fields=["is_superuser"])
        rack_type = ContentType.objects.get_for_model(Rack)
        grants = ObjectPermission.objects.create(name="Flat preview grants", actions=["view", "change", "add"])
        grants.object_types.set(ContentType.objects.exclude(pk=rack_type.pk))
        grants.users.add(self.actor)
        rack_grant = ObjectPermission.objects.create(name="Flat rack grant", actions=["view", "change"])
        rack_grant.object_types.add(rack_type)
        rack_grant.users.add(self.actor)
        self.assertEqual(self._upload().status_code, 302)
        url = reverse("plugins:netbox_data_import:import_preview")
        self.assertContains(self.client.get(url), rack.get_absolute_url())
        before = preview_coordinator(self.client)
        rack_grant.actions = ["change"]
        rack_grant.save(update_fields=["actions"])

        hidden = self.client.get(url)

        self.assertNotContains(hidden, rack.get_absolute_url())
        rows = hidden.context["preview_rows"]
        self.assertEqual(next(row for row in rows if row.object_type == "rack").action, "error")
        self.assertContains(hidden, "rack-a")
        after = preview_coordinator(self.client)
        self.assertEqual((after.revision, after.plan), (before.revision, before.plan))

    def test_device_type_mapping_replans_the_preview(self):
        """A quick mapping saves and replans the active preview in one command."""
        self._upload()
        before = preview_coordinator(self.client)

        response = self.client.post(
            reverse("plugins:netbox_data_import:quick_resolve_device_type"),
            {
                **preview_claim(self.client),
                "source_make": "Source Make",
                "source_model": "Source Model",
                "netbox_mfg_slug": "example",
                "netbox_dt_slug": "example-model",
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["preview_state"], "replanned")
        self.assertEqual(preview_coordinator(self.client).revision, before.revision + 1)
        self.assertTrue(
            DeviceTypeMapping.objects.filter(
                profile=self.profile,
                source_make="Source Make",
                source_model="Source Model",
            ).exists()
        )

    def test_preview_of_a_missing_source_sends_the_operator_to_setup(self):
        """A page load cannot show a preview whose stored input is unavailable."""
        self._upload()
        SourceDocument.objects.get(pk=preview_coordinator(self.client).source_document_id).delete()

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

    def _store_device_candidate_values(self, candidate_values):
        """Put malformed candidate values on the stored Device unit, as an older release could have."""
        plan = stored_plan(self.client)
        device_unit = next(unit for unit in plan["units"] if unit["display"].get("object_type") == "device")
        device_unit["display"].setdefault("extra_data", {})["candidate_values"] = candidate_values
        store_plan(self.client, plan)
        return plan

    def test_preview_refuses_malformed_candidate_values(self):
        """The renderer must reject malformed display data instead of raising an internal error."""
        self._upload()
        plan = self._store_device_candidate_values(["invalid"])

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertEqual(stored_plan(self.client), plan, "a page load must not change the stored preview")

    def test_preview_refuses_malformed_contact_candidate_values(self):
        """Contact suggestions require a source-column mapping, not any JSON value."""
        self._upload()
        plan = self._store_device_candidate_values({"contact": ["invalid"]})

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertEqual(stored_plan(self.client), plan, "a page load must not change the stored preview")

    def test_a_plan_with_an_unknown_schema_is_recovered_by_a_reread(self):
        """A page load cannot read the plan, so it offers the re-read, and the re-read replans it."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")
        self._upload()
        current_version = stored_plan(self.client)["schema_version"]
        store_plan(self.client, {**stored_plan(self.client), "schema_version": 999})

        page = self.client.get(preview_url)

        self.assertContains(page, reverse("plugins:netbox_data_import:preview_reread"))
        self.assertEqual(stored_plan(self.client)["schema_version"], 999)

        reread = self.client.post(
            reverse("plugins:netbox_data_import:preview_reread"), {**preview_claim(self.client), "next": preview_url}
        )

        self.assertRedirects(reread, preview_url, fetch_redirect_response=False)
        self.assertEqual(stored_plan(self.client)["schema_version"], current_version)
        self.assertEqual(self.client.get(preview_url).status_code, 200)

    def test_preview_of_a_target_that_became_unavailable_sends_the_operator_to_setup(self):
        """The page compares the stored plan with live NetBox, which refuses a target that is gone."""
        self._upload()
        self.site.delete()

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))

# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for background import progress."""

import uuid
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django_rq import get_queue

from core.models import Job
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

from netbox_data_import.engine import run_import
from netbox_data_import.jobs import ImportJobRunner
from netbox_data_import.models import ClassRoleMapping, DeviceTypeMapping, ImportProfile, SourceResolution
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin


class _ProgressJob:
    """Record RQ metadata writes without fabricating the RQ Job interface."""

    def __init__(self):
        self.meta = {}
        self.saved_processed = []

    def save_meta(self):
        """Record one metadata write."""
        self.saved_processed.append(self.meta["processed"])


class ImportProgressViewTest(IsolatedRQQueueTestMixin, TestCase):
    """Exercise queued imports through the public wizard views."""

    def setUp(self):
        """Create one valid preview and store it in the user's session."""
        super().setUp()
        self.user = get_user_model().objects.create_superuser(
            username="import-progress-user",
            email="import-progress@example.invalid",
            password="testpass",
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.site = Site.objects.create(name="Import Progress Site", slug="import-progress-site")
        manufacturer = Manufacturer.objects.create(name="Import Progress Vendor", slug="import-progress-vendor")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Import Progress Model",
            slug="import-progress-model",
            u_height=1,
        )
        role = DeviceRole.objects.create(name="Import Progress Device", slug="import-progress-device")
        self.profile = ImportProfile.objects.create(
            name="Import Progress Profile", adapter_config={"create_missing_device_types": False}
        )
        self.class_mapping = ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            creates_rack=False,
            role_slug=role.slug,
        )
        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make=manufacturer.name,
            source_model=self.device_type.model,
            netbox_manufacturer_slug=manufacturer.slug,
            netbox_device_type_slug=self.device_type.slug,
        )
        self.rows = [
            {
                "_row_number": 2,
                "source_id": "PROGRESS-001",
                "device_name": "progress-device",
                "device_class": "Server",
                "make": manufacturer.name,
                "model": self.device_type.model,
                "u_height": 1,
                "rack_name": "",
                "u_position": "",
                "serial": "",
                "asset_tag": "",
                "status": "active",
            }
        ]
        preview = run_import(self.rows, self.profile, {"site": self.site}, dry_run=True, user=self.user)
        session = self.client.session
        session["import_rows"] = self.rows
        session["import_result"] = preview.to_session_dict()
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "progress-test.xlsx",
        }
        session["import_preview_pending"] = True
        session.save()

    def test_run_import_queues_a_resumable_native_job(self):
        """Submitting a valid preview returns a resumable zero-progress page."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("plugins:netbox_data_import:import_run"))

        job = Job.objects.filter(name="Data Import").first()
        self.assertIsNotNone(job)
        self.assertEqual(job.data["job_type"], "netbox_data_import.import")
        self.assertNotIn("source_rows", job.data)
        self.assertNotIn("context_data", job.data)
        progress_url = reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk})
        self.assertRedirects(response, progress_url)

        progress_response = self.client.get(progress_url)
        self.assertContains(progress_response, "Processed 0 of 1 rows")
        self.assertContains(progress_response, "progressbar")
        self.assertContains(progress_response, 'aria-live="polite"')
        self.assertContains(progress_response, 'hx-trigger="every 2s"')
        self.assertNotContains(progress_response, 'hx-trigger="load, every 1s"')

        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(setup_response, "Resume import")
        self.assertContains(setup_response, progress_url)

        session = self.client.session
        session.pop("import_background_job_id")
        session.save()
        fallback_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(fallback_response, "Resume import")
        self.assertContains(fallback_response, progress_url)

    def test_setup_can_resume_an_unsubmitted_preview(self):
        """Leaving the preview does not force the user to upload the file again."""
        preview_url = reverse("plugins:netbox_data_import:import_preview")

        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))

        self.assertContains(setup_response, "Resume preview")
        self.assertContains(setup_response, f'href="{preview_url}"')
        preview_response = self.client.get(preview_url)
        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "progress-device")

    def test_setup_exposes_a_preview_while_an_earlier_import_is_active(self):
        """An active Job does not hide a newer unsubmitted preview."""
        Job.objects.create(
            name="Data Import",
            user=self.user,
            status="pending",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={"job_type": "netbox_data_import.import"},
        )

        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))

        self.assertContains(setup_response, "Resume import")
        self.assertContains(setup_response, "Resume preview")

    def test_completed_job_restoration_preserves_a_newer_preview(self):
        """An older Job result cannot replace a pending preview in another tab."""
        pending_preview = self.client.session["import_result"]
        completed_job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status="completed",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={
                "job_type": "netbox_data_import.import",
                "result": {"rows": [], "counts": {}, "has_errors": False},
                "import_job_id": 123,
            },
        )

        self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": completed_job.pk}))

        session = self.client.session
        self.assertTrue(session["import_preview_pending"])
        self.assertEqual(session["import_result"], pending_preview)
        self.assertEqual(session["import_restored_job_result"], completed_job.data["result"])
        results_response = self.client.get(reverse("plugins:netbox_data_import:import_results"))
        self.assertEqual(results_response.context["job_id"], 123)
        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(setup_response, "Resume preview")

    def test_failed_job_review_does_not_replace_a_newer_preview(self):
        """A failed Job cannot link to a different pending preview."""
        pending_preview = self.client.session["import_result"]
        failed_preview = {"rows": [], "counts": {"errors": 1}, "has_errors": True}
        failed_job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status="failed",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={
                "job_type": "netbox_data_import.import",
                "preview_result": failed_preview,
                "context_data": self.client.session["import_context"],
            },
        )
        progress_url = reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": failed_job.pk})

        blocked_response = self.client.get(progress_url)

        self.assertNotContains(blocked_response, ">Review preview</a>", html=False)
        self.assertEqual(self.client.session["import_result"], pending_preview)

        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        restored_response = self.client.get(progress_url)

        self.assertNotContains(restored_response, ">Review preview</a>", html=False)
        session = self.client.session
        self.assertEqual(session["import_result"], pending_preview)
        self.assertEqual(session["import_rows"], self.rows)
        self.assertFalse(session["import_preview_pending"])

    def test_resumed_preview_rejects_a_deleted_target_site(self):
        """A stale target cannot become an empty target when preview resumes."""
        self.site.delete()

        preview_response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertRedirects(preview_response, reverse("plugins:netbox_data_import:import_setup"))
        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertNotContains(setup_response, "Resume preview")

    def test_resumed_preview_rejects_a_deleted_profile(self):
        """A stale profile cannot leave a permanent resume link."""
        self.profile.delete()

        preview_response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertRedirects(preview_response, reverse("plugins:netbox_data_import:import_setup"))
        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertNotContains(setup_response, "Resume preview")

    def test_setup_shows_feedback_while_generating_the_preview(self):
        """Submitting a workbook immediately shows a loading state."""
        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))

        self.assertContains(setup_response, 'hx-boost="true"')
        self.assertContains(setup_response, 'hx-encoding="multipart/form-data"')
        self.assertContains(setup_response, 'hx-push-url="true"')
        self.assertContains(setup_response, 'hx-indicator="#import-preview-pending"')
        self.assertContains(setup_response, 'hx-disabled-elt="#preview-import-submit"')
        self.assertContains(setup_response, 'id="import-preview-pending"')
        self.assertContains(setup_response, "Reading workbook and generating preview")

    def test_worker_completes_the_import_and_redirects_to_results(self):
        """The real RQ worker persists final progress and the existing results."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(get_queue(job.queue_name).fetch_job(str(job.job_id)))

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.data["processed"], 1)
        self.assertEqual(job.data["total"], 1)
        self.assertNotIn("source_rows", job.data)
        self.assertNotIn("context_data", job.data)
        self.assertTrue(Device.objects.filter(name="progress-device").exists(), job.data)

        session = self.client.session
        session.pop("import_result", None)
        session.pop("import_job_id", None)
        session.save()
        progress_response = self.client.get(
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk})
        )
        self.assertContains(progress_response, "Import complete")
        direct_results_response = self.client.get(reverse("plugins:netbox_data_import:import_results"))
        self.assertContains(direct_results_response, "progress-device")
        self.assertNotIn("import_rows", self.client.session)
        self.assertNotIn("import_context", self.client.session)
        self.assertNotIn("import_source_rows_job_id", self.client.session)

        status_response = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(status_response.status_code, 204)
        self.assertEqual(
            status_response.headers["HX-Redirect"],
            reverse("plugins:netbox_data_import:import_results"),
        )

        results_response = self.client.get(reverse("plugins:netbox_data_import:import_results"))
        self.assertContains(results_response, "progress-device")
        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertNotContains(setup_response, "Resume preview")

    def test_resolution_saved_after_queueing_refuses_the_stale_import(self):
        """The worker replays a late resolution before it validates the accepted preview."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="PROGRESS-001",
            source_column="device_name",
            original_value="progress-device",
            resolved_fields={"device_name": "resolved-progress-device"},
        )

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "failed", job.error)
        self.assertEqual(
            job.data["message"],
            "The import preview changed. Review the refreshed preview before importing.",
        )
        self.assertFalse(Device.objects.filter(name="progress-device").exists())
        self.assertFalse(Device.objects.filter(name="resolved-progress-device").exists())

    def test_status_fragment_reads_live_progress_from_rq(self):
        """The polling endpoint reads progress that is visible before commit."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        rq_job = get_queue(job.queue_name).fetch_job(str(job.job_id))
        rq_job.meta.update({"processed": 1, "total": 4, "phase": "importing"})
        rq_job.save_meta()

        response = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertContains(response, "Processed 1 of 4 rows")
        self.assertContains(response, 'aria-valuenow="25"')

    def test_unchanged_status_poll_does_not_rewrite_the_session(self):
        """Polling an unchanged Job does not persist the user's large session."""
        job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status="pending",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={
                "job_type": ImportJobRunner.job_type,
                "processed": 0,
                "total": 1,
            },
        )
        session = self.client.session
        session["import_background_job_id"] = job.pk
        session["import_preview_pending"] = False
        session.save()

        response = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)

    def test_progress_views_fall_back_when_the_job_queue_was_removed(self):
        """A stale queue name cannot turn either progress endpoint into HTTP 500."""
        job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status="pending",
            job_id=uuid.uuid4(),
            queue_name="removed-import-queue",
            data={
                "job_type": ImportJobRunner.job_type,
                "processed": 3,
                "total": 8,
            },
        )

        progress_response = self.client.get(
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk})
        )
        status_response = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertContains(progress_response, "Processed 3 of 8 rows")
        self.assertContains(status_response, "Processed 3 of 8 rows")

    def test_progress_is_visible_only_to_the_user_who_started_it(self):
        """Another user cannot inspect a queued import or its progress data."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        other_user = get_user_model().objects.create_superuser(
            username="other-import-user",
            email="other-import-user@example.invalid",
            password="testpass",
        )
        other_client = Client()
        other_client.force_login(other_user)

        progress_response = other_client.get(
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk})
        )
        status_response = other_client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(progress_response.status_code, 404)
        self.assertEqual(status_response.status_code, 404)

    def test_progress_routes_reject_an_unrelated_job_with_the_same_name(self):
        """A different runner cannot become this plugin's resumable import."""
        unrelated_job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status="pending",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={"result": {"unrelated": True}},
        )
        progress_url = reverse(
            "plugins:netbox_data_import:import_progress",
            kwargs={"pk": unrelated_job.pk},
        )

        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        progress_response = self.client.get(progress_url)
        status_response = self.client.get(
            reverse(
                "plugins:netbox_data_import:import_progress_status",
                kwargs={"pk": unrelated_job.pk},
            ),
            HTTP_HX_REQUEST="true",
        )

        self.assertNotContains(setup_response, progress_url)
        self.assertEqual(progress_response.status_code, 404)
        self.assertEqual(status_response.status_code, 404)

    def test_changed_preview_fails_safely_and_preserves_the_refreshed_preview(self):
        """A changed preview remains available for review after the worker stops."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(get_queue(job.queue_name).fetch_job(str(job.job_id)))
        ClassRoleMapping.objects.filter(pk=self.class_mapping.pk).update(role_slug="")

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "failed", job.error)
        self.assertEqual(job.data["phase"], "failed")
        self.assertIn("preview changed", job.data["message"].lower())
        self.assertIn("preview_result", job.data)
        self.assertNotIn("source_rows", job.data)
        self.assertEqual(job.data["context_data"], self.client.session["import_context"])
        self.assertEqual(self.client.session["import_source_rows_job_id"], job.pk)
        self.assertEqual(self.client.session["import_rows"], self.rows)
        self.assertFalse(Device.objects.filter(name="progress-device").exists())

        status_response = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(status_response, "Review preview")
        self.assertContains(status_response, "preview changed")

        preview_response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "has no device role configured")
        setup_response = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(setup_response, "Resume preview")

    def test_failed_job_without_session_rows_does_not_offer_a_stale_preview(self):
        """A failed Job does not persist source rows after the session expires."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        ClassRoleMapping.objects.filter(pk=self.class_mapping.pk).update(role_slug="")
        self.run_rq_jobs()
        session = self.client.session
        session.pop("import_rows")
        session.save()

        response = self.client.get(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}))

        job.refresh_from_db()
        self.assertNotIn("source_rows", job.data)
        self.assertNotContains(response, ">Review preview</a>", html=False)

    def test_progress_metadata_is_throttled_and_always_reports_completion(self):
        """Large imports avoid one Redis metadata write for every source row."""
        rq_job = _ProgressJob()

        with patch("netbox_data_import.jobs.get_current_job", return_value=rq_job):
            for processed in range(31):
                ImportJobRunner._publish_progress(processed, 30)

        self.assertEqual(rq_job.saved_processed, [0, 25, 30])

    def test_worker_fails_if_the_selected_location_was_deleted(self):
        """A missing target Location cannot silently become no Location."""
        from dcim.models import Location

        location = Location.objects.create(
            site=self.site,
            name="Import Progress Location",
            slug="import-progress-location",
            status="active",
        )
        preview = run_import(
            self.rows,
            self.profile,
            {"site": self.site, "location": location},
            dry_run=True,
            user=self.user,
        )
        session = self.client.session
        session["import_context"]["location_id"] = location.pk
        session["import_result"] = preview.to_session_dict()
        session.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        location.delete()

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "failed", job.error)
        self.assertIn("location", job.data["message"].lower())
        self.assertFalse(Device.objects.filter(name="progress-device").exists())

    def test_worker_fails_if_the_selected_tenant_was_deleted(self):
        """A missing target Tenant cannot silently become no Tenant."""
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Import Progress Tenant", slug="import-progress-tenant")
        preview = run_import(
            self.rows,
            self.profile,
            {"site": self.site, "tenant": tenant},
            dry_run=True,
            user=self.user,
        )
        session = self.client.session
        session["import_context"]["tenant_id"] = tenant.pk
        session["import_result"] = preview.to_session_dict()
        session.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        tenant.delete()

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "failed", job.error)
        self.assertIn("tenant", job.data["message"].lower())
        self.assertFalse(Device.objects.filter(name="progress-device").exists())

    def test_worker_fails_if_the_profile_was_deleted(self):
        """A deleted profile stops the queued import before any write."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        self.profile.delete()

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "failed", job.error)
        self.assertIn("profile", job.data["message"].lower())
        self.assertFalse(Device.objects.filter(name="progress-device").exists())

    def test_worker_fails_if_the_site_was_deleted(self):
        """A deleted target Site stops the queued import before any write."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("plugins:netbox_data_import:import_run"))
        job = Job.objects.get(name="Data Import")
        self.site.delete()

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "failed", job.error)
        self.assertIn("site", job.data["message"].lower())
        self.assertFalse(Device.objects.filter(name="progress-device").exists())

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The session keeps pristine parsed rows, so a replayed resolution can express a removal."""

import uuid

from threading import Event

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.db.utils import OperationalError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile, SourceResolution
from netbox_data_import.tests.helpers import run_on_separate_connection
from netbox_data_import.views import _serialize_rows

SOURCE_ID = "PRISTINE-1"
PRISTINE_NAME = "TAG-1 - server-a"


def _build_profile(name):
    """Return a profile that maps the three columns the rows below carry."""
    profile = ImportProfile.objects.create(
        name=name,
        adapter_config={"sheet_name": "Data", "source_id_column": "Id", "create_missing_device_types": True},
    )
    ColumnMapping.objects.create(profile=profile, source_column="Name", target_field="device_name")
    ColumnMapping.objects.create(profile=profile, source_column="Tag", target_field="asset_tag")
    ColumnMapping.objects.create(profile=profile, source_column="Class", target_field="device_class")
    return profile


def _pristine_rows():
    """Return the rows exactly as the parser produced them, with no resolution applied."""
    return [
        {
            "_row_number": 2,
            "source_id": SOURCE_ID,
            "device_name": PRISTINE_NAME,
            "asset_tag": "",
            "device_class": "Server",
            "make": "PristineMfg",
            "model": "PristineModel",
        }
    ]


class PristinePreviewRowMixin:
    """Shared setup that drives the real preview view over a seeded session."""

    def build_world(self):
        """Create the user, site, role mapping, profile, and seed the preview session."""
        from dcim.models import DeviceRole, Site

        self.user = get_user_model().objects.create_superuser("pristine-user", "p@example.invalid", "testpass")
        self.client = Client()
        self.client.force_login(self.user)
        self.site = Site.objects.create(name="Pristine Site", slug="pristine-site")
        self.role = DeviceRole.objects.create(name="Pristine Role", slug="pristine-role")
        self.profile = _build_profile("Pristine Profile")
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug=self.role.slug)

        session = self.client.session
        session["import_rows"] = _serialize_rows(_pristine_rows())
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "pristine.xlsx",
        }
        session["import_preview_pending"] = True
        session.save()

    def open_preview(self):
        """Render the preview the way the browser does."""
        return self.client.get(reverse("plugins:netbox_data_import:import_preview"))

    def save_split(self, resolved_fields):
        """Create or replace the saved split resolution for the one source row."""
        resolution, _ = SourceResolution.objects.update_or_create(
            profile=self.profile,
            source_id=SOURCE_ID,
            source_column="device_name",
            defaults={"original_value": PRISTINE_NAME, "resolved_fields": resolved_fields},
        )
        return resolution

    def run_the_worker(self):
        """Execute the queued import through the real job runner."""
        from core.models import Job

        from netbox_data_import.jobs import ImportJobRunner

        session = self.client.session
        job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status="pending",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={"job_type": ImportJobRunner.job_type},
        )
        ImportJobRunner(job).run(session["import_rows"], session["import_context"], session["import_result"])
        return job


class DroppedResolutionFieldTest(PristinePreviewRowMixin, TestCase):
    """Editing a resolution to drop a target field must clear that field."""

    def setUp(self):
        """Build the world and take the first preview."""
        self.build_world()
        self.open_preview()

    def test_the_session_keeps_the_pristine_parsed_row(self):
        """A replayed resolution must never be written back into import_rows."""
        self.save_split({"asset_tag": "TAG-1", "device_name": "server-a"})
        self.open_preview()

        [stored] = self.client.session["import_rows"]
        self.assertEqual(stored["device_name"], PRISTINE_NAME)
        self.assertEqual(stored["asset_tag"], "")

    def test_dropping_the_tag_from_a_resolution_stops_importing_it(self):
        """The operator re-splits and ignores the tag, so the imported device carries no asset tag."""
        from dcim.models import Device

        self.save_split({"asset_tag": "TAG-1", "device_name": "server-a"})
        self.open_preview()

        # The operator reopens the split modal and this time ignores the tag.
        self.save_split({"device_name": "server-a"})
        self.open_preview()

        self.run_the_worker()
        device = Device.objects.get(name="server-a")
        self.assertEqual(device.asset_tag or "", "")


class ResolutionCommittedAfterValidationTest(PristinePreviewRowMixin, TransactionTestCase):
    """A resolution that commits after the worker's validation read must not reach execution."""

    def setUp(self):
        """Build the world, save a first decision, and take the preview the operator approves."""
        self.build_world()
        self.open_preview()
        self.save_split({"asset_tag": "TAG-1", "device_name": "server-a"})
        self.open_preview()

    def test_a_save_committed_after_the_validation_read_is_refused(self):
        """Read-committed does not wait for the open save, so the worker must re-read before it writes."""
        from core.exceptions import JobFailed
        from core.models import Job

        from dcim.models import Device

        def supersede_the_decision(sender, instance, **kwargs):
            """Commit an edited resolution once the worker reaches its validating phase."""
            if (instance.data or {}).get("phase") != "validating":
                return
            post_save.disconnect(supersede_the_decision, sender=Job)

            def write_it():
                SourceResolution.objects.filter(
                    profile_id=self.profile.pk, source_id=SOURCE_ID, source_column="device_name"
                ).update(resolved_fields={"device_name": "server-a-renamed"})

            with run_on_separate_connection(write_it):
                pass

        post_save.connect(supersede_the_decision, sender=Job, weak=False)
        self.addCleanup(post_save.disconnect, supersede_the_decision, sender=Job)

        with self.assertRaises(JobFailed):
            self.run_the_worker()

        self.assertFalse(Device.objects.filter(name="server-a").exists())
        self.assertFalse(Device.objects.filter(name="server-a-renamed").exists())


class UploadStoresPristineRowsTest(TestCase):
    """The real upload path must store pristine rows, not the parser's resolved output."""

    def setUp(self):
        """Authenticate and create a profile that already carries a saved resolution."""
        from dcim.models import Site

        self.user = get_user_model().objects.create_superuser("upload-user", "u@example.invalid", "testpass")
        self.client = Client()
        self.client.force_login(self.user)
        self.site = Site.objects.create(name="Upload Site", slug="upload-site")
        self.profile = ImportProfile.objects.create(
            name="Upload Profile",
            adapter_config={"sheet_name": "Data", "source_id_column": "Id", "create_missing_device_types": True},
        )
        for source_column, target_field in {
            "Id": "source_id",
            "Name": "device_name",
            "Class": "device_class",
            "Make": "make",
            "Model": "model",
        }.items():
            ColumnMapping.objects.create(profile=self.profile, source_column=source_column, target_field=target_field)

    def test_a_resolution_saved_before_upload_is_not_baked_into_the_session(self):
        """`parse_file` used to apply resolutions, which made every uploaded row effective."""
        import os

        from netbox_data_import.engine import parse_file
        from netbox_data_import.models import SourceResolution

        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")
        with open(fixture, "rb") as handle:
            first_source_id = str(parse_file(handle, self.profile)[0].get("source_id", ""))
        self.assertTrue(first_source_id)

        SourceResolution.objects.create(
            profile=self.profile,
            source_id=first_source_id,
            source_column="device_name",
            original_value="whatever",
            resolved_fields={"device_name": "baked-by-parse-file"},
        )

        with open(fixture, "rb") as handle:
            self.client.post(
                reverse("plugins:netbox_data_import:import_setup"),
                {"profile": self.profile.pk, "site": self.site.pk, "excel_file": handle},
            )

        stored = self.client.session.get("import_rows") or []
        self.assertTrue(stored)
        self.assertEqual([r for r in stored if r.get("device_name") == "baked-by-parse-file"], [])


class PolicyWriteSerializationTest(TransactionTestCase):
    """A resolution write and an executing import serialize on the same profile row."""

    def setUp(self):
        """Create the profile whose policy rows the two sides contend for."""
        self.profile = _build_profile("Lock Profile")

    def _attempt_while_the_worker_holds_the_profile(self, write, *, lock=True):
        """Run *write* while another connection holds the same profile row.

        `lock=False` is for a write that reaches the policy lock through its own view.
        """
        from django.db import connection

        from netbox_data_import.models import locked_profile_policy

        started = Event()
        release = Event()
        blocked = []

        def hold_the_profile_like_the_worker():
            """Take the same lock the import execution transaction takes."""
            with locked_profile_policy(self.profile.pk):
                started.set()
                self.assertTrue(release.wait(timeout=10))

        with run_on_separate_connection(hold_the_profile_like_the_worker):
            try:
                self.assertTrue(started.wait(timeout=10))
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout TO '750ms'")
                try:
                    if lock:
                        with locked_profile_policy(self.profile.pk):
                            write()
                    else:
                        write()
                except OperationalError:
                    blocked.append(True)
            finally:
                release.set()
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout TO 0")
        return blocked

    def test_a_new_resolution_cannot_commit_while_an_import_holds_the_profile(self):
        """An insert takes FOR KEY SHARE on the parent, which the worker's FOR UPDATE already blocks."""

        def insert():
            SourceResolution.objects.create(
                profile=self.profile,
                source_id="LOCK-1",
                source_column="device_name",
                original_value="pristine",
                resolved_fields={"device_name": "late-decision"},
            )

        self.assertEqual(self._attempt_while_the_worker_holds_the_profile(insert), [True])
        self.assertFalse(SourceResolution.objects.filter(source_id="LOCK-1").exists())

    def test_an_edited_resolution_cannot_commit_while_an_import_holds_the_profile(self):
        """Updating a child row touches no parent row, so only the writer's own lock serializes it."""
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="LOCK-2",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "first-decision"},
        )

        def edit():
            SourceResolution.objects.filter(profile=self.profile, source_id="LOCK-2").update(
                resolved_fields={"device_name": "second-decision"}
            )

        self.assertEqual(self._attempt_while_the_worker_holds_the_profile(edit), [True])
        row = SourceResolution.objects.get(profile=self.profile, source_id="LOCK-2")
        self.assertEqual(row.resolved_fields, {"device_name": "first-decision"})

    def test_moving_a_resolution_off_a_locked_profile_waits(self):
        """perform_update locked only the destination, so a move could strip the source mid-import."""
        from rest_framework.test import APIClient

        other = _build_profile("Lock Profile Destination")
        resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="LOCK-3",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "decision"},
        )
        user = get_user_model().objects.create_superuser("move-user", "mv@example.invalid", "testpass")
        api = APIClient()
        api.force_authenticate(user=user)

        responses = []

        def move_it():
            responses.append(
                api.patch(
                    f"/api/plugins/data-import/source-resolutions/{resolution.pk}/",
                    {"profile": other.pk},
                    format="json",
                )
            )

        blocked = self._attempt_while_the_worker_holds_the_profile(move_it, lock=False)
        self.assertEqual(
            blocked, [True], f"PATCH did not wait; responses={[(r.status_code, r.content[:200]) for r in responses]}"
        )
        resolution.refresh_from_db()
        self.assertEqual(resolution.profile_id, self.profile.pk)

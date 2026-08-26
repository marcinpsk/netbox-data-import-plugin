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


def _seen(responses):
    """Render collected API responses for an assertion message."""
    return f"responses={[(r.status_code, r.content[:200]) for r in responses]}"


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
        """
        Create or replace the saved field resolution for the source row.
        
        Parameters:
            resolved_fields: Field values to associate with the source row.
        
        Returns:
            The created or updated source resolution.
        """
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
        """Set up the import scenario with a saved resolution and refreshed preview."""
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
            """
            Update the device-name resolution when the job enters its validating phase.
            """
            if (instance.data or {}).get("phase") != "validating":
                return
            post_save.disconnect(supersede_the_decision, sender=Job)

            def write_it():
                """Update the device-name resolution for the test profile."""
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
        """Set up an authenticated client, site, import profile, and column mappings for upload tests."""
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

    def _api(self, username):
        """
        Return an authenticated REST client for the specified username.
        
        Parameters:
            username (str): Username for the superuser used to authenticate the client.
        
        Returns:
            APIClient: An authenticated REST test client.
        """
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user=get_user_model().objects.create_superuser(username, f"{username}@x.invalid", "p"))
        return api

    def _attempt_while_the_worker_holds_the_profile(self, write):
        """
        Execute a write operation while another connection holds the profile lock.
        
        Parameters:
        	write (callable): The write operation to execute while the profile is locked.
        
        Returns:
        	blocked (list): A list containing `True` if the write raises `OperationalError`; otherwise, an empty list.
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
        api = self._api("insert-user")
        responses = []

        def insert():
            """
            Submit a source-resolution creation request while the profile lock is held.
            """
            responses.append(
                api.post(
                    "/api/plugins/data-import/source-resolutions/",
                    {
                        "profile": self.profile.pk,
                        "source_id": "LOCK-1",
                        "source_column": "device_name",
                        "original_value": "pristine",
                        "resolved_fields": {"device_name": "late-decision"},
                    },
                    format="json",
                )
            )

        self.assertEqual(self._attempt_while_the_worker_holds_the_profile(insert), [True], _seen(responses))
        self.assertFalse(SourceResolution.objects.filter(source_id="LOCK-1").exists())

    def test_an_edited_resolution_cannot_commit_while_an_import_holds_the_profile(self):
        """Updating a child row touches no parent row, so only the view's own lock serializes it."""
        resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="LOCK-2",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "first-decision"},
        )
        api = self._api("edit-user")
        responses = []

        def edit():
            """
            Update the source resolution with a second device-name decision and record the API response.
            """
            responses.append(
                api.patch(
                    f"/api/plugins/data-import/source-resolutions/{resolution.pk}/",
                    {"resolved_fields": {"device_name": "second-decision"}},
                    format="json",
                )
            )

        self.assertEqual(self._attempt_while_the_worker_holds_the_profile(edit), [True], _seen(responses))
        resolution.refresh_from_db()
        self.assertEqual(resolution.resolved_fields, {"device_name": "first-decision"})

    def test_moving_a_resolution_to_another_profile_is_refused(self):
        """A row belongs to one profile for life, so no write can span two profile locks."""
        other = _build_profile("Lock Profile Destination")
        resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="LOCK-3",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "decision"},
        )
        api = self._api("move-user")

        response = api.patch(
            f"/api/plugins/data-import/source-resolutions/{resolution.pk}/",
            {"profile": other.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("cannot move to another profile", str(response.data["profile"]))
        resolution.refresh_from_db()
        self.assertEqual(resolution.profile_id, self.profile.pk)

    def test_naming_the_profile_a_resolution_already_has_is_allowed(self):
        """Resending the profile a row already has is not a move, so it must still be accepted."""
        resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="LOCK-4",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "decision"},
        )
        api = self._api("stay-user")

        response = api.patch(
            f"/api/plugins/data-import/source-resolutions/{resolution.pk}/",
            {"profile": self.profile.pk, "resolved_fields": {"device_name": "second-decision"}},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        resolution.refresh_from_db()
        self.assertEqual(resolution.resolved_fields, {"device_name": "second-decision"})


class ProfileLockContractTest(TestCase):
    """The lock helper must never yield while it holds nothing."""

    def test_locking_no_profile_is_refused(self):
        """`filter(pk__in=[])` never reaches the database, so an empty call would lock nothing."""
        from netbox_data_import.models import locked_profile_policy

        with self.assertRaises(ImportProfile.DoesNotExist):
            with locked_profile_policy():
                pass

    def test_locking_only_missing_profiles_is_refused(self):
        """A named profile that no longer exists is unexpected state, not an empty lock set."""
        from netbox_data_import.models import locked_profile_policy

        with self.assertRaises(ImportProfile.DoesNotExist):
            with locked_profile_policy(9_999_999):
                pass


class ResolutionMoveRaceTest(TransactionTestCase):
    """A delete must lock the profile the database says the row is in, not the one it read earlier.

    The REST API refuses to move a saved row, so the move below goes straight through the ORM.
    """

    def setUp(self):
        """Create both profiles, the resolution to delete, and the operator who deletes it."""
        self.source = _build_profile("Move Race Source")
        self.destination = _build_profile("Move Race Destination")
        self.resolution = SourceResolution.objects.create(
            profile=self.source,
            source_id="MOVE-RACE-1",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "decision"},
        )
        self.user = get_user_model().objects.create_superuser("race-user", "race@example.invalid", "testpass")
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_delete_waits_for_the_profile_the_row_moved_to(self):
        """
        Verify that deleting a resolution locks the profile currently assigned to it after a concurrent move.
        """
        from contextlib import ExitStack

        from django.db import connection
        from django.db.models.signals import post_init

        from netbox_data_import.models import locked_profile_policy

        started = Event()
        release = Event()
        fired = []
        stack = ExitStack()

        def hold_the_destination_like_the_worker():
            """Acquire the destination profile lock used during import execution and hold it until released by the test."""
            with locked_profile_policy(self.destination.pk):
                started.set()
                self.assertTrue(release.wait(timeout=10))

        def move_the_row_then_start_the_worker(sender, instance, **kwargs):
            """Move the resolution to another profile while the worker is acquiring its lock."""
            if fired or instance.pk != self.resolution.pk:
                return
            fired.append(True)

            def move_it():
                """
                Move the resolution to the destination import profile.
                """
                SourceResolution.objects.filter(pk=self.resolution.pk).update(profile=self.destination)

            with run_on_separate_connection(move_it):
                pass
            stack.enter_context(run_on_separate_connection(hold_the_destination_like_the_worker))
            self.assertTrue(started.wait(timeout=10))

        post_init.connect(move_the_row_then_start_the_worker, sender=SourceResolution)
        url = reverse("plugins:netbox_data_import:source_resolution_delete", kwargs={"pk": self.resolution.pk})
        blocked = []
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '750ms'")
            try:
                self.client.post(url, {"confirm": True})
            except OperationalError:
                blocked.append(True)
        finally:
            post_init.disconnect(move_the_row_then_start_the_worker, sender=SourceResolution)
            release.set()
            stack.close()
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO 0")

        self.assertEqual(fired, [True], "the move never ran, so the race was not reproduced")
        self.assertEqual(blocked, [True], "the delete did not wait for the profile the row moved to")
        self.assertTrue(SourceResolution.objects.filter(pk=self.resolution.pk).exists())


class ResolutionVanishedUnderTheLockTest(TransactionTestCase):
    """A row deleted between the fetch and the lock is the 404 the fetch itself would have given."""

    def setUp(self):
        """Create the profile, the resolution, and the operator who tries to delete it."""
        self.profile = _build_profile("Vanished Profile")
        self.resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="VANISH-1",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "decision"},
        )
        self.user = get_user_model().objects.create_superuser("vanish-user", "v@example.invalid", "testpass")

    def _delete_the_row_once_it_is_fetched(self):
        """Arrange for the current resolution to be deleted from a separate database connection when it is fetched.
        
        Returns:
            list: A mutable marker containing one item after the deletion callback runs.
        """
        from django.db.models.signals import post_init

        fired = []

        def drop_it(sender, instance, **kwargs):
            """
            Delete the target source resolution when its signal callback is triggered.
            
            Parameters:
                instance: The source resolution associated with the signal event.
            """
            if fired or instance.pk != self.resolution.pk:
                return
            fired.append(True)

            def delete_it():
                """Delete the test resolution if it still exists."""
                SourceResolution.objects.filter(pk=self.resolution.pk).delete()

            with run_on_separate_connection(delete_it):
                pass

        post_init.connect(drop_it, sender=SourceResolution)
        self.addCleanup(post_init.disconnect, drop_it, sender=SourceResolution)
        return fired

    def test_the_delete_view_answers_404(self):
        """The UI delete view fetched the row, so a vanished row is not a server error."""
        fired = self._delete_the_row_once_it_is_fetched()
        client = Client()
        client.force_login(self.user)
        url = reverse("plugins:netbox_data_import:source_resolution_delete", kwargs={"pk": self.resolution.pk})

        response = client.post(url, {"confirm": True})

        self.assertEqual(fired, [True])
        self.assertEqual(response.status_code, 404)

    def test_the_api_delete_answers_404(self):
        """The REST delete reaches the same lock through DRF's own fetch."""
        from rest_framework.test import APIClient

        fired = self._delete_the_row_once_it_is_fetched()
        api = APIClient()
        api.force_authenticate(user=self.user)

        response = api.delete(f"/api/plugins/data-import/source-resolutions/{self.resolution.pk}/")

        self.assertEqual(fired, [True])
        self.assertEqual(response.status_code, 404)


class ResolutionDeletedWhileTheWriteWaitsTest(TransactionTestCase):
    """A write that waited for the lock must not act on a row that was deleted while it waited."""

    def setUp(self):
        """Create the profile, the resolution both sides contend for, and the REST client."""
        from rest_framework.test import APIClient

        self.profile = _build_profile("Resurrection Profile")
        self.resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="GONE-1",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "first-decision"},
        )
        self.api = APIClient()
        self.api.force_authenticate(
            user=get_user_model().objects.create_superuser("gone-user", "g@example.invalid", "testpass")
        )

    def test_a_patch_that_waited_does_not_resurrect_the_row(self):
        """`Model.save()` falls back to INSERT when its UPDATE matches nothing, restoring the old id."""
        from netbox_data_import.models import locked_profile_policy
        from netbox_data_import.tests.helpers import wait_until_a_lock_is_blocked

        responses = []
        holding = Event()

        def patch_it():
            self.assertTrue(holding.wait(timeout=10))
            responses.append(
                self.api.patch(
                    f"/api/plugins/data-import/source-resolutions/{self.resolution.pk}/",
                    {"resolved_fields": {"device_name": "second-decision"}},
                    format="json",
                )
            )

        with run_on_separate_connection(patch_it):
            with locked_profile_policy(self.profile.pk):
                holding.set()
                # The PATCH read the row, then blocks here, so the delete below lands in its gap.
                wait_until_a_lock_is_blocked(self)
                SourceResolution.objects.filter(pk=self.resolution.pk).delete()

        self.assertEqual([r.status_code for r in responses], [404], _seen(responses))
        self.assertFalse(SourceResolution.objects.filter(pk=self.resolution.pk).exists())


class ProfileCascadeLockOrderTest(TransactionTestCase):
    """Deleting a profile must take its policy lock before the cascade reaches the child rows."""

    def setUp(self):
        """Create the profile and one saved resolution for the cascade to collect."""
        self.profile = _build_profile("Cascade Profile")
        self.resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="CASCADE-1",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "first-decision"},
        )

    def test_a_policy_write_does_not_deadlock_with_a_profile_delete(self):
        """The cascade collects children first, so a lock taken after it inverts the writer's order."""
        from netbox_data_import.models import ImportProfile, locked_profile_policy
        from netbox_data_import.tests.helpers import wait_until_a_lock_is_blocked

        holding = Event()

        def delete_the_profile():
            self.assertTrue(holding.wait(timeout=10))
            ImportProfile.objects.get(pk=self.profile.pk).delete()

        with run_on_separate_connection(delete_the_profile):
            with locked_profile_policy(self.profile.pk):
                holding.set()
                wait_until_a_lock_is_blocked(self)
                # Deadlocks here when the cascade already holds the child row and waits for this one.
                SourceResolution.objects.filter(pk=self.resolution.pk).update(
                    resolved_fields={"device_name": "second-decision"}
                )

        self.assertFalse(ImportProfile.objects.filter(pk=self.profile.pk).exists())

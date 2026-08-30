# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The session keeps pristine parsed rows, so a replayed resolution can express a removal."""

from threading import Event

from django.contrib.auth import get_user_model
from django.db.utils import OperationalError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import ColumnMapping, ImportProfile, SourceResolution
from netbox_data_import.tests.helpers import run_on_separate_connection


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


class UploadStoresPristineSourceTest(TestCase):
    """The real upload path keeps the source bytes independent from saved resolutions."""

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

    def test_a_resolution_saved_before_upload_is_not_baked_into_the_source(self):
        """Replanning can change effective rows without modifying the stored upload."""
        import os

        from netbox_data_import.adapters import FlatWorkbookAdapter
        from netbox_data_import.adapter_config import interpreter_config_for
        from netbox_data_import.models import SourceResolution

        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")
        with open(fixture, "rb") as handle:
            original = handle.read()
        batch = FlatWorkbookAdapter.interpret(original, interpreter_config_for(self.profile))
        first_source_id = str(batch.rows[0].get("source_id", ""))
        original_name = str(batch.rows[0].get("device_name", ""))
        self.assertTrue(first_source_id)
        self.assertTrue(original_name)

        SourceResolution.objects.create(
            profile=self.profile,
            source_id=first_source_id,
            source_column="device_name",
            original_value=original_name,
            resolved_fields={"device_name": "resolved-device-name"},
        )

        with open(fixture, "rb") as handle:
            setup = self.client.post(
                reverse("plugins:netbox_data_import:import_setup"),
                {"profile": self.profile.pk, "site": self.site.pk, "excel_file": handle},
            )
        self.assertEqual(setup.status_code, 302, setup.content[:300])

        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.models import SourceDocument

        stored = SourceDocument.objects.get(pk=self.client.session["import_context"]["source_document_id"])
        replanned = ImportEngine.plan(
            self.profile,
            stored,
            self.user,
            {"site_id": self.site.pk, "location_id": None, "tenant_id": None},
        )
        resolved = next(unit for unit in replanned.units if unit.display.get("source_id") == first_source_id)

        self.assertEqual(resolved.display["device_name"], "resolved-device-name")
        self.assertEqual(bytes(stored.content), original)


class PolicyWriteSerializationTest(TransactionTestCase):
    """A resolution write and an executing import serialize on the same profile row."""

    def setUp(self):
        """Create the profile whose policy rows the two sides contend for."""
        self.profile = _build_profile("Lock Profile")

    def _api(self, username):
        """Return an authenticated REST client, so a write reaches the lock through the real view."""
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user=get_user_model().objects.create_superuser(username, f"{username}@x.invalid", "p"))
        return api

    def _attempt_while_the_worker_holds_the_profile(self, write):
        """Run *write* while another connection holds the same profile row."""
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


class ProfileVanishedBeforeTheCreateLockTest(TransactionTestCase):
    """A profile deleted between validation and the lock is the 404 the request would have got."""

    def setUp(self):
        """Create the profile the request names and the operator who posts the resolution."""
        self.profile = _build_profile("Create Race Profile")
        self.user = get_user_model().objects.create_superuser("create-race", "c@example.invalid", "p")

    def test_the_api_answers_404(self):
        """The create path takes the same lock as the update path, so it owes the same answer."""
        from django.db import connection
        from rest_framework.test import APIClient

        deleted = []

        def delete_the_profile_when_the_lock_runs(execute, sql, params, many, context):
            # Stand in for a profile deleted between the serializer's read and the create lock.
            if not deleted and "FOR UPDATE" in sql and ImportProfile._meta.db_table in sql:
                deleted.append(True)

                def delete_it():
                    ImportProfile.objects.get(pk=self.profile.pk).delete()

                with run_on_separate_connection(delete_it):
                    pass
            return execute(sql, params, many, context)

        api = APIClient()
        api.force_authenticate(user=self.user)

        with connection.execute_wrapper(delete_the_profile_when_the_lock_runs):
            response = api.post(
                "/api/plugins/data-import/source-resolutions/",
                {
                    "profile": self.profile.pk,
                    "source_id": "CREATE-RACE-1",
                    "source_column": "device_name",
                    "original_value": "pristine",
                    "resolved_fields": {"device_name": "decision"},
                },
                format="json",
            )

        self.assertEqual(deleted, [True], "the locking statement never ran")
        self.assertEqual(response.status_code, 404, response.content[:300])
        self.assertFalse(SourceResolution.objects.filter(source_id="CREATE-RACE-1").exists())


class ConcurrentPartialUpdateTest(TransactionTestCase):
    """A PATCH holds the row it read, so the lock alone does not stop it overwriting a newer field."""

    def setUp(self):
        """Create the resolution two requests contend for."""
        self.profile = _build_profile("Partial Update Profile")
        self.resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="PATCH-1",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "first-decision"},
        )
        self.user = get_user_model().objects.create_superuser("patch-race", "p@example.invalid", "p")

    def test_a_field_the_request_did_not_send_keeps_the_newer_value(self):
        """The other request committed first, so its field must survive this one's save."""
        from django.db.models.signals import post_init
        from rest_framework.test import APIClient

        fired = []

        def change_another_field(sender, instance, **kwargs):
            """Run once, after the view read the row and before it takes the lock."""
            if fired or instance.pk != self.resolution.pk:
                return
            fired.append(True)

            def write_it():
                SourceResolution.objects.filter(pk=self.resolution.pk).update(original_value="from-the-other-request")

            with run_on_separate_connection(write_it):
                pass

        post_init.connect(change_another_field, sender=SourceResolution)
        self.addCleanup(post_init.disconnect, change_another_field, sender=SourceResolution)
        api = APIClient()
        api.force_authenticate(user=self.user)

        response = api.patch(
            f"/api/plugins/data-import/source-resolutions/{self.resolution.pk}/",
            {"resolved_fields": {"device_name": "second-decision"}},
            format="json",
        )

        self.assertEqual(fired, [True], "the other request never wrote, so the race did not happen")
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.resolution.refresh_from_db()
        self.assertEqual(self.resolution.resolved_fields, {"device_name": "second-decision"})
        self.assertEqual(self.resolution.original_value, "from-the-other-request")


class ConcurrentUpdateCollidesTest(TransactionTestCase):
    """Validation ran against the row as it was read, so the merged row is checked again."""

    def setUp(self):
        """Create the row under edit and the row its merged identity would collide with."""
        self.profile = _build_profile("Collide Profile")
        self.edited = SourceResolution.objects.create(
            profile=self.profile,
            source_id="COLLIDE-A",
            source_column="column-x",
            original_value="pristine",
            resolved_fields={"device_name": "first"},
        )
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="COLLIDE-B",
            source_column="column-y",
            original_value="pristine",
            resolved_fields={"device_name": "other"},
        )
        self.user = get_user_model().objects.create_superuser("collide", "x@example.invalid", "p")

    def test_a_merge_that_would_duplicate_another_row_is_refused(self):
        """(profile, source_id, source_column) is unique, and the merge can reach a taken triple."""
        from django.db.models.signals import post_init
        from rest_framework.test import APIClient

        fired = []

        def move_the_row(sender, instance, **kwargs):
            """Run once, after the view read the row and before it takes the lock."""
            if fired or instance.pk != self.edited.pk:
                return
            fired.append(True)

            def write_it():
                SourceResolution.objects.filter(pk=self.edited.pk).update(source_id="COLLIDE-B")

            with run_on_separate_connection(write_it):
                pass

        post_init.connect(move_the_row, sender=SourceResolution)
        self.addCleanup(post_init.disconnect, move_the_row, sender=SourceResolution)
        api = APIClient()
        api.force_authenticate(user=self.user)

        # Valid against the row as read: (COLLIDE-A, column-y) is free. The merge is not.
        response = api.patch(
            f"/api/plugins/data-import/source-resolutions/{self.edited.pk}/",
            {"source_column": "column-y"},
            format="json",
        )

        self.assertEqual(fired, [True], "the other request never wrote, so the race did not happen")
        self.assertEqual(response.status_code, 400, response.content[:300])
        self.edited.refresh_from_db()
        self.assertEqual(self.edited.source_column, "column-x")


class ConcurrentContactUpdateTest(TransactionTestCase):
    """The Contact candidate rules live in the serializer, so the merged row must meet them too."""

    def setUp(self):
        """Create a Contact candidate resolution the profile can still apply."""
        import json

        self.profile = _build_profile("Contact Merge Profile")
        ColumnMapping.objects.create(profile=self.profile, source_column="Owner", target_field="candidate:contact")
        ColumnMapping.objects.create(
            profile=self.profile, source_column="Owner Email", target_field="candidate:contact"
        )
        self.resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="CONTACT-1",
            source_column="candidate:contact",
            original_value=json.dumps({"Owner": "Ada", "Owner Email": "ada@example.invalid"}),
            resolved_fields={
                "contact_resolution_applied": True,
                "contact_field_sources": {"name": "Owner Email", "email": "Owner Email"},
            },
        )
        self.user = get_user_model().objects.create_superuser("contact-merge", "cm@example.invalid", "p")

    def test_a_selection_of_a_column_the_stored_row_lost_is_refused(self):
        """The other request dropped the column this one selects, so the merge cannot apply."""
        import json

        from django.db.models.signals import post_init
        from rest_framework.test import APIClient

        fired = []

        def drop_the_column(sender, instance, **kwargs):
            """Run once, after the view read the row and before it takes the lock."""
            if fired or instance.pk != self.resolution.pk:
                return
            fired.append(True)

            def write_it():
                SourceResolution.objects.filter(pk=self.resolution.pk).update(
                    original_value=json.dumps({"Owner Email": "ada@example.invalid"})
                )

            with run_on_separate_connection(write_it):
                pass

        post_init.connect(drop_the_column, sender=SourceResolution)
        self.addCleanup(post_init.disconnect, drop_the_column, sender=SourceResolution)
        api = APIClient()
        api.force_authenticate(user=self.user)

        # Valid against the row as read, whose candidate values still carry the Owner column.
        response = api.patch(
            f"/api/plugins/data-import/source-resolutions/{self.resolution.pk}/",
            {
                "resolved_fields": {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {"name": "Owner", "email": "Owner Email"},
                }
            },
            format="json",
        )

        self.assertEqual(fired, [True], "the other request never wrote, so the race did not happen")
        self.assertEqual(response.status_code, 400, response.content[:400])
        self.resolution.refresh_from_db()
        self.assertEqual(
            self.resolution.resolved_fields["contact_field_sources"], {"name": "Owner Email", "email": "Owner Email"}
        )


class ConcurrentProfileMoveTest(TransactionTestCase):
    """A saved resolution may not leave its profile, judged against the row as it now stands."""

    def setUp(self):
        """Create the resolution and the profile another request moves it to."""
        self.origin = _build_profile("Move Origin")
        self.destination = _build_profile("Move Destination")
        self.resolution = SourceResolution.objects.create(
            profile=self.origin,
            source_id="MOVE-1",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "first"},
        )
        self.user = get_user_model().objects.create_superuser("move-race", "m@example.invalid", "p")

    def test_a_request_naming_the_old_profile_cannot_take_the_row_back(self):
        """Naming the profile the row has left is the move the field check refuses."""
        from django.db.models.signals import post_init
        from rest_framework.test import APIClient

        fired = []

        def move_the_row(sender, instance, **kwargs):
            """Run once, after the view read the row and before it takes the lock."""
            if fired or instance.pk != self.resolution.pk:
                return
            fired.append(True)

            def write_it():
                SourceResolution.objects.filter(pk=self.resolution.pk).update(profile=self.destination)

            with run_on_separate_connection(write_it):
                pass

        post_init.connect(move_the_row, sender=SourceResolution)
        self.addCleanup(post_init.disconnect, move_the_row, sender=SourceResolution)
        api = APIClient()
        api.force_authenticate(user=self.user)

        # The request names the profile the row carried when it was read, and has since left.
        response = api.patch(
            f"/api/plugins/data-import/source-resolutions/{self.resolution.pk}/",
            {"profile": self.origin.pk, "resolved_fields": {"device_name": "second"}},
            format="json",
        )

        self.assertEqual(fired, [True], "the other request never wrote, so the race did not happen")
        self.assertEqual(response.status_code, 400, response.content[:300])
        self.resolution.refresh_from_db()
        self.assertEqual(self.resolution.profile_id, self.destination.pk)


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
        """The move commits between the view's read and its lock, so the read value is already stale."""
        from contextlib import ExitStack

        from django.db import connection
        from django.db.models.signals import post_init

        from netbox_data_import.models import locked_profile_policy

        started = Event()
        release = Event()
        fired = []
        stack = ExitStack()

        def hold_the_destination_like_the_worker():
            """Take the lock an import execution of the destination profile holds."""
            with locked_profile_policy(self.destination.pk):
                started.set()
                self.assertTrue(release.wait(timeout=10))

        def move_the_row_then_start_the_worker(sender, instance, **kwargs):
            """Run once, in the gap between the view reading the row and taking its lock."""
            if fired or instance.pk != self.resolution.pk:
                return
            fired.append(True)

            def move_it():
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
        """Connect a one-shot receiver that drops the row from another connection."""
        from django.db.models.signals import post_init

        fired = []

        def drop_it(sender, instance, **kwargs):
            if fired or instance.pk != self.resolution.pk:
                return
            fired.append(True)

            def delete_it():
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

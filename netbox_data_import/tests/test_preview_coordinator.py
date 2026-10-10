# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""One browser session has one active preview, and every preview command is ordered against it.

These tests drive real uploads, real pages and real HTTP commands. Each one posts the claim the page
rendered, so they hold whatever fields the claim carries.
"""

import threading
from html.parser import HTMLParser
from time import monotonic, sleep
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from netbox_data_import.models import (
    CableClassMapping,
    ClassRoleMapping,
    ColumnMapping,
    ExecutionOutcome,
    IgnoredDevice,
    ImportExecution,
    ImportProfile,
    SourceDocument,
)
from netbox_data_import.tests.helpers import preview_coordinator as _coordinator, trace_workbook_bytes, workbook_bytes
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path, patched_path

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _FormInputs(HTMLParser):
    """Collect the hidden preview inputs of the first form whose attributes match."""

    def __init__(self, wanted):
        super().__init__()
        self.wanted = wanted
        self.found = False
        self.inside = False
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form" and not self.found and all(values.get(key) == value for key, value in self.wanted.items()):
            self.found = self.inside = True
        elif tag == "input" and self.inside and (values.get("name") or "").startswith("preview_"):
            self.fields[values["name"]] = values.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


def page_claim(response, **form_attrs) -> dict:
    """Return the preview claim one rendered form carries, failing when the page has no such form."""
    parser = _FormInputs(form_attrs)
    parser.feed(response.content.decode())
    if not parser.found:
        raise AssertionError(f"the page renders no form with {form_attrs}")
    return parser.fields


def _blocked_by(pid) -> bool:
    """Return whether any backend waits on a lock that one backend holds."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))", [pid])
        return cursor.fetchone()[0] > 0


class _PausedRequest:
    """Run one HTTP request on its own connection and hold it at its first matching write."""

    def __init__(self, test, client, table, *args, **kwargs):
        self.test = test
        self.table = f'"{table}"'
        self.paused = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self.pid = None
        self.responses = []
        self.errors = []
        self.thread = threading.Thread(target=self._run, args=(client, args, kwargs), daemon=True)

    def _hold(self, execute, sql, params, many, context):
        if not self.paused.is_set() and sql.lstrip().startswith(("INSERT", "UPDATE")) and self.table in sql:
            self.paused.set()
            if not self.release.wait(timeout=20):
                raise AssertionError("the paused request was never released")
        return execute(sql, params, many, context)

    def _run(self, client, args, kwargs):
        try:
            connection.ensure_connection()
            self.pid = connection.connection.info.backend_pid
            with connection.execute_wrapper(self._hold):
                self.responses.append(client.post(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - the thread hands every failure to the test
            self.errors.append(exc)
        finally:
            self.done.set()
            connection.close()

    def start(self):
        self.thread.start()
        if not self.paused.wait(timeout=20):
            self.thread.join(timeout=20)
            raise AssertionError(f"the request never reached its write: {self.errors or self.responses}")

    def finish(self):
        self.release.set()
        self.thread.join(timeout=30)
        if self.errors:
            raise self.errors[0]
        return self.responses[0]


class _UnpublishedQueueRequest(_PausedRequest):
    """Hold the committed Job before NetBox's next on-commit callback publishes its RQ task."""

    def _hold(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.lstrip().startswith("INSERT") and self.table in sql:
            transaction.on_commit(self._pause_before_push)
        return result

    def _pause_before_push(self):
        self.paused.set()
        if not self.release.wait(timeout=20):
            raise AssertionError("the queue push was never released")


class _Concurrent:
    """Run one HTTP request on its own connection, for a rival of a paused request."""

    def __init__(self, client, *args, **kwargs):
        self.done = threading.Event()
        self.responses = []
        self.errors = []
        self.thread = threading.Thread(target=self._run, args=(client, args, kwargs), daemon=True)
        self.thread.start()

    def _run(self, client, args, kwargs):
        try:
            self.responses.append(client.post(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - the thread hands every failure to the test
            self.errors.append(exc)
        finally:
            self.done.set()
            connection.close()

    def wait_until_done_or_blocked_by(self, pid):
        """Return once this request finished or waits on a lock the paused request holds."""
        deadline = monotonic() + 20
        while monotonic() < deadline:
            if self.done.is_set() or _blocked_by(pid):
                return
            sleep(0.05)
        raise AssertionError("the rival request neither finished nor waited on the paused request")

    def result(self):
        self.thread.join(timeout=30)
        if self.errors:
            raise self.errors[0]
        return self.responses[0]


class _FlatPreviewMixin:
    """A flat Device profile, its import target, and an operator whose two tabs share one session."""

    def build_flat_profile(self):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        self.actor = get_user_model().objects.create_superuser("coordinator-operator", "op@example.invalid", "pw")
        self.client = Client()
        self.client.force_login(self.actor)
        self.site = Site.objects.create(name="Coordinator Site", slug="coordinator-site")
        manufacturer = Manufacturer.objects.create(name="Example", slug="example")
        DeviceType.objects.create(manufacturer=manufacturer, model="Model", slug="example-model", u_height=1)
        DeviceRole.objects.create(name="Server", slug="server")
        self.profile = ImportProfile.objects.create(
            name="Coordinator Profile", adapter_config={"sheet_name": "Data", "update_existing": True}
        )
        for source_column, target_field in (
            ("Source ID", "source_id"),
            ("Class", "device_class"),
            ("Name", "device_name"),
            ("Make", "make"),
            ("Model", "model"),
        ):
            ColumnMapping.objects.create(profile=self.profile, source_column=source_column, target_field=target_field)
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug="server")

    def second_tab(self):
        """Return a client that sends this operator's session cookie, as another tab does."""
        tab = Client()
        tab.cookies = self.client.cookies
        return tab

    def upload_flat(self, client, filename, *names):
        """Upload one flat workbook from the setup page, posting the claim that page rendered."""
        setup = client.get(reverse("plugins:netbox_data_import:import_setup"))
        rows = [[f"D-{index}", "Server", name, "Example", "Model"] for index, name in enumerate(names, start=1)]
        upload = SimpleUploadedFile(
            filename, workbook_bytes(["Source ID", "Class", "Name", "Make", "Model"], rows), content_type=XLSX
        )
        return client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {
                **page_claim(setup, enctype="multipart/form-data"),
                "profile": self.profile.pk,
                "site": self.site.pk,
                "excel_file": upload,
            },
        )


class StaleFlatCommandTest(IsolatedRQQueueTestMixin, _FlatPreviewMixin, TransactionTestCase):
    """A command made on a replaced preview is refused, and it writes nothing."""

    def setUp(self):
        super().setUp()
        self.build_flat_profile()

    def test_a_decision_from_a_replaced_preview_is_refused_without_a_write(self):
        self.upload_flat(self.client, "first.xlsx", "server-a", "server-b")
        page = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        claim = page_claim(page, action=reverse("plugins:netbox_data_import:ignore_device"))
        self.upload_flat(self.second_tab(), "second.xlsx", "server-c")

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_device"),
            {**claim, "profile_id": self.profile.pk, "source_id": "D-1", "device_name": "server-a"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(IgnoredDevice.objects.exists())
        self.assertContains(self.client.get(reverse("plugins:netbox_data_import:import_preview")), "server-c")

    def test_two_commands_with_one_claim_have_one_winner(self):
        self.upload_flat(self.client, "first.xlsx", "server-a", "server-b")
        page = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        claim = page_claim(page, action=reverse("plugins:netbox_data_import:ignore_device"))
        url = reverse("plugins:netbox_data_import:ignore_device")
        first = _PausedRequest(
            self,
            self.client,
            IgnoredDevice._meta.db_table,
            url,
            {**claim, "profile_id": self.profile.pk, "source_id": "D-1", "device_name": "server-a"},
        )
        first.start()
        try:
            rival = _Concurrent(
                self.second_tab(),
                url,
                {**claim, "profile_id": self.profile.pk, "source_id": "D-2", "device_name": "server-b"},
            )
            rival.wait_until_done_or_blocked_by(first.pid)
        finally:
            winner = first.finish()

        self.assertLess(winner.status_code, 400)
        self.assertEqual(rival.result().status_code, 409)
        self.assertEqual(list(IgnoredDevice.objects.values_list("source_id", flat=True)), ["D-1"])

    def test_a_profile_grant_revoked_while_the_command_waits_is_rechecked(self):
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        from netbox_data_import.models import locked_profile_policy

        self.upload_flat(self.client, "first.xlsx", "server-a")
        claim = page_claim(
            self.client.get(reverse("plugins:netbox_data_import:import_preview")),
            action=reverse("plugins:netbox_data_import:ignore_device"),
        )
        before = _coordinator(self.client)
        self.actor.is_superuser = False
        self.actor.save(update_fields=["is_superuser"])
        profile_grant = ObjectPermission.objects.create(name="Preview profile grant", actions=["view", "change"])
        profile_grant.object_types.add(ContentType.objects.get_for_model(ImportProfile))
        profile_grant.users.add(self.actor)
        other_grant = ObjectPermission.objects.create(name="Preview target grants", actions=["view", "add", "change"])
        other_grant.object_types.add(
            ContentType.objects.get_for_model(IgnoredDevice), ContentType.objects.get_for_model(Site)
        )
        other_grant.users.add(self.actor)
        rival = None
        try:
            with locked_profile_policy(self.profile.pk):
                pid = connection.connection.info.backend_pid
                rival = _Concurrent(
                    self.second_tab(),
                    reverse("plugins:netbox_data_import:ignore_device"),
                    {**claim, "source_id": "D-1", "device_name": "server-a"},
                    HTTP_ACCEPT="application/json",
                )
                rival.wait_until_done_or_blocked_by(pid)
                self.assertFalse(rival.done.is_set(), "the command never waited on the profile lock")
                profile_grant.actions = ["view"]
                profile_grant.save(update_fields=["actions"])

        finally:
            if rival is not None:
                response = rival.result()
        self.assertEqual(response.status_code, 404, response.content)
        self.assertFalse(IgnoredDevice.objects.exists())
        after = _coordinator(self.client)
        self.assertEqual((after.revision, after.plan), (before.revision, before.plan))

    def _assert_user_state_rechecked(self, field):
        from netbox_data_import.models import locked_profile_policy

        self.upload_flat(self.client, "first.xlsx", "server-a")
        claim = page_claim(
            self.client.get(reverse("plugins:netbox_data_import:import_preview")),
            action=reverse("plugins:netbox_data_import:ignore_device"),
        )
        before = _coordinator(self.client)
        rival = None
        try:
            with locked_profile_policy(self.profile.pk):
                pid = connection.connection.info.backend_pid
                rival = _Concurrent(
                    self.second_tab(),
                    reverse("plugins:netbox_data_import:ignore_device"),
                    {**claim, "source_id": "D-1", "device_name": "server-a"},
                    HTTP_ACCEPT="application/json",
                )
                rival.wait_until_done_or_blocked_by(pid)
                self.assertFalse(rival.done.is_set(), "the command never waited on the profile lock")
                get_user_model().objects.filter(pk=self.actor.pk).update(**{field: False})
        finally:
            if rival is not None:
                response = rival.result()
        self.assertGreaterEqual(response.status_code, 400, response.content)
        self.assertFalse(IgnoredDevice.objects.exists())
        after = _coordinator(self.client)
        self.assertEqual((after.revision, after.plan), (before.revision, before.plan))

    def test_superuser_revocation_while_the_command_waits_is_rechecked(self):
        self._assert_user_state_rechecked("is_superuser")

    def test_user_deactivation_while_the_command_waits_is_rechecked(self):
        self._assert_user_state_rechecked("is_active")

    def test_role_creation_consumes_the_claim_without_replacing_the_plan(self):
        from dcim.models import DeviceRole

        self.upload_flat(self.client, "first.xlsx", "server-a")
        claim = page_claim(
            self.client.get(reverse("plugins:netbox_data_import:import_preview")),
            action=reverse("plugins:netbox_data_import:ignore_device"),
        )
        before = _coordinator(self.client)
        url = reverse("plugins:netbox_data_import:quick_create_role")
        first = self.client.post(
            url, {**claim, "name": "First role", "slug": "first-role"}, HTTP_ACCEPT="application/json"
        )
        second = self.client.post(
            url, {**claim, "name": "Second role", "slug": "second-role"}, HTTP_ACCEPT="application/json"
        )

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual((second.status_code, second.json().get("code")), (409, "preview_stale"))
        self.assertTrue(DeviceRole.objects.filter(slug="first-role").exists())
        self.assertFalse(DeviceRole.objects.filter(slug="second-role").exists())
        after = _coordinator(self.client)
        self.assertEqual(after.revision, before.revision + 1)
        self.assertEqual(after.plan, before.plan)

    def test_an_old_results_page_leaves_the_newer_preview_alone(self):
        from core.models import Job

        self.upload_flat(self.client, "first.xlsx", "server-a")
        page = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.client.post(
            reverse("plugins:netbox_data_import:import_run"),
            page_claim(page, action=reverse("plugins:netbox_data_import:import_run")),
        )
        job = Job.objects.get(data__job_type="netbox_data_import.import")
        execution = ImportExecution.objects.create(
            profile=self.profile,
            source_document=SourceDocument.objects.get(profile=self.profile, filename="first.xlsx"),
            actor=self.actor,
            outcome=ExecutionOutcome.SUCCEEDED,
        )
        Job.objects.filter(pk=job.pk).update(status="completed", data={**job.data, "import_execution_id": execution.pk})
        status = self.client.get(
            reverse("plugins:netbox_data_import:import_progress_status", kwargs={"pk": job.pk}),
            HTTP_HX_REQUEST="true",
        )
        old_results_url = status.headers["HX-Redirect"]
        self.upload_flat(self.client, "second.xlsx", "server-c")

        results = self.second_tab().get(old_results_url)

        self.assertEqual(results.context["execution"], execution)
        preview = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "server-c")

    def test_a_completed_job_without_results_offers_a_new_import(self):
        from core.models import Job

        self.upload_flat(self.client, "first.xlsx", "server-a")
        page = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.client.post(
            reverse("plugins:netbox_data_import:import_run"),
            page_claim(page, action=reverse("plugins:netbox_data_import:import_run")),
        )
        self.run_rq_jobs()
        job = Job.objects.get(data__job_type="netbox_data_import.import")
        self.assertEqual(job.status, "completed")
        progress_url = reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk})
        self.assertContains(self.client.get(progress_url), "View results")
        data = dict(job.data)
        data.pop("import_execution_id")
        Job.objects.filter(pk=job.pk).update(data=data)

        for view in ("import_progress", "import_progress_status"):
            with self.subTest(view=view):
                response = self.client.get(reverse(f"plugins:netbox_data_import:{view}", kwargs={"pk": job.pk}))
                self.assertContains(response, "Import complete.")
                self.assertContains(response, "Start a new import")
                self.assertContains(response, reverse("plugins:netbox_data_import:import_setup"))
                self.assertNotContains(response, "This page updates automatically.")
                self.assertNotContains(response, "mdi-spin")
                self.assertNotContains(response, "View results")
                self.assertNotContains(response, 'hx-trigger="every 2s"')


class LateSessionSaveTest(IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """An older response that saves its session last cannot bring its preview back."""

    def setUp(self):
        super().setUp()
        self.build_topology()
        self.client.force_login(self.actor)

    def _upload(self, client, path_block, filename):
        setup = client.get(reverse("plugins:netbox_data_import:import_setup"))
        upload = SimpleUploadedFile(filename, trace_workbook_bytes(path_blocks=(path_block,)), content_type=XLSX)
        return client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {
                **page_claim(setup, enctype="multipart/form-data"),
                "profile": self.profile.pk,
                "site": self.site.pk,
                "excel_file": upload,
            },
        )

    def _decide_while_a_setup_runs(self, pause_table):
        """Hold one cable policy decision at its first write to a table while the other tab uploads."""
        self._upload(self.client, direct_path(), "direct.xlsx")
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        claim = page_claim(workspace, action=reverse("plugins:netbox_data_import:trace_cable_policy"))
        identity = workspace.context["selected_trace"].identity
        tab = Client()
        tab.cookies = self.client.cookies
        decision = _PausedRequest(
            self,
            self.client,
            pause_table,
            reverse("plugins:netbox_data_import:trace_cable_policy"),
            {
                **claim,
                "trace": identity,
                "cable_class": "Patch",
                "cable_type": "mmf-om4",
                "cable_profile": "single-1c1p",
            },
        )
        decision.start()
        try:
            setup = _Concurrent(tab, *self._setup_args(tab))
            setup.wait_until_done_or_blocked_by(decision.pid)
        finally:
            decision.finish()
        setup.result()
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def test_a_decision_that_locks_first_commits_and_the_setup_replaces_it(self):
        reread = self._decide_while_a_setup_runs(CableClassMapping._meta.db_table)

        self.assertEqual(reread.status_code, 200)
        # The patched trace states three segments, and the direct one states one.
        self.assertEqual(len(reread.context["selected_trace"].segments), 3)
        self.assertEqual(CableClassMapping.objects.get(profile=self.profile, cable_class="Patch").cable_type, "mmf-om4")

    @override_settings(SESSION_SAVE_EVERY_REQUEST=True)
    def test_a_decision_that_saves_its_session_last_cannot_restore_its_preview(self):
        """NetBox saves the session on every request when LOGIN_PERSISTENCE is on, after the view returned."""
        reread = self._decide_while_a_setup_runs("django_session")

        self.assertEqual(reread.status_code, 200)
        self.assertEqual(len(reread.context["selected_trace"].segments), 3)

    def _setup_args(self, client):
        setup = client.get(reverse("plugins:netbox_data_import:import_setup"))
        upload = SimpleUploadedFile(
            "patched.xlsx", trace_workbook_bytes(path_blocks=(patched_path(),)), content_type=XLSX
        )
        return (
            reverse("plugins:netbox_data_import:import_setup"),
            {
                **page_claim(setup, enctype="multipart/form-data"),
                "profile": self.profile.pk,
                "site": self.site.pk,
                "excel_file": upload,
            },
        )


def _session_request(client, user, method="post"):
    """Return a request that carries this client's server-side session, as the client's next request would."""
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    request = getattr(RequestFactory(), method)("/")
    request.user = user
    request.session = SessionStore(session_key=client.session.session_key)
    return request


class CoordinatorContractTest(IsolatedRQQueueTestMixin, _FlatPreviewMixin, TransactionTestCase):
    """The claim rules, the bootstrap, the session binding and the payload lifetime."""

    def setUp(self):
        super().setUp()
        self.build_flat_profile()

    def ignore(self, claim, source_id="D-1", **headers):
        return self.client.post(
            reverse("plugins:netbox_data_import:ignore_device"),
            {**claim, "profile_id": self.profile.pk, "source_id": source_id, "device_name": source_id},
            **headers,
        )

    def preview_claim(self):
        page = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        return page_claim(page, action=reverse("plugins:netbox_data_import:ignore_device"))

    def test_concurrent_first_visits_leave_one_row(self):
        from netbox_data_import.models import PreviewCoordinator
        from netbox_data_import.preview_coordinator import setup_claim

        claims = []
        paused, release = threading.Event(), threading.Event()

        def hold_the_insert(execute, sql, params, many, context):
            if sql.lstrip().startswith("INSERT") and PreviewCoordinator._meta.db_table in sql and not paused.is_set():
                paused.set()
                release.wait(timeout=20)
            return execute(sql, params, many, context)

        def first_visit():
            try:
                with connection.execute_wrapper(hold_the_insert):
                    claims.append(setup_claim(_session_request(self.client, self.actor, "get")))
            finally:
                connection.close()

        thread = threading.Thread(target=first_visit, daemon=True)
        thread.start()
        self.assertTrue(paused.wait(timeout=20), "the first visit never reached its insert")
        try:
            claims.append(setup_claim(_session_request(self.client, self.actor, "get")))
        finally:
            release.set()
            thread.join(timeout=20)

        self.assertEqual(PreviewCoordinator.objects.count(), 1)
        self.assertEqual(claims[0], claims[1])

    def test_a_command_never_creates_a_row(self):
        from netbox_data_import.models import PreviewCoordinator

        response = self.ignore(
            {"preview_token": "A" * 43, "preview_revision": "1", "preview_document": "", "preview_profile": ""}
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(PreviewCoordinator.objects.exists())
        self.assertFalse(IgnoredDevice.objects.exists())

    def test_every_claim_field_must_match(self):
        self.upload_flat(self.client, "first.xlsx", "server-a", "server-b")
        claim = self.preview_claim()
        tampered = {
            "preview_token": "B" * 43,
            "preview_revision": str(int(claim["preview_revision"]) + 1),
            "preview_document": str(int(claim["preview_document"]) + 1),
            "preview_profile": str(int(claim["preview_profile"]) + 1),
        }
        for name, value in tampered.items():
            with self.subTest(field=name):
                self.assertEqual(self.ignore({**claim, name: value}).status_code, 409)
        for name in claim:
            with self.subTest(missing=name):
                self.assertEqual(
                    self.ignore({key: value for key, value in claim.items() if key != name}).status_code, 409
                )

        self.assertFalse(IgnoredDevice.objects.exists())
        self.assertLess(self.ignore(claim).status_code, 400)

    def test_setup_replaces_any_revision_of_its_generation_but_never_a_newer_one(self):
        self.upload_flat(self.client, "first.xlsx", "server-a", "server-b")
        setup_page = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        stale_setup_claim = page_claim(setup_page, enctype="multipart/form-data")
        self.assertLess(self.ignore(self.preview_claim()).status_code, 400)

        def upload(filename, name):
            upload = SimpleUploadedFile(
                filename,
                workbook_bytes(
                    ["Source ID", "Class", "Name", "Make", "Model"], [["D-9", "Server", name, "Example", "Model"]]
                ),
                content_type=XLSX,
            )
            return self.client.post(
                reverse("plugins:netbox_data_import:import_setup"),
                {**stale_setup_claim, "profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            )

        winner = upload("second.xlsx", "server-c")
        loser = upload("third.xlsx", "server-d")

        self.assertEqual(winner.status_code, 302)
        self.assertEqual(loser.status_code, 409)
        preview = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertContains(preview, "server-c")
        self.assertNotContains(preview, "server-d")
        self.assertFalse(SourceDocument.objects.filter(filename="third.xlsx").exists())

    def test_a_rotated_session_reaches_no_earlier_preview(self):
        self.upload_flat(self.client, "first.xlsx", "server-a")
        claim = self.preview_claim()

        # Logging in again with the same user keeps the key, so the session ends first.
        self.client.logout()
        self.client.force_login(self.actor)

        self.assertEqual(self.ignore(claim).status_code, 409)
        self.assertRedirects(
            self.client.get(reverse("plugins:netbox_data_import:import_preview")),
            reverse("plugins:netbox_data_import:import_setup"),
            fetch_redirect_response=False,
        )

    def test_an_expired_preview_refuses_and_drops_its_payload(self):
        from datetime import timedelta

        from django.utils import timezone

        from netbox_data_import.models import PreviewState

        self.upload_flat(self.client, "first.xlsx", "server-a")
        claim = self.preview_claim()
        row = _coordinator(self.client)
        type(row).objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.ignore(claim)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(IgnoredDevice.objects.exists())
        row.refresh_from_db()
        self.assertEqual(row.state, PreviewState.EXPIRED)
        self.assertIsNone(row.plan)
        self.assertNotEqual(row.preview_token, claim["preview_token"])
        self.assertEqual(row.revision, int(claim["preview_revision"]) + 1)

    def test_housekeeping_expires_payloads_and_deletes_long_expired_rows(self):
        from datetime import timedelta

        from django.utils import timezone

        from netbox_data_import.models import PreviewCoordinator, PreviewState
        from netbox_data_import.preview_coordinator import expire_previews

        now = timezone.now()
        self.upload_flat(self.client, "first.xlsx", "server-a")
        live = _coordinator(self.client)
        expiring = PreviewCoordinator.objects.create(
            session_binding="e" * 64,
            owner=self.actor,
            preview_token="T" * 43,
            state=PreviewState.READY,
            profile_id=live.profile_id,
            source_document_id=live.source_document_id,
            context=live.context,
            plan=live.plan,
            expires_at=now - timedelta(minutes=1),
        )
        long_gone = PreviewCoordinator.objects.create(
            session_binding="g" * 64,
            owner=self.actor,
            preview_token="G" * 43,
            state=PreviewState.EMPTY,
            expires_at=now - PreviewCoordinator.PAYLOAD_LIFETIME - timedelta(minutes=1),
        )

        self.assertEqual(expire_previews(now=now), (1, 1))

        expiring.refresh_from_db()
        self.assertEqual(expiring.state, PreviewState.EXPIRED)
        self.assertIsNone(expiring.plan)
        self.assertNotEqual(expiring.preview_token, "T" * 43)
        self.assertFalse(PreviewCoordinator.objects.filter(pk=long_gone.pk).exists())
        live.refresh_from_db()
        self.assertEqual(live.state, PreviewState.READY)
        with self.assertRaises(ValueError):
            expire_previews(now=now.replace(tzinfo=None))

    def test_a_plan_too_large_to_store_rolls_the_decision_back(self):
        from netbox_data_import.tests.plugins_config import override_plugins_config

        self.upload_flat(self.client, "first.xlsx", "server-a")
        claim = self.preview_claim()
        before = _coordinator(self.client)

        with override_plugins_config(netbox_data_import={"preview_max_plan_bytes": 16}):
            response = self.ignore(claim, HTTP_ACCEPT="application/json")

        self.assertEqual(response.status_code, 413)
        self.assertFalse(IgnoredDevice.objects.exists())
        self.assertFalse(ImportExecution.objects.exists())
        after = _coordinator(self.client)
        self.assertEqual((after.revision, after.plan), (before.revision, before.plan))


def _database_effects():
    """Return every plugin row, every Device and role, and every Job, to compare across a refused command."""
    from core.models import Job
    from dcim.models import Device, DeviceRole
    from django.apps import apps

    models = [*apps.get_app_config("netbox_data_import").get_models(), Device, DeviceRole, Job]
    return {model._meta.label: sorted(map(repr, model.objects.order_by("pk").values().iterator())) for model in models}


# Each preview command with a body it would accept on the preview it names, keyed by route name.
FLAT_COMMANDS = {
    "import_run": {},
    "preview_reread": {"next": "/"},
    "preview_discard": {},
    "ignore_device": {"source_id": "D-1", "device_name": "server-a"},
    "unignore_device": {"source_id": "D-1"},
    "ignore_field_difference": {"row_number": "2", "target_field": "serial"},
    "unignore_field_difference": {"row_number": "2", "target_field": "serial"},
    "sync_device_field": {"field": "serial", "row_number": "2"},
    "sync_placement": {"row_number": "2"},
    "save_resolution": {
        "source_id": "D-1",
        "source_column": "device_name",
        "original_value": "server-a",
        "resolved_fields": '{"device_name": "server-z"}',
    },
    "resolve_duplicate_name": {"row_number": "2", "source_id": "D-1", "new_name": "server-z"},
    "ignore_duplicate_serial": {"row_number": "2", "source_id": "D-1"},
    "ignore_position": {"row_number": "2", "source_id": "D-1"},
    "quick_resolve_manufacturer": {"source_make": "Example", "netbox_mfg_slug": "example"},
    "quick_resolve_device_type": {"source_make": "Example", "source_model": "Model"},
    "quick_add_class_mapping": {"source_class": "Server", "mapping_action": "ignore"},
    "quick_add_column_mapping": {"source_column": "Name", "target_field": "device_name"},
    "quick_create_role": {"name": "Stale Role", "slug": "stale-role"},
    "match_existing_device": {"source_id": "D-1", "netbox_device_id": "1"},
    "auto_match_devices": {},
    "sync_single_row": {"row_number": "2"},
    "unlink_device": {"source_id": "D-1"},
}
TRACE_COMMANDS = {
    "trace_sync": {"identity": "trace"},
    "trace_sync_all": {},
    "trace_sync_cancel": {"job_id": "1"},
    "trace_cable_policy": {"cable_class": "Patch", "cable_type": "mmf-om4", "cable_profile": "single-1c1p"},
    "trace_segment_policy": {"segment": "0", "cable_type": "mmf-om4", "cable_profile": "single-1c1p"},
    "trace_segment_policy:clear": {"segment": "0", "clear": "1"},
    "trace_resolve_termination": {"field_key": "key", "object_type": "dcim.interface", "object_id": "1"},
    "trace_resolve_device": {"device_key": "DEV-A", "device_id": "1"},
    "trace_location_mapping": {"location_key": "ROOM 1", "location_id": "1"},
    "trace_location_mapping:clear": {"location_key": "ROOM 1", "clear": "1"},
    "trace_request_proposal": {"field_key": "key"},
    "trace_request_all_proposals": {},
    "trace_cancel_proposal": {"proposal_id": "1"},
    "trace_accept_proposal": {"proposal_id": "1"},
    "trace_reject_proposal": {"proposal_id": "1"},
}
CLAIMED_READS = (
    "contact_suggestion",
    "trace_termination_candidates",
    "trace_device_candidates",
    "trace_location_candidates",
    "trace_proposal",
    "trace_sync_status",
)
FORMATS = {
    "html": {},
    "json": {"HTTP_ACCEPT": "application/json"},
    "htmx": {"HTTP_HX_REQUEST": "true"},
}


class StaleClaimMatrixTest(IsolatedRQQueueTestMixin, _FlatPreviewMixin, CableTopologyMixin, TransactionTestCase):
    """Every preview command and every claimed read refuses the claim of a replaced preview, writing nothing."""

    def setUp(self):
        super().setUp()
        self.build_flat_profile()
        self.build_topology()

    def _replaced_claim(self, upload):
        """Open one preview, keep its page claim, then let the other tab replace it."""
        from netbox_data_import.tests.helpers import preview_claim

        upload(self.client, "first")
        claim = preview_claim(self.client)
        upload(self.second_tab(), "second")
        return claim

    def _upload_flat(self, client, name):
        self.upload_flat(client, f"{name}.xlsx", "server-a", "server-b")

    def _upload_trace(self, client, name):
        setup = client.get(reverse("plugins:netbox_data_import:import_setup"))
        upload = SimpleUploadedFile(
            f"{name}.xlsx", trace_workbook_bytes(path_blocks=(direct_path(),)), content_type=XLSX
        )
        client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {
                **page_claim(setup, enctype="multipart/form-data"),
                "profile": self.profile.pk,
                "site": self.site.pk,
                "excel_file": upload,
            },
        )

    def _assert_refused(self, commands, claim, *, trace):
        for key, body in commands.items():
            route = key.split(":")[0]
            for fmt, headers in FORMATS.items():
                with self.subTest(route=key, format=fmt):
                    before = _database_effects()
                    data = {**claim, **body, **({"trace": "trace"} if trace else {})}
                    response = self.client.post(reverse(f"plugins:netbox_data_import:{route}"), data, **headers)
                    self.assertEqual(response.status_code, 409, response.content[:300])
                    if fmt == "json":
                        self.assertEqual(response.json()["ok"], False)
                    self.assertEqual(_database_effects(), before)

    def test_every_flat_command_refuses_a_replaced_preview(self):
        claim = self._replaced_claim(self._upload_flat)
        self._assert_refused(FLAT_COMMANDS, claim, trace=False)

    def test_every_trace_command_refuses_a_replaced_preview(self):
        self.profile = ImportProfile.objects.get(name="Cable Traces")
        claim = self._replaced_claim(self._upload_trace)
        self._assert_refused(TRACE_COMMANDS, claim, trace=True)

    def test_restoring_a_failed_import_refuses_a_replaced_preview(self):
        from core.models import Job

        claim = self._replaced_claim(self._upload_flat)
        job = Job.objects.create(
            name="Data Import",
            user=self.actor,
            status="failed",
            job_id=uuid4(),
            data={"job_type": "netbox_data_import.import"},
        )
        before = _database_effects()

        response = self.client.post(reverse("plugins:netbox_data_import:import_restore", kwargs={"pk": job.pk}), claim)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(_database_effects(), before)

    def test_every_claimed_read_refuses_a_replaced_preview(self):
        claim = self._replaced_claim(self._upload_flat)
        for route in CLAIMED_READS:
            with self.subTest(route=route):
                response = self.client.get(
                    reverse(f"plugins:netbox_data_import:{route}"),
                    {**claim, "source_id": "D-1", "field_key": "key", "device_key": "DEV-A", "location_key": "ROOM 1"},
                    HTTP_ACCEPT="application/json",
                )
                self.assertEqual(response.status_code, 409)

    def test_the_matrix_covers_every_command_route(self):
        """A new preview command must join the matrix, so its stale claim is proved refused."""
        from netbox_data_import.tests.test_preview_state_ownership import CLAIMED_READS as INVENTORY_READS
        from netbox_data_import.tests.test_preview_state_ownership import COMMANDS

        covered = {key.split(":")[0] for key in (*FLAT_COMMANDS, *TRACE_COMMANDS)} | {"import_setup", "import_restore"}
        self.assertEqual(covered, set(COMMANDS))
        self.assertEqual(set(CLAIMED_READS), set(INVENTORY_READS))


class CommandOrderingTest(IsolatedRQQueueTestMixin, _FlatPreviewMixin, TransactionTestCase):
    """Two commands on one claim serialize on the coordinator row: the waiter is refused and writes nothing."""

    def setUp(self):
        super().setUp()
        self.build_flat_profile()
        self.upload_flat(self.client, "first.xlsx", "server-a", "server-b")

    def _race(self, first_url, first_body, first_table, rival_url, rival_body):
        from netbox_data_import.tests.helpers import preview_claim

        claim = preview_claim(self.client)
        first = _PausedRequest(self, self.client, first_table, first_url, {**claim, **first_body})
        first.start()
        try:
            rival = _Concurrent(self.second_tab(), rival_url, {**claim, **rival_body})
            rival.wait_until_done_or_blocked_by(first.pid)
            self.assertFalse(rival.done.is_set(), "the rival finished while the first command held the coordinator")
        finally:
            winner = first.finish()
        return winner, rival.result()

    def test_a_reread_waiting_on_a_decision_is_refused_and_replaces_nothing(self):
        from netbox_data_import.tests.helpers import stored_plan

        winner, rival = self._race(
            reverse("plugins:netbox_data_import:ignore_device"),
            {"source_id": "D-1", "device_name": "server-a"},
            IgnoredDevice._meta.db_table,
            reverse("plugins:netbox_data_import:preview_reread"),
            {"next": "/"},
        )

        self.assertLess(winner.status_code, 400)
        self.assertEqual(rival.status_code, 409)
        ignored = next(unit for unit in stored_plan(self.client)["units"] if "D-1" in unit["identity"])
        self.assertEqual(ignored["disposition"], "excluded")

    def test_a_decision_waiting_on_a_reread_is_refused_without_a_write(self):
        from netbox_data_import.models import PreviewCoordinator

        winner, rival = self._race(
            reverse("plugins:netbox_data_import:preview_reread"),
            {"next": "/"},
            PreviewCoordinator._meta.db_table,
            reverse("plugins:netbox_data_import:ignore_device"),
            {"source_id": "D-1", "device_name": "server-a"},
        )

        self.assertLess(winner.status_code, 400)
        self.assertEqual(rival.status_code, 409)
        self.assertFalse(IgnoredDevice.objects.exists())

    def test_a_decision_waiting_on_a_discard_is_refused_without_a_write(self):
        from netbox_data_import.models import PreviewCoordinator, PreviewState
        from netbox_data_import.tests.helpers import preview_coordinator

        winner, rival = self._race(
            reverse("plugins:netbox_data_import:preview_discard"),
            {},
            PreviewCoordinator._meta.db_table,
            reverse("plugins:netbox_data_import:ignore_device"),
            {"source_id": "D-1", "device_name": "server-a"},
        )

        self.assertLess(winner.status_code, 400)
        self.assertEqual(rival.status_code, 409)
        self.assertFalse(IgnoredDevice.objects.exists())
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.EMPTY)

    def test_housekeeping_skips_a_preview_a_command_holds(self):
        from datetime import timedelta

        from django.utils import timezone

        from netbox_data_import.preview_coordinator import expire_previews
        from netbox_data_import.tests.helpers import preview_claim, preview_coordinator

        decision = _PausedRequest(
            self,
            self.client,
            IgnoredDevice._meta.db_table,
            reverse("plugins:netbox_data_import:ignore_device"),
            {**preview_claim(self.client), "source_id": "D-1", "device_name": "server-a"},
        )
        decision.start()
        try:
            expired = expire_previews(now=timezone.now() + timedelta(days=60))
        finally:
            response = decision.finish()

        self.assertEqual(expired[0], 0)
        self.assertLess(response.status_code, 400)
        self.assertEqual(preview_coordinator(self.client).state, "ready")

    def test_a_purged_source_refuses_the_command_with_no_write(self):
        from netbox_data_import.tests.helpers import preview_claim

        claim = preview_claim(self.client)
        SourceDocument.objects.filter(filename="first.xlsx").delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:ignore_device"),
            {**claim, "source_id": "D-1", "device_name": "server-a"},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(IgnoredDevice.objects.exists())

    def test_restore_does_not_fail_a_final_job_before_its_queue_push(self):
        from core.choices import JobStatusChoices
        from core.models import Job
        from dcim.models import Device
        from django_rq import get_queue
        from netbox_data_import.models import PreviewState
        from netbox_data_import.tests.helpers import preview_claim

        queued = _UnpublishedQueueRequest(
            self,
            self.client,
            Job._meta.db_table,
            reverse("plugins:netbox_data_import:import_run"),
            preview_claim(self.client),
        )
        queued.start()
        try:
            job = Job.objects.get(data__job_type="netbox_data_import.import")
            self.assertIsNone(get_queue(job.queue_name).fetch_job(str(job.job_id)))
            before = _coordinator(self.client)
            refused = self.second_tab().post(
                reverse("plugins:netbox_data_import:import_restore", kwargs={"pk": job.pk}),
                preview_claim(self.client),
            )
            self.assertEqual(refused.status_code, 409)
            job.refresh_from_db()
            self.assertEqual(job.status, JobStatusChoices.STATUS_PENDING)
            self.assertEqual(_coordinator(self.client).revision, before.revision)
            self.assertEqual(_coordinator(self.client).state, PreviewState.SUBMITTED)
        finally:
            response = queued.finish()
        self.assertEqual(response.status_code, 302)
        self.run_rq_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.error)
        self.assertEqual(set(Device.objects.values_list("name", flat=True)), {"server-a", "server-b"})
        self.assertEqual(ImportExecution.objects.get(job=job).outcome, ExecutionOutcome.SUCCEEDED)


class TraceQueueOrderingTest(IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """Queue publication and Device resolution preserve one ordered preview."""

    def setUp(self):
        super().setUp()
        self.build_topology()
        self.client.force_login(self.actor)
        from netbox_data_import.tests.helpers import upload_preview

        upload = SimpleUploadedFile(
            "direct.xlsx", trace_workbook_bytes(path_blocks=(direct_path(),)), content_type=XLSX
        )
        upload_preview(self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.identity = workspace.context["selected_trace"].identity

    def second_tab(self):
        tab = Client()
        tab.cookies = self.client.cookies.copy()
        return tab

    def test_missing_task_recovery_distinguishes_job_age_and_status(self):
        from datetime import timedelta
        from unittest.mock import patch
        from core.choices import JobStatusChoices
        from core.models import Job
        from django.utils import timezone
        from netbox_data_import.jobs import ImportJobRunner, import_job_abandoned

        now = timezone.now()
        job = Job.objects.create(name=ImportJobRunner.name, user=self.actor, job_id=uuid4(), queue_name="default")
        cases = (
            (JobStatusChoices.STATUS_PENDING, 0, False),
            (JobStatusChoices.STATUS_PENDING, 59, False),
            (JobStatusChoices.STATUS_PENDING, 60, True),
            (JobStatusChoices.STATUS_PENDING, 61, True),
            (JobStatusChoices.STATUS_RUNNING, 0, True),
            (JobStatusChoices.STATUS_COMPLETED, 61, False),
        )
        for status, seconds, abandoned in cases:
            with self.subTest(status=status, seconds=seconds):
                Job.objects.filter(pk=job.pk).update(status=status, created=now - timedelta(seconds=seconds))
                job.refresh_from_db()
                with patch("netbox_data_import.jobs.timezone.now", autospec=True, return_value=now):
                    self.assertEqual(import_job_abandoned(job), abandoned)

    def test_reread_does_not_fail_a_retained_sync_before_its_queue_push(self):
        from core.choices import JobStatusChoices
        from core.models import Job
        from dcim.models import Cable
        from django_rq import get_queue
        from netbox_data_import.models import PreviewState
        from netbox_data_import.preview_coordinator import SYNC_QUEUED
        from netbox_data_import.tests.helpers import preview_claim

        queued = _UnpublishedQueueRequest(
            self,
            self.client,
            Job._meta.db_table,
            reverse("plugins:netbox_data_import:trace_sync"),
            {**preview_claim(self.client), "identity": self.identity},
        )
        queued.start()
        try:
            job = Job.objects.get(data__job_type="netbox_data_import.import")
            self.assertIsNone(get_queue(job.queue_name).fetch_job(str(job.job_id)))
            before = _coordinator(self.client)
            refused = self.second_tab().post(
                reverse("plugins:netbox_data_import:preview_reread"),
                preview_claim(self.client),
            )
            self.assertContains(refused, SYNC_QUEUED, status_code=409)
            job.refresh_from_db()
            self.assertEqual(job.status, JobStatusChoices.STATUS_PENDING)
            self.assertEqual(_coordinator(self.client).revision, before.revision)
            self.assertEqual(_coordinator(self.client).state, PreviewState.SYNC_PENDING)
        finally:
            response = queued.finish()
        self.assertEqual(response.status_code, 302)
        self.run_rq_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.error)
        self.assertEqual(Cable.objects.count(), 1)
        self.assertEqual(ImportExecution.objects.get(job=job).outcome, ExecutionOutcome.SUCCEEDED)

    def _race_resolution_and_sync(self, *, queue_first):
        from core.models import Job
        from netbox_data_import.models import TraceDeviceResolution
        from netbox_data_import.tests.helpers import preview_claim

        claim = preview_claim(self.client)
        resolution = (
            reverse("plugins:netbox_data_import:trace_resolve_device"),
            {**claim, "device_key": "DEV-A", "device_id": self.device_a.pk, "search": "DEV-A"},
            TraceDeviceResolution._meta.db_table,
        )
        sync = (
            reverse("plugins:netbox_data_import:trace_sync"),
            {**claim, "identity": self.identity},
            Job._meta.db_table,
        )
        first_args, rival_args = (sync, resolution) if queue_first else (resolution, sync)
        first = _PausedRequest(self, self.client, first_args[2], first_args[0], first_args[1])
        first.start()
        try:
            rival = _Concurrent(self.second_tab(), rival_args[0], rival_args[1])
            rival.wait_until_done_or_blocked_by(first.pid)
            self.assertFalse(rival.done.is_set(), "the rival finished before the first command committed")
        finally:
            winner = first.finish()
        loser = rival.result()
        self.assertLess(winner.status_code, 400, winner.content)
        self.assertEqual(loser.status_code, 409, loser.content)
        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), int(queue_first))
        self.assertEqual(TraceDeviceResolution.objects.filter(profile=self.profile).count(), int(not queue_first))

    def test_device_resolution_waits_for_sync_queueing_and_is_refused(self):
        self._race_resolution_and_sync(queue_first=True)

    def test_sync_queueing_waits_for_device_resolution_and_is_refused(self):
        self._race_resolution_and_sync(queue_first=False)


class OldJobCompletionTest(IsolatedRQQueueTestMixin, _FlatPreviewMixin, TransactionTestCase):
    """An import Job queued from an older preview finishes without touching the preview that replaced it."""

    def setUp(self):
        super().setUp()
        self.build_flat_profile()

    def test_a_finished_job_leaves_the_replacement_preview_unchanged(self):
        from core.models import Job
        from dcim.models import Device

        from netbox_data_import.models import PreviewState
        from netbox_data_import.tests.helpers import preview_claim, preview_coordinator

        self.upload_flat(self.client, "first.xlsx", "server-a")
        self.client.post(reverse("plugins:netbox_data_import:import_run"), preview_claim(self.client))
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.SUBMITTED)
        self.upload_flat(self.client, "second.xlsx", "server-c")
        replacement = preview_coordinator(self.client)

        self.run_rq_jobs()

        self.assertEqual(Job.objects.get(data__job_type="netbox_data_import.import").status, "completed")
        self.assertTrue(Device.objects.filter(name="server-a").exists())
        after = preview_coordinator(self.client)
        self.assertEqual(
            (after.preview_token, after.revision, after.state, after.context, after.plan, after.job_id),
            (
                replacement.preview_token,
                replacement.revision,
                PreviewState.READY,
                replacement.context,
                replacement.plan,
                None,
            ),
        )


class ConcurrentSetupTest(IsolatedRQQueueTestMixin, _FlatPreviewMixin, TransactionTestCase):
    """Two uploads from one setup page race on the coordinator row: one replaces the preview, one is refused."""

    def setUp(self):
        super().setUp()
        self.build_flat_profile()

    def _upload(self, claim, filename, name):
        rows = [["D-1", "Server", name, "Example", "Model"]]
        upload = SimpleUploadedFile(
            filename, workbook_bytes(["Source ID", "Class", "Name", "Make", "Model"], rows), content_type=XLSX
        )
        return (
            reverse("plugins:netbox_data_import:import_setup"),
            {**claim, "profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
        )

    def test_two_setups_from_one_generation_have_one_winner(self):
        setup = self.client.get(reverse("plugins:netbox_data_import:import_setup"))
        claim = page_claim(setup, enctype="multipart/form-data")
        first = _PausedRequest(
            self, self.client, SourceDocument._meta.db_table, *self._upload(claim, "a.xlsx", "server-a")
        )
        first.start()
        try:
            rival = _Concurrent(self.second_tab(), *self._upload(claim, "b.xlsx", "server-b"))
            rival.wait_until_done_or_blocked_by(first.pid)
            self.assertFalse(rival.done.is_set(), "the rival setup finished while the first held the coordinator")
        finally:
            winner = first.finish()

        self.assertEqual(winner.status_code, 302)
        self.assertEqual(rival.result().status_code, 409)
        self.assertEqual(list(SourceDocument.objects.values_list("filename", flat=True)), ["a.xlsx"])
        self.assertContains(self.client.get(reverse("plugins:netbox_data_import:import_preview")), "server-a")

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""With a branch active or selected, every plugin entry point refuses and reads no plugin data."""

import itertools
from functools import partial
import uuid
from contextlib import ExitStack
from datetime import timedelta

import pytest

from netbox_data_import import branching

if not branching.installed():
    pytest.skip("netbox-branching is not an installed app", allow_module_level=True)

from core.choices import JobStatusChoices
from core.models import Job
from dcim.models import Device
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, URLResolver, get_resolver, resolve, reverse
from django.utils import timezone
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.constants import BRANCH_HEADER, COOKIE_NAME
from netbox_branching.models import Branch
from netbox_branching.utilities import activate_branch

from netbox_data_import.jobs import ImportJobRunner, ResolutionProposalJob, SourceDocumentRetentionJob
from netbox_data_import.models import (
    CableClassMapping,
    DeviceImportSource,
    ImportExecution,
    ImportProfile,
    SourceDocument,
    locked_profile_policy,
)
from netbox_data_import.tests.helpers import make_dcim_objects, provision_branch, store_workbook_document

APP_LABEL = "netbox_data_import"
# One value per URL converter the plugin uses: an integer key, a DRF format suffix, a slug, a UUID.
URL_ARGUMENT_CANDIDATES = ("1", "json", "a", str(uuid.UUID(int=1)))
GRAPHQL_QUERIES = {
    "import_profile": "{ import_profile(id: 1) { id } }",
    "import_profile_list": "{ import_profile_list { id } }",
    "cable_class_mapping": "{ cable_class_mapping(id: 1) { id } }",
    "cable_class_mapping_list": "{ cable_class_mapping_list { id } }",
}


def _is_plugin_callback(callback) -> bool:
    """Restate the refusal scope: the module of the view function, view class or DRF viewset."""
    owner = getattr(callback, "view_class", None) or getattr(callback, "cls", None) or callback
    return owner.__module__ == APP_LABEL or owner.__module__.startswith(f"{APP_LABEL}.")


def _plugin_patterns(patterns, namespace=()):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _plugin_patterns(
                pattern.url_patterns, (*namespace, pattern.namespace) if pattern.namespace else namespace
            )
        elif _is_plugin_callback(pattern.callback):
            yield ":".join(part for part in (*namespace, pattern.name) if part), pattern


def plugin_urls() -> list[str]:
    """Return one URL for every plugin-owned callback in the complete URL tree."""
    urls = []
    for name, pattern in _plugin_patterns(get_resolver().url_patterns):
        keys = list(pattern.pattern.regex.groupindex)
        for values in itertools.product(URL_ARGUMENT_CANDIDATES, repeat=len(keys)):
            try:
                url = reverse(name, kwargs=dict(zip(keys, values, strict=True)))
            except NoReverseMatch:
                continue
            if resolve(url).func is pattern.callback:
                urls.append(url)
                break
        else:
            raise AssertionError(f"No URL reaches the plugin callback {name}.")
    return urls


def _json_code(response):
    if response.get("Content-Type") != "application/json":
        return None
    payload = response.json()
    return payload.get("code") if isinstance(payload, dict) else None


class RefusalMessageTest(SimpleTestCase):
    """The refusal names the active branch so that it reads as a name."""

    def test_the_branch_name_is_quoted(self):
        message = branching.refusal_message(Branch(name="read surface"))

        self.assertTrue(message.endswith("the active branch is \u201cread surface\u201d."), message)


class SuperuserClientMixin:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser("branch-admin", "branch@example.invalid", "testpass")
        self.client.force_login(self.user)


class PluginCallbackRefusalTest(SuperuserClientMixin, TransactionTestCase):
    """Guard 1: every plugin-owned URL callback refuses a request with a real branch active."""

    def test_every_plugin_callback_refuses_inside_a_branch(self):
        branch = provision_branch(self, "guard one")
        api_root = reverse("api-root")
        urls = plugin_urls()
        self.assertIn(reverse("plugins:netbox_data_import:importprofile_list"), urls)
        self.assertIn(reverse("plugins-api:netbox_data_import-api:importprofile-list"), urls)

        for url in urls:
            with self.subTest(url=url):
                if url.startswith(api_root):
                    response = self.client.get(url, headers={BRANCH_HEADER: branch.schema_id})
                    self.assertEqual((response.status_code, _json_code(response)), (409, branching.REFUSAL_CODE))
                else:
                    self.client.cookies[COOKIE_NAME] = branch.schema_id
                    response = self.client.get(url)
                    self.assertContains(response, "data-branch-refusal", status_code=409)
                    self.assertContains(response, branching.refusal_message(branch), status_code=409)


class BranchSelectorTest(SuperuserClientMixin, TransactionTestCase):
    """A branch selector without an active branch is refused, except the explicit switch to main."""

    def setUp(self):
        super().setUp()
        self.stale = Branch(name="merged")
        self.stale.save(provision=False)
        Branch.objects.filter(pk=self.stale.pk).update(status=BranchStatusChoices.MERGED)
        self.list_url = reverse("plugins:netbox_data_import:importprofile_list")
        self.api_url = reverse("plugins-api:netbox_data_import-api:importprofile-list")

    def test_a_stale_branch_cookie_is_refused(self):
        self.client.cookies[COOKIE_NAME] = self.stale.schema_id

        response = self.client.get(self.list_url)

        self.assertContains(response, branching.refusal_message(None), status_code=409)

    def test_a_stale_branch_header_is_refused(self):
        response = self.client.get(self.api_url, headers={BRANCH_HEADER: self.stale.schema_id})

        self.assertEqual((response.status_code, _json_code(response)), (409, branching.REFUSAL_CODE))

    def test_the_header_takes_precedence_over_an_empty_branch_parameter(self):
        response = self.client.get(f"{self.api_url}?_branch=", headers={BRANCH_HEADER: self.stale.schema_id})

        self.assertEqual((response.status_code, _json_code(response)), (409, branching.REFUSAL_CODE))

    def test_graphql_refuses_a_stale_selector(self):
        selectors = {
            "cookie": ({COOKIE_NAME: self.stale.schema_id}, {}),
            "header": ({}, {BRANCH_HEADER: self.stale.schema_id}),
        }
        for selector, (cookies, headers) in selectors.items():
            with self.subTest(selector=selector):
                self.client.cookies.clear()
                self.client.force_login(self.user)
                for name, value in cookies.items():
                    self.client.cookies[name] = value

                response = self.client.post(
                    "/graphql/",
                    data={"query": GRAPHQL_QUERIES["import_profile_list"]},
                    content_type="application/json",
                    headers=headers,
                )

                messages = [error["message"] for error in response.json().get("errors", [])]
                self.assertEqual(messages, [branching.refusal_message(None)])

    def test_a_ui_request_ignores_the_branch_header(self):
        response = self.client.get(f"{self.list_url}?_branch=", headers={BRANCH_HEADER: self.stale.schema_id})

        self.assertEqual(response.status_code, 200)

    def test_an_empty_branch_parameter_switches_to_main(self):
        ready = Branch(name="ready")
        ready.save(provision=False)
        Branch.objects.filter(pk=ready.pk).update(status=BranchStatusChoices.READY)
        self.client.cookies[COOKIE_NAME] = ready.schema_id

        response = self.client.get(f"{self.list_url}?_branch=")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.cookies[COOKIE_NAME].value, "")


class BranchReadSurfaceTest(SuperuserClientMixin, TransactionTestCase):
    """GraphQL fields refuse in a branch, and the Device card shows a notice instead of import data."""

    def setUp(self):
        super().setUp()
        site, _manufacturer, device_type, role = make_dcim_objects("Surface")
        self.device = Device.objects.create(name="surface-device", site=site, device_type=device_type, role=role)
        profile = ImportProfile.objects.create(name="Surface profile", source_adapter="trace_workbook")
        DeviceImportSource.objects.create(
            device=self.device, profile=profile, source_id="row-7", extra_columns={"Rack Row": "Row-R7"}
        )
        CableClassMapping.objects.create(profile=profile, cable_class="Copper")
        self.branch = provision_branch(self, "read surface")

    def test_the_graphql_fields_refuse_inside_a_branch(self):
        for field, query in GRAPHQL_QUERIES.items():
            with self.subTest(field=field):
                response = self.client.post(
                    "/graphql/",
                    data={"query": query},
                    content_type="application/json",
                    headers={BRANCH_HEADER: self.branch.schema_id},
                )

                messages = [error["message"] for error in response.json().get("errors", [])]
                self.assertEqual(messages, [branching.refusal_message(self.branch)])

    def test_the_device_card_shows_a_main_only_notice_inside_a_branch(self):
        self.client.cookies[COOKIE_NAME] = self.branch.schema_id

        response = self.client.get(self.device.get_absolute_url())

        self.assertContains(response, "data-import-data-main-only")
        self.assertNotContains(response, "Row-R7")


class BackgroundRefusalTest(TransactionTestCase):
    """Jobs and the policy lock refuse under activate_branch() before they read plugin data."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("branch-worker")
        self.profile = ImportProfile.objects.create(name="Worker profile")
        self.document = store_workbook_document(self.profile, ["Id"], [["1"]], self.user, "stale.xlsx")
        stale = timezone.now() - SourceDocument.RETENTION - timedelta(days=1)
        SourceDocument.objects.filter(pk=self.document.pk).update(created=stale)
        self.branch = provision_branch(self, "background")

    def _plugin_queries_in_branch(self, action):
        """Run *action* with the branch active, and return every query on a plugin table."""
        with ExitStack() as stack:
            captures = [
                stack.enter_context(CaptureQueriesContext(connections[alias]))
                for alias in ("default", self.branch.connection_name)
            ]
            stack.enter_context(activate_branch(self.branch))
            action()
        return [query["sql"] for capture in captures for query in capture if f"{APP_LABEL}_" in query["sql"]]

    def test_each_job_refuses_before_it_reads_plugin_data(self):
        runs = {
            ImportJobRunner: {
                "profile_id": self.profile.pk,
                "source_document_id": self.document.pk,
                "accepted_plan": {},
                "selection": [],
                "idempotency_key": "branch-refusal",
            },
            ResolutionProposalJob: {"proposal_id": 1},
            SourceDocumentRetentionJob: {},
        }
        for runner, kwargs in runs.items():
            with self.subTest(job=runner.__name__):
                job = Job.objects.create(name=runner.__name__, job_id=uuid.uuid4(), user=self.user)

                queries = self._plugin_queries_in_branch(partial(runner.handle, job, **kwargs))

                job.refresh_from_db()
                self.assertEqual(queries, [])
                self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
                self.assertIn(branching.refusal_message(self.branch), [entry["message"] for entry in job.log_entries])
        self.assertTrue(SourceDocument.objects.filter(pk=self.document.pk).exists())
        self.assertFalse(ImportExecution.objects.exists())

    def test_without_a_branch_a_job_runs(self):
        job = Job.objects.create(name="Retention on main", job_id=uuid.uuid4(), user=self.user)

        SourceDocumentRetentionJob.handle(job)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertFalse(SourceDocument.objects.filter(pk=self.document.pk).exists())

    def test_the_policy_lock_refuses_before_it_locks(self):
        def lock():
            with self.assertRaisesMessage(branching.BranchActive, branching.refusal_message(self.branch)):
                with locked_profile_policy(self.profile.pk):
                    pass

        self.assertEqual(self._plugin_queries_in_branch(lock), [])

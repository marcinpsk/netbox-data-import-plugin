# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A per-trace sync shows every other trace it executes, and executes only the selection the page showed."""

from dataclasses import replace
from io import BytesIO
from html import unescape
from urllib.parse import quote, urlencode

from core.models import Job
from dcim.models import Interface
from django.test import TestCase
from django.urls import reverse
from django_rq import get_queue

from netbox_data_import.jobs import ImportJobRunner
from netbox_data_import.plan import Disposition, ImportPlan
from netbox_data_import.review_workspace import SYNC_NOTHING, SYNC_SELECTION_CHANGED, SYNC_SELECTION_INVALID
from netbox_data_import.tests.helpers import (
    preview_claim,
    store_plan,
    stored_plan,
    trace_endpoint_line,
    trace_segment,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.test_cable_harness import CHANNELS, HarnessTopologyMixin, panel_leg
from netbox_data_import.tests.test_cable_module import (
    PANEL_1_REAR,
    PANEL_2_REAR,
    CableTopologyMixin,
    direct_path,
    patched_path,
)

JSON = {"HTTP_ACCEPT": "application/json"}
DEVICE_C = trace_termination("DEV-C", "", "eth0", "Port")
DEVICE_D = trace_termination("DEV-D", "", "eth1", "NIC")


def workspace_url(trace=""):
    """Return the workspace URL a refusal goes back to: the named trace, or the first trace for none."""
    url = reverse("plugins:netbox_data_import:trace_workspace")
    return f"{url}?{urlencode({'trace': trace})}" if trace else url


def trunk_sharer():
    """Return the trace from DEV-C to DEV-D that states the same trunk segment as the patched path."""
    return (
        trace_endpoint_line(DEVICE_C),
        trace_endpoint_line(DEVICE_D),
        (
            trace_segment(DEVICE_C, "Patch", trace_termination("PANEL-1", "", "F2", "Position Front")),
            trace_segment(PANEL_1_REAR, "Trunk", PANEL_2_REAR),
            trace_segment(trace_termination("PANEL-2", "", "F2", "Position Front"), "Patch", DEVICE_D),
        ),
    )


class _SyncClosureMixin(IsolatedRQQueueTestMixin):
    """Upload a workbook, open one trace, and post its sync form as the page renders it."""

    def upload(self, *blocks):
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        response = upload_preview(
            self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload}, follow=True
        )
        self.assertEqual(response.status_code, 200)

    def traces(self):
        page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        return {trace.endpoints["from"]: trace for trace in page.context["traces"]}

    def open(self, trace):
        page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), {"trace": trace.identity})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["selected_trace"].identity, trace.identity)
        return page

    def posted_selection(self, page):
        """Return the identities the page's sync form posts, in order."""
        form = page.content.decode().split('data-trace-action="sync"')[0].rsplit("<form", 1)[1]
        return [unescape(chunk.split('"', 1)[0]) for chunk in form.split('name="selection" value="')[1:]]

    def sync(self, selection, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("plugins:netbox_data_import:trace_sync"),
                {**preview_claim(self.client), "selection": list(selection)},
                **extra,
            )

    def queued_selection(self):
        (job,) = Job.objects.filter(data__job_type=ImportJobRunner.job_type)
        return get_queue(job.queue_name).fetch_job(str(job.job_id)).kwargs["selection"]

    def assert_nothing_queued(self):
        self.assertFalse(Job.objects.filter(data__job_type=ImportJobRunner.job_type).exists())


class DependencyOwnerClosureTest(_SyncClosureMixin, CableTopologyMixin, TestCase):
    """A trace whose shared trunk waits on another trace's Logical Cable deletion names that trace."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()
        cls.rebuild_panel("PANEL-1", fronts=2)
        cls.rebuild_panel("PANEL-2", fronts=2)
        Interface.objects.create(device=cls.make_device("DEV-C"), name="eth0", type="1000base-t")
        Interface.objects.create(device=cls.make_device("DEV-D"), name="eth1", type="1000base-t")
        cls.connect(cls.eth0, cls.eth1)

    def setUp(self):
        super().setUp()
        self.upload(patched_path(), trunk_sharer())
        traces = self.traces()
        self.owner, self.sharer = traces["DEV-A eth0"], traces["DEV-C eth0"]
        self.assertEqual([self.owner.disposition, self.sharer.disposition], [Disposition.ACTIONABLE] * 2)
        self.assertTrue(self.owner.deletes_logical_cable)

    def test_the_page_names_the_owner_and_posts_both(self):
        page = self.open(self.sharer)

        self.assertContains(page, "data-sync-members")
        self.assertContains(page, 'data-sync-member="dependency"', count=1)
        self.assertNotContains(page, 'data-sync-member="companion"')
        self.assertContains(page, f'href="?trace={quote(self.owner.identity)}"')
        self.assertContains(page, self.owner.name)
        self.assertEqual(self.posted_selection(page), [self.sharer.identity, self.owner.identity])

    def test_the_displayed_selection_queues_exactly_that_selection(self):
        response = self.sync(self.posted_selection(self.open(self.sharer)))

        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual(self.queued_selection(), [self.sharer.identity, self.owner.identity])

    def test_a_selection_without_the_owner_is_refused_as_stale(self):
        response = self.sync([self.sharer.identity], **JSON)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"ok": False, "error": SYNC_SELECTION_CHANGED, "code": "preview_stale"})
        self.assert_nothing_queued()

    def test_the_same_traces_in_another_order_are_refused(self):
        response = self.sync([self.owner.identity, self.sharer.identity], **JSON)

        self.assertEqual((response.status_code, response.json()["error"]), (409, SYNC_SELECTION_CHANGED))
        self.assert_nothing_queued()

    def test_a_refused_form_post_returns_to_the_trace_it_names(self):
        response = self.sync([self.sharer.identity])

        self.assertContains(response, SYNC_SELECTION_CHANGED, status_code=409)
        self.assertEqual(response.context["next_url"], workspace_url(self.sharer.identity))


class PlainTraceClosureTest(_SyncClosureMixin, CableTopologyMixin, TestCase):
    """A trace that needs no other trace renders no list, and its sync queues the trace alone."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()
        Interface.objects.create(device=cls.make_device("DEV-G"), name="eth0", type="1000base-t")
        Interface.objects.create(device=cls.make_device("DEV-H"), name="eth0", type="1000base-t")

    def setUp(self):
        super().setUp()
        self.upload(
            direct_path(),
            direct_path(trace_termination("DEV-G", "", "eth0", "Port"), trace_termination("DEV-H", "", "eth0", "Port")),
        )
        traces = self.traces()
        self.plain, self.other = traces["DEV-A eth0"], traces["DEV-G eth0"]

    def test_the_page_lists_no_other_trace_and_syncs_the_trace_alone(self):
        page = self.open(self.plain)

        self.assertNotContains(page, "data-sync-members")
        self.assertEqual(self.posted_selection(page), [self.plain.identity])
        response = self.sync(self.posted_selection(page))
        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual(self.queued_selection(), [self.plain.identity])

    def test_a_closure_that_grew_after_the_page_rendered_is_refused(self):
        """Only the stored plan changes, so the claim the page posts still names the current revision."""
        shown = self.posted_selection(self.open(self.plain))
        plan = ImportPlan.from_dict(stored_plan(self.client))
        grown = replace(
            plan,
            units=tuple(
                replace(unit, selected_with=(self.other.identity,))
                if unit.identity == self.plain.identity
                else replace(unit, selected_with=(self.plain.identity,))
                for unit in plan.units
            ),
        )
        store_plan(self.client, grown.to_dict())

        response = self.sync(shown, **JSON)

        self.assertEqual((response.status_code, response.json()["error"]), (409, SYNC_SELECTION_CHANGED))
        self.assert_nothing_queued()

    def test_a_malformed_or_unknown_selection_is_refused_before_a_job_exists(self):
        for selection in (
            [],
            [""],
            [self.plain.identity, self.plain.identity],
            ["cable:trace:unknown"],
            [self.plain.identity, "cable:trace:unknown"],
        ):
            with self.subTest(selection=selection):
                response = self.sync(selection, **JSON)

                self.assertEqual((response.status_code, response.json()["error"]), (400, SYNC_SELECTION_INVALID))
        self.assert_nothing_queued()

    def test_a_malformed_or_unknown_form_selection_returns_to_its_trace_with_the_refusal(self):
        """A form or HTMX post goes back to the trace it names first, as every other non-stale refusal does."""
        unknown = "cable:trace:unknown"
        for selection, trace in (
            ([], ""),
            ([""], ""),
            ([self.plain.identity, self.plain.identity], self.plain.identity),
            ([unknown], unknown),
            ([self.plain.identity, unknown], self.plain.identity),
        ):
            expected = workspace_url(trace)
            for headers, status, location in ((), 302, "Location"), ({"HTTP_HX_REQUEST": "true"}, 204, "HX-Redirect"):
                with self.subTest(selection=selection, htmx=bool(headers)):
                    response = self.sync(selection, **dict(headers))

                    self.assertEqual((response.status_code, response[location]), (status, expected))
                    self.assertContains(self.client.get(expected), SYNC_SELECTION_INVALID)
        self.assert_nothing_queued()


class NoChangeTraceSyncTest(_SyncClosureMixin, CableTopologyMixin, TestCase):
    """A trace that NetBox already holds has no changes, so its sync is refused before a Job exists."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()
        cls.connect(cls.eth0, cls.eth1)

    def setUp(self):
        super().setUp()
        self.upload(direct_path())
        (self.trace,) = self.traces().values()
        self.assertEqual(self.trace.disposition, Disposition.NO_OP)

    def test_a_sync_of_a_trace_without_changes_is_refused(self):
        for headers in ({}, JSON):
            with self.subTest(json=bool(headers)):
                response = self.sync([self.trace.identity], **headers)

                if headers:
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"ok": False, "error": SYNC_NOTHING})
                else:
                    self.assertRedirects(response, workspace_url(self.trace.identity), fetch_redirect_response=False)
                    self.assertContains(self.client.get(workspace_url(self.trace.identity)), SYNC_NOTHING)
        self.assert_nothing_queued()


@CHANNELS
class HarnessCompanionClosureTest(_SyncClosureMixin, HarnessTopologyMixin, TestCase):
    """Syncing one leg of a breakout harness writes every leg, so the page names every other leg."""

    @classmethod
    def setUpTestData(cls):
        cls.build_harness_topology()

    def setUp(self):
        super().setUp()
        self.resolve_lanes(range(1, 5))
        self.upload(*(panel_leg(number) for number in range(1, 5)))
        # The plan lists the legs in channel order, which is the order of the closure.
        self.legs = list(self.client.get(reverse("plugins:netbox_data_import:trace_workspace")).context["traces"])
        self.assertEqual([leg.disposition for leg in self.legs], [Disposition.ACTIONABLE] * 4)

    def test_the_page_names_every_other_leg_as_a_companion(self):
        first, *others = self.legs

        page = self.open(first)

        self.assertContains(page, 'data-sync-member="companion"', count=3)
        self.assertNotContains(page, 'data-sync-member="dependency"')
        for leg in others:
            self.assertContains(page, f'href="?trace={quote(leg.identity)}"')
        self.assertEqual(self.posted_selection(page), [leg.identity for leg in self.legs])

    def test_the_displayed_selection_queues_every_leg(self):
        response = self.sync(self.posted_selection(self.open(self.legs[0])))

        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual(self.queued_selection(), [leg.identity for leg in self.legs])

    def test_a_selection_that_leaves_out_or_reorders_a_leg_is_refused(self):
        first, second, third, fourth = (leg.identity for leg in self.legs)
        for selection in ([first, second, third], [first, second, fourth, third]):
            with self.subTest(selection=selection):
                response = self.sync(selection, **JSON)

                self.assertEqual((response.status_code, response.json()["error"]), (409, SYNC_SELECTION_CHANGED))
        self.assert_nothing_queued()

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Proposal actions update the workspace in place, through real previews and the real coordinator."""

import html
import pathlib
import re
from html.parser import HTMLParser
import uuid
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import patch

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from dcim.models import Device, Interface, Site
from django.contrib.messages import get_messages
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django_rq import get_queue
from django_rq.queues import DjangoRQ
from redis.exceptions import ConnectionError as RedisConnectionError

from netbox_data_import.field_keys import termination_field_key
from netbox_data_import.jobs import ResolutionProposalJob
from netbox_data_import.models import (
    ImportProfile,
    PreviewState,
    ProposalFailureReason,
    ProposalOutcome,
    ProposalStatus,
    ResolutionProposal,
)
from netbox_data_import.resolution_proposals import claim_proposal, complete_proposal
from netbox_data_import.tests.helpers import (
    preview_claim,
    preview_coordinator,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
    user_with_object_permission,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.plugins_config import override_plugins_config
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path
from netbox_data_import.tests.test_inference_backend import ALLOWLIST, FALLBACK

HTMX = {"HX-Request": "true"}
BACKEND = {"inference_backend": FALLBACK, "inference_backend_origin_allowlist": ALLOWLIST}


FIXTURE = pathlib.Path(__file__).parent / "js" / "trace_proposal_fixture.js"
ACTIVE_COUNT = re.compile(r'<div\b[^>]*id="ndiActiveProposals"[^>]*hx-swap-oob="true"[^>]*>\s*(\S+)\s*</div>')
COUNTED_AT = re.compile(r'<div\b[^>]*id="ndiActiveProposals"[^>]*data-counted-at="(\d+)"')


class _HtmxAttributes(HTMLParser):
    """Collect the hx-* attributes of every element that carries one, in document order."""

    def __init__(self):
        super().__init__()
        self.elements: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        found = {name: value for name, value in attrs if name.startswith("hx-")}
        if found:
            self.elements.append((tag, found))


def htmx_attributes(content: str) -> list[tuple[str, dict]]:
    parser = _HtmxAttributes()
    parser.feed(content)
    return parser.elements


def field(device, port):
    return termination_field_key(device=device, cards="", port=port, kind="interface")


class InPlacePreviewMixin:
    """Two traces with three open terminations, so one page can ask about several fields."""

    def setUp(self):
        super().setUp()
        # Ask AI refuses without an Inference Backend, so each test has one unless it removes it.
        self.enterContext(override_plugins_config(netbox_data_import=BACKEND))
        self.client.force_login(self.actor)
        self.first = field("DEV-A", "absent-port")
        self.second = field("DEV-B", "absent-b")
        self.third = field("DEV-A", "absent-c")
        upload = BytesIO(
            trace_workbook_bytes(
                path_blocks=[
                    direct_path(
                        from_end=trace_termination("DEV-A", "", "absent-port", "Port"),
                        to_end=trace_termination("DEV-B", "", "absent-b", "Port"),
                    ),
                    direct_path(
                        from_end=trace_termination("DEV-A", "", "absent-c", "Port"),
                        to_end=trace_termination("DEV-B", "", "eth1", "Port"),
                    ),
                ]
            )
        )
        upload.name = "traces.xlsx"
        response = upload_preview(
            self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)

    def post(self, action, claim=None, **data):
        """Post one workspace command as htmx sends it: the claim the page holds, no JSON Accept header."""
        url = reverse(f"plugins:netbox_data_import:{action}")
        # A TransactionTestCase commits for real, so only a TestCase holds callbacks to run.
        capture = getattr(self, "captureOnCommitCallbacks", None)
        with capture(execute=True) if capture else nullcontext():
            return self.client.post(url, {**(claim or preview_claim(self.client)), **data}, headers=HTMX)

    def ask(self, field_key, claim=None):
        response = self.post("trace_request_proposal", claim, field_key=field_key)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response

    def completed(self, field_key):
        self.ask(field_key)
        proposal = ResolutionProposal.objects.get(field_key=field_key, status=ProposalStatus.QUEUED)
        entry = proposal.candidate_snapshot["candidates"][0]
        self.assertTrue(claim_proposal(proposal.pk))
        self.assertTrue(
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.CANDIDATE,
                explanation="The candidate matches the source label.",
                selected_candidate_id=entry["candidate_id"],
                selected_object_type=ObjectType.objects.get_for_model(Interface),
                selected_object_id=entry["object_id"],
            )
        )
        return proposal


class ProposalInPlaceTest(InPlacePreviewMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    """Each proposal action answers with what it changed, and only acceptance moves the claim."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def card(self, response):
        """Return the one card a fragment response rendered, and the field it names."""
        content = response.content.decode()
        cards = re.findall(r'<li\b[^>]*data-proposal-field="([^"]+)"', content)
        self.assertEqual(len(cards), 1, content[:500])
        self.assertNotIn("<html", content)
        return html.unescape(cards[0]), content

    def test_ask_ai_on_two_fields_with_one_claim_queues_both(self):
        """The operator clicks Ask AI on several fields without a reload, so each post carries one claim."""
        claim = preview_claim(self.client)

        self.ask(self.first, claim)
        self.ask(self.second, claim)

        self.assertEqual(
            sorted(ResolutionProposal.objects.values_list("field_key", "status")),
            sorted([(self.first, ProposalStatus.QUEUED), (self.second, ProposalStatus.QUEUED)]),
        )
        self.assertEqual(preview_claim(self.client), claim)

    def test_ask_ai_answers_with_the_card_it_changed_which_polls_while_pending(self):
        response = self.ask(self.first)

        key, html = self.card(response)
        self.assertEqual(key, self.first)
        self.assertRegex(html, r'<li\b[^>]*hx-trigger="every 3s"')
        self.assertIn("Waiting for the backend", html)
        claim = preview_claim(self.client)
        self.assertIn(f'value="{claim["preview_token"]}"', html)

    def test_reject_and_cancel_answer_with_the_card_and_keep_the_claim(self):
        claim = preview_claim(self.client)
        proposal = self.completed(self.first)
        self.ask(self.second, claim)
        queued = ResolutionProposal.objects.get(field_key=self.second)

        rejected = self.post("trace_reject_proposal", claim, proposal_id=proposal.pk)
        cancelled = self.post("trace_cancel_proposal", claim, proposal_id=queued.pk)

        self.assertEqual(rejected.status_code, 200, rejected.content[:500])
        self.assertEqual(cancelled.status_code, 200, cancelled.content[:500])
        self.assertEqual(self.card(rejected)[0], self.first)
        self.assertEqual(self.card(cancelled)[0], self.second)
        self.assertNotRegex(self.card(cancelled)[1], r"<li\b[^>]*hx-trigger=")
        self.assertEqual(ResolutionProposal.objects.get(pk=proposal.pk).decision, "rejected")
        self.assertEqual(ResolutionProposal.objects.get(pk=queued.pk).status, ProposalStatus.CANCELLED)
        self.assertEqual(preview_claim(self.client), claim)

    def test_a_poll_answers_with_the_card(self):
        self.ask(self.first)

        response = self.client.get(
            reverse("plugins:netbox_data_import:trace_proposal"),
            {**preview_claim(self.client), "field_key": self.first},
            headers=HTMX,
        )

        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(self.card(response)[0], self.first)

    def test_a_failed_card_names_its_failure_and_offers_to_ask_again(self):
        from netbox_data_import.resolution_proposals import fail_proposal

        self.ask(self.first)
        proposal = ResolutionProposal.objects.get(field_key=self.first)
        self.assertTrue(fail_proposal(proposal.pk, reason=ProposalFailureReason.BACKEND_REFUSAL))

        response = self.client.get(
            reverse("plugins:netbox_data_import:trace_proposal"),
            {**preview_claim(self.client), "field_key": self.first},
            headers=HTMX,
        )

        _key, content = self.card(response)
        self.assertRegex(content, r"<p\b[^>]*data-proposal-failure>[^<]*\(backend_refusal\)</p>")
        self.assertRegex(content, r'data-proposal-action="request"[^>]*>(<span[^>]*></span>)?Ask AI again</button>')
        self.assertNotRegex(content, r"<li\b[^>]*hx-trigger=")

    def test_accept_swaps_the_workspace_and_its_new_claim_is_the_one_a_later_post_needs(self):
        proposal = self.completed(self.first)
        old = preview_claim(self.client)
        trace = re.search(
            r'name="trace" value="([^"]*)"',
            self.client.get(reverse("plugins:netbox_data_import:trace_workspace")).content.decode(),
        ).group(1)

        response = self.post("trace_accept_proposal", old, proposal_id=proposal.pk, trace=trace)

        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assertIn(reverse("plugins:netbox_data_import:trace_workspace"), response["Location"])
        page = self.client.get(response["Location"], headers=HTMX)
        new = page.context["preview_claim"].fields()
        self.assertEqual(int(new["preview_revision"]), int(old["preview_revision"]) + 1)
        self.assertContains(page, f'name="preview_revision" value="{new["preview_revision"]}"')
        self.assertNotContains(page, f'name="preview_revision" value="{old["preview_revision"]}"')
        self.assertEqual(self.post("trace_request_proposal", old, field_key=self.second).status_code, 409)
        self.ask(self.second, new)

    def test_ask_ai_for_all_queues_every_eligible_field_and_names_each_skip(self):
        self.ask(self.second)
        claim = preview_claim(self.client)

        with override_plugins_config(netbox_data_import=BACKEND):
            response = self.post("trace_request_all_proposals", claim)

        self.assertEqual(response.status_code, 302, response.content[:500])
        active = ResolutionProposal.objects.filter(status=ProposalStatus.QUEUED)
        self.assertEqual(
            sorted(active.values_list("field_key", flat=True)), sorted([self.first, self.second, self.third])
        )
        self.assertEqual(ResolutionProposal.objects.count(), 3)
        self.assertEqual(preview_claim(self.client), claim)
        notes = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn("Asked AI about 2 terminations.", notes)
        self.assertIn("Skipped 1 termination: This field already has an active Resolution Proposal. (1)", notes)

    def test_ask_ai_for_all_reads_one_inventory_per_device_kind_and_role(self):
        """The first and third fields share DEV-A, so the command reads its candidates once."""
        with CaptureQueriesContext(connection) as queries:
            response = self.post("trace_request_all_proposals")

        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assertEqual(ResolutionProposal.objects.filter(status=ProposalStatus.QUEUED).count(), 3)
        # Only the candidate ranking of an inventory read annotates _ndi_order.
        rankings = [query["sql"] for query in queries.captured_queries if '"_ndi_order"' in query["sql"]]
        self.assertEqual(len(rankings), 2, rankings)

    def test_ask_ai_for_all_refuses_without_an_inference_backend(self):
        with override_plugins_config(netbox_data_import={}):
            response = self.post("trace_request_all_proposals")

        self.assertFalse(ResolutionProposal.objects.exists())
        self.assertIn(response.status_code, (204, 302))
        notes = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn("No Inference Backend is enabled or configured as a fallback.", notes)

    def test_ask_ai_for_all_refuses_a_stale_claim(self):
        claim = preview_claim(self.client)
        self.completed(self.first)
        proposal = ResolutionProposal.objects.get(field_key=self.first)
        self.post("trace_accept_proposal", claim, proposal_id=proposal.pk)

        with override_plugins_config(netbox_data_import=BACKEND):
            response = self.post("trace_request_all_proposals", claim)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(ResolutionProposal.objects.count(), 1)

    def test_ask_ai_for_all_needs_the_profile_change_permission(self):
        viewer = user_with_object_permission(
            f"viewer-{uuid.uuid4().hex}",
            [
                (ImportProfile, ["view"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
            ],
        )
        claim = preview_claim(self.client)
        self.client.force_login(viewer)

        with override_plugins_config(netbox_data_import=BACKEND):
            response = self.post("trace_request_all_proposals", claim)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(ResolutionProposal.objects.exists())

    def active_count(self, response) -> str:
        """Return the active proposal count a card answer swaps into the summary strip."""
        counts = ACTIVE_COUNT.findall(response.content.decode())
        self.assertEqual(len(counts), 1, response.content[-800:])
        return counts[0]

    def test_each_card_answer_updates_the_active_proposal_count_in_the_strip(self):
        page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertRegex(page.content.decode(), r'<div\b[^>]*id="ndiActiveProposals"[^>]*>\s*0\s*</div>')
        self.assertNotIn(
            "hx-swap-oob", re.search(r'<div\b[^>]*id="ndiActiveProposals"[^>]*>', page.content.decode()).group()
        )

        counts = [self.active_count(self.ask(key)) for key in (self.first, self.second, self.third)]
        queued = ResolutionProposal.objects.get(field_key=self.second)
        cancelled = self.post("trace_cancel_proposal", proposal_id=queued.pk)
        poll = self.client.get(
            reverse("plugins:netbox_data_import:trace_proposal"),
            {**preview_claim(self.client), "field_key": self.first},
            headers=HTMX,
        )

        self.assertEqual(counts, ["1", "2", "3"])
        self.assertEqual(self.active_count(cancelled), "2")
        self.assertEqual(self.active_count(poll), "2")

    def test_the_card_markup_is_the_htmx_contract_the_browser_fixture_copies(self):
        """The Playwright fixture copies these attributes, so the real render and the fixture must agree."""
        self.completed(self.first)
        response = self.ask(self.second)
        card_id = re.search(r'<li\b[^>]*\bid="([^"]+)"', response.content.decode()).group(1)
        disable = f"#{card_id} [data-proposal-action]"
        card_swap = {
            "hx-target": "closest [data-proposal-field]",
            "hx-swap": "outerHTML",
            "hx-sync": "closest [data-proposal-field]:replace",
            "hx-disabled-elt": disable,
        }
        accept = {
            "hx-target": "#page-content",
            "hx-select": "#page-content",
            "hx-swap": "outerHTML",
            "hx-push-url": "true",
            "hx-sync": "closest [data-proposal-field]:replace",
            "hx-disabled-elt": disable,
        }
        read = reverse("plugins:netbox_data_import:trace_proposal")
        expected = [
            ("li", {"hx-trigger": "every 3s", "hx-swap": "outerHTML", "hx-sync": "this:abort"}),
            ("form", {"hx-post": reverse("plugins:netbox_data_import:trace_accept_proposal"), **accept}),
            ("form", {"hx-post": reverse("plugins:netbox_data_import:trace_reject_proposal"), **card_swap}),
            ("form", {"hx-post": reverse("plugins:netbox_data_import:trace_request_proposal"), **card_swap}),
            ("form", {"hx-post": reverse("plugins:netbox_data_import:trace_cancel_proposal"), **card_swap}),
            ("div", {"hx-swap-oob": "true"}),
        ]
        rendered = htmx_attributes(response.content.decode())
        self.assertTrue(rendered[0][1].pop("hx-get").startswith(f"{read}?"))
        self.assertEqual(rendered, expected)
        fixture = FIXTURE.read_text(encoding="utf-8")
        for _tag, attributes in expected:
            for name, value in attributes.items():
                if name not in ("hx-post", "hx-disabled-elt", "hx-swap-oob"):
                    self.assertIn(f'{name}="{value}"', fixture)
        self.assertIn('hx-disabled-elt="#${id} [data-proposal-action]"', fixture)

    def test_an_ended_session_answers_the_card_with_a_refusal_not_the_login_page(self):
        claim = preview_claim(self.client)
        self.client.logout()

        poll = self.client.get(
            reverse("plugins:netbox_data_import:trace_proposal"), {**claim, "field_key": self.first}, headers=HTMX
        )
        command = self.post("trace_request_proposal", claim, field_key=self.first)

        for response in (poll, command):
            self.assertEqual(response.status_code, 401, response.content[:300])
            self.assertEqual(
                response.json(), {"ok": False, "error": "Your session has ended. Reload the page to log in again."}
            )
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_each_count_names_when_the_database_counted_it_so_a_late_answer_cannot_win(self):
        page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        stamps = [int(COUNTED_AT.search(page.content.decode()).group(1))]
        for key in (self.first, self.second):
            stamps.append(int(COUNTED_AT.search(self.ask(key).content.decode()).group(1)))

        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), 3)

    def test_an_ended_session_answers_ask_ai_for_all_with_a_refusal_not_the_login_page(self):
        claim = preview_claim(self.client)
        self.client.logout()

        with override_plugins_config(netbox_data_import=BACKEND):
            response = self.post("trace_request_all_proposals", claim)

        self.assertEqual(response.status_code, 401, response.content[:300])
        self.assertEqual(
            response.json(), {"ok": False, "error": "Your session has ended. Reload the page to log in again."}
        )
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_an_ended_session_answers_every_workspace_swap_with_a_refusal_not_the_login_page(self):
        """Each of these forms swaps #page-content, so a login page would empty the workspace."""
        claim = preview_claim(self.client)
        self.client.logout()

        for route in (
            "preview_reread",
            "trace_cable_policy",
            "trace_segment_policy",
            "trace_location_mapping",
            "trace_resolve_device",
            "trace_resolve_termination",
        ):
            with self.subTest(route=route):
                response = self.post(route, claim)
                self.assertEqual(response.status_code, 401, response.content[:300])
                self.assertEqual(
                    response.json(),
                    {"ok": False, "error": "Your session has ended. Reload the page to log in again."},
                )

    def test_an_ended_session_still_sends_a_plain_form_post_to_the_login_page(self):
        claim = preview_claim(self.client)
        self.client.logout()

        response = self.client.post(reverse("plugins:netbox_data_import:preview_reread"), claim)

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_ask_ai_refuses_without_an_inference_backend_as_ask_ai_for_all_does(self):
        claim = preview_claim(self.client)

        with override_plugins_config(netbox_data_import={}):
            single = self.post("trace_request_proposal", claim, field_key=self.first)
            every = self.post("trace_request_all_proposals", claim)

        self.assertEqual(single.status_code, 409, single.content[:300])
        self.assertEqual(
            single.json(), {"ok": False, "error": "No Inference Backend is enabled or configured as a fallback."}
        )
        self.assertIn(
            "No Inference Backend is enabled or configured as a fallback.",
            [str(m) for m in get_messages(every.wsgi_request)],
        )
        self.assertFalse(ResolutionProposal.objects.exists())
        self.assertEqual(preview_claim(self.client), claim)

    def test_the_summary_strip_offers_ask_ai_for_all(self):
        with override_plugins_config(netbox_data_import=BACKEND):
            page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertRegex(
            page.content.decode(),
            r'<form\b[^>]*hx-post="{}"'.format(
                re.escape(reverse("plugins:netbox_data_import:trace_request_all_proposals"))
            ),
        )


class AskAllQueueFailureTest(InPlacePreviewMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """NetBox pushes each Job from `on_commit`, so a failed push meets attempt rows that already committed."""

    def setUp(self):
        self.build_topology()
        super().setUp()

    def test_a_failed_push_fails_every_attempt_the_command_created(self):
        self.ask(self.second)
        asked = ResolutionProposal.objects.get(field_key=self.second)

        with (
            override_plugins_config(netbox_data_import=BACKEND),
            patch.object(DjangoRQ, "enqueue_call", autospec=True, side_effect=RedisConnectionError),
        ):
            response = self.post("trace_request_all_proposals")

        self.assertEqual(response.status_code, 204, response.content[:500])
        notes = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn("The proposal queue is unavailable. Try again later.", notes)
        failed = ResolutionProposal.objects.exclude(pk=asked.pk)
        self.assertEqual(sorted(failed.values_list("field_key", flat=True)), sorted([self.first, self.third]))
        self.assertEqual(
            set(failed.values_list("status", "failure_reason")),
            {(ProposalStatus.FAILED, ProposalFailureReason.QUEUE_UNAVAILABLE)},
        )
        asked.refresh_from_db()
        self.assertEqual(asked.status, ProposalStatus.QUEUED)

    def test_a_push_after_a_pushed_one_fails_only_the_attempts_no_worker_will_run(self):
        """The first task reached the queue, so its attempt stays queued; the rest never will."""
        original = DjangoRQ.enqueue_call
        pushes = []

        def second_push_fails(queue, *args, **kwargs):
            pushes.append(kwargs.get("job_id"))
            if len(pushes) == 1:
                return original(queue, *args, **kwargs)
            raise RedisConnectionError

        with (
            override_plugins_config(netbox_data_import=BACKEND),
            patch.object(DjangoRQ, "enqueue_call", autospec=True, side_effect=second_push_fails),
        ):
            response = self.post("trace_request_all_proposals")

        self.assertEqual(response.status_code, 204, response.content[:500])
        self.assertEqual(len(pushes), 2)
        rows = {row.field_key: row for row in ResolutionProposal.objects.select_related("job")}
        pushed = next(row for row in rows.values() if str(row.job.job_id) == pushes[0])
        self.assertEqual(pushed.status, ProposalStatus.QUEUED)
        self.assertEqual(pushed.job.status, JobStatusChoices.STATUS_PENDING)
        self.assertIsNotNone(get_queue().fetch_job(pushes[0]))
        others = [row for row in rows.values() if row.pk != pushed.pk]
        self.assertEqual(len(others), 2)
        self.assertEqual(
            {(row.status, row.failure_reason, row.job.status) for row in others},
            {(ProposalStatus.FAILED, ProposalFailureReason.QUEUE_UNAVAILABLE, JobStatusChoices.STATUS_ERRORED)},
        )
        proposal_jobs = Job.objects.filter(name=ResolutionProposalJob.Meta.name)
        self.assertEqual(proposal_jobs.filter(status=JobStatusChoices.STATUS_PENDING).count(), 1)

    def test_compensation_never_fails_an_attempt_a_worker_already_took(self):
        """A queue that cannot answer is no proof the task is missing, so only a queued attempt may fail."""
        from rq.job import Job as RQJob

        def a_worker_takes_it_and_the_push_fails(queue, *args, **kwargs):
            self.assertTrue(claim_proposal(ResolutionProposal.objects.get(field_key=self.first).pk))
            raise RedisConnectionError

        with (
            patch.object(DjangoRQ, "enqueue_call", autospec=True, side_effect=a_worker_takes_it_and_the_push_fails),
            patch.object(RQJob, "fetch", side_effect=RedisConnectionError),
        ):
            response = self.post("trace_request_proposal", field_key=self.first)

        self.assertEqual(response.status_code, 503, response.content[:300])
        proposal = ResolutionProposal.objects.select_related("job").get(field_key=self.first)
        self.assertEqual((proposal.status, proposal.failure_reason), (ProposalStatus.RUNNING, ""))
        self.assertEqual(proposal.job.status, JobStatusChoices.STATUS_PENDING)
        entry = proposal.candidate_snapshot["candidates"][0]
        self.assertTrue(
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.CANDIDATE,
                explanation="The worker's answer still lands.",
                selected_candidate_id=entry["candidate_id"],
                selected_object_type=ObjectType.objects.get_for_model(Interface),
                selected_object_id=entry["object_id"],
            )
        )

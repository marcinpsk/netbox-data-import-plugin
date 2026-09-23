# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Proposal workspace commands through real previews, permissions, and the job queue."""

import uuid
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from dcim.models import Device, Interface, Site
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django_rq import get_queue
from redis.exceptions import ConnectionError as RedisConnectionError

from netbox_data_import.field_keys import SELECT_TERMINATION_TASK, termination_field_key
from netbox_data_import.inference_backend import proposal_eligible_set_limit
from netbox_data_import.jobs import ImportJobRunner, ResolutionProposalJob
from netbox_data_import.models import (
    ImportProfile,
    ProposalFailureReason,
    ProposalOutcome,
    ProposalStatus,
    ResolutionProposal,
    TerminationResolution,
)
from netbox_data_import.preview_row_actions import (
    PREVIEW_DIRTY_SESSION_KEY,
    PREVIEW_PLAN_SESSION_KEY,
    PREVIEW_REVISION_SESSION_KEY,
    retained_sync_block_reason,
)
from netbox_data_import.proposal_tasks import CandidateSnapshot
from netbox_data_import.resolution_proposals import cancel_proposal, claim_proposal, complete_proposal
from netbox_data_import.tests.helpers import trace_termination, trace_workbook_bytes, user_with_object_permission
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path


class ProposalErrorEnvelopeTest(SimpleTestCase):
    def test_invalid_proposal_id_does_not_disclose_exception_text(self):
        """A validation exception must not expose its text through the JSON boundary."""
        import json

        from netbox_data_import.views import InvalidProposalId, _TraceProposalMixin

        class InvalidProposalView(_TraceProposalMixin, View):
            def get(self, _request):
                raise InvalidProposalId("Sensitive implementation detail.")

        response = InvalidProposalView.as_view()(RequestFactory().get("/proposal"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content), {"ok": False, "error": "Enter a valid proposal_id integer."})

    def test_unexpected_value_errors_escape_the_proposal_envelope(self):
        """A programming failure is not invalid operator input."""
        from netbox_data_import.views import _TraceProposalMixin

        class ProgrammingFailureView(_TraceProposalMixin, View):
            def get(self, _request):
                raise ValueError("programming failure")

        with self.assertRaisesMessage(ValueError, "programming failure"):
            ProgrammingFailureView.as_view()(RequestFactory().get("/proposal"))


class ProposalWorkspaceTest(IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)
        self.field_key = termination_field_key(device="DEV-A", cards="", port="absent-port", kind="interface")
        upload = BytesIO(
            trace_workbook_bytes(
                path_blocks=[
                    direct_path(
                        from_end=trace_termination("DEV-A", "", "absent-port", "Port"),
                        to_end=trace_termination("DEV-B", "", "eth1", "Port"),
                    )
                ]
            )
        )
        upload.name = "traces.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(PREVIEW_PLAN_SESSION_KEY, self.client.session)

    def call(self, action, *, accept="application/json", **data):
        data.setdefault("preview_revision", self.client.session.get(PREVIEW_REVISION_SESSION_KEY, ""))
        if data["preview_revision"] is None:
            data.pop("preview_revision")
        url = reverse(f"plugins:netbox_data_import:trace_{action}")
        method = self.client.get if action == "proposal" else self.client.post
        with self.captureOnCommitCallbacks(execute=True):
            return method(url, data, headers={} if accept is None else {"accept": accept})

    def request_proposal(self):
        response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 200, response.content)
        return ResolutionProposal.objects.get(pk=response.json()["proposal_id"])

    def completed(self, *, no_match=False):
        proposal = self.request_proposal()
        entry = proposal.candidate_snapshot["candidates"][0]
        self.assertTrue(claim_proposal(proposal.pk))
        self.assertTrue(
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.NO_MATCH if no_match else ProposalOutcome.CANDIDATE,
                explanation="The candidate matches the source label.",
                selected_candidate_id=entry["candidate_id"],
                selected_object_type=ObjectType.objects.get_for_model(Interface),
                selected_object_id=entry["object_id"],
            )
        )
        return proposal

    def operator(self, *, decide=False, view_only=False, device=True, profile_scope=None):
        grants = [
            (
                ImportProfile,
                ["view"] if view_only else ["view", "change"],
                {"pk": self.profile.pk if profile_scope is None else profile_scope},
            ),
            (Site, ["view"], {}),
            (Interface, ["view"], {}),
        ]
        if device:
            grants.append((Device, ["view"], {"site_id": self.site.pk}))
        if decide:
            grants.append((TerminationResolution, ["add"], {"profile_id": self.profile.pk}))
        actor = user_with_object_permission(f"operator-{uuid.uuid4().hex}", grants)
        self.login_with_preview(actor)
        return actor

    def login_with_preview(self, actor):
        preview = {key: value for key, value in self.client.session.items() if key.startswith("import_")}
        self.client.force_login(actor)
        session = self.client.session
        session.update(preview)
        session.save()

    def assert_unwritten(self, proposal):
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "")
        self.assertIsNone(proposal.decided_by_id)
        self.assertIsNone(proposal.decided_at)
        self.assertIsNone(proposal.written_resolution_id)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_request_without_accept_requires_preview_revision(self):
        for revision in (None, "obsolete"):
            with self.subTest(revision=revision):
                response = self.call(
                    "request_proposal", accept=None, field_key=self.field_key, preview_revision=revision
                )
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json(), {"ok": False, "error": "No import preview in progress."})
                self.assertFalse(ResolutionProposal.objects.exists())

    def test_cancel_without_accept_requires_preview_revision(self):
        proposal = self.request_proposal()
        for revision in (None, "obsolete"):
            with self.subTest(revision=revision):
                response = self.call("cancel_proposal", accept=None, proposal_id=proposal.pk, preview_revision=revision)
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json(), {"ok": False, "error": "No import preview in progress."})
                proposal.refresh_from_db()
                self.assertEqual(proposal.status, ProposalStatus.QUEUED)
                self.assertEqual(ResolutionProposal.objects.count(), 1)

    def test_request_and_cancel_without_accept_allow_current_preview_revision(self):
        response = self.call("request_proposal", accept=None, field_key=self.field_key)
        self.assertEqual(response.status_code, 200, response.content)
        proposal = ResolutionProposal.objects.get(pk=response.json()["proposal_id"])
        self.assertEqual(proposal.status, ProposalStatus.QUEUED)
        response = self.call("cancel_proposal", accept=None, proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.CANCELLED)

    def test_reject_without_planning_target_access_preserves_accept_permission_check(self):
        proposal = self.completed()
        actor = user_with_object_permission(
            "profile-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Device, ["view"], {"site_id": self.site.pk}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile_id": self.profile.pk}),
            ],
        )
        self.login_with_preview(actor)
        self.assertFalse(Site.objects.restrict(actor, "view").filter(pk=self.site.pk).exists())
        response = self.call("accept_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"ok": False, "error": "That termination cannot be resolved here."})
        self.assert_unwritten(proposal)

        response = self.call("reject_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "rejected")
        self.assertEqual(proposal.decided_by_id, actor.pk)
        self.assertIsNotNone(proposal.decided_at)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_request_creates_queued_attempt_and_real_job_with_id_only(self):
        self.operator()
        proposal = self.request_proposal()
        self.assertEqual(proposal.status, ProposalStatus.QUEUED)
        self.assertEqual(proposal.field_key, self.field_key)
        self.assertEqual(proposal.resolved_device_id, self.device_a.pk)
        self.assertEqual(proposal.source_evidence["port"], "absent-port")
        self.assertEqual(proposal.candidate_snapshot["candidates"][0]["object_id"], self.eth0.pk)
        job = Job.objects.get(name=ResolutionProposalJob.Meta.name)
        queued = get_queue().fetch_job(str(job.job_id))
        self.assertIsNotNone(queued)
        self.assertEqual(queued.kwargs, {"job": job, "proposal_id": proposal.pk})
        self.assertEqual(job.user_id, proposal.requested_by_id)

    def test_the_request_records_the_background_job_on_the_attempt(self):
        """Nothing else links the two, so an unrecorded job leaves the card unable to say anything."""
        self.operator()

        proposal = self.request_proposal()

        self.assertEqual(proposal.job_id, Job.objects.get(name=ResolutionProposalJob.Meta.name).pk)

    def card(self):
        """Return the card the workspace polls for, as the logged-in operator reads it."""
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["presentation"]

    def test_a_queued_card_reports_where_its_background_job_is(self):
        """A job no worker has taken reads exactly like one that started, which is the whole bug."""
        self.operator()
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.request_proposal()

        card = self.card()

        self.assertIn("Pending", card["job_status"])
        self.assertIn("requested", card["job_status"])
        self.assertEqual(card["job_note"], "")
        self.assertEqual(card["page_status"], "")

    def test_a_card_names_a_background_job_that_ended_without_a_result(self):
        """A worker killed mid-run leaves the attempt active forever, with nothing else to show it."""
        self.operator()
        proposal = self.request_proposal()
        self.assertTrue(claim_proposal(proposal.pk))
        Job.objects.filter(pk=proposal.job_id).update(status=JobStatusChoices.STATUS_ERRORED)

        card = self.card()

        self.assertIn("Errored", card["job_status"])
        self.assertIn("ended without recording a result", card["job_note"])

    def test_a_card_says_when_no_background_job_is_recorded(self):
        """The job row can go, and a queued attempt with no job is never going to run."""
        self.operator()
        proposal = self.request_proposal()
        Job.objects.filter(pk=proposal.job_id).delete()

        card = self.card()

        self.assertIn("none recorded", card["job_status"])
        self.assertIn("No background job is recorded", card["job_note"])

    def test_a_settled_card_reports_no_background_job(self):
        """Once the attempt has its answer the job is spent, so the line would only be noise."""
        self.operator()
        self.completed()

        card = self.card()

        self.assertEqual(card["job_status"], "")
        self.assertEqual(card["job_note"], "")

    def test_request_keeps_the_resolved_device_and_candidates_from_one_inventory_read(self):
        from django.db import connection

        resolution_reads = []

        def rename_after_resolved_device_read(execute, sql, params, many, context):
            """Rename both Devices after the one-name query has read the original row."""
            result = execute(sql, params, many, context)
            device_table = connection.ops.quote_name(Device._meta.db_table)
            query_values = tuple(params or ())
            if (
                f"FROM {device_table}" in sql
                and "UPPER" in sql
                and "DEV-A" in query_values
                and "DEV-B" not in query_values
            ):
                resolution_reads.append(True)
                if len(resolution_reads) == 1:
                    Device.objects.filter(pk=self.device_a.pk).update(name="DEV-A-previous")
                    Device.objects.filter(pk=self.device_b.pk).update(name="DEV-A")
            return result

        with connection.execute_wrapper(rename_after_resolved_device_read):
            proposal = self.request_proposal()

        self.assertEqual(resolution_reads, [True])
        self.assertEqual(proposal.resolved_device_id, self.device_a.pk)
        self.assertEqual(proposal.candidate_snapshot["candidates"][0]["object_id"], self.eth0.pk)

    def dense_device(self, *names):
        """Give DEV-A more eligible interfaces than one request offers."""
        for name in names or ("eth5", "eth6"):
            Interface.objects.create(device=self.device_a, name=name, type="1000base-t")

    def no_match(self, proposal):
        """Settle one attempt the way a backend that found nothing in its page would."""
        self.assertTrue(claim_proposal(proposal.pk))
        self.assertTrue(
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.NO_MATCH,
                explanation="No candidate in this page names the source port.",
            )
        )

    def test_a_dense_device_is_offered_one_page_and_asked_again_for_the_next(self):
        """120 candidates used to refuse the request outright; they are searched in turns now."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            first = self.request_proposal()
            self.assertEqual(first.candidate_snapshot["total"], 3)
            self.assertEqual((first.candidate_snapshot["page_offset"], first.candidate_snapshot["page_size"]), (0, 2))
            self.no_match(first)

            second = self.request_proposal()

        self.assertEqual(second.candidate_snapshot["page_offset"], 2)
        self.assertEqual(second.candidate_snapshot["total"], 3)

    def test_the_search_starts_again_once_the_last_page_found_nothing(self):
        """The whole set has been seen, so the next request is a fresh search, not a fourth page."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())
            self.no_match(self.request_proposal())

            third = self.request_proposal()

        self.assertEqual(third.candidate_snapshot["page_offset"], 0)

    def test_a_changed_eligible_set_restarts_the_search(self):
        """A new port renumbers every page, so continuing from the old offset would skip candidates."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())
            Interface.objects.create(device=self.device_a, name="eth7", type="1000base-t")

            second = self.request_proposal()

        self.assertEqual(second.candidate_snapshot["page_offset"], 0)
        self.assertEqual(second.candidate_snapshot["total"], 4)

    def test_a_candidate_answer_does_not_advance_the_page(self):
        """Only a page that found nothing is exhausted; an answered one is waiting for a decision."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            proposal = self.request_proposal()
            entry = proposal.candidate_snapshot["candidates"][0]
            self.assertTrue(claim_proposal(proposal.pk))
            self.assertTrue(
                complete_proposal(
                    proposal.pk,
                    outcome=ProposalOutcome.CANDIDATE,
                    explanation="The label matches.",
                    selected_candidate_id=entry["candidate_id"],
                    selected_object_type=ObjectType.objects.get_for_model(Interface),
                    selected_object_id=entry["object_id"],
                )
            )

            second = self.request_proposal()

        self.assertEqual(second.candidate_snapshot["page_offset"], 0)

    def card_action(self, key, card=None):
        """Return one command as the card offers it."""
        return next(item for item in (card or self.card())["actions"] if item["key"] == key)

    def test_a_paged_card_names_the_candidates_it_searched(self):
        """A no_match means nothing without the range it searched."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())

        self.assertEqual(self.card()["page_status"], "Searched candidates 1-2 of 3.")

    def test_a_card_rebuilds_its_offered_page_once(self):
        """Polling a card must not deserialize its complete candidate set twice."""
        from netbox_data_import.cable_target import UNRESOLVED
        from netbox_data_import.netbox_reader import NetBoxReader
        from netbox_data_import.proposal_presentation import ProposalPresentation

        class CountingPresentation(ProposalPresentation):
            offered_page_calls = 0

            def offered_page(self, proposal):
                self.offered_page_calls += 1
                return super().offered_page(proposal)

        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())
            reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)
            presentation = CountingPresentation(profile=self.profile, actor=self.actor, reader=reader)
            payload = presentation.fields(({"field_key": self.field_key, "state": UNRESOLVED},))

        self.assertEqual(payload[self.field_key]["presentation"]["page_status"], "Searched candidates 1-2 of 3.")
        self.assertEqual(presentation.offered_page_calls, 1)

    def test_a_no_match_with_a_next_page_offers_the_next_one(self):
        """Nothing in this page is not nothing on the Device, and the card has to say which."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())

        card = self.card()
        self.assertEqual(self.card_action("request", card)["label"], "Ask AI: next 1")
        self.assertEqual(
            self.card_action("accept", card)["reason"],
            "No match in candidates 1-2 of 3. Ask AI for the next 1.",
        )

    def test_a_no_match_over_the_whole_set_stays_a_plain_no_match(self):
        """With every candidate searched there is no next page and nothing left to offer."""
        self.no_match(self.request_proposal())

        card = self.card()
        self.assertEqual(card["page_status"], "")
        self.assertEqual(self.card_action("request", card)["label"], "Ask AI")
        self.assertEqual(
            self.card_action("accept", card)["reason"],
            "The backend found no match. There is no candidate to accept.",
        )

    def test_the_next_page_offer_names_one_page_not_the_whole_remainder(self):
        """Five candidates in pages of two leave three, but the next request only sends two."""
        self.dense_device("eth5", "eth6", "eth7", "eth8")
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())

            card = self.card()

        self.assertEqual(self.card_action("request", card)["label"], "Ask AI: next 2")
        self.assertEqual(
            self.card_action("accept", card)["reason"],
            "No match in candidates 1-2 of 5. Ask AI for the next 2.",
        )

    def test_a_changed_set_advertises_a_restart_and_not_a_continuation(self):
        """The next request restarts on a changed set, so promising a continuation is a lie."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())
            Interface.objects.create(device=self.device_a, name="eth7", type="1000base-t")

            card = self.card()

        self.assertEqual(self.card_action("request", card)["label"], "Ask AI")
        self.assertEqual(
            self.card_action("accept", card)["reason"],
            "The resolved Device or eligible candidates changed. Request a new proposal.",
        )

    def test_a_replaced_resolved_device_restarts_the_search(self):
        """The same ports moved wholesale, so the candidate set matches while the Device did not."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())
            old_name = self.device_a.name
            self.device_a.name = "Former DEV-A"
            self.device_a.save()
            replacement = Device.objects.create(
                name=old_name, site=self.site, device_type=self.device_type, role=self.role
            )
            Interface.objects.filter(device=self.device_a).update(device=replacement)
            # The card already calls this a restart; the request has to agree with it.
            self.assertEqual(self.card_action("request")["label"], "Ask AI")

            second = self.request_proposal()

        self.assertEqual(second.candidate_snapshot["page_offset"], 0)

    def test_a_rejected_candidate_moves_the_search_to_the_next_page(self):
        """Otherwise one confident wrong answer on page one hides every later candidate for good."""
        from netbox_data_import.proposal_decisions import reject_proposal

        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            proposal = self.request_proposal()
            entry = proposal.candidate_snapshot["candidates"][0]
            self.assertTrue(claim_proposal(proposal.pk))
            self.assertTrue(
                complete_proposal(
                    proposal.pk,
                    outcome=ProposalOutcome.CANDIDATE,
                    explanation="The label matches.",
                    selected_candidate_id=entry["candidate_id"],
                    selected_object_type=ObjectType.objects.get_for_model(Interface),
                    selected_object_id=entry["object_id"],
                )
            )
            self.assertTrue(reject_proposal(proposal.pk, operator=self.actor))

            second = self.request_proposal()

        self.assertEqual(second.candidate_snapshot["page_offset"], 2)

    def test_a_request_refuses_a_predecessor_it_observed_running(self):
        """The worker can settle between the read and the insert, and page one is searched twice."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            first = self.request_proposal()
            self.assertTrue(claim_proposal(first.pk))
            settled = []

            proposal_table = connection.ops.quote_name(ResolutionProposal._meta.db_table)

            def settle_the_predecessor_after_the_active_read(execute, sql, params, many, context):
                result = execute(sql, params, many, context)
                if (
                    not settled
                    and sql.startswith("SELECT")
                    and f"FROM {proposal_table}" in sql
                    and '"status" IN' in sql
                ):
                    settled.append(True)
                    complete_proposal(first.pk, outcome=ProposalOutcome.NO_MATCH, explanation="Nothing in this page.")
                return result

            with connection.execute_wrapper(settle_the_predecessor_after_the_active_read):
                response = self.call("request_proposal", field_key=self.field_key)

        self.assertEqual(settled, [True])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(ResolutionProposal.objects.count(), 1)

    def answer_with(self, proposal, position):
        """Settle one attempt on the candidate at *position* in its whole set."""
        entry = proposal.candidate_snapshot["candidates"][position]
        self.assertTrue(claim_proposal(proposal.pk))
        self.assertTrue(
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.CANDIDATE,
                explanation="The label matches.",
                selected_candidate_id=entry["candidate_id"],
                selected_object_type=ObjectType.objects.get_for_model(Interface),
                selected_object_id=entry["object_id"],
            )
        )
        return entry

    def test_a_candidate_found_on_a_later_page_is_accepted_and_written(self):
        """Paging is worthless if the answer it finds cannot be applied."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            self.no_match(self.request_proposal())
            second = self.request_proposal()
            self.assertEqual(second.candidate_snapshot["page_offset"], 2)
            entry = self.answer_with(second, 2)

        response = self.call("accept_proposal", proposal_id=second.pk)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(TerminationResolution.objects.get(profile=self.profile).selected_object_id, entry["object_id"])

    def test_a_change_outside_the_offered_page_still_refuses_acceptance(self):
        """The whole set is the evidence, so a candidate the prompt never saw still ages it."""
        self.dense_device()
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 2}}):
            proposal = self.request_proposal()
            self.answer_with(proposal, 0)
        outside = Interface.objects.get(device=self.device_a, name="eth6")
        offered = CandidateSnapshot.from_json(proposal.candidate_snapshot)
        self.assertNotIn(outside.pk, [entry.object_id for entry in offered.page])
        outside.name = "eth6 renamed"
        outside.save()

        response = self.call("accept_proposal", proposal_id=proposal.pk)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_second_active_request_refuses_and_terminal_attempt_allows_retry(self):
        proposal = self.request_proposal()
        response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 409)
        self.assertIn("active", response.json()["error"])
        self.assertEqual(ResolutionProposal.objects.count(), 1)
        self.assertEqual(Job.objects.filter(name=ResolutionProposalJob.Meta.name).count(), 1)
        cancel_proposal(proposal.pk)
        retry = self.request_proposal()
        self.assertNotEqual(proposal.pk, retry.pk)
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(response.json()["proposal"]["id"], retry.pk)

    def test_request_refuses_retired_adapter_and_discards_preview(self):
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired-adapter")

        response = self.call("request_proposal", field_key=self.field_key)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        self.assertIn("retired-adapter", response.json()["error"])
        self.assertFalse(ResolutionProposal.objects.exists())
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_enqueue_failure_fails_attempt_and_allows_retry(self):
        with patch.object(ResolutionProposalJob, "enqueue", autospec=True, side_effect=RedisConnectionError):
            response = self.call("request_proposal", field_key=self.field_key)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "The proposal queue is unavailable. Try again later."},
        )
        proposal = ResolutionProposal.objects.get(profile=self.profile, field_key=self.field_key)
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.QUEUE_UNAVAILABLE)
        retry = self.request_proposal()
        self.assertNotEqual(retry.pk, proposal.pk)
        self.assertEqual(retry.status, ProposalStatus.QUEUED)

    def test_unexpected_enqueue_failure_propagates_after_releasing_attempt(self):
        with patch.object(ResolutionProposalJob, "enqueue", autospec=True, side_effect=TypeError("Programming error.")):
            with self.assertRaises(TypeError):
                self.call("request_proposal", field_key=self.field_key)

        proposal = ResolutionProposal.objects.get(profile=self.profile, field_key=self.field_key)
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.QUEUE_UNAVAILABLE)
        self.assertNotEqual(self.request_proposal().pk, proposal.pk)

    def test_proposal_actions_report_missing_or_non_numeric_ids(self):
        for action in ("cancel_proposal", "accept_proposal", "reject_proposal"):
            for data in ({}, {"proposal_id": "abc"}):
                with self.subTest(action=action, data=data):
                    response = self.call(action, **data)

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"ok": False, "error": "Enter a valid proposal_id integer."})

    def test_request_refuses_automatically_resolved_field(self):
        response = self.call(
            "request_proposal", field_key=termination_field_key(device="DEV-B", cards="", port="eth1", kind="interface")
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("already resolved", response.json()["error"])
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_request_rechecks_resolution_after_the_preview(self):
        Interface.objects.create(device=self.device_a, name="absent-port", type="1000base-t")
        response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_request_refuses_saved_resolution_without_replanning(self):
        proposal = self.completed()
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 200)
        response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ResolutionProposal.objects.count(), 1)

    def test_no_candidates_has_its_own_reason(self):
        self.eth0.delete()
        response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "no_candidates")
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_too_many_candidates_has_its_own_reason(self):
        """Past the eligible-set ceiling the answer is pre-filtering, not another page."""
        existing = Interface.objects.filter(device=self.device_a).count()
        Interface.objects.bulk_create(
            Interface(device=self.device_a, name=f"extra-{number}", type="1000base-t")
            for number in range(proposal_eligible_set_limit() + 1 - existing)
        )

        response = self.call("request_proposal", field_key=self.field_key)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "too_many_candidates")
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_a_set_over_the_page_size_is_paged_instead_of_refused(self):
        """This is the refusal operators hit on dense equipment; it is a page now, not an error."""
        Interface.objects.create(device=self.device_a, name="extra", type="1000base-t")

        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 1}}):
            response = self.call("request_proposal", field_key=self.field_key)

        self.assertEqual(response.status_code, 200, response.content)
        stored = ResolutionProposal.objects.get(pk=response.json()["proposal_id"]).candidate_snapshot
        self.assertEqual((stored["total"], stored["page_size"]), (2, 1))

    def test_read_computes_candidate_staleness_with_view_only_profile_access(self):
        proposal = self.completed()
        self.operator(view_only=True)
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["proposal"]["id"], proposal.pk)
        self.assertEqual(
            response.json()["staleness"],
            {"is_stale": False, "resolved_device_changed": False, "candidates_changed": False},
        )
        self.eth0.name = "changed"
        self.eth0.save()
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(
            response.json()["staleness"],
            {"is_stale": True, "resolved_device_changed": False, "candidates_changed": True},
        )

    def test_read_computes_device_staleness(self):
        self.completed()
        self.device_a.name = "changed"
        self.device_a.save()
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(
            response.json()["staleness"],
            {"is_stale": True, "resolved_device_changed": True, "candidates_changed": True},
        )

    def test_read_uses_actor_inventory_scope(self):
        proposal = self.completed()
        self.operator(view_only=True, device=False)
        response = self.call("proposal", field_key=self.field_key)
        payload = response.json()
        self.assertTrue(payload["staleness"]["resolved_device_changed"])
        self.assertEqual(payload["proposal"], {"id": proposal.pk})
        self.assertEqual(payload["presentation"]["candidate"], "")

    def test_read_without_an_attempt_returns_null(self):
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(
            {
                key: response.json()[key]
                for key in ("ok", "proposal", "staleness", "history_display", "history_has_more", "history_url")
            },
            {
                "ok": True,
                "proposal": None,
                "staleness": None,
                "history_display": [],
                "history_has_more": False,
                "history_url": None,
            },
        )

    def test_malformed_candidate_object_type_is_logged_and_refused(self):
        proposal = self.completed()
        candidate_snapshot = proposal.candidate_snapshot
        candidate_snapshot["candidates"][0]["object_type"] = "invalid"
        ResolutionProposal.objects.filter(pk=proposal.pk).update(candidate_snapshot=candidate_snapshot)

        with self.assertLogs("netbox_data_import.views", level="WARNING") as operator_log:
            response = self.call("accept_proposal", proposal_id=proposal.pk)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"ok": False, "error": "That termination cannot be resolved here."})
        self.assertIn("TraceAcceptProposalView: termination refused", operator_log.output[0])

    def test_cancel_queued_and_running_by_another_operator(self):
        for running in (False, True):
            with self.subTest(running=running):
                self.login_with_preview(self.actor)
                proposal = self.request_proposal()
                if running:
                    claim_proposal(proposal.pk)
                actor = self.operator()
                self.assertNotEqual(actor.pk, proposal.requested_by_id)
                response = self.call("cancel_proposal", proposal_id=proposal.pk)
                self.assertEqual(response.status_code, 200)
                proposal.refresh_from_db()
                self.assertEqual(proposal.status, ProposalStatus.CANCELLED)

    def test_cancel_requires_device_access(self):
        proposal = self.request_proposal()
        self.operator(device=False)
        with self.assertLogs("netbox_data_import.views", level="WARNING") as operator_log:
            response = self.call("cancel_proposal", proposal_id=proposal.pk)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "Permission denied: this action is outside your NetBox object permissions."},
        )
        self.assertIn("dcim.view_device", operator_log.output[0])
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.QUEUED)

    def test_request_requires_device_access(self):
        self.operator(device=False)
        response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "The resolved Device is unavailable or outside your view permission."},
        )
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_accept_by_another_operator_writes_resolution_and_requires_recalculation(self):
        proposal = self.completed()
        actor = self.operator(decide=True)
        self.assertNotEqual(actor.pk, proposal.requested_by_id)
        before = self.client.session[PREVIEW_PLAN_SESSION_KEY]
        revision = self.client.session[PREVIEW_REVISION_SESSION_KEY]
        response = self.call("accept_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(response.json()["preview_state"], "recalculation_required")
        row = TerminationResolution.objects.get(profile=self.profile)
        self.assertEqual(row.selected_object_id, self.eth0.pk)
        self.assertEqual(proposal.written_resolution_id, row.pk)
        self.assertEqual(proposal.decision, "accepted")
        self.assertEqual(proposal.decided_by_id, actor.pk)
        self.assertIsNotNone(proposal.decided_at)
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], before)
        self.assertEqual(self.client.session[PREVIEW_REVISION_SESSION_KEY], revision)
        self.assertTrue(self.client.session[PREVIEW_DIRTY_SESSION_KEY])

    def test_reject_by_another_operator_records_decision_only(self):
        proposal = self.completed(no_match=True)
        actor = self.operator(decide=True)
        response = self.call("reject_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 200)
        proposal.refresh_from_db()
        self.assertNotEqual(actor.pk, proposal.requested_by_id)
        self.assertEqual(proposal.decision, "rejected")
        self.assertEqual(proposal.decided_by_id, actor.pk)
        self.assertIsNotNone(proposal.decided_at)
        self.assertEqual(proposal.status, ProposalStatus.COMPLETED)
        self.assertIsNone(proposal.written_resolution_id)
        self.assertFalse(TerminationResolution.objects.exists())

    def test_acceptance_requires_the_resolution_permission(self):
        proposal = self.completed()
        self.operator()

        response = self.call("accept_proposal", proposal_id=proposal.pk)

        self.assertEqual(response.status_code, 403)
        self.assert_unwritten(proposal)

    def test_acceptance_of_an_existing_resolution_requires_change_scope(self):
        proposal = self.completed()
        resolution = TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
            selected_object_type=ObjectType.objects.get_for_model(Interface),
            selected_object_id=self.eth0.pk,
            selected_display_name="Previous selection",
        )
        actor = user_with_object_permission(
            "add-only-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile_id": self.profile.pk}),
            ],
        )
        self.login_with_preview(actor)

        self.assertIn("permission", self.presentation()["actions"][2]["reason"])
        response = self.call("accept_proposal", proposal_id=proposal.pk)

        self.assertEqual(response.status_code, 403)
        proposal.refresh_from_db()
        resolution.refresh_from_db()
        self.assertEqual(proposal.decision, "")
        self.assertIsNone(proposal.written_resolution_id)
        self.assertEqual(resolution.selected_display_name, "Previous selection")

    def test_acceptance_fails_closed_for_an_invalid_existing_resolution_scope(self):
        proposal = self.completed()
        resolution = TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
            selected_object_type=ObjectType.objects.get_for_model(Interface),
            selected_object_id=self.eth0.pk,
            selected_display_name="Previous selection",
        )
        actor = user_with_object_permission(
            "invalid-change-scope-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["change"], {"missing_field": "value"}),
            ],
        )
        self.login_with_preview(actor)

        self.assertIn("permission", self.presentation()["actions"][2]["reason"])
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 403)
        proposal.refresh_from_db()
        resolution.refresh_from_db()
        self.assertEqual(proposal.decision, "")
        self.assertIsNone(proposal.written_resolution_id)
        self.assertEqual(resolution.selected_display_name, "Previous selection")

    def test_rejection_takes_the_workspace_permission_instead(self):
        """Rejection writes no Row Resolution, so specification 7.6 scopes it by the preview."""
        proposal = self.completed()
        self.operator()

        response = self.call("reject_proposal", proposal_id=proposal.pk)

        self.assertEqual(response.status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "rejected")
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_stale_acceptance_refuses_without_any_write(self):
        proposal = self.completed()
        self.eth0.name = "changed"
        self.eth0.save()
        before = dict(self.client.session)
        response = self.call("accept_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 409)
        self.assert_unwritten(proposal)
        self.assertEqual(dict(self.client.session), before)

    def test_accept_preserves_retained_preview_and_sync_guard(self):
        proposal = self.completed()
        context = self.client.session["import_context"]
        Job.objects.create(
            name=ImportJobRunner.name,
            user=self.actor,
            job_id=uuid.uuid4(),
            status=JobStatusChoices.STATUS_PENDING,
            data={
                "job_type": ImportJobRunner.job_type,
                "keeps_preview": True,
                "profile_id": self.profile.pk,
                "source_document_id": context["source_document_id"],
            },
        )
        before = self.client.session[PREVIEW_PLAN_SESSION_KEY]
        self.assertTrue(retained_sync_block_reason(self.client.session, self.actor))
        response = self.call("accept_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], before)
        self.assertTrue(retained_sync_block_reason(self.client.session, self.actor))
        self.assertTrue(self.client.session[PREVIEW_DIRTY_SESSION_KEY])
        self.assertTrue(TerminationResolution.objects.exists())

    def test_all_endpoints_refuse_without_preview(self):
        proposal = self.completed()
        session = self.client.session
        session.pop("import_preview_pending")
        session.save()
        for action in ("request_proposal", "proposal", "cancel_proposal", "accept_proposal", "reject_proposal"):
            with self.subTest(action=action):
                response = self.call(action, field_key=self.field_key, proposal_id=proposal.pk)
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.json()["ok"])
        self.assert_unwritten(proposal)
        self.assertEqual(ResolutionProposal.objects.count(), 1)

    def test_all_endpoints_enforce_profile_scope(self):
        proposal = self.completed()
        self.operator(decide=True, profile_scope=self.profile.pk + 1)
        for action in ("request_proposal", "proposal", "cancel_proposal", "accept_proposal", "reject_proposal"):
            with self.subTest(action=action):
                response = self.call(action, field_key=self.field_key, proposal_id=proposal.pk)
                self.assertEqual(response.status_code, 409)
        self.assert_unwritten(proposal)

    def test_commands_cannot_address_another_profiles_proposal(self):
        proposal = self.completed()
        other = ImportProfile.objects.create(name="Other", source_adapter="trace_workbook", adapter_config={})
        ResolutionProposal.objects.filter(pk=proposal.pk).update(profile=other)
        for action in ("cancel_proposal", "accept_proposal", "reject_proposal"):
            with self.subTest(action=action):
                response = self.call(action, proposal_id=proposal.pk)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"ok": False, "error": "That proposal is no longer available."})
        self.assertIsNone(self.call("proposal", field_key=self.field_key).json()["proposal"])
        self.assert_unwritten(proposal)

    def test_proposal_deletion_races_use_the_json_404_envelope(self):
        """Each row that can disappear mid-request maps to the controller's JSON contract."""
        import json

        from django.http import Http404
        from django.test import RequestFactory
        from django.views import View

        from netbox_data_import.views import _TraceProposalMixin

        cases = (
            (Http404(), "That proposal is no longer available."),
            (ImportProfile.DoesNotExist(), "The import profile is no longer available."),
            (ResolutionProposal.DoesNotExist(), "That proposal is no longer available."),
        )
        for failure, message in cases:
            with self.subTest(failure=type(failure).__name__):

                class DisappearingProposalView(_TraceProposalMixin, View):
                    def get(self, _request, _failure=failure):
                        raise _failure

                response = DisappearingProposalView.as_view()(RequestFactory().get("/proposal"))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(json.loads(response.content), {"ok": False, "error": message})

    def test_invalid_field_and_unoffered_field_are_refused(self):
        for key in ("invalid", termination_field_key(device="DEV-A", cards="", port="invented", kind="interface")):
            with self.subTest(key=key):
                self.assertEqual(self.call("request_proposal", field_key=key).status_code, 400)
        self.assertFalse(ResolutionProposal.objects.exists())

    def test_terminal_cancel_and_repeat_decisions_refuse(self):
        proposal = self.completed()
        self.assertEqual(self.call("cancel_proposal", proposal_id=proposal.pk).status_code, 409)
        self.assertEqual(self.call("reject_proposal", proposal_id=proposal.pk).status_code, 200)
        for action in ("accept_proposal", "reject_proposal"):
            self.assertEqual(self.call(action, proposal_id=proposal.pk).status_code, 409)
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "rejected")
        self.assertFalse(TerminationResolution.objects.exists())

    def test_all_endpoints_refuse_a_stale_preview_revision(self):
        proposal = self.completed()
        for action in ("request_proposal", "proposal", "cancel_proposal", "accept_proposal", "reject_proposal"):
            with self.subTest(action=action):
                response = self.call(
                    action, field_key=self.field_key, proposal_id=proposal.pk, preview_revision="obsolete"
                )
                self.assertEqual(response.status_code, 409)
        self.assert_unwritten(proposal)
        self.assertEqual(ResolutionProposal.objects.count(), 1)

    def test_no_match_cannot_be_accepted(self):
        proposal = self.completed(no_match=True)
        response = self.call("accept_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 409)
        self.assert_unwritten(proposal)

    def test_queued_proposal_cannot_be_decided(self):
        proposal = self.request_proposal()
        for action in ("accept_proposal", "reject_proposal"):
            with self.subTest(action=action):
                self.assertEqual(self.call(action, proposal_id=proposal.pk).status_code, 409)
        self.assert_unwritten(proposal)
        self.assertEqual(proposal.status, ProposalStatus.QUEUED)

    def test_accept_obeys_resolution_object_permission_constraints(self):
        proposal = self.completed()
        actor = user_with_object_permission(
            "out-of-scope-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile_id": self.profile.pk + 1}),
            ],
        )
        self.login_with_preview(actor)
        self.assertIn("permission", self.presentation()["actions"][2]["reason"])
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 403)
        self.assert_unwritten(proposal)

    def test_accept_does_not_treat_the_synthetic_primary_key_as_real(self):
        proposal = self.completed()
        actor = user_with_object_permission(
            "synthetic-primary-key-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile__termination_resolutions__pk": -1}),
            ],
        )
        self.login_with_preview(actor)

        self.assertIn("permission", self.presentation()["actions"][2]["reason"])
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 403)
        self.assert_unwritten(proposal)

    def test_accept_detects_an_implicit_related_primary_key_lookup(self):
        proposal = self.completed()
        actor = user_with_object_permission(
            "implicit-primary-key-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile__termination_resolutions": -1}),
            ],
        )
        self.login_with_preview(actor)

        self.assertIn("permission", self.presentation()["actions"][2]["reason"])
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 403)
        self.assert_unwritten(proposal)

    def test_accept_fails_closed_for_an_empty_related_primary_key_set(self):
        proposal = self.completed()
        actor = user_with_object_permission(
            "empty-primary-key-set-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile__termination_resolutions__pk__in": []}),
            ],
        )
        self.login_with_preview(actor)

        self.assertIn("permission", self.presentation()["actions"][2]["reason"])
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 403)
        self.assert_unwritten(proposal)

    def test_accept_allows_a_cyclic_primary_key_constraint_with_a_saved_witness(self):
        proposal = self.completed()
        sibling = TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=termination_field_key(device="dev-a", cards="", port="other-port", kind="interface"),
            selected_object_type=ObjectType.objects.get_for_model(Interface),
            selected_object_id=self.eth0.pk,
            selected_display_name="Existing selection",
        )
        actor = user_with_object_permission(
            "primary-key-witness-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (TerminationResolution, ["add"], {"profile__termination_resolutions__pk": sibling.pk}),
            ],
        )
        self.login_with_preview(actor)

        self.assertEqual(self.presentation()["actions"][2]["reason"], "")
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "accepted")
        self.assertEqual(TerminationResolution.objects.filter(profile=self.profile).count(), 2)

    def test_a_primary_key_witness_does_not_hide_the_candidate_from_another_cyclic_path(self):
        proposal = self.completed()
        sibling = TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=termination_field_key(device="dev-a", cards="", port="other-port", kind="interface"),
            selected_object_type=ObjectType.objects.get_for_model(Interface),
            selected_object_id=self.eth0.pk,
            selected_display_name="Existing selection",
        )
        actor = user_with_object_permission(
            "nested-primary-key-witness-decider",
            [
                (ImportProfile, ["view", "change"], {"pk": self.profile.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
                (
                    TerminationResolution,
                    ["add"],
                    {
                        "profile__termination_resolutions__pk": sibling.pk,
                        "profile__termination_resolutions__profile__termination_resolutions__field_key": (
                            self.field_key
                        ),
                    },
                ),
            ],
        )
        self.login_with_preview(actor)

        self.assertEqual(self.presentation()["actions"][2]["reason"], "")
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 200)
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "accepted")
        self.assertEqual(TerminationResolution.objects.filter(profile=self.profile).count(), 2)

    def test_proposal_survives_replanning_after_its_field_leaves_the_preview(self):
        proposal = self.completed()
        before = self.client.session[PREVIEW_REVISION_SESSION_KEY]
        self.client.force_login(self.actor)
        upload = BytesIO(
            trace_workbook_bytes(
                path_blocks=[
                    direct_path(
                        from_end=trace_termination("DEV-A", "", "new-port", "Port"),
                        to_end=trace_termination("DEV-B", "", "eth1", "Port"),
                    )
                ]
            )
        )
        upload.name = "same-traces.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.client.session[PREVIEW_REVISION_SESSION_KEY], before)
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(response.json()["proposal"]["id"], proposal.pk)
        self.assertFalse(response.json()["staleness"]["is_stale"])

    def test_profile_view_alone_can_read_when_planning_target_is_not_visible(self):
        proposal = self.completed()
        actor = user_with_object_permission("profile-viewer", [(ImportProfile, ["view"], {"pk": self.profile.pk})])
        self.login_with_preview(actor)
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["proposal"]["id"], proposal.pk)
        self.assertIsNone(response.json()["staleness"])
        self.assertIn("outside your view scope", response.json()["staleness_error"])

    def presentation(self, field_key=None):
        return self.call("proposal", field_key=field_key or self.field_key).json()["presentation"]

    def test_history_returns_every_attempt_newest_first_with_status_and_outcome(self):
        first = self.completed(no_match=True)
        second = self.request_proposal()
        cancel_proposal(second.pk)
        latest = self.request_proposal()
        payload = self.call("proposal", field_key=self.field_key).json()
        self.assertEqual(payload["proposal"]["id"], latest.pk)
        self.assertEqual(
            [(row["id"], row["status"], row["outcome"]) for row in payload["history_display"]],
            [
                (latest.pk, "Queued", "No outcome"),
                (second.pk, "Cancelled", "No outcome"),
                (first.pk, "Completed", "No match"),
            ],
        )
        self.assertFalse(payload["history_has_more"])
        self.assertNotIn("history", payload)

    def test_workspace_history_is_limited_to_ten_recent_attempts(self):
        attempts = [self.completed(no_match=True).pk for _ in range(12)]
        ResolutionProposal.objects.filter(pk__in=attempts).update(created=timezone.now())
        ResolutionProposal.objects.filter(pk=attempts[-2]).update(
            candidate_snapshot={"sentinel": "older-snapshot-must-not-be-serialized"}
        )

        response = self.call("proposal", field_key=self.field_key)
        payload = response.json()

        expected = list(reversed(attempts[-10:]))
        self.assertEqual([row["id"] for row in payload["history_display"]], expected)
        self.assertTrue(payload["history_has_more"])
        self.assertNotIn("history", payload)
        self.assertNotIn("older-snapshot-must-not-be-serialized", response.content.decode())
        history_url = urlsplit(payload["history_url"])
        self.assertEqual(
            parse_qs(history_url.query),
            {"profile_id": [str(self.profile.pk)], "field_key": [self.field_key]},
        )

    def test_history_excludes_other_profiles_and_fields(self):
        other = self.completed()
        other_profile = ImportProfile.objects.create(name="Other history", source_adapter="trace_workbook")
        ResolutionProposal.objects.filter(pk=other.pk).update(profile=other_profile)
        wrong_field = self.completed()
        ResolutionProposal.objects.filter(pk=wrong_field.pk).update(
            field_key=termination_field_key(device="DEV-B", cards="", port="absent-port", kind="interface")
        )
        own = self.completed()
        payload = self.call("proposal", field_key=self.field_key).json()
        self.assertEqual([row["id"] for row in payload["history_display"]], [own.pk])
        self.operator(view_only=True, profile_scope=other_profile.pk)
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("history", response.json())

    def test_profile_view_only_can_read_history_without_target_access(self):
        proposal = self.completed()
        actor = user_with_object_permission("history-viewer", [(ImportProfile, ["view"], {"pk": self.profile.pk})])
        self.login_with_preview(actor)
        payload = self.call("proposal", field_key=self.field_key).json()
        self.assertEqual([row["id"] for row in payload["history_display"]], [proposal.pk])
        history = self.client.get(payload["history_url"])
        self.assertEqual(history.status_code, 200)
        self.assertEqual([row["id"] for row in history.json()["results"]], [proposal.pk])
        self.assertIsNone(payload["staleness"])
        actions = {action["key"]: action for action in payload["presentation"]["actions"]}
        self.assertTrue(all(actions[key]["reason"] for key in ("request", "cancel", "accept")))
        self.assertEqual(actions["reject"]["reason"], "")

    def test_workspace_supplies_affordances_without_editing_the_plan(self):
        from netbox_data_import.tests.test_inference_backend import ALLOWLIST, FALLBACK

        before = self.client.session[PREVIEW_PLAN_SESSION_KEY]
        with override_settings(
            PLUGINS_CONFIG={
                "netbox_data_import": {
                    "inference_backend": FALLBACK,
                    "inference_backend_origin_allowlist": ALLOWLIST,
                }
            }
        ):
            response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        fields = response.context["proposal_fields"]
        self.assertEqual(fields[self.field_key]["presentation"]["actions"][0]["reason"], "")
        resolved = termination_field_key(device="DEV-B", cards="", port="eth1", kind="interface")
        self.assertIn("already resolved", fields[resolved]["presentation"]["actions"][0]["reason"])
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], before)
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {}}):
            self.assertIn("No Inference Backend", self.presentation()["actions"][0]["reason"])

    def test_unusable_candidate_set_disables_request_with_the_endpoint_reason(self):
        self.eth0.delete()

        request_action = self.presentation()["actions"][0]
        response = self.call("request_proposal", field_key=self.field_key)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(request_action["reason"], response.json()["error"])

    def test_active_proposal_disables_request_and_allows_another_operator_to_cancel(self):
        self.request_proposal()
        self.operator()
        actions = {row["key"]: row for row in self.presentation()["actions"]}
        self.assertIn("active proposal", actions["request"]["reason"])
        self.assertEqual(actions["cancel"]["reason"], "")
        self.assertTrue(self.presentation()["pending"])
        self.assertEqual(self.presentation()["field_state"], "proposed")
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertEqual(response.context["summary"]["active_proposals"], 1)
        self.assertTrue(response.context["selected_trace"].terminations[0]["proposal"]["pending"])
        self.operator(device=False)
        actions = {row["key"]: row for row in self.presentation()["actions"]}
        self.assertIn("Device", actions["cancel"]["reason"])

    def test_candidate_drift_does_not_disable_pending_cancellation(self):
        proposal = self.request_proposal()
        self.eth0.delete()

        actions = {row["key"]: row for row in self.presentation()["actions"]}
        response = self.call("cancel_proposal", proposal_id=proposal.pk)

        self.assertEqual(actions["cancel"]["reason"], "")
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.CANCELLED)

    def test_candidate_actions_follow_decision_permission_and_staleness(self):
        self.completed()
        self.operator()
        data = self.presentation()
        self.assertEqual(data["badge"], "Proposal - not applied")
        self.assertEqual(data["candidate"], "eth0 (Interface)")
        actions = {row["key"]: row for row in data["actions"]}
        self.assertIn("permission", actions["accept"]["reason"])
        self.assertEqual(actions["reject"]["reason"], "")
        self.operator(decide=True)
        self.assertEqual(self.presentation()["actions"][2]["reason"], "")
        self.eth0.name = "renamed"
        self.eth0.save()
        data = self.presentation()
        self.assertEqual(data["badge"], "Proposal - stale, not applied")
        self.assertEqual(data["field_state"], "stale")
        self.assertIn("changed", data["actions"][2]["reason"])

    def test_no_match_has_disabled_accept_and_explanation(self):
        self.completed(no_match=True)
        data = self.presentation()
        self.assertIn("no match", data["actions"][2]["reason"])
        self.assertEqual(data["explanation"], "The candidate matches the source label.")
        self.assertFalse(data["pending"])

    def test_accept_then_reread_presents_accepted_termination(self):
        proposal = self.completed()
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 200)
        before = self.client.session[PREVIEW_REVISION_SESSION_KEY]
        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {
                "preview_revision": before,
            },
            follow=True,
        )
        self.assertNotEqual(self.client.session[PREVIEW_REVISION_SESSION_KEY], before)
        data = response.context["proposal_fields"][self.field_key]["presentation"]
        self.assertEqual(data["field_state"], "accepted")
        self.assertEqual(data["badge"], "Accepted")
        self.assertIn("already resolved", data["actions"][0]["reason"])

    def test_failed_card_retains_typed_reason_backend_and_attempt_count(self):
        from netbox_data_import.models import ProposalFailureReason
        from netbox_data_import.resolution_proposals import fail_proposal

        proposal = self.request_proposal()
        claim_proposal(proposal.pk)
        fail_proposal(
            proposal.pk,
            reason=ProposalFailureReason.BACKEND_REFUSAL,
            backend_metadata={"backend_model": "fixture-model", "attempts": [{"attempt": 1}]},
        )
        data = self.presentation()
        self.assertEqual(data["failure_code"], ProposalFailureReason.BACKEND_REFUSAL)
        self.assertEqual(data["field_state"], ProposalStatus.FAILED)
        self.assertEqual(data["failure"], "Backend refusal")
        self.assertEqual(data["attempt_count"], 1)
        self.assertEqual(data["metadata"], [{"label": "backend model", "value": "fixture-model"}])
        self.assertEqual(data["actions"][0]["label"], "Ask AI again")
        for action in data["actions"][2:]:
            self.assertEqual(action["reason"], "The proposal failed: Backend refusal (backend_refusal).")

    def test_candidate_missing_from_the_snapshot_reads_as_unacceptable(self):
        """The reader must refuse what acceptance refuses, not fail the whole workspace."""
        proposal = self.request_proposal()
        self.assertTrue(claim_proposal(proposal.pk))
        entry = proposal.candidate_snapshot["candidates"][0]
        self.assertTrue(
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.CANDIDATE,
                explanation="The candidate matches the source label.",
                selected_candidate_id="absent-from-the-snapshot",
                selected_object_type=ObjectType.objects.get_for_model(Interface),
                selected_object_id=entry["object_id"],
            )
        )
        data = self.presentation()
        self.assertEqual(data["candidate"], "")
        self.assertIn("snapshot", data["actions"][2]["reason"])
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertEqual(response.status_code, 200)

    def test_the_reader_and_acceptance_agree_on_a_missing_snapshot_candidate(self):
        """A card that offered Accept here would offer an action the writer always refuses."""
        proposal = self.request_proposal()
        self.assertTrue(claim_proposal(proposal.pk))
        entry = proposal.candidate_snapshot["candidates"][0]
        self.assertTrue(
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.CANDIDATE,
                explanation="The candidate matches the source label.",
                selected_candidate_id="absent-from-the-snapshot",
                selected_object_type=ObjectType.objects.get_for_model(Interface),
                selected_object_id=entry["object_id"],
            )
        )
        self.operator(decide=True)
        self.assertNotEqual(self.presentation()["actions"][2]["reason"], "")
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 409)
        self.assert_unwritten(proposal)

    def test_every_field_state_wears_its_own_badge_modifier(self):
        """Specification 10.2 needs automatically resolved to read differently from manually resolved."""
        from netbox_data_import.proposal_presentation import STATE_STYLES

        self.assertEqual(len(set(STATE_STYLES.values())), len(STATE_STYLES))
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        resolved = termination_field_key(device="DEV-B", cards="", port="eth1", kind="interface")
        rendered = response.content.decode()
        self.assertEqual(response.context["proposal_fields"][resolved]["presentation"]["state_style"], "auto")
        self.assertIn('class="badge ndi-trace-state-auto"', rendered)
        self.assertIn('class="badge ndi-trace-state-unresolved"', rendered)
        # A dark-theme override carries the same class name, so each rule is matched at its own line.
        for style in set(STATE_STYLES.values()):
            self.assertRegex(rendered, rf"(?m)^\s*\.ndi-trace-state-{style} \{{")

    def test_backend_resolution_runs_once_for_all_displayed_fields(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_data_import.tests.test_inference_backend import ALLOWLIST, make_row

        make_row(enabled=True)
        with (
            override_settings(
                PLUGINS_CONFIG={
                    "netbox_data_import": {
                        "inference_backend_origin_allowlist": ALLOWLIST,
                    }
                }
            ),
            CaptureQueriesContext(connection) as queries,
        ):
            response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        fields = response.context["proposal_fields"]
        self.assertGreater(len(fields), 1)
        self.assertEqual(fields[self.field_key]["presentation"]["actions"][0]["reason"], "")
        backend_reads = [query for query in queries if 'FROM "netbox_data_import_inferencebackend"' in query["sql"]]
        self.assertEqual(len(backend_reads), 1)

    def test_profile_view_permission_disables_request_and_allows_reject(self):
        proposal = self.completed()
        self.operator(view_only=True)
        actions = {row["key"]: row for row in self.presentation()["actions"]}
        self.assertIn("permission", actions["request"]["reason"])
        self.assertEqual(actions["reject"]["reason"], "")

        response = self.call("reject_proposal", proposal_id=proposal.pk)

        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "rejected")

    def test_fields_reuse_inventory_for_the_same_device_kind_and_role(self):
        """Two proposal fields with one eligibility key must not repeat its inventory reads."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_data_import.cable_target import UNRESOLVED
        from netbox_data_import.netbox_reader import NetBoxReader
        from netbox_data_import.proposal_presentation import ProposalPresentation

        first = self.completed()
        first.refresh_from_db()
        second_key = termination_field_key(device="DEV-A", cards="", port="another-port", kind="interface")
        ResolutionProposal.objects.create(
            profile=self.profile,
            task_type=first.task_type,
            field_key=second_key,
            status=first.status,
            source_evidence={"port": "another-port"},
            resolved_device_type=first.resolved_device_type,
            resolved_device_id=first.resolved_device_id,
            prompt_version=first.prompt_version,
            response_schema_version=first.response_schema_version,
            candidate_snapshot=first.candidate_snapshot,
            requested_by=first.requested_by,
            outcome=first.outcome,
            selected_candidate_id=first.selected_candidate_id,
            selected_object_type=first.selected_object_type,
            selected_object_id=first.selected_object_id,
            explanation=first.explanation,
        )
        fields = (
            {"field_key": self.field_key, "state": UNRESOLVED},
            {"field_key": second_key, "state": UNRESOLVED},
        )
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)
        presentation = ProposalPresentation(profile=self.profile, actor=self.actor, reader=reader)

        with CaptureQueriesContext(connection) as queries:
            payloads = presentation.fields(fields)

        self.assertFalse(payloads[self.field_key]["staleness"]["is_stale"])
        self.assertFalse(payloads[second_key]["staleness"]["is_stale"])
        device_reads = [query for query in queries if 'FROM "dcim_device"' in query["sql"]]
        candidate_reads = [query for query in queries if 'FROM "dcim_interface"' in query["sql"]]
        self.assertEqual(len(device_reads), 1)
        self.assertEqual(len(candidate_reads), 2)

    def test_mapped_peer_has_a_manual_reason(self):
        from netbox_data_import.field_keys import MAPPED_PEER_ROLE

        key = termination_field_key(
            device="DEV-A", cards="", port="absent-port", kind="interface", role=MAPPED_PEER_ROLE
        )
        data = self.presentation(key)
        self.assertTrue(data["actions"][0]["reason"])
        self.assertIn("mapped peer", data["actions"][1]["reason"])

    def test_history_for_a_field_outside_the_preview_disables_every_command(self):
        proposal = self.completed()
        other_key = termination_field_key(device="DEV-A", cards="", port="older-port", kind="interface")
        ResolutionProposal.objects.filter(pk=proposal.pk).update(field_key=other_key)
        payload = self.call("proposal", field_key=other_key).json()
        self.assertEqual([row["id"] for row in payload["history_display"]], [proposal.pk])
        self.assertTrue(all(action["reason"] for action in payload["presentation"]["actions"]))

    def test_active_history_outside_the_preview_disables_cancel(self):
        proposal = self.request_proposal()
        other_key = termination_field_key(device="DEV-A", cards="", port="older-port", kind="interface")
        ResolutionProposal.objects.filter(pk=proposal.pk).update(field_key=other_key)
        payload = self.call("proposal", field_key=other_key).json()
        self.assertTrue(all(action["reason"] for action in payload["presentation"]["actions"]))

    def test_workspace_history_requires_profile_view_scope_even_with_preview_access(self):
        from netbox_data_import.tests.test_inference_backend import ALLOWLIST, FALLBACK

        self.completed()
        other = ImportProfile.objects.create(name="Visible profile", source_adapter="trace_workbook")
        actor = user_with_object_permission(
            "preview-without-history",
            [
                (ImportProfile, ["change"], {"pk": self.profile.pk}),
                (ImportProfile, ["view"], {"pk": other.pk}),
                (Site, ["view"], {}),
                (Device, ["view"], {}),
                (Interface, ["view"], {}),
            ],
        )
        self.login_with_preview(actor)
        with override_settings(
            PLUGINS_CONFIG={
                "netbox_data_import": {
                    "inference_backend": FALLBACK,
                    "inference_backend_origin_allowlist": ALLOWLIST,
                }
            }
        ):
            response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        payload = response.context["proposal_fields"][self.field_key]
        self.assertEqual((payload["proposal"], payload["history_display"]), (None, []))
        self.assertIsNone(payload["history_url"])
        self.assertEqual(response.context["summary"]["active_proposals"], "Not permitted")
        self.assertTrue(all(action["reason"] for action in payload["presentation"]["actions"]))

    def test_workspace_renders_actions_reasons_and_the_controller_contract(self):
        import json
        import re

        proposal = self.completed(no_match=True)
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        html = re.sub(r"<template\b.*?</template>", "", response.content.decode(), flags=re.DOTALL)
        buttons = re.findall(r'<button\b[^>]*data-proposal-action="([^"]+)"([^>]*)>', html)
        self.assertEqual(
            [(key, "disabled" in attributes, "hidden" in attributes) for key, attributes in buttons],
            [
                ("accept", True, False),
                ("reject", False, False),
                ("request", True, False),
                ("cancel", True, False),
            ],
        )
        reason = response.context["proposal_fields"][self.field_key]["presentation"]["actions"][2]["reason"]
        self.assertRegex(
            html, rf'<div\b(?![^>]*\bhidden\b)[^>]*data-proposal-reason="accept"[^>]*>{re.escape(reason)}</div>'
        )
        self.assertRegex(html, r'<script src="[^"]*/trace_proposals.js[^"]*"></script>')
        script = re.search(r'<script id="traceProposalFields" type="application/json">(.*?)</script>', html)
        self.assertEqual(json.loads(script.group(1))[self.field_key]["proposal"]["id"], proposal.pk)

    def test_a_queued_card_renders_the_background_job_line(self):
        """The operator reads the page, not the JSON, so the first render has to carry the line."""
        import re

        self.request_proposal()

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        html = re.sub(r"<template\b.*?</template>", "", response.content.decode(), flags=re.DOTALL)
        line = re.search(r"<div\b(?![^>]*\bhidden\b)[^>]*data-proposal-job[^-][^>]*>([^<]*)</div>", html)
        self.assertIsNotNone(line, html[html.index("data-proposal-field") :][:2000])
        self.assertIn("Background job: Pending", line.group(1))
        self.assertRegex(html, r"<div\b[^>]*data-proposal-job-note[^>]*\bhidden\b")

    def test_no_proposal_has_only_field_actions_and_no_history(self):
        import re

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        html = re.sub(r"<template\b.*?</template>", "", response.content.decode(), flags=re.DOTALL)
        field = re.search(r"<li\b[^>]*data-proposal-field=.*?</li>", html, re.DOTALL).group()
        self.assertNotIn('data-proposal-action="accept"', field)
        self.assertNotIn('data-proposal-action="reject"', field)
        self.assertNotIn('class="ndi-proposal-card', field)
        self.assertNotIn("Proposal history", field)
        self.assertIn("Choose termination</button>", field)
        for key, reason in [
            ("request", "No Inference Backend is enabled or configured as a fallback."),
            ("cancel", "There is no active proposal."),
        ]:
            self.assertRegex(field, rf'<button\b[^>]*data-proposal-action="{key}"[^>]*disabled')
            self.assertRegex(
                field, rf'<div\b(?![^>]*\bhidden\b)[^>]*data-proposal-reason="{key}"[^>]*>{re.escape(reason)}</div>'
            )

    def test_settled_terminations_collapse_below_the_topology_panels(self):
        import re

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        html = re.sub(r"<template\b.*?</template>", "", response.content.decode(), flags=re.DOTALL)
        settled = re.search(r"<details\b[^>]*data-trace-settled.*?</details>", html, re.DOTALL)
        self.assertIsNotNone(settled)
        group = settled.group()
        self.assertNotRegex(group.split(">", 1)[0], r"\bopen\b")
        self.assertIn("1 termination(s) resolved automatically by exact name match", group)
        self.assertIn("DEV-B eth1", group)
        self.assertIn("interface", group)
        self.assertIn("<td>eth1</td>", group)
        self.assertIn('class="badge ndi-trace-state-auto"', group)
        self.assertNotIn("data-proposal-field", group)
        self.assertNotIn("<button", group)
        self.assertNotIn("DEV-A absent-port", group)
        attention = html[html.index("data-trace-terminations") : settled.start()]
        self.assertRegex(attention, r"DEV-A absent-port</strong>\s*<span[^>]*data-proposal-state")
        self.assertNotIn("DEV-B eth1", attention)
        self.assertLess(html.index("Proposed physical topology"), html.index("data-trace-terminations"))
        self.assertLess(html.index("data-trace-devices"), html.index("data-trace-terminations"))

    def test_automatic_match_with_history_stays_in_attention(self):
        proposal = self.completed(no_match=True)
        resolved = termination_field_key(device="DEV-B", cards="", port="eth1", kind="interface")
        ResolutionProposal.objects.filter(pk=proposal.pk).update(field_key=resolved)
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertNotContains(response, "data-trace-settled")
        self.assertContains(response, "data-proposal-field=", count=2)

    def test_all_settled_terminations_explain_that_none_need_attention(self):
        Interface.objects.create(device=self.device_a, name="absent-port", type="1000base-t")
        self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertContains(response, "Every termination on this trace resolves to a NetBox port.")
        self.assertContains(response, "2 termination(s) resolved automatically by exact name match")
        self.assertNotContains(response, "data-proposal-field=")

    def test_decided_proposal_explains_both_disabled_decisions(self):
        proposal = self.completed(no_match=True)
        self.call("reject_proposal", proposal_id=proposal.pk)
        self.operator(view_only=True)
        self.assertEqual(
            [action["reason"] for action in self.presentation()["actions"][2:]],
            ["This proposal already has a decision.", "This proposal already has a decision."],
        )

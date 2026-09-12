# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Proposal workspace commands through real previews, permissions, and the job queue."""

import uuid
from io import BytesIO
from unittest.mock import patch

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from dcim.models import Device, Interface, Site
from django.test import TestCase, override_settings
from django.urls import reverse
from django_rq import get_queue
from redis.exceptions import ConnectionError as RedisConnectionError

from netbox_data_import.field_keys import termination_field_key
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
from netbox_data_import.resolution_proposals import cancel_proposal, claim_proposal, complete_proposal
from netbox_data_import.tests.helpers import trace_termination, trace_workbook_bytes, user_with_object_permission
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path


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
            with self.assertRaises(RedisConnectionError):
                self.call("request_proposal", field_key=self.field_key)

        proposal = ResolutionProposal.objects.get(profile=self.profile, field_key=self.field_key)
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.failure_reason, ProposalFailureReason.QUEUE_UNAVAILABLE)
        retry = self.request_proposal()
        self.assertNotEqual(retry.pk, proposal.pk)
        self.assertEqual(retry.status, ProposalStatus.QUEUED)

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
        Interface.objects.create(device=self.device_a, name="extra", type="1000base-t")
        with override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_proposal_candidate_limit": 1}}):
            response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "too_many_candidates")
        self.assertFalse(ResolutionProposal.objects.exists())

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
        self.completed()
        self.operator(view_only=True, device=False)
        response = self.call("proposal", field_key=self.field_key)
        self.assertTrue(response.json()["staleness"]["resolved_device_changed"])

    def test_read_without_an_attempt_returns_null(self):
        response = self.call("proposal", field_key=self.field_key)
        self.assertEqual(response.json(), {"ok": True, "proposal": None, "staleness": None})

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
        response = self.call("cancel_proposal", proposal_id=proposal.pk)
        self.assertEqual(response.status_code, 403)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.QUEUED)

    def test_request_requires_device_access(self):
        self.operator(device=False)
        response = self.call("request_proposal", field_key=self.field_key)
        self.assertEqual(response.status_code, 400)
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
                self.assertEqual(self.call(action, proposal_id=proposal.pk).status_code, 404)
        self.assertIsNone(self.call("proposal", field_key=self.field_key).json()["proposal"])
        self.assert_unwritten(proposal)

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
        self.assertEqual(self.call("accept_proposal", proposal_id=proposal.pk).status_code, 403)
        self.assert_unwritten(proposal)

    def test_proposal_survives_a_new_preview_of_the_same_source(self):
        proposal = self.completed()
        before = self.client.session[PREVIEW_REVISION_SESSION_KEY]
        self.client.force_login(self.actor)
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

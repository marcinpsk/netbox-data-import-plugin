# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Read proposal history through HTTP with real permissions and backend diagnostics."""

from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers, viewsets

from netbox_data_import.api.serializers import ResolutionProposalSerializer
from netbox_data_import.api.views import ResolutionProposalViewSet, _ProfileScopedQuerySetMixin
from netbox_data_import.models import ImportProfile, ProposalFailureReason, ProposalStatus, ResolutionProposal
from netbox_data_import.proposal_jobs import run_proposal
from netbox_data_import.resolution_proposals import cancel_proposal
from netbox_data_import.tests.helpers import user_with_object_permission
from netbox_data_import.tests.test_inference_adapter import completion, serving
from netbox_data_import.tests.test_inference_credentials import SECRET
from netbox_data_import.tests.test_proposal_jobs import WorkerFixture, answer
from netbox_data_import.tests.test_resolution_proposals import ProposalFixture


class ResolutionProposalAPITest(WorkerFixture, ProposalFixture):
    def setUp(self):
        self.proposal = self.frozen_proposal()
        self.list_url = reverse("plugins-api:netbox_data_import-api:resolutionproposal-list")
        self.detail_url = reverse(
            "plugins-api:netbox_data_import-api:resolutionproposal-detail", args=[self.proposal.pk]
        )
        self.history_url = reverse("plugins-api:netbox_data_import-api:resolutionproposalhistory-list")
        self.viewer = user_with_object_permission("proposal-viewer", [(ResolutionProposal, ["view"], None)])
        self.client.force_login(self.viewer)

    def test_plain_model_uses_plain_drf_bases(self):
        self.assertEqual(
            ResolutionProposalViewSet.__bases__, (_ProfileScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet)
        )
        self.assertEqual(ResolutionProposalSerializer.__bases__, (serializers.ModelSerializer,))

    def test_list_returns_stored_proposal(self):
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(
            response.json()["results"],
            [
                {
                    "id": self.proposal.pk,
                    "profile": self.profile.pk,
                    "task_type": "select_termination",
                    "field_key": self.field_key,
                    "status": "queued",
                    "source_evidence": {"port": "Eth1/1"},
                    "resolved_device_type": self.device_type_ct.pk,
                    "resolved_device_id": self.device.pk,
                    "prompt_version": 1,
                    "response_schema_version": 1,
                    "candidate_snapshot": self.proposal.candidate_snapshot,
                    "requested_by": self.operator.pk,
                    "created": self.proposal.created.isoformat().replace("+00:00", "Z"),
                    "last_updated": self.proposal.last_updated.isoformat().replace("+00:00", "Z"),
                    "outcome": "",
                    "selected_candidate_id": "",
                    "selected_object_type": None,
                    "selected_object_id": None,
                    "explanation": "",
                    "backend_metadata": None,
                    "response_diagnostic": None,
                    "failure_reason": "",
                    "decision": "",
                    "decided_by": None,
                    "decided_at": None,
                    "written_resolution": None,
                }
            ],
        )

    def test_detail_returns_completed_proposal(self):
        self.complete(self.proposal)

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], self.proposal.pk)
        self.assertEqual(data["profile"], self.profile.pk)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["outcome"], "candidate")
        self.assertEqual(data["selected_candidate_id"], "candidate-0001")
        self.assertEqual(data["selected_object_type"], self.interface_ct.pk)
        self.assertEqual(data["selected_object_id"], self.interface.pk)
        self.assertEqual(data["explanation"], "The port name matches exactly.")

    def assert_write_refused(self, method, url):
        writer = user_with_object_permission(
            "proposal-writer", [(ResolutionProposal, ["view", "add", "change", "delete"], None)]
        )
        self.client.force_login(writer)
        before = list(ResolutionProposal.objects.values())

        response = getattr(self.client, method)(url, data={}, content_type="application/json")

        self.assertEqual(response.status_code, 405)
        self.assertEqual(list(ResolutionProposal.objects.values()), before)

    def test_post_is_refused(self):
        self.assert_write_refused("post", self.list_url)

    def test_put_is_refused(self):
        self.assert_write_refused("put", self.detail_url)

    def test_patch_is_refused(self):
        self.assert_write_refused("patch", self.detail_url)

    def test_delete_is_refused(self):
        self.assert_write_refused("delete", self.detail_url)

    def test_user_without_view_permission_is_refused(self):
        self.client.force_login(user_with_object_permission("proposal-denied", []))
        for url in (self.list_url, self.detail_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_anonymous_user_is_refused(self):
        self.client.logout()
        for url in (self.list_url, self.detail_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_profile_filter_returns_only_matching_proposals(self):
        self.profile = ImportProfile.objects.create(name="Other Proposal Profile", source_adapter="trace_workbook")
        other = self.frozen_proposal()

        unfiltered = self.client.get(self.list_url)
        self.assertEqual(unfiltered.status_code, 200)
        self.assertEqual({row["id"] for row in unfiltered.json()["results"]}, {self.proposal.pk, other.pk})
        for proposal in (self.proposal, other):
            with self.subTest(profile=proposal.profile_id):
                response = self.client.get(self.list_url, {"profile_id": proposal.profile_id})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["count"], 1)
                self.assertEqual([row["id"] for row in response.json()["results"]], [proposal.pk])

    def test_field_filter_exposes_the_complete_paginated_history(self):
        cancel_proposal(self.proposal.pk)
        attempts = [self.proposal.pk]
        for _ in range(11):
            proposal = self.frozen_proposal()
            attempts.append(proposal.pk)
            cancel_proposal(proposal.pk)
        ResolutionProposal.objects.filter(pk__in=attempts).update(created=timezone.now())
        other = self.make_proposal(field_key=self.other_field_key)
        profile_viewer = user_with_object_permission(
            "profile-history-viewer", [(ImportProfile, ["view"], {"pk": self.profile.pk})]
        )
        self.client.force_login(profile_viewer)

        response = self.client.get(
            self.history_url,
            {"profile_id": self.profile.pk, "field_key": self.field_key, "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 12)
        seen = [row["id"] for row in response.json()["results"]]
        while response.json()["next"]:
            response = self.client.get(response.json()["next"])
            self.assertEqual(response.status_code, 200)
            seen.extend(row["id"] for row in response.json()["results"])
        self.assertEqual(seen, list(reversed(attempts)))
        self.assertNotIn(other.pk, seen)

    def test_history_endpoint_requires_one_valid_profile_and_field_key(self):
        actor = user_with_object_permission("history-filter-viewer", [(ImportProfile, ["view"], None)])
        self.client.force_login(actor)
        cases = (
            {},
            {"profile_id": self.profile.pk},
            {"field_key": self.field_key},
            {"profile_id": "not-a-number", "field_key": self.field_key},
            {"profile_id": self.profile.pk, "field_key": "not-a-field-key"},
        )
        for params in cases:
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.history_url, params).status_code, 400)

    def test_history_endpoint_uses_import_profile_view_scope_and_refuses_writes(self):
        response = self.client.get(
            self.history_url,
            {"profile_id": self.profile.pk, "field_key": self.field_key},
        )
        self.assertEqual(response.status_code, 404)

        actor = user_with_object_permission(
            "profile-history-viewer", [(ImportProfile, ["view"], {"pk": self.profile.pk})]
        )
        self.client.force_login(actor)
        response = self.client.post(
            self.history_url,
            {"profile_id": self.profile.pk, "field_key": self.field_key},
        )
        self.assertEqual(response.status_code, 405)

    def test_backend_credential_echo_is_absent_from_responses(self):
        payload = completion(answer(explanation=SECRET), model=SECRET, id=SECRET)
        with serving(payload=payload) as (root, seen, allowed), self.configured(root, allowed):
            run_proposal(self.proposal.pk)
        self.proposal.refresh_from_db()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["headers"]["authorization"], f"Bearer {SECRET}")
        self.assertEqual(self.proposal.status, ProposalStatus.FAILED)
        self.assertEqual(self.proposal.failure_reason, ProposalFailureReason.INVALID_RESPONSE)

        for url in (self.list_url, self.detail_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(SECRET, response.content.decode())
                data = response.json()
                row = data["results"][0] if url == self.list_url else data
                self.assertEqual(row["id"], self.proposal.pk)
                self.assertEqual(row["response_diagnostic"], self.proposal.response_diagnostic)
                self.assertTrue(row["response_diagnostic"]["redacted"])
                self.assertEqual(row["backend_metadata"], self.proposal.backend_metadata)
                self.assertEqual(row["backend_metadata"]["backend_source"], "database")

    def test_constrained_viewer_lists_only_permitted_profile_proposals(self):
        self.profile = ImportProfile.objects.create(name="Permitted Proposal Profile", source_adapter="trace_workbook")
        permitted = self.frozen_proposal()
        viewer = user_with_object_permission(
            "constrained-proposal-viewer", [(ResolutionProposal, ["view"], {"profile_id": self.profile.pk})]
        )
        self.client.force_login(viewer)

        response = self.client.get(self.list_url)

        self.assertEqual([row["id"] for row in response.json()["results"]], [permitted.pk])

    def test_constrained_viewer_cannot_read_another_profile_proposal(self):
        other = ImportProfile.objects.create(name="Permitted Proposal Profile", source_adapter="trace_workbook")
        viewer = user_with_object_permission(
            "constrained-proposal-viewer", [(ResolutionProposal, ["view"], {"profile_id": other.pk})]
        )
        self.client.force_login(viewer)

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 404)

    def test_non_numeric_profile_filter_returns_bad_request(self):
        response = self.client.get(self.list_url, {"profile_id": "abc"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"profile_id": "Enter a whole number."})

    def test_proposals_are_absent_from_graphql(self):
        response = self.client.post(
            "/graphql/",
            data={"query": "{ __schema { types { name } queryType { fields { name } } } }"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("errors", response.json())
        schema = response.json()["data"]["__schema"]
        self.assertIn("ImportProfileType", [item["name"] for item in schema["types"]])
        names = [item["name"] for item in schema["types"] + schema["queryType"]["fields"]]
        self.assertFalse([name for name in names if "resolutionproposal" in name.replace("_", "").lower()])

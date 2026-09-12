# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Resolution Proposal row and its transition service (specification 7.1, 7.2, 7.3)."""

from core.models import ObjectType
from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from netbox_data_import.field_keys import SELECT_TERMINATION_TASK, TERMINATION_ROLE, termination_field_key
from netbox_data_import.models import (
    ImportProfile,
    ProposalDecision,
    ProposalFailureReason,
    ProposalOutcome,
    ProposalStatus,
    ResolutionProposal,
    TerminationResolution,
    index_digest,
)
from netbox_data_import.resolution_proposals import (
    ActiveProposalExists,
    cancel_proposal,
    claim_proposal,
    complete_proposal,
    decide_proposal,
    fail_proposal,
    request_proposal,
)
from netbox_data_import.tests.helpers import make_dcim_objects

User = get_user_model()


class ProposalFixture(TestCase):
    """One profile, one resolved Device, and one candidate interface for every proposal test."""

    @classmethod
    def setUpTestData(cls):
        cls.operator = User.objects.create_superuser("proposal-operator", "operator@example.com", "testpass")
        cls.profile = ImportProfile.objects.create(
            name="Proposal Profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        site, _manufacturer, device_type, role = make_dcim_objects("Proposal")
        cls.device = Device.objects.create(name="Proposal Device", site=site, device_type=device_type, role=role)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet 1/1")
        cls.device_type_ct = ObjectType.objects.get_for_model(cls.device)
        cls.interface_ct = ObjectType.objects.get_for_model(cls.interface)
        cls.field_key = termination_field_key(
            device=cls.device.name,
            cards="",
            port=cls.interface.name,
            kind="interface",
            role=TERMINATION_ROLE,
        )
        cls.other_field_key = termination_field_key(
            device=cls.device.name,
            cards="",
            port="Ethernet 1/2",
            kind="interface",
            role=TERMINATION_ROLE,
        )

    def make_proposal(self, field_key=None):
        """Create one queued proposal through the public request path."""
        return request_proposal(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=field_key or self.field_key,
            source_evidence={"port": "Eth1/1"},
            resolved_device_type=self.device_type_ct,
            resolved_device_id=self.device.pk,
            prompt_version=1,
            response_schema_version=1,
            candidate_snapshot={"total": 1, "candidates": [{"candidate_id": "candidate-0001"}]},
            requested_by=self.operator,
        )

    def complete(self, proposal, outcome=ProposalOutcome.CANDIDATE):
        """Move one proposal to completed through the service, claiming it first."""
        claim_proposal(proposal.pk)
        return complete_proposal(
            proposal.pk,
            outcome=outcome,
            explanation="The port name matches exactly.",
            selected_candidate_id="candidate-0001",
            selected_object_type=self.interface_ct,
            selected_object_id=self.interface.pk,
        )

    def status_of(self, proposal):
        """Read the status back from the database rather than from the in-memory instance."""
        return ResolutionProposal.objects.get(pk=proposal.pk).status


class ProposalEdgeTableTest(ProposalFixture):
    """Section 7.2 permits five edges. Every other transition has to be refused."""

    def place_in(self, proposal, status):
        """Write one row directly into *status*, carrying the content that status requires."""
        content = {"status": status}
        if status == ProposalStatus.COMPLETED:
            content |= {
                "outcome": ProposalOutcome.CANDIDATE,
                "selected_candidate_id": "candidate-0001",
                "selected_object_type": self.interface_ct,
                "selected_object_id": self.interface.pk,
            }
        if status == ProposalStatus.FAILED:
            content |= {"failure_reason": ProposalFailureReason.INVALID_RESPONSE}
        ResolutionProposal.objects.filter(pk=proposal.pk).update(**content)

    def attempt(self, proposal, target):
        """Call the service function that moves a row to *target*."""
        if target == ProposalStatus.RUNNING:
            return claim_proposal(proposal.pk)
        if target == ProposalStatus.COMPLETED:
            return complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.NO_MATCH,
                explanation="The evidence does not distinguish the candidates.",
            )
        if target == ProposalStatus.FAILED:
            return fail_proposal(proposal.pk, reason=ProposalFailureReason.INVALID_RESPONSE)
        return cancel_proposal(proposal.pk)

    def test_every_permitted_edge_is_taken(self):
        for start, targets in ProposalStatus.EDGES.items():
            for target in targets:
                with self.subTest(edge=f"{start}->{target}"):
                    proposal = self.make_proposal()
                    self.place_in(proposal, start)

                    self.assertIs(self.attempt(proposal, target), True)
                    self.assertEqual(self.status_of(proposal), target)
                    proposal.delete()

    def test_a_second_claim_is_refused(self):
        proposal = self.make_proposal()
        self.assertIs(claim_proposal(proposal.pk), True)
        self.assertEqual(self.status_of(proposal), ProposalStatus.RUNNING)

        self.assertIs(claim_proposal(proposal.pk), False)

    def test_every_other_transition_is_refused(self):
        """The rowcount is the refusal, so a forbidden edge returns False and leaves the row alone."""
        moves = (ProposalStatus.RUNNING, ProposalStatus.COMPLETED, ProposalStatus.FAILED, ProposalStatus.CANCELLED)
        for start, permitted in ProposalStatus.EDGES.items():
            for target in moves:
                if target in permitted or target == start:
                    continue
                with self.subTest(edge=f"{start}->{target}"):
                    proposal = self.make_proposal()
                    self.place_in(proposal, start)

                    self.assertIs(self.attempt(proposal, target), False)
                    self.assertEqual(self.status_of(proposal), start)
                    proposal.delete()


class OneActiveProposalTest(ProposalFixture):
    """Section 7.1 permits at most one active proposal per bound key."""

    def test_a_second_active_proposal_for_one_key_is_refused(self):
        self.make_proposal()

        with self.assertRaises(ActiveProposalExists):
            self.make_proposal()

    def test_another_key_is_unaffected(self):
        self.make_proposal()

        second = self.make_proposal(field_key=self.other_field_key)

        self.assertEqual(second.status, ProposalStatus.QUEUED)

    def test_duplicate_request_keeps_the_callers_transaction_usable(self):
        with transaction.atomic():
            first = self.make_proposal()
            with self.assertRaises(ActiveProposalExists):
                self.make_proposal()

            self.assertEqual(ResolutionProposal.objects.get(pk=first.pk).status, ProposalStatus.QUEUED)

    def test_a_new_proposal_is_allowed_once_the_first_is_terminal(self):
        """A retry is always a new row, so the index must stop blocking the key at a terminal status."""
        for terminal in ProposalStatus.TERMINAL:
            with self.subTest(terminal=terminal):
                first = self.make_proposal()
                if terminal == ProposalStatus.COMPLETED:
                    self.complete(first)
                elif terminal == ProposalStatus.FAILED:
                    fail_proposal(first.pk, reason=ProposalFailureReason.TIMEOUT)
                else:
                    cancel_proposal(first.pk)

                second = self.make_proposal()

                self.assertEqual(second.status, ProposalStatus.QUEUED)
                first.delete()
                second.delete()


class CancelVersusLateResponseTest(ProposalFixture):
    """Section 7.5: a late response never overwrites a cancelled row."""

    def test_a_response_arriving_after_cancellation_is_discarded(self):
        proposal = self.make_proposal()
        claim_proposal(proposal.pk)
        self.assertIs(cancel_proposal(proposal.pk), True)

        accepted = complete_proposal(
            proposal.pk,
            outcome=ProposalOutcome.CANDIDATE,
            explanation="The worker finished after the operator cancelled.",
            selected_candidate_id="candidate-0001",
            selected_object_type=self.interface_ct,
            selected_object_id=self.interface.pk,
        )

        self.assertIs(accepted, False)
        row = ResolutionProposal.objects.get(pk=proposal.pk)
        self.assertEqual(row.status, ProposalStatus.CANCELLED)
        self.assertEqual(row.outcome, "")

    def test_a_late_failure_cannot_overwrite_a_cancelled_row_either(self):
        proposal = self.make_proposal()
        cancel_proposal(proposal.pk)

        self.assertIs(fail_proposal(proposal.pk, reason=ProposalFailureReason.TIMEOUT), False)
        self.assertEqual(self.status_of(proposal), ProposalStatus.CANCELLED)


class ProposalDecisionTest(ProposalFixture):
    """Section 7.2: the decision is one-shot, and it never changes the status."""

    def decided(self, proposal):
        """Return the decision fields as the database holds them."""
        row = ResolutionProposal.objects.get(pk=proposal.pk)
        return row.decision, row.decided_at, row.status

    def test_a_completed_proposal_can_be_decided(self):
        proposal = self.make_proposal()
        self.complete(proposal)

        self.assertIs(decide_proposal(proposal.pk, decision=ProposalDecision.REJECTED, operator=self.operator), True)

        decision, decided_at, status = self.decided(proposal)
        self.assertEqual(decision, ProposalDecision.REJECTED)
        self.assertIsNotNone(decided_at)
        self.assertEqual(status, ProposalStatus.COMPLETED)

    def test_a_second_decision_is_refused(self):
        proposal = self.make_proposal()
        self.complete(proposal)
        decide_proposal(proposal.pk, decision=ProposalDecision.REJECTED, operator=self.operator)

        again = decide_proposal(proposal.pk, decision=ProposalDecision.REJECTED, operator=self.operator)

        self.assertIs(again, False)
        self.assertEqual(self.decided(proposal)[0], ProposalDecision.REJECTED)

    def test_an_undecided_non_completed_proposal_cannot_be_decided(self):
        for status in (ProposalStatus.QUEUED, ProposalStatus.FAILED, ProposalStatus.CANCELLED):
            with self.subTest(status=status):
                proposal = self.make_proposal()
                if status == ProposalStatus.FAILED:
                    fail_proposal(proposal.pk, reason=ProposalFailureReason.BACKEND_REFUSAL)
                elif status == ProposalStatus.CANCELLED:
                    cancel_proposal(proposal.pk)

                refused = decide_proposal(proposal.pk, decision=ProposalDecision.REJECTED, operator=self.operator)

                self.assertIs(refused, False)
                self.assertEqual(self.decided(proposal)[0], "")
                proposal.delete()

    def test_acceptance_links_the_written_resolution(self):
        proposal = self.make_proposal()
        self.complete(proposal)
        resolution = TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
            selected_object_type=self.interface_ct,
            selected_object_id=self.interface.pk,
            selected_display_name=str(self.interface),
        )

        decide_proposal(
            proposal.pk,
            decision=ProposalDecision.ACCEPTED,
            operator=self.operator,
            written_resolution=resolution,
        )

        self.assertEqual(ResolutionProposal.objects.get(pk=proposal.pk).written_resolution, resolution)

    def test_acceptance_without_a_written_resolution_is_refused(self):
        proposal = self.make_proposal()
        self.complete(proposal)

        with self.assertRaises(ValueError):
            decide_proposal(proposal.pk, decision=ProposalDecision.ACCEPTED, operator=self.operator)

        self.assertEqual(self.decided(proposal), ("", None, ProposalStatus.COMPLETED))


class ProposalConstraintTest(ProposalFixture):
    """The database refuses a row whose content contradicts its status or its outcome."""

    def assert_refused(self, **content):
        """Write *content* onto a fresh proposal and require the database to reject it."""
        proposal = self.make_proposal()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResolutionProposal.objects.filter(pk=proposal.pk).update(**content)

    def test_a_completed_row_needs_an_outcome(self):
        self.assert_refused(status=ProposalStatus.COMPLETED)

    def test_completion_rejects_an_unknown_outcome(self):
        proposal = self.make_proposal()
        self.assertTrue(claim_proposal(proposal.pk))

        with self.assertRaises(ValueError):
            complete_proposal(proposal.pk, outcome="unknown", explanation="Invalid input must not be stored.")

        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.RUNNING)
        self.assertEqual(proposal.outcome, "")

    def test_an_outcome_needs_a_completed_row(self):
        self.assert_refused(outcome=ProposalOutcome.NO_MATCH)

    def test_queue_unavailable_is_a_valid_failure_reason(self):
        proposal = self.make_proposal()

        self.assertTrue(fail_proposal(proposal.pk, reason="queue_unavailable"))

        proposal.refresh_from_db()
        proposal.full_clean()
        self.assertEqual(proposal.status, ProposalStatus.FAILED)
        self.assertEqual(proposal.get_failure_reason_display(), "Queue unavailable")

    def test_a_failed_row_needs_a_reason(self):
        self.assert_refused(status=ProposalStatus.FAILED)

    def test_failure_rejects_an_unknown_reason(self):
        proposal = self.make_proposal()

        with self.assertRaises(ValueError):
            fail_proposal(proposal.pk, reason="unknown")

        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.QUEUED)
        self.assertEqual(proposal.failure_reason, "")

    def test_a_reason_needs_a_failed_row(self):
        self.assert_refused(failure_reason=ProposalFailureReason.TIMEOUT)

    def test_a_candidate_outcome_needs_a_selection(self):
        self.assert_refused(
            status=ProposalStatus.COMPLETED,
            outcome=ProposalOutcome.CANDIDATE,
            selected_candidate_id="candidate-0001",
        )

    def test_a_candidate_outcome_needs_a_candidate_id(self):
        self.assert_refused(
            status=ProposalStatus.COMPLETED,
            outcome=ProposalOutcome.CANDIDATE,
            selected_candidate_id="",
            selected_object_type=self.interface_ct,
            selected_object_id=self.interface.pk,
        )

    def test_completing_a_candidate_without_an_id_is_refused(self):
        proposal = self.make_proposal()
        self.assertTrue(claim_proposal(proposal.pk))

        with self.assertRaisesMessage(ValueError, "A candidate outcome requires selected_candidate_id."):
            complete_proposal(
                proposal.pk,
                outcome=ProposalOutcome.CANDIDATE,
                explanation="The candidate matches the source label.",
                selected_object_type=self.interface_ct,
                selected_object_id=self.interface.pk,
            )

        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.RUNNING)
        self.assertEqual(proposal.outcome, "")

    def test_a_no_match_outcome_carries_no_selection(self):
        self.assert_refused(
            status=ProposalStatus.COMPLETED,
            outcome=ProposalOutcome.NO_MATCH,
            selected_object_type=self.interface_ct,
            selected_object_id=self.interface.pk,
        )

    def test_a_half_attributed_decision_is_refused(self):
        proposal = self.make_proposal()
        self.complete(proposal)
        for half in ({"decision": ProposalDecision.ACCEPTED}, {"decided_at": "2026-09-10T00:00:00Z"}):
            with self.subTest(half=sorted(half)):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ResolutionProposal.objects.filter(pk=proposal.pk).update(**half)

    def test_a_decision_needs_a_completed_row(self):
        self.assert_refused(decision=ProposalDecision.ACCEPTED, decided_at="2026-09-10T00:00:00Z")

    def test_decision_rejects_an_unknown_value(self):
        proposal = self.make_proposal()
        self.complete(proposal)

        with self.assertRaises(ValueError):
            decide_proposal(proposal.pk, decision="unknown", operator=self.operator)

        proposal.refresh_from_db()
        self.assertEqual(proposal.decision, "")
        self.assertIsNone(proposal.decided_at)


class ProposalFieldKeyTest(ProposalFixture):
    """The digest is what the index carries, so no caller may store one that disagrees."""

    def test_the_digest_is_derived_on_save(self):
        proposal = self.make_proposal()

        self.assertEqual(proposal.field_key_digest, index_digest(self.field_key))

    def test_a_stored_digest_that_disagrees_is_overwritten(self):
        proposal = self.make_proposal()
        proposal.field_key_digest = "0" * 64

        proposal.save()

        self.assertEqual(ResolutionProposal.objects.get(pk=proposal.pk).field_key_digest, index_digest(self.field_key))

    def test_a_partial_save_of_the_key_updates_the_digest(self):
        """`update_fields` naming the key alone would otherwise leave the index on the old digest."""
        proposal = self.make_proposal()
        proposal.field_key = self.other_field_key

        proposal.save(update_fields=["field_key"])

        self.assertEqual(
            ResolutionProposal.objects.get(pk=proposal.pk).field_key_digest, index_digest(self.other_field_key)
        )

    def test_a_noncanonical_field_key_is_rejected(self):
        proposal = self.make_proposal()
        proposal.field_key = "device|card|port"

        with self.assertRaises(ValidationError) as caught:
            proposal.full_clean()

        self.assertIn("field_key", caught.exception.message_dict)

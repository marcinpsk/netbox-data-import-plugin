# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolution Proposal lifecycle: request, claim, complete, fail, cancel, decide (section 7.2).

Every ordering-dependent rule is a conditional `UPDATE` whose rowcount is the refusal. A read followed
by a write would let a cancellation and a late worker response both believe they won.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    ProposalDecision,
    ProposalFailureReason,
    ProposalOutcome,
    ProposalStatus,
    ResolutionProposal,
)
from .proposal_tasks import CandidateSnapshot

__all__ = [
    "ActiveProposalExists",
    "cancel_proposal",
    "claim_proposal",
    "complete_proposal",
    "decide_proposal",
    "fail_proposal",
    "request_proposal",
]


class ActiveProposalExists(Exception):
    """One key already has a queued or running proposal, which the partial unique index refuses."""


def request_proposal(
    *,
    profile,
    task_type,
    field_key,
    source_evidence,
    resolved_device_type,
    resolved_device_id,
    prompt_version,
    response_schema_version,
    candidate_snapshot: CandidateSnapshot,
    requested_by=None,
) -> ResolutionProposal:
    """Create the queued row that is also the attempt record, before any backend call."""
    if not isinstance(candidate_snapshot, CandidateSnapshot):
        raise TypeError("candidate_snapshot must be a CandidateSnapshot.")
    proposal = ResolutionProposal(
        profile=profile,
        task_type=task_type,
        field_key=field_key,
        status=ProposalStatus.QUEUED,
        source_evidence=source_evidence,
        resolved_device_type=resolved_device_type,
        resolved_device_id=resolved_device_id,
        prompt_version=prompt_version,
        response_schema_version=response_schema_version,
        candidate_snapshot=candidate_snapshot.as_json(),
        requested_by=requested_by,
    )
    try:
        with transaction.atomic():
            proposal.save()
    except IntegrityError as exc:
        if "ndi_resolutionproposal_one_active" not in str(exc):
            raise
        raise ActiveProposalExists("This field already has an active Resolution Proposal.") from exc
    return proposal


def _transition(proposal_id, *, allowed_from, **values) -> bool:
    """Move one row between statuses, returning whether this caller is the one that moved it."""
    values["last_updated"] = timezone.now()
    moved = ResolutionProposal.objects.filter(pk=proposal_id, status__in=allowed_from).update(**values)
    return moved == 1


def claim_proposal(proposal_id) -> bool:
    """Take a queued row for one worker. A second worker gets False and must not call the backend."""
    return _transition(proposal_id, allowed_from=(ProposalStatus.QUEUED,), status=ProposalStatus.RUNNING)


def _validate_choice(value, choices, *, name) -> None:
    """Reject a value outside one persisted lifecycle vocabulary."""
    if not any(value == allowed for allowed, _label in choices):
        raise ValueError(f"Unsupported {name}.")


def complete_proposal(
    proposal_id,
    *,
    outcome,
    explanation,
    selected_candidate_id="",
    selected_object_type=None,
    selected_object_id=None,
    backend_metadata=None,
    response_diagnostic=None,
) -> bool:
    """Record the outcome of a running row. False means it was cancelled or already terminal."""
    _validate_choice(outcome, ProposalOutcome.CHOICES, name="proposal outcome")
    if outcome == ProposalOutcome.CANDIDATE and not selected_candidate_id:
        raise ValueError("A candidate outcome requires selected_candidate_id.")
    if outcome == ProposalOutcome.NO_MATCH:
        selected_candidate_id, selected_object_type, selected_object_id = "", None, None
    return _transition(
        proposal_id,
        allowed_from=(ProposalStatus.RUNNING,),
        status=ProposalStatus.COMPLETED,
        outcome=outcome,
        explanation=explanation,
        selected_candidate_id=selected_candidate_id,
        selected_object_type=selected_object_type,
        selected_object_id=selected_object_id,
        backend_metadata=backend_metadata,
        response_diagnostic=response_diagnostic,
    )


def fail_proposal(proposal_id, *, reason, response_diagnostic=None, backend_metadata=None) -> bool:
    """Fail a queued or running row with its typed reason and whatever the call received."""
    _validate_choice(reason, ProposalFailureReason.CHOICES, name="proposal failure reason")
    return _transition(
        proposal_id,
        allowed_from=ProposalStatus.ACTIVE,
        status=ProposalStatus.FAILED,
        failure_reason=reason,
        response_diagnostic=response_diagnostic,
        backend_metadata=backend_metadata,
    )


def cancel_proposal(proposal_id) -> bool:
    """Cancel a queued or running row. A response arriving afterwards cannot move it again."""
    return _transition(proposal_id, allowed_from=ProposalStatus.ACTIVE, status=ProposalStatus.CANCELLED)


def decide_proposal(proposal_id, *, decision, operator=None, written_resolution=None) -> bool:
    """Set the one-shot decision fields, which never change the status.

    Only a completed row that nobody has decided yet qualifies, so the rowcount also refuses a second
    decision racing the first.
    """
    _validate_choice(decision, ProposalDecision.CHOICES, name="proposal decision")
    if decision == ProposalDecision.ACCEPTED and written_resolution is None:
        raise ValueError("An accepted proposal requires a written resolution.")
    values = {
        "decision": decision,
        "decided_by": operator,
        "decided_at": timezone.now(),
        "last_updated": timezone.now(),
    }
    if decision == ProposalDecision.ACCEPTED:
        values["written_resolution"] = written_resolution
    decided = ResolutionProposal.objects.filter(pk=proposal_id, status=ProposalStatus.COMPLETED, decision="").update(
        **values
    )
    return decided == 1

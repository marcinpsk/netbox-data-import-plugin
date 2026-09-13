# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Revalidate proposal evidence and serialize explicit operator decisions."""

from django.db import transaction
from utilities.permissions import get_permission_for_model

from .inference_backend import proposal_candidate_limit
from .models import (
    ImportProfile,
    ProposalDecision,
    ProposalOutcome,
    ProposalStatus,
    ResolutionProposal,
    locked_profile_policy,
)
from .object_permissions import ObjectPermissionDenied
from .proposal_tasks import CandidateSnapshot, proposal_inventory_staleness, proposal_task
from .resolution_proposals import decide_proposal


def proposal_staleness(proposal, *, netbox_reader=None, inventory=None):
    """Compare current inventory with frozen evidence without changing the proposal."""
    if inventory is None:
        if netbox_reader is None:
            raise ValueError("Proposal staleness requires current inventory or a scoped NetBox reader.")
        inventory = proposal_task(proposal.task_type).inventory(
            profile=proposal.profile,
            field_key=proposal.field_key,
            netbox_reader=netbox_reader,
            limit=proposal_candidate_limit(),
        )
    return proposal_inventory_staleness(proposal, inventory)


def accept_proposal(proposal_id, *, operator, netbox_reader) -> bool:
    """Write one fresh candidate decision under the profile, proposal, and resolution locks."""
    if operator is None or netbox_reader.actor != operator:
        raise ValueError("Acceptance requires a reader scoped to the deciding operator.")
    profile_id = ResolutionProposal.objects.values_list("profile_id", flat=True).get(pk=proposal_id)
    with locked_profile_policy(profile_id):
        proposal = ResolutionProposal.objects.select_for_update().get(pk=proposal_id, profile_id=profile_id)
        task = proposal_task(proposal.task_type)
        if (
            proposal.status != ProposalStatus.COMPLETED
            or proposal.outcome != ProposalOutcome.CANDIDATE
            or proposal.decision
        ):
            # atomic-exit-safe: proposal-refused-before-write
            return False
        proposal.profile = ImportProfile.objects.get(pk=profile_id)
        snapshot = CandidateSnapshot.from_json(proposal.candidate_snapshot)
        entry = next(
            (entry for entry in snapshot.entries if entry.candidate_id == proposal.selected_candidate_id), None
        )
        if entry is None:
            # atomic-exit-safe: proposal-refused-before-write
            return False
        receipt = task.write_resolution_if_fresh(
            proposal=proposal,
            entry=entry,
            actor=operator,
            netbox_reader=netbox_reader,
            limit=proposal_candidate_limit(),
        )
        if receipt is None:
            # atomic-exit-safe: proposal-refused-before-write
            return False
        decided = decide_proposal(
            proposal.pk,
            decision=ProposalDecision.ACCEPTED,
            operator=operator,
            written_resolution=receipt.written_resolution_id,
        )
        if not decided:
            transaction.set_rollback(True)
    return decided


def reject_proposal(proposal_id, *, operator) -> bool:
    """Record one rejection without changing status or requiring fresh inventory.

    Rejection writes no Row Resolution, so it takes the workspace permission and not the permission
    to create one (specification 7.6).
    """
    profile_id = ResolutionProposal.objects.values_list("profile_id", flat=True).get(pk=proposal_id)
    if not ImportProfile.objects.restrict(operator, "view").filter(pk=profile_id).exists():
        raise ObjectPermissionDenied(get_permission_for_model(ImportProfile, "view"))
    with locked_profile_policy(profile_id):
        proposal = ResolutionProposal.objects.select_for_update().get(pk=proposal_id, profile_id=profile_id)
        decided = decide_proposal(proposal.pk, decision=ProposalDecision.REJECTED, operator=operator)
    return decided

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Supply proposal cards and action reasons to the Review Workspace."""

from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.urls import reverse

from .api.serializers import ResolutionProposalSerializer
from .field_keys import SELECT_TERMINATION_TASK, TERMINATION_ROLE, parse_termination_field_key
from .inference_backend import NoActiveInferenceBackend, resolve_active_backend
from .inference_trust import InvalidInferenceConfiguration
from .models import ImportProfile, ProposalDecision, ProposalOutcome, ProposalStatus, ResolutionProposal
from .proposal_decisions import proposal_staleness
from .proposal_tasks import CandidateSnapshot, proposal_task
from .cable_target import AUTOMATICALLY_RESOLVED, MANUALLY_RESOLVED, UNRESOLVED

#: The badge modifier each field state wears, so the template needs no state vocabulary of its own.
STATE_STYLES = {
    "": "unknown",
    UNRESOLVED: "unresolved",
    AUTOMATICALLY_RESOLVED: "auto",
    MANUALLY_RESOLVED: "manual",
    "proposed": "proposed",
    ProposalDecision.ACCEPTED: "accepted",
    "stale": "stale",
    ProposalStatus.FAILED: "failed",
}

RECENT_PROPOSAL_HISTORY_LIMIT = 10


def group_terminations(fields):
    """Keep exact matches without proposal history in the compact settled group."""
    attention, settled = [], []
    for field in fields:
        display = field["proposal"]
        group = (
            settled if display["field_state"] == AUTOMATICALLY_RESOLVED and not field["proposal_history"] else attention
        )
        group.append(field)
    return attention, settled


def _action(key, label, reason):
    return {
        "key": key,
        "label": label,
        "reason": reason,
        "url": reverse(f"plugins:netbox_data_import:trace_{key}_proposal"),
    }


class ProposalPresentation:
    """Read one profile's proposal display with one backend lookup per response."""

    def __init__(self, *, profile, actor, reader):
        self.profile = profile
        self.actor = actor
        self.reader = reader
        self.preview_allowed = ImportProfile.objects.restrict(actor, "change").filter(pk=profile.pk).exists()
        self.view_reason = ""
        if not ImportProfile.objects.restrict(actor, "view").filter(pk=profile.pk).exists():
            self.view_reason = "You do not have permission to view proposals for this Import Profile."
        self.backend_reason = ""
        try:
            resolve_active_backend()
        except NoActiveInferenceBackend:
            self.backend_reason = "No Inference Backend is enabled or configured as a fallback."
        except (InvalidInferenceConfiguration, ValidationError):
            self.backend_reason = "The active Inference Backend configuration is invalid."

    def fields(self, fields):
        """Return each displayed field and its bounded history within the authorized profile."""
        if self.view_reason:
            return {field["field_key"]: self.field(field, None, [], False) for field in fields}
        histories: dict[str, list] = {field["field_key"]: [] for field in fields}
        rows = (
            ResolutionProposal.objects.filter(
                profile=self.profile, task_type=SELECT_TERMINATION_TASK, field_key__in=histories
            )
            .annotate(
                history_position=Window(
                    expression=RowNumber(),
                    partition_by=F("field_key"),
                    order_by=(F("created").desc(), F("pk").desc()),
                )
            )
            .filter(history_position__lte=RECENT_PROPOSAL_HISTORY_LIMIT + 1)
            .only("pk", "field_key", "created", "status", "outcome", "decision", "failure_reason")
            .order_by("field_key", "history_position")
        )
        for row in rows:
            histories[row.field_key].append(row)
        current_ids = [history[0].pk for history in histories.values() if history]
        current = {
            proposal.pk: proposal
            for proposal in ResolutionProposal.objects.filter(pk__in=current_ids).select_related(
                "profile", "resolved_device_type", "selected_object_type", "written_resolution"
            )
        }
        return {
            field["field_key"]: self.field(
                field,
                current.get(history[0].pk) if history else None,
                history[:RECENT_PROPOSAL_HISTORY_LIMIT],
                len(history) > RECENT_PROPOSAL_HISTORY_LIMIT,
            )
            for field in fields
            for history in (histories[field["field_key"]],)
        }

    def field(self, field, proposal, history, history_has_more):
        """Serialize the current attempt, freshness, actions, and recent summaries."""
        record = ResolutionProposalSerializer(proposal).data if proposal is not None else None
        history_url = None
        if history:
            query = urlencode({"profile_id": self.profile.pk, "field_key": field["field_key"]})
            history_url = f"{reverse('plugins-api:netbox_data_import-api:resolutionproposalhistory-list')}?{query}"
        payload = {
            "ok": True,
            "proposal": record,
            "history_display": [
                {
                    "id": row.pk,
                    "created": row.created.isoformat(),
                    "status": row.get_status_display(),
                    "outcome": row.get_outcome_display() or "No outcome",
                    "decision": row.get_decision_display(),
                    "failure": row.get_failure_reason_display(),
                }
                for row in history
            ],
            "history_has_more": history_has_more,
            "history_url": history_url,
            "staleness": None,
        }
        if self.reader is None:
            payload["staleness_error"] = "The saved import target is gone or outside your view scope."
        elif proposal is not None:
            stale = proposal_staleness(proposal, netbox_reader=self.reader)
            payload["staleness"] = {
                "is_stale": stale.is_stale,
                "resolved_device_changed": stale.resolved_device_changed,
                "candidates_changed": stale.candidates_changed,
            }
        payload["presentation"] = self.card(field, proposal, payload)
        return payload

    def request_permission_reason(self, field):
        """Explain preview access, task eligibility, and resolved Device access."""
        if not self.preview_allowed:
            return "You do not have permission to request proposals for this Import Profile."
        if parse_termination_field_key(field["field_key"])["role"] != TERMINATION_ROLE:
            return "Ask AI supports termination fields only. Choose the mapped peer manually."
        if not field.get("offered", True):
            return "This preview asked no question about that termination."
        if (
            self.reader is None
            or proposal_task(SELECT_TERMINATION_TASK).resolved_device(
                field_key=field["field_key"], netbox_reader=self.reader
            )
            is None
        ):
            return "The resolved Device is unavailable or outside your view permission."
        return ""

    def card(self, field, proposal, payload):
        """Derive all proposal vocabulary and legal actions in one place."""
        pending = proposal is not None and proposal.status in ProposalStatus.ACTIVE
        completed = proposal is not None and proposal.status == ProposalStatus.COMPLETED
        state = field["state"]
        if proposal is not None and proposal.decision == ProposalDecision.ACCEPTED:
            resolution = proposal.written_resolution
            if resolution is not None and (
                resolution.selected_object_type_id == proposal.selected_object_type_id
                and resolution.selected_object_id == proposal.selected_object_id
            ):
                state = ProposalDecision.ACCEPTED
        stale = payload["staleness"]
        stale_reason = payload.get("staleness_error", "")
        if stale and stale["is_stale"]:
            stale_reason = "The resolved Device or eligible candidates changed. Request a new proposal."
        candidate, missing, selected_entry = self.selected_candidate(proposal)
        if missing:
            # Acceptance refuses this row, so the card must not offer an action the writer declines.
            stale_reason = "The selected candidate is no longer in the request snapshot. Request a new proposal."
        actions = self.actions(field, proposal, state, pending, completed, stale_reason, selected_entry)
        badge = proposal.get_status_display() if proposal is not None else "No proposal"
        if completed:
            badge = "Proposal - stale, not applied" if stale_reason else "Proposal - not applied"
        if proposal is not None and proposal.decision:
            badge = proposal.get_decision_display()
        if state == UNRESOLVED and proposal is not None and not proposal.decision:
            if pending or proposal.outcome == ProposalOutcome.CANDIDATE:
                state = "proposed"
            if completed and stale_reason:
                state = "stale"
            if proposal.status == ProposalStatus.FAILED:
                state = ProposalStatus.FAILED
        metadata = (proposal.backend_metadata or {}) if proposal is not None else {}
        return {
            "has_proposal": proposal is not None,
            "pending": pending,
            "field_state": state,
            "state_style": STATE_STYLES[state],
            "badge": badge,
            "candidate": candidate,
            "explanation": proposal.explanation if proposal is not None else "",
            "failure": proposal.get_failure_reason_display() if proposal is not None else "",
            "failure_code": proposal.failure_reason if proposal is not None else "",
            "metadata": [
                {"label": key.replace("_", " "), "value": value}
                for key, value in metadata.items()
                if key != "attempts" and isinstance(value, (str, int, float))
            ],
            "attempt_count": len(metadata.get("attempts", [])),
            "actions": actions,
        }

    @staticmethod
    def selected_candidate(proposal):
        """Return the chosen candidate's display, and whether the snapshot no longer holds it."""
        if proposal is None or proposal.outcome != ProposalOutcome.CANDIDATE:
            return "", False, None
        snapshot = CandidateSnapshot.from_json(proposal.candidate_snapshot)
        entry = next(
            (row for row in snapshot.entries if row.candidate_id == proposal.selected_candidate_id),
            None,
        )
        if entry is None:
            return "", True, None
        return f"{entry.display_name} ({str(proposal.selected_object_type.name).capitalize()})", False, entry

    def actions(self, field, proposal, state, pending, completed, stale_reason, selected_entry):
        """Return every command with its current permission and lifecycle refusal."""
        permission_reason = self.request_permission_reason(field)
        request_reason = permission_reason
        if not request_reason and state != UNRESOLVED:
            request_reason = "This termination is already resolved."
        if not request_reason and pending:
            request_reason = "An active proposal already exists for this field."
        request_reason = request_reason or self.backend_reason
        if not field.get("offered", True):
            request_reason = "This preview asked no question about that termination."
        decision_reason = "" if completed else "Wait for a completed proposal."
        if proposal is not None and proposal.status == ProposalStatus.FAILED:
            decision_reason = (
                f"The proposal failed: {proposal.get_failure_reason_display()} ({proposal.failure_reason})."
            )
        if not field.get("offered", True):
            decision_reason = "This preview asked no question about that termination."
        accept_reason = decision_reason
        if not accept_reason and proposal.outcome == ProposalOutcome.NO_MATCH:
            accept_reason = "The backend found no match. There is no candidate to accept."
        accept_reason = accept_reason or stale_reason
        if not self.preview_allowed:
            accept_reason = "You do not have permission to save a termination resolution."
        elif selected_entry is not None:
            assessment = proposal_task(SELECT_TERMINATION_TASK).assess_resolution_write(
                profile=self.profile,
                field_key=proposal.field_key,
                entry=selected_entry,
                actor=self.actor,
            )
            if not assessment.allowed:
                accept_reason = "You do not have permission to save a termination resolution."
        reject_reason = decision_reason if self.preview_allowed else "You do not have permission to reject proposals."
        if proposal is not None and proposal.decision:
            accept_reason = reject_reason = "This proposal already has a decision."
        actions = [
            _action(
                "request",
                "Ask AI again" if proposal is not None and proposal.status == ProposalStatus.FAILED else "Ask AI",
                request_reason,
            ),
            _action("cancel", "Cancel", permission_reason or ("" if pending else "There is no active proposal.")),
            _action("accept", "Accept", accept_reason),
            _action("reject", "Reject", reject_reason),
        ]
        return [{**action, "reason": self.view_reason or action["reason"]} for action in actions]

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Supply proposal cards and action reasons to the Review Workspace."""

from django.core.exceptions import ValidationError
from django.urls import reverse

from .api.serializers import ResolutionProposalSerializer
from .field_keys import SELECT_TERMINATION_TASK, TERMINATION_ROLE, parse_termination_field_key
from .inference_backend import NoActiveInferenceBackend, resolve_active_backend
from .inference_trust import InvalidInferenceConfiguration
from .models import ImportProfile, ProposalDecision, ProposalOutcome, ProposalStatus, ResolutionProposal
from .proposal_decisions import proposal_staleness
from .proposal_tasks import proposal_task
from .review_workspace import UNRESOLVED


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
        """Return each displayed field and its complete history within the authorized profile."""
        if self.view_reason:
            return {field["field_key"]: self.field(field, []) for field in fields}
        histories: dict[str, list] = {field["field_key"]: [] for field in fields}
        rows = (
            ResolutionProposal.objects.filter(
                profile=self.profile, task_type=SELECT_TERMINATION_TASK, field_key__in=histories
            )
            .select_related("profile", "resolved_device_type", "selected_object_type", "written_resolution")
            .order_by("-created", "-pk")
        )
        for row in rows:
            histories[row.field_key].append(row)
        return {field["field_key"]: self.field(field, histories[field["field_key"]]) for field in fields}

    def field(self, field, history):
        """Serialize the current attempt, freshness, actions, and every earlier attempt."""
        proposal = history[0] if history else None
        records = ResolutionProposalSerializer(history, many=True).data
        payload = {"ok": True, "proposal": records[0] if records else None, "history": records, "staleness": None}
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
        payload["history_display"] = [
            {
                "id": row.pk,
                "created": row.created.isoformat(),
                "status": row.get_status_display(),
                "outcome": row.get_outcome_display() or "No outcome",
                "decision": row.get_decision_display(),
                "failure": row.get_failure_reason_display(),
            }
            for row in history
        ]
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
        actions = self.actions(field, proposal, state, pending, completed, stale_reason)
        badge = proposal.get_status_display() if proposal is not None else "No proposal"
        if completed:
            badge = "Proposal - stale, not applied" if stale_reason else "Proposal - not applied"
        if proposal is not None and proposal.decision:
            badge = proposal.get_decision_display()
        candidate = ""
        if proposal is not None and proposal.outcome == ProposalOutcome.CANDIDATE:
            entry = next(
                row
                for row in proposal.candidate_snapshot["candidates"]
                if row["candidate_id"] == proposal.selected_candidate_id
            )
            candidate = f"{entry['display_name']} ({str(proposal.selected_object_type.name).capitalize()})"
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

    def actions(self, field, proposal, state, pending, completed, stale_reason):
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
        if not field.get("offered", True):
            decision_reason = "This preview asked no question about that termination."
        if proposal is not None and proposal.decision:
            decision_reason = "This proposal already has a decision."
        accept_reason = decision_reason
        if not accept_reason and proposal.outcome == ProposalOutcome.NO_MATCH:
            accept_reason = "The backend found no match. There is no candidate to accept."
        accept_reason = accept_reason or stale_reason
        if not self.preview_allowed or not self.actor.has_perm("netbox_data_import.add_terminationresolution"):
            accept_reason = "You do not have permission to save a termination resolution."
        reject_reason = decision_reason if self.preview_allowed else "You do not have permission to reject proposals."
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

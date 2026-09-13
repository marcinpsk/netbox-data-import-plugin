# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The `select_termination` proposal task (specification 7.1).

The only module that knows termination kinds and the `TerminationResolution` row. The lifecycle sees
a task type, a field key and a generic Candidate Snapshot, and nothing else.
"""

from dataclasses import dataclass

from .cable_target import eligible_terminations, resolved_device_for
from .field_keys import SELECT_TERMINATION_TASK, TERMINATION_ROLE, parse_termination_field_key
from .proposal_tasks import CandidateSet, register_proposal_task, snapshot_from

__all__ = [
    "DecisionReceipt",
    "SelectTerminationTask",
    "UnsupportedProposalRole",
]


class UnsupportedProposalRole(Exception):
    """Proposals are requested for the termination role in this delivery (section 7.1)."""


@dataclass(frozen=True)
class DecisionReceipt:
    """What an accepted decision wrote. The coordinator stores the id and never reads the row."""

    written_resolution_id: int


def _label_for(candidate) -> str:
    """Return the `app_label.model` key one termination is recorded under."""
    return f"{candidate._meta.app_label}.{candidate._meta.model_name}"


def _name_for(candidate) -> str:
    """Return the display name at snapshot time, which a rename changes."""
    return str(candidate)


class SelectTerminationTask:
    """Retrieve, snapshot and resolve one termination field key."""

    task_type = SELECT_TERMINATION_TASK

    def _require_termination_role(self, field_key) -> dict:
        parsed = parse_termination_field_key(field_key)
        if parsed["role"] != TERMINATION_ROLE:
            raise UnsupportedProposalRole(f"A proposal is not requested for the '{parsed['role']}' role.")
        return parsed

    def current(self, *, profile, field_key, netbox_reader, limit):
        """Return the Candidate Snapshot as the world stands now, for a request or a freshness read."""
        self._require_termination_role(field_key)
        # The picker and a proposal request share this query, so both see one eligibility rule.
        eligible = eligible_terminations(field_key, netbox_reader, profile=profile, limit=limit)
        return snapshot_from(
            CandidateSet(objects=eligible.candidates, total=eligible.total),
            label_for=_label_for,
            name_for=_name_for,
            limit=limit,
        )

    def resolved_device(self, *, field_key, netbox_reader):
        """Return the one Device this key resolves to now, or None when it does not resolve to one."""
        self._require_termination_role(field_key)
        return resolved_device_for(field_key, netbox_reader)

    def require_decision_permission(self, actor) -> None:
        """Require the permission to create a manual Row Resolution for either decision."""
        from .object_permissions import ObjectPermissionDenied

        permission = "netbox_data_import.add_terminationresolution"
        if actor is None or not actor.has_perm(permission):
            raise ObjectPermissionDenied(permission)

    def write_resolution(self, *, profile, field_key, entry, actor) -> DecisionReceipt:
        """Upsert the Row Resolution the accepted candidate names, and return only its id."""
        from core.models import ObjectType

        from .models import TerminationResolution
        from .object_permissions import save_permission_scoped_object

        app_label, model = entry.object_type.split(".", 1)
        object_type = ObjectType.objects.get(app_label=app_label, model=model)
        lookup = {"profile": profile, "task_type": self.task_type, "field_key": field_key}
        values = {
            "selected_object_type": object_type,
            "selected_object_id": entry.object_id,
            "selected_display_name": entry.display_name,
        }
        candidate = TerminationResolution(**lookup, **values)
        candidate.full_clean(validate_unique=False, validate_constraints=False)
        saved = save_permission_scoped_object(actor, TerminationResolution, lookup, values)
        return DecisionReceipt(written_resolution_id=saved.instance.pk)


register_proposal_task(SELECT_TERMINATION_TASK, SelectTerminationTask())

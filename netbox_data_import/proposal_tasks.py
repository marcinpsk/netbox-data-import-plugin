# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The closed proposal task-type registry and the generic Candidate Snapshot (specification 7.9).

This is the seam that keeps termination specifics out of the lifecycle. A later task type adds a key
shape and a candidate retrieval, and changes nothing here.
"""

from dataclasses import dataclass

from .proposal_response import candidate_id_for, validate_candidate_ids

#: Why a request cannot be made, or why a snapshot cannot establish freshness.
NO_CANDIDATES = "no_candidates"
TOO_MANY_CANDIDATES = "too_many_candidates"

__all__ = [
    "NO_CANDIDATES",
    "TOO_MANY_CANDIDATES",
    "CandidateSet",
    "CandidateSnapshot",
    "CandidateSnapshotEntry",
    "ProposalInventory",
    "UnknownProposalTask",
    "UnusableCandidateSet",
    "proposal_task",
    "register_proposal_task",
    "snapshot_from",
]


class UnknownProposalTask(Exception):
    """No task type with that name is registered. The registry is closed on purpose."""


class UnusableCandidateSet(Exception):
    """The eligible set cannot back a proposal. `reason` is one of the two module constants."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class CandidateSnapshotEntry:
    """One candidate as it stood at snapshot time (section 7.3)."""

    candidate_id: str
    object_type: str
    object_id: int
    display_name: str

    def as_json(self) -> dict:
        """Return the stored form. Only strings and integers cross into the row."""
        return {
            "candidate_id": self.candidate_id,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class CandidateSnapshot:
    """The immutable candidate set one request was built from."""

    entries: tuple[CandidateSnapshotEntry, ...]
    total: int

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        """Return the opaque identifiers this request offered, in order."""
        return tuple(entry.candidate_id for entry in self.entries)

    def as_json(self) -> dict:
        """Return the stored form of the whole snapshot."""
        return {"total": self.total, "candidates": [entry.as_json() for entry in self.entries]}

    @classmethod
    def from_json(cls, stored) -> "CandidateSnapshot":
        """Rebuild a stored snapshot so a read-time comparison works on the same shape."""
        entries = tuple(CandidateSnapshotEntry(**entry) for entry in stored["candidates"])
        return cls(entries=entries, total=stored["total"])

    def matches(self, other) -> bool:
        """Compare the whole set, display names included: a rename changed the evidence."""
        return self.total == other.total and self.entries == other.entries


@dataclass(frozen=True)
class CandidateSet:
    """What a task type retrieves: the objects themselves and the uncapped matching total."""

    objects: tuple
    total: int


@dataclass(frozen=True)
class ProposalInventory:
    """The resolved Device and eligible candidates one freshness read observed together."""

    resolved_device: object | None
    candidate_snapshot: CandidateSnapshot | None


def snapshot_from(candidate_set, *, label_for, name_for, limit) -> CandidateSnapshot:
    """Turn one retrieved set into a snapshot, refusing a set that cannot back a proposal.

    A truncated result must never establish freshness, so the retrieved count has to equal the
    uncapped total. Identifiers are positional and opaque; nothing about the object leaks into them.
    """
    total = candidate_set.total
    if total == 0:
        raise UnusableCandidateSet(NO_CANDIDATES, "No eligible candidate exists for this field.")
    if total > limit:
        raise UnusableCandidateSet(
            TOO_MANY_CANDIDATES,
            f"{total} eligible candidates exceed the configured bound of {limit}.",
        )
    if len(candidate_set.objects) != total:
        raise UnusableCandidateSet(
            TOO_MANY_CANDIDATES,
            f"The retrieval returned {len(candidate_set.objects)} of {total} candidates.",
        )
    entries = tuple(
        CandidateSnapshotEntry(
            candidate_id=candidate_id_for(position),
            object_type=label_for(candidate),
            object_id=candidate.pk,
            display_name=name_for(candidate),
        )
        for position, candidate in enumerate(candidate_set.objects)
    )
    validate_candidate_ids(entry.candidate_id for entry in entries)
    return CandidateSnapshot(entries=entries, total=total)


_REGISTRY: dict = {}


def register_proposal_task(task_type, task) -> None:
    """Add one task type to the closed registry, at import time."""
    _REGISTRY[task_type] = task


def proposal_task(task_type):
    """Return the registered task, refusing a name the deployment never declared."""
    try:
        return _REGISTRY[task_type]
    except KeyError as exc:
        raise UnknownProposalTask(f"'{task_type}' is not a proposal task type.") from exc

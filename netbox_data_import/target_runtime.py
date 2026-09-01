# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The seam every Target Module implements and the coordinator calls.

Section 2.1 fixes these call shapes. The seam lives apart from the implementations so a Target
Module can depend on it without depending on another Target Module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .plan import PlannedChange, SynchronizationUnit


class PreconditionFailed(Exception):
    """Target state moved between planning and the write, so the change no longer applies."""


@dataclass(frozen=True)
class ExecutionContext:
    """What a Target Module needs while the coordinator's transaction is open."""

    actor: Any
    reader: Any
    profile: Any


@dataclass(frozen=True)
class DeletedObject:
    """One object a Planned Change removed, for the execution's deleted-object snapshot."""

    object_type: str
    object_id: int
    display: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Return the serialized form the audit row stores."""
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "display": self.display,
            "detail": dict(self.detail),
        }


class TargetModuleRuntime(Protocol):
    """What the coordinator may call on a Target Module."""

    key: str
    consumes: frozenset[str]

    def plan(self, source_batch, profile, catalog, netbox_reader) -> list[SynchronizationUnit]:
        """Return the Synchronization Units this module owns for the whole batch."""
        ...

    def apply(self, planned_change: PlannedChange, execution_context) -> Any:
        """Apply one Planned Change inside the coordinator's transaction."""
        ...


__all__ = (
    "DeletedObject",
    "ExecutionContext",
    "PreconditionFailed",
    "TargetModuleRuntime",
)

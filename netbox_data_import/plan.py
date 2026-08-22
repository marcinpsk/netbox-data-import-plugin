# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The target-neutral Import Plan.

An Import Plan is a serializable derived artifact: Synchronization Units holding typed Planned
Changes, their dispositions, and structured diagnostics. It is the contract between the Import
Engine, the Review Workspace, the session, and a background job payload.

This module holds no NetBox import. Every value that enters a plan passes through a canonical JSON
round trip, so a live ORM object, a queryset, a callable, or a template fragment cannot reach one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

_DIAGNOSTIC_CODE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*\.[a-z0-9]+(?:_[a-z0-9]+)*$")


class PlanError(Exception):
    """Base class for the Import Plan failures the coordinator reports."""


class PlanInvalid(PlanError):
    """A plan violates a structural invariant and cannot execute."""


class PlanSchemaMismatch(PlanError):
    """A serialized plan states a schema version this release does not execute."""


class Disposition:
    """The exactly-one state a Synchronization Unit carries (section 4.2)."""

    ACTIONABLE = "actionable"
    NO_OP = "no-op"
    BLOCKED = "blocked"
    INVALID = "invalid"
    EXCLUDED = "excluded"

    ALL = frozenset({ACTIONABLE, NO_OP, BLOCKED, INVALID, EXCLUDED})


class Severity:
    """Diagnostic severity (section 4.2)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    ALL = frozenset({INFO, WARNING, ERROR})


def canonical_json(value: Any) -> str:
    """Return the canonical serialization used for every fingerprint.

    ``allow_nan`` stays off: NaN and Infinity are not JSON, PostgreSQL rejects them in a JSONField,
    and NaN never equals itself, which would make two identical shared changes look conflicting.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint_of(value: Any) -> str:
    """Return the SHA-256 digest of the canonical serialization of *value*."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _plain(value: Any, label: str) -> Any:
    """Return *value* as detached plain JSON data, rejecting anything a plan may not carry."""
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise PlanInvalid(f"{label} must be JSON-serializable plan data: {exc}") from exc


def _elements(value, kind, label: str) -> tuple:
    """Return *value* as a tuple, rejecting any element that is not an instance of *kind*."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise PlanInvalid(f"{label} must be a list or tuple of {kind.__name__} objects.")
    for item in value:
        if not isinstance(item, kind):
            raise PlanInvalid(f"{label} must hold {kind.__name__} objects, not {type(item).__name__}.")
    return tuple(value)


def _identities(value, label: str) -> tuple[str, ...]:
    """Return *value* as a tuple of identity strings.

    A bare string is refused because tuple("rack:1") would silently become six identities.
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise PlanInvalid(f"{label} must be a list or tuple of identity strings.")
    for item in value:
        if not isinstance(item, str):
            raise PlanInvalid(f"{label} must hold identity strings, not {type(item).__name__}.")
    return tuple(value)


@dataclass(frozen=True)
class Diagnostic:
    """One structured finding attached to a plan or a unit."""

    code: str
    severity: str
    identities: tuple[str, ...] = ()
    display: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate the code namespace and the severity vocabulary."""
        if not _DIAGNOSTIC_CODE.match(self.code or ""):
            raise PlanInvalid(f"Diagnostic code '{self.code}' must use the '<domain>.<condition>' form.")
        if self.severity not in Severity.ALL:
            raise PlanInvalid(f"Unknown diagnostic severity '{self.severity}'.")
        object.__setattr__(self, "identities", _identities(self.identities, "Diagnostic identities"))
        object.__setattr__(self, "display", _plain(self.display, "Diagnostic display"))

    def __hash__(self):
        """Hash over the serialized form, which mirrors the generated equality."""
        return hash(canonical_json(self.to_dict()))

    @property
    def fingerprint_data(self):
        """Return the decision inputs: the code, the severity, and the affected identities."""
        return {"code": self.code, "severity": self.severity, "identities": list(self.identities)}

    def to_dict(self) -> dict:
        """Return the serialized form."""
        return {
            "code": self.code,
            "severity": self.severity,
            "identities": list(self.identities),
            "display": dict(self.display),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Diagnostic:
        """Rebuild a diagnostic from its serialized form."""
        return cls(
            code=data["code"],
            severity=data["severity"],
            identities=tuple(data.get("identities", ())),
            display=data.get("display", {}),
        )


@dataclass(frozen=True)
class PlannedChange:
    """One typed target write with its dependencies and target-state preconditions."""

    identity: str
    target_module: str
    operation: str
    payload: dict
    dependencies: tuple[str, ...] = ()
    preconditions: dict = field(default_factory=dict)

    def __post_init__(self):
        """Detach the mappings from planning state and reject anything a plan may not carry."""
        if not self.identity:
            raise PlanInvalid("A Planned Change needs a stable identity.")
        for name in ("target_module", "operation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise PlanInvalid(f"A Planned Change needs a non-empty {name.replace('_', ' ')}.")
        object.__setattr__(self, "dependencies", _identities(self.dependencies, "Planned Change dependencies"))
        object.__setattr__(self, "payload", _plain(self.payload, "Planned Change payload"))
        object.__setattr__(self, "preconditions", _plain(self.preconditions, "Planned Change preconditions"))

    def __hash__(self):
        """Hash over the serialized form, which mirrors the generated equality."""
        return hash(canonical_json(self.to_dict()))

    @property
    def fingerprint_data(self):
        """Return every decision input this change contributes."""
        return {
            "identity": self.identity,
            "target_module": self.target_module,
            "operation": self.operation,
            "payload": self.payload,
            "dependencies": list(self.dependencies),
            "preconditions": self.preconditions,
        }

    def to_dict(self) -> dict:
        """Return a detached copy of the serialized form, which is exactly the fingerprint data."""
        return json.loads(canonical_json(self.fingerprint_data))

    @classmethod
    def from_dict(cls, data: dict) -> PlannedChange:
        """Rebuild a change from its serialized form."""
        return cls(
            identity=data["identity"],
            target_module=data["target_module"],
            operation=data["operation"],
            payload=data.get("payload", {}),
            dependencies=tuple(data.get("dependencies", ())),
            preconditions=data.get("preconditions", {}),
        )


@dataclass(frozen=True)
class SynchronizationUnit:
    """The smallest independently reviewable and executable part of a plan."""

    identity: str
    disposition: str
    changes: tuple[PlannedChange, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    display: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate the disposition and detach the display data."""
        if not self.identity:
            raise PlanInvalid("A Synchronization Unit needs a stable identity.")
        if self.disposition not in Disposition.ALL:
            raise PlanInvalid(f"Unknown disposition '{self.disposition}'.")
        object.__setattr__(self, "changes", _elements(self.changes, PlannedChange, "Synchronization Unit changes"))
        object.__setattr__(
            self, "diagnostics", _elements(self.diagnostics, Diagnostic, "Synchronization Unit diagnostics")
        )
        object.__setattr__(self, "display", _plain(self.display, "Synchronization Unit display"))

    def __hash__(self):
        """Hash over the serialized form, which mirrors the generated equality."""
        return hash(canonical_json(self.to_dict()))

    @property
    def fingerprint_data(self):
        """Return the decision inputs, which exclude the display wording."""
        return {
            "identity": self.identity,
            "disposition": self.disposition,
            "changes": [change.fingerprint_data for change in self.changes],
            "diagnostics": [diagnostic.fingerprint_data for diagnostic in self.diagnostics],
        }

    @property
    def fingerprint(self) -> str:
        """Return this unit's canonical fingerprint, so selection compares one unit at a time."""
        return fingerprint_of(self.fingerprint_data)

    def to_dict(self) -> dict:
        """Return the serialized form."""
        return {
            "identity": self.identity,
            "disposition": self.disposition,
            "changes": [change.to_dict() for change in self.changes],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "display": dict(self.display),
        }

    @classmethod
    def from_dict(cls, data: dict) -> SynchronizationUnit:
        """Rebuild a unit from its serialized form."""
        return cls(
            identity=data["identity"],
            disposition=data["disposition"],
            changes=tuple(PlannedChange.from_dict(item) for item in data.get("changes", ())),
            diagnostics=tuple(Diagnostic.from_dict(item) for item in data.get("diagnostics", ())),
            display=data.get("display", {}),
        )


@dataclass(frozen=True)
class ImportPlan:
    """A serializable plan: units, diagnostics, and the inputs its fingerprint covers."""

    units: tuple[SynchronizationUnit, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    source_fingerprint: str = ""
    profile_fingerprint: str = ""
    actor: str = ""
    planning_context: dict = field(default_factory=dict)
    revision: int = 1
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        """Detach the planning context from planning state."""
        object.__setattr__(self, "units", _elements(self.units, SynchronizationUnit, "Import Plan units"))
        object.__setattr__(self, "diagnostics", _elements(self.diagnostics, Diagnostic, "Import Plan diagnostics"))
        object.__setattr__(self, "planning_context", _plain(self.planning_context, "Planning context"))
        counts = Counter(unit.identity for unit in self.units)
        duplicates = sorted(identity for identity, count in counts.items() if count > 1)
        if duplicates:
            raise PlanInvalid(f"Synchronization Unit identities must be unique: {', '.join(duplicates)}.")

    def __hash__(self):
        """Hash over the serialized form, which mirrors the generated equality."""
        return hash(canonical_json(self.to_dict()))

    @property
    def fingerprint_data(self):
        """Return the decision inputs of section 4.3, which exclude display data and the revision."""
        return {
            "schema_version": self.schema_version,
            "units": [unit.fingerprint_data for unit in self.units],
            "diagnostics": [diagnostic.fingerprint_data for diagnostic in self.diagnostics],
            "source_fingerprint": self.source_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "actor": self.actor,
            "planning_context": self.planning_context,
        }

    @property
    def fingerprint(self) -> str:
        """Return the canonical plan fingerprint."""
        return fingerprint_of(self.fingerprint_data)

    def unit(self, identity: str) -> SynchronizationUnit | None:
        """Return the unit stored under *identity*, or None."""
        for unit in self.units:
            if unit.identity == identity:
                return unit
        return None

    def to_dict(self) -> dict:
        """Return the serialized form the session and a job payload carry."""
        return {
            "schema_version": self.schema_version,
            "units": [unit.to_dict() for unit in self.units],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "source_fingerprint": self.source_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "actor": self.actor,
            "planning_context": dict(self.planning_context),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ImportPlan:
        """Rebuild a plan, rejecting a schema version this release does not execute.

        Every other malformed payload also raises a PlanError, so one caller-side ``except PlanError``
        covers a corrupted session entry or job payload (section 4.8).
        """
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise PlanSchemaMismatch(f"Import Plan schema version {version} is not version {SCHEMA_VERSION}.")
        try:
            return cls(
                units=tuple(SynchronizationUnit.from_dict(item) for item in data["units"]),
                diagnostics=tuple(Diagnostic.from_dict(item) for item in data["diagnostics"]),
                source_fingerprint=data.get("source_fingerprint", ""),
                profile_fingerprint=data.get("profile_fingerprint", ""),
                actor=data.get("actor", ""),
                planning_context=data.get("planning_context", {}),
                revision=data.get("revision", 1),
                schema_version=version,
            )
        except PlanError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PlanInvalid(f"The serialized Import Plan is malformed: {exc!r}") from exc


def executable_units(units) -> tuple[SynchronizationUnit, ...]:
    """Return only the actionable units.

    Section 4.6: blocked, invalid, excluded, and no-op units never enter an execution transaction.
    """
    return tuple(unit for unit in units if unit.disposition == Disposition.ACTIONABLE)


def merge_changes(units, *, reconciled=()) -> tuple[PlannedChange, ...]:
    """Merge the changes of *units* into one deterministic acyclic execution order.

    Identical identities are shared and execute once. A conflicting payload or precondition, a
    dangling dependency, and a cycle each make the plan invalid (section 4.4).

    *reconciled* holds the identities a previous execution already applied. They satisfy a
    dependency without being merged in, which is what section 4.5 means by a dependency that is
    already reconciled. Passing an identity here never adds work to the selection.

    This merges whatever units it is given. Filter with ``executable_units`` before executing.
    """
    merged: dict[str, PlannedChange] = {}
    order: list[str] = []
    for unit in units:
        for change in unit.changes:
            existing = merged.get(change.identity)
            if existing is None:
                merged[change.identity] = change
                order.append(change.identity)
            elif existing.fingerprint_data != change.fingerprint_data:
                raise PlanInvalid(
                    f"Planned Change '{change.identity}' appears with conflicting content, so the plan cannot share it."
                )

    already_applied = frozenset(reconciled)
    for identity, change in merged.items():
        for dependency in change.dependencies:
            if dependency not in merged and dependency not in already_applied:
                raise PlanInvalid(
                    f"Planned Change '{identity}' depends on '{dependency}', "
                    "which the selection neither contains nor has reconciled."
                )

    return _topological_order(merged, order)


def _topological_order(merged: dict, order: list[str]) -> tuple[PlannedChange, ...]:
    """Return the changes in dependency order, breaking ties by first appearance."""
    position = {identity: index for index, identity in enumerate(order)}
    state: dict[str, int] = {}
    result: list[PlannedChange] = []

    def visit(identity: str, path: tuple[str, ...]):
        """Depth-first visit that reports the identities forming a cycle."""
        if state.get(identity) == 2:
            return
        if state.get(identity) == 1:
            cycle = " -> ".join(path[path.index(identity) :] + (identity,))
            raise PlanInvalid(f"The Planned Change dependencies form a cycle: {cycle}.")
        state[identity] = 1
        for dependency in sorted(
            (dep for dep in merged[identity].dependencies if dep in merged), key=lambda dep: position[dep]
        ):
            visit(dependency, path + (identity,))
        state[identity] = 2
        result.append(merged[identity])

    for identity in order:
        visit(identity, ())
    return tuple(result)


__all__ = (
    "SCHEMA_VERSION",
    "Diagnostic",
    "Disposition",
    "ImportPlan",
    "PlanError",
    "PlanInvalid",
    "PlanSchemaMismatch",
    "PlannedChange",
    "Severity",
    "SynchronizationUnit",
    "canonical_json",
    "executable_units",
    "fingerprint_of",
    "merge_changes",
)

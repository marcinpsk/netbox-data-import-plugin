# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Cable Target Module: Patched Path Replacement planning and writes.

Section 6 gives one complete Source Trace one Synchronization Unit: it deletes the single direct
Logical Cable when one exists, then creates every physical segment the trace states. It reads the
decisions and the Cable policy the Import Profile holds, and it writes Cables and provenance rows.

It consumes Source Traces by output kind, so it imports no Source Adapter. It reads PortMapping
rows and never writes one, and it never creates or mutates a CablePath.

Live target state that no Planned Change carries joins the unit through an `info` diagnostic.
Visible reused Cables and PortMapping rows contribute identities. Hidden Cable drift contributes
only its diagnostic code. Execution replans inside its transaction, so a material change makes the
accepted unit stale and rolls the write back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from . import cable_harness
from .cable_disclosure import (
    DEVICE_ROW,
    WORDING_CODES,
    DISCLOSURE_SOURCE,
    SEGMENT_END_SOURCES,
    wording_sources,
    device_source,
    disclosed_cable,
    disclosed_policy,
    disclosure_source,
    segment_end_sources,
    termination_sources,
)
from .catalog import OutputKind, TargetModuleKey
from .field_keys import (
    ADMITTED_TERMINATION_MODELS,
    CABLE_END_KINDS,
    FRONT_PORT_MODEL,
    MAPPED_PEER_ROLE,
    REAR_PORT_MODEL,
    SELECT_TERMINATION_TASK,
    TERMINATION_ROLE,
    claimed_termination_kind,
    parse_termination_field_key,
    same_device_and_cards,
    termination_field_key,
)
from .identity import CANONICAL_NAME, identity_text, matching_search, with_name_identity
from .object_permissions import enforce_saved_object_permission
from .plan import (
    Diagnostic,
    Disposition,
    PlannedChange,
    Severity,
    SynchronizationUnit,
    fingerprint_of,
    validate_diagnostic_display,
)
from .target_runtime import DeletedObject, PreconditionFailed
from .trace_device_resolution import (
    STALE as DEVICE_STALE,
    collect_trace_device_evidence,
    resolve_trace_devices,
    resolved_trace_device,
    source_device_key,
)
from .values import source_text

CABLE_STATUS = "connected"
ELIGIBLE_TERMINATION_LIMIT = 20

# Section 10.2 badge vocabulary. T8 adds the proposal states to the same set.
UNRESOLVED = "unresolved"
AUTOMATICALLY_RESOLVED = "automatically resolved"
MANUALLY_RESOLVED = "manually resolved"

CREATE_SEGMENT = "create"
REUSE_SEGMENT = "reuse existing"

# A PortMapping joins a front port to a rear port; no other Cable End Kind passes a path through.
_PASS_THROUGH_PEERS = {FRONT_PORT_MODEL: REAR_PORT_MODEL, REAR_PORT_MODEL: FRONT_PORT_MODEL}


@dataclass(frozen=True)
class EligibleTerminations:
    """One capped page of eligible terminations and its uncapped matching total."""

    candidates: tuple[Any, ...]
    total: int


def _model_for_label(label: str):
    """Return the model class one recorded object-type label names."""
    from django.apps import apps

    app_label, _, model_name = label.partition(".")
    return apps.get_model(app_label, model_name)


def _view_permission(label: str) -> str:
    """Return the NetBox view permission for the model one object-type label names."""
    app_label, _, model_name = label.partition(".")
    return f"{app_label}.view_{model_name}"


def _stored_label(stored) -> str:
    """Return the object-type label one saved termination decision selected."""
    return f"{stored.selected_object_type.app_label}.{stored.selected_object_type.model}"


def _object_identity(label: str, object_id: int) -> str:
    """Return the plan identity string for one target object."""
    return f"{label}:{object_id}"


@dataclass(frozen=True)
class _Termination:
    """One source Termination Reference bound to a NetBox object."""

    object_type: str
    object_id: int
    device_id: int
    display: str
    topology_display: str

    @property
    def model_name(self) -> str:
        """Return the NetBox model name, the form NetBox's compatible-termination table uses."""
        return self.object_type.partition(".")[2]

    @property
    def key(self) -> tuple[str, int]:
        """Return the comparison key that names this object across queries."""
        return self.object_type, self.object_id

    @property
    def identity(self) -> str:
        """Return the plan identity string for this object."""
        return _object_identity(self.object_type, self.object_id)

    def as_json(self) -> list:
        """Return the serializable object and resolved Device a creation precondition carries."""
        return [self.object_type, self.object_id, self.device_id]


@dataclass(frozen=True)
class _DesiredSegment:
    """One physical Cable the trace states."""

    index: int
    left: _Termination
    right: _Termination
    cable_class: str

    @property
    def key(self) -> str:
        """Return the direction-independent key two traces share for one identical segment."""
        first, second = sorted((self.left.key, self.right.key))
        return f"{_object_identity(*first)}|{_object_identity(*second)}"

    @property
    def change_identity(self) -> str:
        """Return the Planned Change identity this segment creates under."""
        return f"cable:create:{self.key}"

    @property
    def terminations(self) -> tuple[_Termination, _Termination]:
        """Return both ends in canonical order."""
        return self.left, self.right

    def as_json(self) -> list:
        """Return the sorted termination records a payload and a precondition carry."""
        return sorted((self.left.as_json(), self.right.as_json()))


@dataclass(frozen=True)
class _ExistingCable:
    """One existing Cable and the termination sets it holds."""

    cable: Any
    a_side: frozenset
    b_side: frozenset
    # Side, connector, positions and object of every CableTermination row: what a harness reuse compares.
    snapshot: frozenset = frozenset()

    @property
    def multi_termination(self) -> bool:
        """Return whether either side holds more than one termination."""
        return len(self.a_side) > 1 or len(self.b_side) > 1

    @property
    def terminations(self) -> list:
        """Return the sorted termination pairs a precondition records."""
        return sorted([label, object_id] for label, object_id in self.a_side | self.b_side)

    def satisfies(self, segment: _DesiredSegment) -> bool:
        """Return whether this Cable is exactly the unordered pair *segment* states."""
        return self.connects(segment.left, segment.right)

    def connects(self, first: _Termination, second: _Termination) -> bool:
        """Return whether this Cable directly joins the two given terminations, one end each."""
        return (self.a_side, self.b_side) in (
            ({first.key}, {second.key}),
            ({second.key}, {first.key}),
        )


def _delete_identity(cable_pk: int) -> str:
    """Return the Planned Change identity that removes one Logical Cable.

    The unit's dependency graph and the deletion change both name it, so one helper states it.
    """
    return f"cable:delete:{cable_pk}"


def _cable_review_snapshot(cable) -> dict:
    """Read the Logical Cable metadata that review and deletion must share."""
    return {
        "display": str(cable),
        "description": cable.description,
        "tags": tuple(sorted(cable.tags.values_list("name", flat=True))),
    }


def _deleted_cable_review_fingerprint(cable) -> str:
    """Hash the review metadata whose plaintext must not survive Cable deletion."""
    return fingerprint_of(_cable_review_snapshot(cable))


@dataclass(frozen=True, eq=False)
class _Leg:
    """One trace's segment from a channel child to the far end its parent's harness cables."""

    analysis: Any
    segment: _DesiredSegment
    child: _Termination
    far: _Termination


@dataclass(eq=False)
class _Harness:
    """One channelized parent, the legs the batch states on it, and the traces that block it."""

    parent: cable_harness.Parent
    termination: _Termination
    legs: list = field(default_factory=list)
    unsupported: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    unchannelized: list = field(default_factory=list)
    intent: cable_harness.CableIntent | None = None
    cable_type: str | None = None
    reused: _ExistingCable | None = None
    # Every port of the harness with its Device, and the sources they give every finding about it.
    wording_ends: list = field(default_factory=list)
    wording: dict = field(default_factory=dict)

    @property
    def change_identity(self) -> str:
        """Return the Planned Change identity every leg unit carries for this harness."""
        return f"cable:create:harness:{self.termination.identity}"

    @property
    def pending(self) -> bool:
        """Return whether the harness can be written and no existing Cable already is it."""
        return self.intent is not None and self.reused is None

    @property
    def ends(self) -> list[_Termination]:
        """Return the parent and every far end the harness claims."""
        return [self.termination, *(leg.far for leg in self.legs)]

    def settle_wording(self, resolved: Mapping[str, Mapping[tuple, _Termination]]) -> None:
        """Bind every finding about this harness to every port it involves, each with its Device.

        A finding on one leg can describe the parent, a sibling child, another leg's far end or a trace
        that blocks the harness, so the whole harness is one disclosure unit (section 6.6).
        """
        ends = [self.termination, *(end for leg in self.legs for end in (leg.child, leg.far))]
        blockers = (*self.unsupported, *self.unresolved, *(analysis for analysis, _port in self.unchannelized))
        ends.extend(end for analysis in blockers for end in resolved.get(analysis.identity, {}).values())
        children = [
            ((child.component._meta.label_lower, child.component.pk), child.component.device_id)
            for child in self.parent.children
            if child.channel is not None
        ]
        self.wording_ends = [*((end.key, end.device_id) for end in ends), *children]
        self.wording = wording_sources(self.wording_ends)

    def companions(self, leg: _Leg) -> tuple[str, ...]:
        """Return the unit identity of every other leg, which must be selected together with *leg*."""
        return tuple(sorted(other.analysis.identity for other in self.legs if other is not leg))


@dataclass
class _TraceAnalysis:
    """What one Source Trace contributes, built in planning order."""

    trace: Any
    identity: str
    display: dict
    diagnostics: list = field(default_factory=list)
    endpoints: tuple = ()
    segments: list = field(default_factory=list)
    segment_pairs_resolved: bool = False
    proven: dict = field(default_factory=dict)
    policies: dict = field(default_factory=dict)
    # Index i means a verified PortMapping joins segment i to segment i + 1.
    joined: set = field(default_factory=set)
    devices: dict = field(default_factory=dict)
    terminations: dict = field(default_factory=dict)
    resolution_started: bool = False
    topology_read: bool = False
    logical_cable: Any = None
    retains_logical_cable: bool = False
    blocked: bool = False
    invalid: bool = False
    # The one segment that leaves a channel child, which the harness of its parent writes.
    leg: Any = None
    harness: Any = None
    selected_with: tuple = ()

    @property
    def stopped(self) -> bool:
        """Return whether a finding makes further planning for this trace meaningless."""
        return self.blocked or self.invalid

    def is_leg(self, segment: _DesiredSegment) -> bool:
        """Return whether *segment* is this trace's harness leg rather than an ordinary segment."""
        return self.leg is not None and segment.index == self.leg.segment.index

    @property
    def pending(self) -> list:
        """Return the ordinary desired segments no existing Cable already proves."""
        return [segment for segment in self.segments if segment.index not in self.proven and not self.is_leg(segment)]

    @property
    def harness_pending(self) -> bool:
        """Return whether this trace's harness still has to be created."""
        return self.harness is not None and self.harness.pending

    @property
    def deleted_logical_cable(self):
        """Return the Logical Cable this trace removes, which a Cable that proves its path is not."""
        return None if self.retains_logical_cable else self.logical_cable

    @property
    def delete_identity(self) -> str | None:
        """Return the Planned Change identity that removes this trace's Logical Cable."""
        removed = self.deleted_logical_cable
        return None if removed is None else _delete_identity(removed.cable.pk)

    def error(self, code: str, display: dict, identities=(), evidence=None) -> None:
        """Record one blocking or invalidating finding."""
        validate_diagnostic_display(code, display)
        self.diagnostics.append(
            Diagnostic(
                code=code,
                severity=Severity.ERROR,
                identities=tuple(identities),
                display=display,
                evidence=evidence or {},
            )
        )

    def note(self, code: str, display: dict, identities=(), evidence=None) -> None:
        """Record one review note whose identities keep the unit honest about live state."""
        validate_diagnostic_display(code, display)
        self.diagnostics.append(
            Diagnostic(
                code=code,
                severity=Severity.INFO,
                identities=tuple(identities),
                display=display,
                evidence=evidence or {},
            )
        )

    def warn(self, code: str, display: dict, identities=(), evidence=None) -> None:
        """Record a finding the operator has to see, which changes no disposition."""
        validate_diagnostic_display(code, display)
        self.diagnostics.append(
            Diagnostic(
                code=code,
                severity=Severity.WARNING,
                identities=tuple(identities),
                display=display,
                evidence=evidence or {},
            )
        )

    def block(self, code: str, display: dict, identities=(), evidence=None) -> None:
        """Record a finding an operator decision or a NetBox correction can resolve."""
        self.error(code, display, identities, evidence)
        self.blocked = True

    def refuse(self, code: str, display: dict, identities=()) -> None:
        """Record a finding no decision inside this plugin can resolve."""
        self.error(code, display, identities)
        self.invalid = True

    def reopen(self, termination_key: tuple[str, int]) -> None:
        """Keep each end resolved to one termination in attention, so its picker can choose another."""
        source = disclosure_source(*termination_key)
        for record in self.terminations.values():
            if record.get(DISCLOSURE_SOURCE) == source:
                record["incompatible"] = True


def _endpoint_label(reference) -> str:
    """Return the operator-facing name of one Termination Reference."""
    parts = [source_text(reference.device), source_text(reference.cards), source_text(reference.port)]
    return " ".join(part for part in parts if part)


def _reference_display(reference) -> dict:
    """Return the source values one Termination Reference states."""
    return {
        "device": source_text(reference.device),
        "cards": source_text(reference.cards),
        "port": source_text(reference.port),
        "port_class": source_text(reference.port_class),
    }


def _field_key(reference, role: str = TERMINATION_ROLE) -> str:
    """Return the canonical field key one Termination Reference resolves under."""
    return termination_field_key(
        device=reference.device,
        cards=reference.cards,
        port=reference.port,
        kind=claimed_termination_kind(reference.port_class),
        role=role,
    )


def _source_record(trace, segment_index: int) -> dict:
    """Return the provenance one Source Trace contributes to one created Cable."""
    provenance = trace.provenance[0]
    return {
        "trace_identity": trace.identity,
        "segment_index": segment_index,
        "from_text": provenance.from_text,
        "to_text": provenance.to_text,
        "direction": provenance.direction,
        "workbook_fingerprint": provenance.workbook_fingerprint,
        "sheet": provenance.sheet,
        "block_ordinal": provenance.block_ordinal,
        "row_start": provenance.row_start,
        "row_end": provenance.row_end,
        "export_timestamp": provenance.export_timestamp,
    }


def resolved_device_for(field_key: str, netbox_reader, *, profile, _lock_rows=False):
    """Return the one visible Device a termination field key names, or None when it names no single one.

    A proposal freezes this reference and revalidates it, so the retrieval and the freshness check
    have to agree on what "the resolved Device" means.
    """
    key = parse_termination_field_key(field_key)["device"]
    return resolved_trace_device(profile=profile, reader=netbox_reader, key=key, lock_rows=_lock_rows)


def _named_terminations(netbox_reader, label: str, device_id: int, identities) -> dict[str, list]:
    """Return one Device's rows of one model whose name identity is wanted, each marked as visible or not.

    The read is unscoped, so the actor's view scope cannot change which model the exact-name rule
    stops at. Only the visibility mark comes from the actor's scope.
    """
    from django.db.models import Exists, OuterRef

    rows = (
        with_name_identity(_model_for_label(label).objects.filter(device_id=device_id))
        .filter(**{f"{CANONICAL_NAME}__in": sorted(identities)})
        .annotate(actor_may_view=Exists(netbox_reader.terminations(label).filter(pk=OuterRef("pk"))))
        .order_by("pk")
    )
    named: dict[str, list] = {}
    for row in rows:
        named.setdefault(getattr(row, CANONICAL_NAME), []).append(row)
    return named


def _exact_name_match(kind: str, identity: str, named_for) -> Any | None:
    """Return the one row the section 6.1 exact-name rule resolves a port *identity* to, or None.

    The rule stops at the first admitted model with any match. Several matches or a hidden one leave
    the field open there, and never fall through to a later model.
    """
    for label in ADMITTED_TERMINATION_MODELS[kind]:
        matches = named_for(label).get(identity, [])
        if matches:
            return matches[0] if len(matches) == 1 and matches[0].actor_may_view else None
    return None


def _mapped_peer_sources(parsed: dict, device, netbox_reader, profile) -> tuple:
    """Return the opposite ports one resolved pass-through port maps to, as (label, rows) sources."""
    from .models import TerminationResolution, index_digest

    base_label = ADMITTED_TERMINATION_MODELS[parsed["kind"]][0]
    if base_label not in _PASS_THROUGH_PEERS:
        return ()
    base_field_key = termination_field_key(
        device=parsed["device"],
        cards=parsed["cards"],
        port=parsed["port"],
        kind=parsed["kind"],
        role=TERMINATION_ROLE,
    )
    stored = (
        TerminationResolution.objects.filter(
            profile=profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=base_field_key,
            field_key_digest=index_digest(base_field_key),
        )
        .select_related("selected_object_type")
        .first()
    )
    if stored is None:
        identity = parsed["port"]
        base = _exact_name_match(
            parsed["kind"],
            identity,
            partial(_named_terminations, netbox_reader, device_id=device.pk, identities=(identity,)),
        )
    elif _stored_label(stored) == base_label:
        base = netbox_reader.terminations(base_label).filter(pk=stored.selected_object_id, device_id=device.pk).first()
    else:
        base = None
    if base is None:
        return ()
    mappings = netbox_reader.port_mappings()
    if base_label == FRONT_PORT_MODEL:
        peer_ids = mappings.filter(front_port_id=base.pk).values_list("rear_port_id", flat=True)
    else:
        peer_ids = mappings.filter(rear_port_id=base.pk).values_list("front_port_id", flat=True)
    peer_label = _PASS_THROUGH_PEERS[base_label]
    return ((peer_label, netbox_reader.terminations(peer_label).filter(device_id=device.pk, pk__in=peer_ids)),)


_RESOLVED_DEVICE_UNSET = object()


def eligible_terminations(
    field_key: str,
    netbox_reader,
    *,
    profile,
    search: str = "",
    limit: int = ELIGIBLE_TERMINATION_LIMIT,
    offset: int = 0,
    _resolved_device=_RESOLVED_DEVICE_UNSET,
    _lock_rows: bool = False,
) -> EligibleTerminations:
    """Return one page of candidates for a canonical termination field key.

    The termination role offers every model the claimed kind admits on the resolved Device, as one
    set with one count, one bound, and one order (section 6.1). The mapped-peer role starts from the
    profile's saved base resolution or the exact-name match, then offers its opposite ports. The
    reader keeps both inside the actor's view scope. The picker and a proposal request share this
    query. The models count and page in the database as one set, so no read holds more than *limit*
    rows.
    """
    parsed = parse_termination_field_key(field_key)
    device = (
        resolved_device_for(field_key, netbox_reader, profile=profile, _lock_rows=_lock_rows)
        if _resolved_device is _RESOLVED_DEVICE_UNSET
        else _resolved_device
    )
    if device is None:
        return EligibleTerminations(candidates=(), total=0)
    if parsed["role"] == MAPPED_PEER_ROLE:
        sources = _mapped_peer_sources(parsed, device, netbox_reader, profile)
    else:
        sources = tuple(
            (label, netbox_reader.terminations(label).filter(device_id=device.pk))
            for label in ADMITTED_TERMINATION_MODELS[parsed["kind"]]
        )
    wanted = identity_text(search)
    sources = tuple((label, matching_search(rows, search)) for label, rows in sources)
    if _lock_rows:
        # One global order, object type then id, as the segment locks take it.
        for _label, rows in sorted(sources, key=lambda source: source[0]):
            tuple(rows.order_by("pk").select_for_update(of=("self",)).values_list("pk", flat=True))
    total = sum(rows.count() for _label, rows in sources)
    if offset >= total:
        return EligibleTerminations(candidates=(), total=total)
    ranked = [_ranked_keys(rows, wanted, order) for order, (_label, rows) in enumerate(sources)]
    combined = ranked[0].union(*ranked[1:], all=True) if len(ranked) > 1 else ranked[0]
    page = combined.order_by("-_ndi_exact", "_ndi_name", "_ndi_order", "_ndi_pk")[offset : offset + limit]
    wanted_ids: dict[int, list[int]] = {}
    for _exact, _name, order, pk in page:
        wanted_ids.setdefault(order, []).append(pk)
    loaded = {order: sources[order][1].in_bulk(ids) for order, ids in wanted_ids.items()}
    # A row deleted, moved, or renamed after the ranking query is no longer offered.
    return EligibleTerminations(
        candidates=tuple(loaded[order][pk] for _exact, _name, order, pk in page if pk in loaded[order]),
        total=total,
    )


def _ranked_keys(rows, wanted: str, order: int):
    """Return one model's order keys for the combined order, which the database pages as one set.

    The bytewise collation orders names the same way across every admitted model.
    """
    from django.db.models import Case, F, IntegerField, Value, When
    from django.db.models.functions import Collate

    if wanted:
        exact = Case(When(**{CANONICAL_NAME: wanted}, then=Value(1)), default=Value(0), output_field=IntegerField())
        rows = with_name_identity(rows)
    else:
        exact = Value(0, output_field=IntegerField())
    return (
        rows.order_by()
        .annotate(
            _ndi_exact=exact,
            _ndi_name=Collate("name", "C"),
            _ndi_order=Value(order, output_field=IntegerField()),
            _ndi_pk=F("pk"),
        )
        .values_list("_ndi_exact", "_ndi_name", "_ndi_order", "_ndi_pk")
    )


class _CableBatch:
    """Plan every Source Trace in one batch, so an identical shared segment plans once."""

    def __init__(self, traces, profile, netbox_reader, *, lock_plan_references: bool = False):
        traces = tuple(traces)
        self.profile = profile
        self.reader = netbox_reader
        self.actor = netbox_reader.actor
        self.lock_plan_references = lock_plan_references
        self.analyses = [self._new_analysis(trace) for trace in traces]
        self._objects: dict[tuple[str, int], Any] = {}
        self._visible: dict[tuple[str, int], bool] = {}
        self._named: dict[tuple[int, str], dict[str, list]] = {}
        self._asked_identities: dict[int, set[str]] = {}
        self._resolved: dict[str, dict[tuple, _Termination]] = {}
        self._mappings_by_front: dict[int, list] = {}
        self._mappings_by_rear: dict[int, list] = {}
        self._mapping_ports: dict[tuple[str, int], Any] = {}
        self._existing: dict[int, _ExistingCable] = {}
        self._cable_review_snapshots: dict[int, dict] = {}
        self._occupied: dict[tuple[str, int], _ExistingCable] = {}
        self._mapping_rows: dict[str, Any] | None = None
        self._overrides: dict[str, Any] = {}
        self._overrides_by_trace: dict[str, list] = {}
        self._parents: dict[int, cable_harness.Parent] = {}
        self._harnesses: dict[int, _Harness] = {}
        self._unsupported_traces: list[tuple[_TraceAnalysis, _Harness, int]] = []
        self._stored = self._stored_resolutions()
        active_traces = (analysis.trace for analysis in self.analyses if not analysis.stopped)
        self._device_evidence = collect_trace_device_evidence(active_traces)
        self._device_resolutions = resolve_trace_devices(
            profile=self.profile,
            reader=self.reader,
            evidence=self._device_evidence,
            lock_rows=self.lock_plan_references,
        )
        self._device_displays = {
            resolution.device.pk: str(resolution.device)
            for resolution in self._device_resolutions.values()
            if resolution.device is not None
        }
        self._collect_asked_ports()
        self._resolve_terminations()
        self._load_mappings()
        self._build_segments()
        self._load_overrides()
        self._lock_harness_parents()
        self._lock_segment_terminations()
        self._find_legs()
        self._load_existing_cables()
        self._classify()
        self._plan_harnesses()
        self._decide()
        self._assess_media()
        self._block_planned_termination_conflicts()
        self._block_conflicting_creations()
        self._deletes_by_segment = self._shared_deletes()
        self._sources_by_segment = self._shared_sources()

    def units(self) -> list[SynchronizationUnit]:
        """Return one Synchronization Unit per Source Trace, in batch order."""
        return [self._unit(analysis) for analysis in self.analyses]

    def _new_analysis(self, trace) -> _TraceAnalysis:
        """Return the analysis record one Source Trace starts from."""
        summary = trace.endpoint_summary
        provenance = trace.provenance[0]
        display = {
            "name": f"{_endpoint_label(summary.from_termination)} to {_endpoint_label(summary.to_termination)}",
            "row_number": provenance.row_start,
            "sheet": provenance.sheet,
            "trace_identity": trace.identity,
            "from_text": provenance.from_text,
            "to_text": provenance.to_text,
            "segment_count": len(trace.segments),
        }
        analysis = _TraceAnalysis(trace=trace, identity=f"cable:trace:{trace.identity}", display=display)
        for error in trace.errors:
            analysis.refuse(error.code, {"message": error.message, "row_number": error.row_number})
        return analysis

    @staticmethod
    def _references(trace) -> list:
        """Return every Termination Reference one trace resolves, endpoints included."""
        summary = trace.endpoint_summary
        references = [summary.from_termination, summary.to_termination]
        for segment in trace.segments:
            references.extend((segment.left, segment.right))
        return references

    def _stored_resolutions(self) -> dict[str, Any]:
        """Return the operator's saved termination decisions, keyed by field key."""
        from .models import TerminationResolution

        field_keys = set()
        for analysis in self.analyses:
            if analysis.stopped:
                continue
            for reference in self._references(analysis.trace):
                field_keys.add(_field_key(reference))
                field_keys.add(_field_key(reference, MAPPED_PEER_ROLE))
        if not field_keys:
            return {}
        from .models import index_digest

        # The unique constraint indexes the digest, and nothing indexes the unbounded field key.
        rows = TerminationResolution.objects.filter(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key_digest__in=sorted(index_digest(key) for key in field_keys),
        ).select_related("selected_object_type")
        return {row.field_key: row for row in rows}

    def _collect_asked_ports(self) -> None:
        """Collect, per resolved Device, the name identity of every port the exact-name rule is asked about."""
        for analysis in self.analyses:
            if analysis.stopped:
                continue
            for reference in self._references(analysis.trace):
                device = self._device_resolutions[source_device_key(reference.device)].device
                if device is not None and _field_key(reference) not in self._stored:
                    self._asked_identities.setdefault(device.pk, set()).add(identity_text(reference.port))

    def _named_for(self, device_id: int, label: str) -> dict[str, list]:
        """Return one Device's asked rows of one model by name identity, read once per batch."""
        if (device_id, label) not in self._named:
            self._named[(device_id, label)] = _named_terminations(
                self.reader, label, device_id, self._asked_identities[device_id]
            )
        return self._named[(device_id, label)]

    def _resolve_terminations(self) -> None:
        """Bind every Termination Reference to one NetBox object, or record why it stays open."""
        for analysis in self.analyses:
            if analysis.stopped:
                continue
            analysis.resolution_started = True
            resolved: dict[tuple, _Termination] = {}
            for reference in self._references(analysis.trace):
                if reference.identity_key in resolved:
                    continue
                termination = self._resolve_one(analysis, reference)
                if termination is not None:
                    resolved[reference.identity_key] = termination
            self._resolved[analysis.identity] = resolved

    def _resolve_one(self, analysis: _TraceAnalysis, reference) -> _Termination | None:
        """Return the NetBox object one Termination Reference names, or record the open decision."""
        resolution = self._device_resolutions[source_device_key(reference.device)]
        question = resolution.to_question()
        if resolution.device is not None:
            question[DISCLOSURE_SOURCE] = device_source(resolution.device.pk)
        analysis.devices.setdefault(resolution.evidence.key, question)
        if resolution.device is None:
            code = "trace.device_resolution_stale" if resolution.state == DEVICE_STALE else "trace.device_unresolved"
            analysis.block(
                code,
                {**_reference_display(reference), "matches": resolution.exact_match_count},
            )
            # With no resolved Device there is nothing to pick from, so the picker cannot help here.
            self._record_resolution(
                analysis,
                reference,
                UNRESOLVED,
                None,
                reason="Resolve the source Device before choosing one of its ports.",
            )
            return None
        device = resolution.device
        stored = self._stored.get(_field_key(reference))
        if stored is not None:
            termination = self._stored_termination(analysis, reference, device, stored)
            state = UNRESOLVED if termination is None else MANUALLY_RESOLVED
            self._record_resolution(analysis, reference, state, termination)
            return termination
        match = _exact_name_match(
            claimed_termination_kind(reference.port_class),
            identity_text(reference.port),
            partial(self._named_for, device.pk),
        )
        if match is None:
            # A count would disclose hidden matches, so the finding states the source values alone.
            analysis.block("cable.termination_unresolved", _reference_display(reference))
            self._record_resolution(analysis, reference, UNRESOLVED, None)
            return None
        termination = self._termination(match)
        self._record_resolution(analysis, reference, AUTOMATICALLY_RESOLVED, termination)
        return termination

    @staticmethod
    def _record_resolution(
        analysis: _TraceAnalysis,
        reference,
        state: str,
        termination,
        reason: str = "",
        role: str = TERMINATION_ROLE,
    ) -> None:
        """Record how one Termination Reference was settled, for its badge and its picker."""
        key = _field_key(reference, role)
        label = _endpoint_label(reference)
        record = {
            "field_key": key,
            "label": f"{label} (mapped peer)" if role == MAPPED_PEER_ROLE else label,
            "state": state,
            "selected": "" if termination is None else termination.display,
            "selected_type": "" if termination is None else termination.object_type,
            # A saved decision keeps this spelling beside its key, so a later identity change can rekey it.
            "source": {"device": reference.device, "cards": reference.cards, "port": reference.port},
            "selectable": not reason,
            "reason": reason,
            # An end NetBox cannot cable, alone or with its peer, reopens its picker however it was resolved.
            "incompatible": False,
        }
        if termination is not None:
            record[DISCLOSURE_SOURCE] = disclosure_source(*termination.key)
        analysis.terminations.setdefault(key, record)

    def _stored_termination(self, analysis: _TraceAnalysis, reference, device, stored) -> _Termination | None:
        """Return the object one saved decision selected, rechecked against current target state."""
        label = _stored_label(stored)
        if label not in CABLE_END_KINDS:
            analysis.refuse(
                "cable.unsupported_termination_kind",
                {**_reference_display(reference), "selected_object_type": label},
            )
            return None
        claimed_kind = claimed_termination_kind(reference.port_class)
        if label not in ADMITTED_TERMINATION_MODELS[claimed_kind]:
            analysis.block(
                "cable.termination_kind_mismatch",
                {**_reference_display(reference), "claimed_kind": claimed_kind, "selected_object_type": label},
            )
            return None
        # A saved selection that left the resolved Device no longer answers the question it was asked.
        selected = self.reader.terminations(label).filter(pk=stored.selected_object_id, device_id=device.pk).first()
        if selected is None:
            # The saved name may belong to a port the actor cannot view, so the finding states source values alone.
            analysis.block("cable.termination_unresolved", _reference_display(reference))
            return None
        return self._termination(selected)

    def _termination(self, component) -> _Termination:
        """Return the plan-side record of one resolved NetBox termination."""
        label = component._meta.label_lower
        self._objects[(label, component.pk)] = component
        return _Termination(
            object_type=label,
            object_id=component.pk,
            device_id=component.device_id,
            display=str(component),
            topology_display=f"{self._device_displays[component.device_id]} {component.name}",
        )

    def _load_mappings(self) -> None:
        """Read every PortMapping row on a Device this batch resolved a pass-through port on."""
        device_ids = {
            termination.device_id
            for resolved in self._resolved.values()
            for termination in resolved.values()
            if termination.object_type in _PASS_THROUGH_PEERS
        }
        if device_ids:
            mappings = self.reader.port_mappings().filter(device_id__in=sorted(device_ids)).order_by("pk")
            if self.lock_plan_references:
                mappings = mappings.select_for_update(of=("self",))
            # Indexed on load, because every pass-through end asks for the rows of one port.
            for row in mappings:
                self._mappings_by_front.setdefault(row.front_port_id, []).append(row)
                self._mappings_by_rear.setdefault(row.rear_port_id, []).append(row)
            ids_by_label = {
                FRONT_PORT_MODEL: set(self._mappings_by_front),
                REAR_PORT_MODEL: set(self._mappings_by_rear),
            }
            for label, object_ids in ids_by_label.items():
                for component in self.reader.terminations(label).filter(pk__in=object_ids):
                    self._mapping_ports[(label, component.pk)] = component

    def _peers_of(self, termination: _Termination) -> list:
        """Return the PortMapping rows that link one pass-through port to its opposite side."""
        if termination.object_type == FRONT_PORT_MODEL:
            return self._mappings_by_front.get(termination.object_id, [])
        if termination.object_type == REAR_PORT_MODEL:
            return self._mappings_by_rear.get(termination.object_id, [])
        return []

    def _peer_termination(self, mapping, termination: _Termination) -> _Termination | None:
        """Return the visible opposite port one PortMapping row links to *termination*."""
        if termination.object_type == FRONT_PORT_MODEL:
            peer_key = REAR_PORT_MODEL, mapping.rear_port_id
        else:
            peer_key = FRONT_PORT_MODEL, mapping.front_port_id
        component = self._mapping_ports.get(peer_key)
        return None if component is None else self._termination(component)

    def _mapped_peers(self, analysis: _TraceAnalysis, reference, termination: _Termination) -> list | None:
        """Return visible mapped peers, or block when one peer is outside the actor's view scope."""
        peers = []
        for mapping in self._peers_of(termination):
            peer = self._peer_termination(mapping, termination)
            if peer is None:
                analysis.block(
                    "cable.permission_denied",
                    {
                        **_reference_display(reference),
                        "permission": _view_permission(_PASS_THROUGH_PEERS[termination.object_type]),
                    },
                )
                return None
            peers.append((peer, mapping))
        return peers

    def _build_segments(self) -> None:
        """Verify every Pass-Through Claim and turn Segment Evidence into desired segments."""
        for analysis in self.analyses:
            if analysis.stopped:
                continue
            resolved = self._resolved.get(analysis.identity, {})
            summary = analysis.trace.endpoint_summary
            endpoints = tuple(
                resolved.get(reference.identity_key) for reference in (summary.from_termination, summary.to_termination)
            )
            if any(endpoint is None for endpoint in endpoints):
                continue
            analysis.endpoints = endpoints
            self._build_one(analysis, resolved)

    def _build_one(self, analysis: _TraceAnalysis, resolved: dict) -> None:
        """Bind one trace's Segment Evidence, substituting a mapped peer where the source repeats a port."""
        segments = analysis.trace.segments
        left_ends: list[_Termination] = []
        right_ends: list[_Termination] = []
        for segment in segments:
            left, right = resolved.get(segment.left.identity_key), resolved.get(segment.right.identity_key)
            if left is None or right is None:
                return
            left_ends.append(left)
            right_ends.append(right)
        for index in range(len(segments) - 1):
            if not same_device_and_cards(segments[index].right, segments[index + 1].left):
                continue
            entry = self._continue_path(analysis, segments[index + 1].left, right_ends[index], left_ends[index + 1])
            if entry is None:
                return
            left_ends[index + 1] = entry
            analysis.joined.add(index)
        analysis.segment_pairs_resolved = True
        analysis.segments = [
            _DesiredSegment(
                index=index,
                left=left_ends[index],
                right=right_ends[index],
                cable_class=source_text(segment.cable_class),
            )
            for index, segment in enumerate(segments)
        ]
        for index, segment in enumerate(segments):
            if left_ends[index].key != right_ends[index].key:
                continue
            analysis.block(
                "cable.segment_self_connection",
                {
                    "segment_index": index,
                    "cable_class": source_text(segment.cable_class),
                    "termination": left_ends[index].display,
                    **termination_sources(left_ends[index].key),
                },
                identities=(left_ends[index].identity,),
            )
            return
        self._block_incompatible_segments(analysis)

    @staticmethod
    def _block_incompatible_segments(analysis: _TraceAnalysis) -> None:
        """Block each segment whose two ends NetBox's own compatible-termination table does not pair."""
        from dcim.constants import COMPATIBLE_TERMINATION_TYPES

        stated = analysis.trace.segments
        for segment in analysis.segments:
            if segment.right.model_name in COMPATIBLE_TERMINATION_TYPES[segment.left.model_name]:
                continue
            ends = (_field_key(stated[segment.index].left), _field_key(stated[segment.index].right))
            for key in ends:
                analysis.terminations[key]["incompatible"] = True
            analysis.block(
                "cable.incompatible_terminations",
                {
                    "segment_index": segment.index,
                    "left_model": segment.left.object_type,
                    "right_model": segment.right.object_type,
                    "left_field_key": ends[0],
                    "right_field_key": ends[1],
                    **termination_sources(segment.left.key, segment.right.key),
                },
                identities=(segment.left.identity, segment.right.identity),
            )

    def _continue_path(self, analysis: _TraceAnalysis, reference, exit_end, entry_end) -> _Termination | None:
        """Return the termination the next cable end takes where the path passes through a panel."""
        if exit_end.key != entry_end.key:
            return self._verified_pass_through(analysis, reference, exit_end, entry_end)
        mapped_peers = self._mapped_peers(analysis, reference, exit_end)
        if mapped_peers is None:
            return None
        peers: dict[tuple[str, int], Any] = {}
        for peer, row in mapped_peers:
            peers.setdefault(peer.key, (peer, row))
        if not peers:
            analysis.refuse(
                "cable.pass_through_not_mapped",
                {
                    **_reference_display(reference),
                    "entry": exit_end.display,
                    "exit": entry_end.display,
                    "mapped": [],
                    **termination_sources(exit_end.key, entry_end.key),
                },
                identities=(exit_end.identity,),
            )
            return None
        if len(peers) > 1:
            return self._chosen_peer(analysis, reference, exit_end, peers)
        peer, mapping = next(iter(peers.values()))
        return self._substituted(analysis, reference, exit_end, peer, mapping)

    def _substituted(self, analysis: _TraceAnalysis, reference, exit_end, peer, mapping) -> _Termination:
        """Record one same-port continuation and return the mapped peer it substitutes."""
        analysis.note(
            "cable.same_port_continuation",
            {
                **_reference_display(reference),
                "port": exit_end.display,
                "peer": peer.display,
                **termination_sources(exit_end.key, peer.key),
            },
            identities=(exit_end.identity, peer.identity, _object_identity("dcim.portmapping", mapping.pk)),
        )
        return peer

    def _chosen_peer(self, analysis: _TraceAnalysis, reference, exit_end, peers) -> _Termination | None:
        """Return the mapped peer the operator selected, or block on the several NetBox offers."""
        stored = self._stored.get(_field_key(reference, MAPPED_PEER_ROLE))
        if stored is not None:
            selected = (_stored_label(stored), stored.selected_object_id)
            if selected in peers:
                peer, mapping = peers[selected]
                self._record_resolution(analysis, reference, MANUALLY_RESOLVED, peer, role=MAPPED_PEER_ROLE)
                return self._substituted(analysis, reference, exit_end, peer, mapping)
        self._record_resolution(analysis, reference, UNRESOLVED, None, role=MAPPED_PEER_ROLE)
        analysis.block(
            "cable.ambiguous_mapped_peer",
            {
                **_reference_display(reference),
                "port": exit_end.display,
                "peers": sorted(peer.display for peer, _mapping in peers.values()),
                **termination_sources(exit_end.key, *sorted(peers)),
            },
            identities=(exit_end.identity, *sorted(_object_identity(*key) for key in peers)),
        )
        return None

    def _verified_pass_through(self, analysis: _TraceAnalysis, reference, exit_end, entry_end) -> _Termination | None:
        """Return the stated entry port once a PortMapping row proves the panel joins the two ports."""
        mapping = None
        if {exit_end.object_type, entry_end.object_type} == {FRONT_PORT_MODEL, REAR_PORT_MODEL}:
            front, rear = (exit_end, entry_end) if exit_end.object_type == FRONT_PORT_MODEL else (entry_end, exit_end)
            mapping = next(
                (row for row in self._peers_of(front) if row.rear_port_id == rear.object_id),
                None,
            )
        if mapping is None:
            mapped = []
            for row in self._peers_of(exit_end):
                peer = self._peer_termination(row, exit_end)
                if peer is not None:
                    mapped.append(peer)
            analysis.refuse(
                "cable.pass_through_not_mapped",
                {
                    **_reference_display(reference),
                    "entry": exit_end.display,
                    "exit": entry_end.display,
                    "mapped": sorted(peer.display for peer in mapped),
                    **termination_sources(exit_end.key, entry_end.key, *sorted({peer.key for peer in mapped})),
                },
                identities=(exit_end.identity, entry_end.identity),
            )
            return None
        analysis.note(
            "cable.pass_through_verified",
            {
                **_reference_display(reference),
                "entry": exit_end.display,
                "exit": entry_end.display,
                **termination_sources(exit_end.key, entry_end.key),
            },
            identities=(exit_end.identity, entry_end.identity, _object_identity("dcim.portmapping", mapping.pk)),
        )
        return entry_end

    def _load_existing_cables(self) -> None:
        """Read every Cable that already holds a termination this batch resolved."""
        from dcim.models import Cable, CableTermination

        wanted = {
            termination.key for analysis in self.analyses for termination in self._analysis_terminations(analysis)
        }
        wanted.update(harness.termination.key for harness in self._harnesses.values())
        # Occupancy is unscoped: a Cable the actor cannot view still holds the termination.
        cable_ids = {
            component.cable_id for key, component in self._objects.items() if key in wanted and component.cable_id
        }
        if not cable_ids:
            return
        cables = Cable.objects.filter(pk__in=sorted(cable_ids)).order_by("pk")
        terminations = CableTermination.objects.filter(cable_id__in=sorted(cable_ids)).order_by("pk")
        if self.lock_plan_references:
            cables = cables.select_for_update(of=("self",))
            terminations = terminations.select_for_update(of=("self",))
        # Each Cable lock precedes its CableTermination locks, as it does during deletion.
        locked = list(cables)
        sides: dict[int, dict[str, set]] = {}
        snapshots: dict[int, set] = {}
        for row in terminations:
            label = row.termination_type.model_class()._meta.label_lower
            sides.setdefault(row.cable_id, {"A": set(), "B": set()})[row.cable_end].add((label, row.termination_id))
            snapshots.setdefault(row.cable_id, set()).add(
                (row.cable_end, row.connector, tuple(row.positions or ()), label, row.termination_id)
            )
        if self.lock_plan_references:
            from core.models import ObjectType
            from django.db import connection
            from extras.models import TaggedItem

            associations = list(
                TaggedItem.objects.filter(
                    content_type_id=ObjectType.objects.get_for_model(Cable).pk,
                    object_id__in=sorted(cable_ids),
                )
                .order_by("pk")
                .select_for_update(of=("self",))
            )
            tag_ids = sorted({association.tag_id for association in associations})
            if tag_ids:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM extras_tag WHERE id = ANY(%s) ORDER BY id FOR SHARE",
                        [tag_ids],
                    )
                    cursor.fetchall()
        for cable in locked:
            ends = sides.get(cable.pk, {"A": set(), "B": set()})
            existing = _ExistingCable(
                cable=cable,
                a_side=frozenset(ends["A"]),
                b_side=frozenset(ends["B"]),
                snapshot=frozenset(snapshots.get(cable.pk, ())),
            )
            self._existing[cable.pk] = existing
            for key in existing.a_side | existing.b_side:
                self._occupied[key] = existing

    def _lock_segment_terminations(self) -> None:
        """Lock every desired segment termination in global object-type and primary-key order."""
        if not self.lock_plan_references:
            return
        ids_by_label: dict[str, set[int]] = {}
        for analysis in self.analyses:
            for segment in analysis.segments:
                for termination in segment.terminations:
                    ids_by_label.setdefault(termination.object_type, set()).add(termination.object_id)
        for label in sorted(ids_by_label):
            object_ids = sorted(ids_by_label[label])
            for object_id in object_ids:
                self._objects.pop((label, object_id), None)
            components = (
                _model_for_label(label).objects.filter(pk__in=object_ids).order_by("pk").select_for_update(of=("self",))
            )
            for component in components:
                self._objects[(label, component.pk)] = component

    def _channel_parent(self, termination: _Termination) -> int | None:
        """Return the parent of one resolved termination that is a channel child, from its latest row."""
        component = self._objects.get(termination.key)
        return None if component is None else cable_harness.channel_parent(component)

    def _child_parents(self) -> set[int]:
        """Return the parent of every resolved termination that is a channel child."""
        return {
            parent
            for resolved in self._resolved.values()
            for termination in resolved.values()
            if (parent := self._channel_parent(termination)) is not None
        }

    def _lock_harness_parents(self) -> None:
        """Lock each parent of a resolved channel child, then every Interface under it, before the segment locks."""
        if self.lock_plan_references:
            self._parents = cable_harness.read_parents(self._child_parents(), lock=True)

    def _find_legs(self) -> None:
        """Give each trace that reaches a channel child to its parent's harness, as a leg or as a blocker.

        A child can move to another parent between the first read and its lock, so that parent is read,
        and locked, here.
        """
        parents = self._child_parents()
        self._parents.update(cable_harness.read_parents(parents - self._parents.keys(), lock=self.lock_plan_references))
        self._forget_orphaned_children()
        for analysis in self.analyses:
            self._place_trace(analysis)
        for analysis in self.analyses:
            self._block_harness_of_ordinary_parent(analysis)
        for parent_pk in sorted(self._harnesses):
            self._gather_cohort(self._harnesses[parent_pk])
        for parent_pk in sorted(self._harnesses):
            self._harnesses[parent_pk].settle_wording(self._resolved)
        # Its wording names the whole harness, which is known only once every trace is placed.
        for analysis, harness, segment_index in self._unsupported_traces:
            analysis.block(
                "cable.harness_unsupported",
                self._harness_display(harness, segment_index, trace=analysis.display["name"]),
                identities=(harness.termination.identity,),
            )

    def _forget_orphaned_children(self) -> None:
        """Forget each resolved channel child whose parent row is gone, as any row gone since resolution.

        An unlocked preview reads a parent after its children, so another transaction can delete both
        in between. The child is then no leg, its write skips the row, and execution refuses the plan.
        """
        for key, component in list(self._objects.items()):
            parent = cable_harness.channel_parent(component)
            if parent is not None and parent not in self._parents:
                del self._objects[key]

    def _harness(self, parent_pk: int) -> _Harness:
        """Return the harness of one parent, created on first use."""
        if parent_pk not in self._harnesses:
            parent = self._parents[parent_pk]
            self._harnesses[parent_pk] = _Harness(
                parent=parent, termination=self._channel_termination(parent.component)
            )
        return self._harnesses[parent_pk]

    def _channel_termination(self, component) -> _Termination:
        """Return the plan record of a parent or a child, which no trace may have resolved a Device for."""
        # On a virtual chassis a parent and its children can sit on different member Devices.
        self._device_displays.setdefault(component.device_id, str(component.device))
        return self._termination(component)

    def _place_trace(self, analysis: _TraceAnalysis) -> None:
        """Record one trace as the leg of its parent's harness, or as a trace that blocks that harness."""
        touched = sorted(
            {
                parent
                for termination in self._resolved.get(analysis.identity, {}).values()
                if (parent := self._channel_parent(termination)) is not None
            }
        )
        if not touched:
            return
        legs = [
            (segment, end, parent)
            for segment in analysis.segments
            for end in segment.terminations
            if (parent := self._channel_parent(end)) is not None
        ]
        if len(legs) > 1:
            # A child at both ends of one segment, or a trace between two harnesses.
            self._unsupported_traces.append((analysis, self._harness(touched[0]), legs[0][0].index))
            for parent in touched:
                self._harness(parent).unsupported.append(analysis)
            return
        if not legs:
            # The trace names a channel child but no physical first hop from it.
            for parent in touched:
                self._harness(parent).unresolved.append(analysis)
            return
        ((segment, child, parent),) = legs
        harness = self._harness(parent)
        far = segment.right if child.key == segment.left.key else segment.left
        analysis.leg = _Leg(analysis=analysis, segment=segment, child=child, far=far)
        analysis.harness = harness
        harness.legs.append(analysis.leg)

    def _block_harness_of_ordinary_parent(self, analysis: _TraceAnalysis) -> None:
        """Block the harness of each parent this trace names as an ordinary segment end: the source states two paths on it."""
        for segment in analysis.segments:
            if analysis.is_leg(segment):
                continue
            for end in segment.terminations:
                harness = self._harnesses.get(end.object_id) if end.object_type == cable_harness.INTERFACE else None
                if harness is not None and analysis not in harness.unsupported:
                    harness.unsupported.append(analysis)

    def _gather_cohort(self, harness: _Harness) -> None:
        """Read the source cohorts of one harness as negative evidence (section 6.4).

        A cohort is the device and cards label of a reference that resolves to a child of the parent.
        Any trace with an unresolved reference in a cohort blocks the harness, so a lane the source
        states but the plan cannot resolve never leaves a shorter harness behind.
        """
        members = [leg.analysis for leg in harness.legs] + harness.unsupported + harness.unresolved
        cohorts = {
            reference.identity_key[:2]
            for analysis in members
            for reference in self._references(analysis.trace)
            if (termination := self._resolved.get(analysis.identity, {}).get(reference.identity_key)) is not None
            and self._channel_parent(termination) == harness.parent.pk
        }
        for analysis in self.analyses:
            resolved = self._resolved.get(analysis.identity, {})
            keys = {reference.identity_key for reference in self._references(analysis.trace)}
            for key in sorted(key for key in keys if key[:2] in cohorts):
                termination = resolved.get(key)
                component = None if termination is None else self._objects.get(termination.key)
                if termination is None and analysis.identity not in {other.identity for other in harness.unresolved}:
                    harness.unresolved.append(analysis)
                elif component is not None and cable_harness.is_plain_interface(component):
                    harness.unchannelized.append((analysis, termination))

    @staticmethod
    def _harness_display(harness: _Harness, segment_index: int, *ports: _Termination, **fields) -> dict:
        """Return the display every finding about a harness carries: the leg, the parent, and the ports it names."""
        return {
            "segment_index": segment_index,
            "parent": harness.termination.display,
            **fields,
            **harness.wording,
            # The parent's Device authorizes its channel facts too: on a virtual chassis it can be hidden alone.
            **termination_sources(
                harness.termination.key, (DEVICE_ROW, harness.parent.component.device_id), *(port.key for port in ports)
            ),
        }

    def _plan_harnesses(self) -> None:
        """Decide each harness once, then give each of its leg units the same findings."""
        for parent_pk in sorted(self._harnesses):
            harness = self._harnesses[parent_pk]
            if not harness.legs:
                # Only blocked traces touch this parent, and each already carries its own finding.
                continue
            self._refuse_deleted_far_ends(harness)
            harness.legs.sort(
                key=lambda leg: (cable_harness.channel(self._objects[leg.child.key]), leg.analysis.identity)
            )
            findings = self._harness_findings(harness)
            missing = self._parent_permission_missing(harness)
            for leg in harness.legs:
                if missing is not None:
                    self._hide_leg(leg, harness, missing)
                    continue
                for kind, code, ports, fields in findings:
                    getattr(leg.analysis, kind)(
                        code,
                        self._harness_display(harness, leg.segment.index, *ports, **fields),
                        identities=(harness.termination.identity, *(port.identity for port in ports)),
                    )
                    if code == cable_harness.NOT_CONNECTABLE:
                        leg.analysis.reopen(ports[0].key)
                self._settle_leg(leg, harness)

    def _refuse_deleted_far_ends(self, harness: _Harness) -> None:
        """Refuse a harness whose far end the locked reread no longer found, as the segment write refuses one.

        A child row that is gone makes its trace no leg, so only a far end can be missing here.
        """
        for leg in harness.legs:
            if leg.far.key not in self._objects:
                label, object_id = leg.far.key
                raise PreconditionFailed(f"{label} {object_id} is gone, so the breakout Cable cannot be created.")

    def _harness_findings(self, harness: _Harness) -> list[tuple[str, str, tuple, dict]]:
        """Return each finding of one harness as (severity method, code, ports it names, display fields)."""
        parent = harness.parent
        on_parent = self._existing.get(parent.component.cable_id)
        faults = parent.faults("" if on_parent is None else on_parent.cable.profile)
        if faults:
            return [
                (
                    "refuse",
                    "cable.harness_channel_state_invalid",
                    (self._channel_termination(child.component),),
                    {"port": str(child.component), "channel": child.channel, "fault": fault},
                )
                for child, fault in faults
            ]
        findings: list[tuple[str, str, tuple, dict]] = [
            ("block", "cable.harness_unsupported", (), {"trace": analysis.display["name"]})
            for analysis in harness.unsupported
        ]
        findings.extend(
            ("block", "cable.harness_member_unresolved", (), {"trace": analysis.display["name"]})
            for analysis in harness.unresolved
        )
        try:
            harness.intent = cable_harness.harness_intent(
                parent.component, [(self._objects[leg.child.key], self._objects[leg.far.key]) for leg in harness.legs]
            )
        except cable_harness.IntentRefused as refusal:
            kind = "refuse" if refusal.code == "cable.harness_channel_state_invalid" else "block"
            ports = () if refusal.port is None else (self._channel_termination(refusal.port),)
            findings.append((kind, refusal.code, ports, dict(refusal.facts)))
        if self._harness_cable_types(harness):
            findings.append(("block", "cable.harness_type_conflict", (), {}))
        findings.extend(self._harness_occupancy(harness))
        findings.extend(
            (
                "warn",
                "cable.harness_member_unchannelized",
                (port,),
                {"trace": analysis.display["name"], "port": port.display},
            )
            for analysis, port in harness.unchannelized
        )
        if harness.intent is not None and harness.intent.trailing:
            findings.append(("warn", "cable.harness_partial", (), {"missing": list(harness.intent.trailing)}))
        return findings

    def _harness_cable_types(self, harness: _Harness) -> bool:
        """Settle the one Cable Type every leg's policy agrees on, and return whether the legs disagree.

        Only the Cable Type dimension applies to a leg: the channel topology decides the profile.
        """
        from .cable_policy import policy_choice_errors

        types = set()
        for leg in harness.legs:
            row = self._policy_row(leg.segment)
            display = {"segment_index": leg.segment.index, "cable_class": leg.segment.cable_class}
            if row is None or not row.cable_type_resolved:
                leg.analysis.block("cable.cableclass_unmapped", display)
            elif policy_choice_errors(row.cable_type, None):
                leg.analysis.block("cable.policy_stale", display)
            else:
                types.add(row.cable_type)
        if len(types) == 1:
            harness.cable_type = next(iter(types))
        return len(types) > 1

    def _harness_occupancy(self, harness: _Harness) -> list[tuple[str, str, tuple, dict]]:
        """Reuse the Cable on the parent when its termination snapshot is the harness, else block each held end."""
        on_parent = self._occupied.get(harness.termination.key)
        if (
            harness.intent is not None
            and on_parent is not None
            and harness.intent.proved_by(on_parent.cable.profile, on_parent.snapshot)
        ):
            harness.reused = on_parent
            return []
        findings = []
        for port in harness.ends:
            occupying = self._occupied.get(port.key)
            if occupying is not None:
                cable_display, _identities = self._cable_diagnostic_disclosure(occupying.cable)
                findings.append(("block", "cable.harness_occupied", (port,), {"port": port.display, **cable_display}))
        return findings

    def _parent_permission_missing(self, harness: _Harness) -> str | None:
        """Return the view permission the actor lacks on the parent Interface or on its Device (section 6.6)."""
        parent = harness.parent.component
        if not self.reader.terminations(cable_harness.INTERFACE).filter(pk=parent.pk).exists():
            return _view_permission(cable_harness.INTERFACE)
        # On a virtual chassis the parent can sit on another member Device than its children.
        if not self.reader.devices().filter(pk=parent.device_id).exists():
            return _view_permission("dcim.device")
        return None

    def _hide_leg(self, leg: _Leg, harness: _Harness, permission: str) -> None:
        """Block one leg whose parent, or its Device, the actor cannot view, and state no fact of that parent.

        As for a hidden Cable or mapped peer, only the evidence holds the parent's facts, so a change
        to them still moves the unit.
        """
        # Reuse is a fact of the parent, so a hidden leg names its companions whatever the parent holds.
        leg.analysis.selected_with = harness.companions(leg)
        leg.analysis.block(
            "cable.permission_denied",
            {"permission": permission},
            evidence=self._harness_evidence(harness),
        )

    def _leg_channels(self, harness: _Harness) -> list[int]:
        """Return the channel of each leg, in leg order."""
        return [cable_harness.channel(self._objects[leg.child.key]) for leg in harness.legs]

    def _harness_evidence(self, harness: _Harness) -> dict:
        """Return the harness facts every leg unit fingerprints (section 6.4)."""
        return {
            "parent": harness.termination.identity,
            **harness.parent.evidence(),
            # The child's Device is read from its latest row: in a virtual chassis a child can change member.
            "legs": [
                [number, _object_identity("dcim.device", self._objects[leg.child.key].device_id), leg.far.identity]
                for number, leg in zip(self._leg_channels(harness), harness.legs, strict=True)
            ],
            "profile": "" if harness.intent is None else harness.intent.profile,
            "reused": harness.reused is not None,
        }

    def _settle_leg(self, leg: _Leg, harness: _Harness) -> None:
        """Record what the harness means for one leg unit: reuse or companions, then its fingerprinted state."""
        analysis = leg.analysis
        if harness.reused is not None:
            analysis.proven[leg.segment.index] = harness.reused
            cable_display, cable_identities = self._cable_diagnostic_disclosure(harness.reused.cable)
            analysis.note(
                "cable.segment_reused",
                # The Cable proves this segment only as the harness, so the reuse is a fact of the harness.
                {"segment_index": leg.segment.index, **cable_display, **harness.wording},
                identities=(*cable_identities, harness.termination.identity, leg.far.identity),
            )
        else:
            analysis.selected_with = harness.companions(leg)
        evidence = self._harness_evidence(harness)
        analysis.note(
            "cable.harness_state",
            self._harness_display(
                harness,
                leg.segment.index,
                channels=harness.parent.channels,
                legs=self._leg_channels(harness),
                profile=evidence["profile"],
            ),
            identities=(harness.termination.identity, leg.child.identity, leg.far.identity),
            evidence=evidence,
        )

    @staticmethod
    def _analysis_terminations(analysis: _TraceAnalysis) -> list[_Termination]:
        """Return every resolved termination one trace's plan depends on."""
        terminations = list(analysis.endpoints)
        for segment in analysis.segments:
            terminations.extend(segment.terminations)
        return terminations

    def _may_view(self, row: Any) -> bool:
        """Return whether the actor may read one row, asking NetBox once per row in this batch."""
        if self.actor is None:
            return True
        key = (row._meta.label_lower, row.pk)
        if key not in self._visible:
            # NetBox answers each object-level check with its own query.
            self._visible[key] = self.actor.has_perm(f"{row._meta.app_label}.view_{row._meta.model_name}", row)
        return self._visible[key]

    def _cable_diagnostic_disclosure(self, cable: Any) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Return the diagnostic fields and identity the actor may see for one Cable."""
        cable_visible = self._may_view(cable)
        if not cable_visible:
            return {"cable_visible": False}, ()
        return (
            disclosed_cable(cable),
            (_object_identity("dcim.cable", cable.pk),),
        )

    def _classify(self) -> None:
        """Decide which segments already exist, then which Cable is each trace's Logical Cable.

        Every desired segment of the batch is classified before any Logical Cable is chosen, so a
        Cable that proves one trace's segment can never be deleted as another trace's leftover.
        """
        pending = self._writing()
        for analysis in pending:
            # Only these analyses look at the live topology, so only they may report what is there.
            analysis.topology_read = True
            for segment in analysis.pending:
                candidate = self._occupied.get(segment.left.key)
                proven = candidate if candidate is not None and candidate.satisfies(segment) else None
                if proven is not None:
                    analysis.proven[segment.index] = proven
                    self._note_reuse(analysis, segment, proven)
        proven_ids = {item.cable.pk for analysis in pending for item in analysis.proven.values()}
        for analysis in pending:
            analysis.logical_cable = self._logical_cable(analysis, proven_ids)
            if analysis.segments:
                self._report_conflicts(analysis)
            else:
                self._classify_endpoint_evidence(analysis)

    def _logical_cable(self, analysis: _TraceAnalysis, proven_ids) -> _ExistingCable | None:
        """Return the direct endpoint-to-endpoint Cable that proves no desired segment and is no breakout."""
        first, second = analysis.endpoints
        existing = self._occupied.get(first.key)
        if (
            existing is not None
            and existing.cable.pk not in proven_ids
            and existing.connects(first, second)
            and not cable_harness.is_breakout(existing.cable, (self._objects[first.key], self._objects[second.key]))
        ):
            return existing
        return None

    def _classify_endpoint_evidence(self, analysis: _TraceAnalysis) -> None:
        """Decide an Endpoint Summary fallback, which states endpoints and no physical path."""
        if analysis.logical_cable is None:
            analysis.block(
                "trace.endpoint_evidence_only",
                {"name": analysis.display["name"]},
                identities=tuple(endpoint.identity for endpoint in analysis.endpoints),
            )
            return
        # The stated endpoints are already joined, so the evidence is satisfied and nothing is removed.
        cable_display, cable_identities = self._cable_diagnostic_disclosure(analysis.logical_cable.cable)
        analysis.note(
            "cable.segment_reused",
            {"segment_index": 0, **cable_display},
            identities=cable_identities,
        )
        analysis.retains_logical_cable = True

    def _note_reuse(self, analysis: _TraceAnalysis, segment: _DesiredSegment, proven: _ExistingCable) -> None:
        """Record the proven physical segment, so its live state joins the unit fingerprint."""
        cable_display, cable_identities = self._cable_diagnostic_disclosure(proven.cable)
        analysis.note(
            "cable.segment_reused",
            {"segment_index": segment.index, **cable_display},
            identities=(*cable_identities, segment.left.identity, segment.right.identity),
        )
        drift = self._attribute_drift(segment, proven.cable)
        if drift:
            drift_display = drift if cable_display["cable_visible"] else {}
            analysis.note(
                "cable.attribute_drift",
                {"segment_index": segment.index, **cable_display, **drift_display},
                identities=cable_identities,
            )

    def _attribute_drift(self, segment: _DesiredSegment, cable) -> dict:
        """Return the reused Cable attributes that differ from what this import would have written."""
        row = self._policy_row(segment)
        drift = {}
        if cable.status != CABLE_STATUS:
            drift["status"] = cable.status
        if row is not None and row.cable_type_resolved and (cable.type or None) != row.cable_type:
            drift["type"] = cable.type or ""
        if row is not None and row.cable_profile_resolved and (cable.profile or None) != row.cable_profile:
            drift["profile"] = cable.profile or ""
        if cable.label:
            drift["label"] = cable.label
        return drift

    def _report_conflicts(self, analysis: _TraceAnalysis) -> None:
        """Block the trace when a Cable this import may not touch holds a termination it needs."""
        removed = analysis.deleted_logical_cable
        logical_id = None if removed is None else removed.cable.pk
        for segment in analysis.pending:
            for termination in segment.terminations:
                occupying = self._occupied.get(termination.key)
                if occupying is None or occupying.cable.pk == logical_id:
                    continue
                code = (
                    "cable.multi_termination_conflict" if occupying.multi_termination else "cable.termination_occupied"
                )
                cable_display, cable_identities = self._cable_diagnostic_disclosure(occupying.cable)
                display = {
                    "segment_index": segment.index,
                    "port": termination.display,
                    **termination_sources(termination.key),
                    **cable_display,
                }
                identities = [termination.identity, *cable_identities]
                analysis.block(code, display, identities=identities)

    def _cable_class_mapping(self, cable_class: str):
        """Return the Cable policy row one CableClass value carries, or None."""
        if self._mapping_rows is None:
            from .models import CableClassMapping

            rows = CableClassMapping.objects.filter(profile=self.profile)
            self._mapping_rows = {row.cable_class: row for row in rows}
        return self._mapping_rows.get(cable_class)

    def _policy_row(self, segment: _DesiredSegment):
        """Return the stored decision that governs one segment, or None while nothing decides it."""
        from .cable_policy import policy_in_force

        return policy_in_force(self._overrides.get(segment.key), self._cable_class_mapping(segment.cable_class))

    def _load_overrides(self) -> None:
        """Read every override this batch can apply, and every one its own traces may have lost."""
        from django.db.models import Q

        from .models import CableSegmentOverride

        keys = {segment.key for analysis in self.analyses for segment in analysis.segments}
        identities = {analysis.trace.identity for analysis in self.analyses}
        rows = CableSegmentOverride.objects.filter(profile=self.profile).filter(
            Q(segment_key__in=keys) | Q(source_trace_identity__in=identities)
        )
        self._overrides = {row.segment_key: row for row in rows}
        # Uniqueness is by pair, so one trace and one position can own several overrides at once.
        self._overrides_by_trace = {}
        for row in rows:
            self._overrides_by_trace.setdefault(row.source_trace_identity, []).append(row)

    def _report_lost_overrides(self, analysis: _TraceAnalysis) -> None:
        """Report each override of this trace that no longer governs any segment it states.

        The comparison is by pair, not by position: a trace identity names its endpoints, so a
        revised path renumbers every segment after the one it inserts without losing anything.
        """
        stated = {segment.key for segment in analysis.segments}
        for stored in self._overrides_by_trace.get(analysis.trace.identity, ()):
            if stored.segment_key in stated:
                continue
            analysis.note(
                "cable.segment_override_lost",
                disclosed_policy(
                    stored,
                    self._may_view(stored),
                    {
                        "segment_index": stored.segment_index,
                        "cable_type": stored.cable_type_display(),
                        "cable_profile": stored.cable_profile_display(),
                    },
                ),
            )

    def _decide(self) -> None:
        """Settle the Cable policy and the write permissions every actionable trace needs."""
        for analysis in self.analyses:
            # A refused Segment Evidence block also leaves no segments, and states no path at all.
            if (analysis.trace.valid and not analysis.trace.segments) or analysis.segment_pairs_resolved:
                self._report_lost_overrides(analysis)
            if analysis.stopped or not analysis.endpoints:
                continue
            for segment in analysis.pending:
                policy = self._cable_policy(analysis, segment)
                if policy is not None:
                    analysis.policies[segment.index] = policy
                    self._check_write(analysis, segment, policy)
            self._check_permissions(analysis)

    def _check_write(self, analysis: _TraceAnalysis, segment: _DesiredSegment, policy: dict) -> None:
        """Build one segment's Cable write through the one constructor, and block the trace when it refuses."""
        ends = [self._objects.get(end.key) for end in segment.terminations]
        # A row gone since resolution is refused by the write itself.
        if None in ends:
            return
        try:
            cable_harness.ordinary_intent(*ends, profile=policy["cable_profile"] or "")
        except cable_harness.IntentRefused as refusal:
            (port,) = [end for end in segment.terminations if self._objects[end.key] is refusal.port]
            analysis.block(
                refusal.code,
                {
                    "segment_index": segment.index,
                    **refusal.facts,
                    **termination_sources(port.key),
                    # An ordinary segment's refusal describes the refused port alone, so only that port authorizes it.
                    **(wording_sources([(port.key, port.device_id)]) if refusal.code in WORDING_CODES else {}),
                },
                identities=(port.identity,),
            )
            if refusal.code in {cable_harness.NOT_CONNECTABLE, cable_harness.CHANNELIZED}:
                analysis.reopen(port.key)

    def _cable_policy(self, analysis: _TraceAnalysis, segment: _DesiredSegment) -> dict | None:
        """Return the Cable Type and Cable Profile one new segment is written with."""
        from .cable_policy import policy_choice_errors

        display = {"segment_index": segment.index, "cable_class": segment.cable_class}
        row = self._policy_row(segment)
        policy = None if row is None else row.decided_policy()
        if policy is None:
            analysis.block("cable.cableclass_unmapped", display)
            return None
        errors = policy_choice_errors(policy["cable_type"], policy["cable_profile"])
        for error in errors.values():
            analysis.block(error.code, display)
        if errors:
            return None
        return policy

    def _assess_media(self) -> None:
        """Report each run of verified pass-throughs whose segments state two known media."""
        for analysis in self.analyses:
            if not analysis.topology_read or not analysis.segment_pairs_resolved or not analysis.endpoints:
                continue
            observations = [self._media_observation(analysis, segment) for segment in analysis.segments]
            for span in self._media_spans(analysis, observations):
                self._report_media_mismatch(analysis, span)

    def _media_observation(self, analysis: _TraceAnalysis, segment: _DesiredSegment) -> dict:
        """Return what one segment says about the medium of the run it belongs to."""
        from .cable_policy import cable_profile_splits_a_span, decisive_media_family, policy_choice_errors

        proven = analysis.proven.get(segment.index)
        visible = True
        source = None
        if proven is not None:
            cable_type, cable_profile = proven.cable.type or "", proven.cable.profile or ""
            disclosure = self._cable_diagnostic_disclosure(proven.cable)[0]
            visible = disclosure["cable_visible"]
            source = disclosure.get("disclosure_source")
            origin = "cable"
        else:
            row = self._policy_row(segment)
            policy = analysis.policies.get(segment.index)
            if policy is None and row is not None:
                decided = row.decided_policy()
                if decided is not None and not policy_choice_errors(decided["cable_type"], decided["cable_profile"]):
                    policy = decided
            policy = policy or {}
            cable_type, cable_profile = policy.get("cable_type") or "", policy.get("cable_profile") or ""
            disclosure = disclosed_policy(row, self._may_view(row), {}) if row is not None else {}
            visible = DISCLOSURE_SOURCE in disclosure
            source = disclosure.get(DISCLOSURE_SOURCE)
            origin = "policy"
        return {
            "segment_index": segment.index,
            "cable_type": cable_type,
            "family": decisive_media_family(cable_type),
            "retained": proven is not None,
            "visible": visible,
            # A breakout Cable fans out, so a leg ends the run whatever its policy states.
            "splits": analysis.is_leg(segment) or cable_profile_splits_a_span(cable_profile),
            "disclosure_source": source,
            "origin": origin,
        }

    @staticmethod
    def _media_spans(analysis: _TraceAnalysis, observations: list) -> list:
        """Return each run of segments a verified mapping joins, cut where a Cable fans out.

        A shared Device name proves nothing: only a PortMapping the plan verified joins two segments.
        """
        spans: list[list[dict]] = [[]]
        for observation in observations:
            if observation["splits"]:
                spans.append([])
                continue
            if not (spans[-1] and (observation["segment_index"] - 1) in analysis.joined):
                spans.append([])
            spans[-1].append(observation)
        return [span for span in spans if len(span) > 1]

    def _report_media_mismatch(self, analysis: _TraceAnalysis, span: list) -> None:
        """Warn where one verified passive run carries two media families, and say who can fix it."""
        decided = [item for item in span if item["family"]]
        if len({item["family"] for item in decided}) < 2:
            return
        resolved = {segment.index: segment for segment in analysis.segments}
        # A hidden Cable still decides the run, and still states nothing about itself.
        analysis.warn(
            "cable.media_family_mismatch",
            {"segments": [self._media_display(item, include_source=True) for item in decided]},
            identities=tuple(
                identity
                for item in decided
                for identity in (
                    resolved[item["segment_index"]].left.identity,
                    resolved[item["segment_index"]].right.identity,
                )
            ),
            evidence={"segments": [self._media_display(item, for_evidence=True) for item in decided]},
        )

    @staticmethod
    def _media_display(observation: dict, *, include_source: bool = False, for_evidence: bool = False) -> dict:
        """Return stable media facts, with row identity only in presentation data."""
        visible = observation["visible"] or (for_evidence and observation["origin"] == "policy")
        record = {
            "segment_index": observation["segment_index"],
            "retained": observation["retained"],
            "visible": visible,
            "origin": observation["origin"],
        }
        if not visible:
            return record
        record.update(cable_type=observation["cable_type"], family=observation["family"])
        if include_source and observation["disclosure_source"] is not None:
            record["disclosure_source"] = observation["disclosure_source"]
        return record

    def _check_permissions(self, analysis: _TraceAnalysis) -> None:
        """Block the trace when the actor may not make every Cable write it asks for."""
        if self.actor is None:
            return
        if (analysis.pending or analysis.harness_pending) and not self.actor.has_perm("dcim.add_cable"):
            analysis.block("cable.permission_denied", {"permission": "dcim.add_cable"})
            return
        logical = analysis.deleted_logical_cable
        if logical is not None and not self.actor.has_perm("dcim.delete_cable", logical.cable):
            cable_display, cable_identities = self._cable_diagnostic_disclosure(logical.cable)
            analysis.block(
                "cable.permission_denied",
                {"permission": "dcim.delete_cable", **cable_display},
                identities=cable_identities,
            )

    def _block_planned_termination_conflicts(self) -> None:
        """Block every actionable trace whose planned Cables compete for one free termination.

        A claim is keyed by the Cable it creates, so traces that share one segment or one harness agree.
        Two different Cables also conflict inside one trace, such as a harness and a segment back to its far end.
        """
        claims: dict[tuple[str, int], list[tuple[_TraceAnalysis, str, int, _Termination, _Harness | None]]] = {}
        for analysis in self._writing():
            for segment in analysis.pending:
                for termination in segment.terminations:
                    claims.setdefault(termination.key, []).append(
                        (analysis, segment.key, segment.index, termination, None)
                    )
            if analysis.harness_pending:
                harness, index = analysis.harness, analysis.leg.segment.index
                for termination in harness.ends:
                    claims.setdefault(termination.key, []).append(
                        (analysis, harness.change_identity, index, termination, harness)
                    )
        for records in claims.values():
            for analysis, claim, index, termination, harness in records:
                for other, other_claim, other_index, _other_termination, other_harness in records:
                    if other_claim == claim:
                        continue
                    competitor = (
                        {"competing_segment": other_index}
                        if other is analysis
                        else {"competing_trace": other.display["name"]}
                    )
                    # A claim that only a harness makes is a fact of that harness, so the conflict hides with it.
                    harnesses = [item for item in (harness, other_harness) if item is not None]
                    analysis.block(
                        "cable.planned_termination_conflict",
                        {
                            "segment_index": index,
                            "termination": termination.display,
                            **competitor,
                            **termination_sources(termination.key),
                            **(
                                wording_sources([end for item in harnesses for end in item.wording_ends])
                                if harnesses
                                else {}
                            ),
                        },
                        identities=(termination.identity,),
                    )

    def _block_conflicting_creations(self) -> None:
        """Block traces that resolve one shared segment to different Cable policies."""
        contributors: dict[str, list] = {}
        for analysis in self.analyses:
            if analysis.invalid or not analysis.endpoints:
                continue
            for segment in analysis.pending:
                policy = analysis.policies.get(segment.index)
                if policy is not None:
                    contributors.setdefault(segment.key, []).append((analysis, segment, policy))
        for records in contributors.values():
            # One shared Planned Change carries one payload, so only the effective policy may differ.
            policies = {(policy["cable_type"], policy["cable_profile"]) for _analysis, _segment, policy in records}
            if len(policies) < 2:
                continue
            for analysis, segment, policy in records:
                row = self._policy_row(segment)
                analysis.block(
                    "cable.resolved_segment_conflict",
                    disclosed_policy(
                        row,
                        self._may_view(row),
                        {
                            "segment_index": segment.index,
                            "cable_type": policy["cable_type"],
                            "cable_profile": policy["cable_profile"],
                            "terminations": segment.as_json(),
                            **termination_sources(segment.left.key, segment.right.key),
                        },
                    ),
                    identities=(segment.left.identity, segment.right.identity),
                )

    def _shared_deletes(self) -> dict[str, tuple[str, ...]]:
        """Return, per desired segment, every Logical Cable deletion its creation waits for.

        Two traces that state one identical segment share one Planned Change, so the change names
        the deletions of both and reads the same in either unit.
        """
        deletes: dict[str, set] = {}
        for analysis in self._writing():
            if analysis.delete_identity is not None:
                for segment in analysis.pending:
                    deletes.setdefault(segment.key, set()).add(analysis.delete_identity)
        return {key: tuple(sorted(values)) for key, values in deletes.items()}

    def _shared_sources(self) -> dict[str, list]:
        """Return, per desired segment, the provenance of every Source Trace that states it."""
        sources: dict[str, list] = {}
        for analysis in self.analyses:
            # Unlike _shared_deletes: a repaired trace reuses the Cable, so creation records it or nothing does.
            if analysis.invalid:
                continue
            for segment in analysis.pending:
                sources.setdefault(segment.key, []).append(_source_record(analysis.trace, segment.index))
        return {
            key: sorted(records, key=lambda record: (record["trace_identity"], record["segment_index"]))
            for key, records in sources.items()
        }

    def _writing(self) -> list[_TraceAnalysis]:
        """Return the analyses that still contribute writes to this plan."""
        return [analysis for analysis in self.analyses if not analysis.stopped and analysis.endpoints]

    def _unit(self, analysis: _TraceAnalysis) -> SynchronizationUnit:
        """Return the one Synchronization Unit one Source Trace produces."""
        changes = self._changes(analysis)
        if analysis.invalid:
            disposition = Disposition.INVALID
        elif analysis.blocked:
            disposition = Disposition.BLOCKED
        elif changes:
            disposition = Disposition.ACTIONABLE
        else:
            disposition = Disposition.NO_OP
        return SynchronizationUnit(
            identity=analysis.identity,
            disposition=disposition,
            changes=changes,
            diagnostics=tuple(analysis.diagnostics),
            display={
                **analysis.display,
                "detail": self._detail(analysis, disposition),
                "trace": self._workspace(analysis, bool(changes)),
            },
            selected_with=analysis.selected_with,
        )

    def _workspace(self, analysis: _TraceAnalysis, writes: bool) -> dict:
        """Return what the three review workspace panels show for one Source Trace."""
        summary = analysis.trace.endpoint_summary
        resolved = {segment.index: segment for segment in analysis.segments}
        by_reference = self._resolved.get(analysis.identity, {})
        segments = []
        stated_segments = list(analysis.trace.segments)
        for index, stated in enumerate(stated_segments):
            planned = resolved.get(index)
            source_left, source_right = _endpoint_label(stated.left), _endpoint_label(stated.right)
            left = source_left if planned is None else planned.left.topology_display
            right = source_right if planned is None else planned.right.topology_display
            segments.append(
                {
                    "index": index,
                    "cable_class": source_text(stated.cable_class),
                    "source_left": source_left,
                    "source_right": source_right,
                    "left": left,
                    "right": right,
                    # A path that re-enters one panel implies a claim the source never states.
                    "pass_through": index > 0 and same_device_and_cards(stated_segments[index - 1].right, stated.left),
                    # Planning substituted the entry port only where the source re-used one port.
                    "substituted": self._entered_through_claim(planned, by_reference.get(stated.left.identity_key)),
                    # Presentation adds "conflict" from the findings the viewer may read.
                    "status": self._segment_status(analysis, index, planned, writes),
                    # An override cannot change a Cable this plan keeps, so the panel says which is which.
                    "retained": index in analysis.proven,
                    # The end that holds the channel child, so presentation hides the mark with that end.
                    "breakout_leg": self._leg_end(analysis, planned),
                    **self._policy_display(planned),
                    **self._end_sources(planned),
                }
            )
        cable_classes = list(dict.fromkeys(source_text(segment.cable_class) for segment in stated_segments))
        return {
            "identity": analysis.trace.identity,
            "endpoints": {
                "from": _endpoint_label(summary.from_termination),
                "to": _endpoint_label(summary.to_termination),
            },
            "segments": segments,
            "cable_policies": [self._cable_class_display(cable_class) for cable_class in cable_classes],
            "logical_cable": self._logical_cable_display(analysis),
            "resolution_started": analysis.resolution_started,
            "topology_known": analysis.topology_read,
            # A unit with no changes proposes nothing, so the panel must not offer to delete one.
            "deletes_logical_cable": writes and analysis.deleted_logical_cable is not None,
            "devices": list(analysis.devices.values()),
            "terminations": list(analysis.terminations.values()),
        }

    def _cable_class_display(self, cable_class: str) -> dict:
        """Return one CableClass decision for the workspace policy table."""
        row = self._cable_class_mapping(cable_class)
        display = {
            "cable_class": cable_class,
            "cable_type": "Unresolved" if row is None else row.cable_type_display(),
            "cable_profile": "Unresolved" if row is None else row.cable_profile_display(),
            "policy": (None if row is None else row.decided_policy()) or {},
        }
        return display if row is None else disclosed_policy(row, self._may_view(row), display)

    @staticmethod
    def _leg_end(analysis: _TraceAnalysis, planned: _DesiredSegment | None) -> str:
        """Return the segment end that holds the harness leg's channel child, or nothing for an ordinary segment."""
        if planned is None or not analysis.is_leg(planned):
            return ""
        return "left" if planned.left.key == analysis.leg.child.key else "right"

    def _policy_display(self, planned: _DesiredSegment | None) -> dict:
        """Return the Cable policy in force for one resolved segment, and whether it is an override."""
        if planned is None:
            return {"segment_key": "", "overridden": False, "cable_type": "", "cable_profile": "", "policy": {}}
        row = self._policy_row(planned)
        display = {
            "segment_key": planned.key,
            "overridden": planned.key in self._overrides,
            "cable_type": "Unresolved" if row is None else row.cable_type_display(),
            "cable_profile": "Unresolved" if row is None else row.cable_profile_display(),
            # The stored values, not their labels, so the workspace can offer what is in force.
            "policy": (None if row is None else row.decided_policy()) or {},
        }
        return display if row is None else disclosed_policy(row, self._may_view(row), display)

    @staticmethod
    def _end_sources(planned: _DesiredSegment | None) -> dict:
        """Return the rows that authorize a planned segment's two NetBox Device and port names."""
        if planned is None:
            return {}
        return {
            source_key: segment_end_sources(end.key, end.device_id)
            for source_key, end in zip(SEGMENT_END_SOURCES.values(), planned.terminations, strict=True)
        }

    @staticmethod
    def _entered_through_claim(planned: _DesiredSegment | None, stated: _Termination | None) -> bool:
        """Return whether planning entered this segment through a port the source never stated."""
        return planned is not None and stated is not None and planned.left.key != stated.key

    @staticmethod
    def _segment_status(analysis: _TraceAnalysis, index: int, planned, writes: bool) -> str:
        """Return the verdict the proposed panel shows for one segment, or none for an unplanned one."""
        if planned is None:
            return ""
        if index in analysis.proven:
            return REUSE_SEGMENT
        # A blocked unit contributes no change, so nothing here is going to be created.
        return CREATE_SEGMENT if writes else ""

    def _logical_cable_display(self, analysis: _TraceAnalysis) -> dict | None:
        """Return the Logical Cable NetBox holds for this trace, as far as the actor may see it."""
        if analysis.logical_cable is None:
            return None
        cable = analysis.logical_cable.cable
        disclosure, _identities = self._cable_diagnostic_disclosure(cable)
        if not disclosure["cable_visible"]:
            return {"visible": False, "display": "", "description": "", "tags": []}
        review = self._cable_review_snapshot(cable)
        return {
            "visible": True,
            "display": review["display"],
            "description": review["description"],
            "tags": review["tags"],
            "disclosure_source": disclosure["disclosure_source"],
        }

    def _cable_review_snapshot(self, cable) -> dict:
        """Reuse one Cable review value for all changes and displays in this plan."""
        if cable.pk not in self._cable_review_snapshots:
            self._cable_review_snapshots[cable.pk] = _cable_review_snapshot(cable)
        return self._cable_review_snapshots[cable.pk]

    def _changes(self, analysis: _TraceAnalysis) -> tuple[PlannedChange, ...]:
        """Return the deletion and the creations one actionable trace performs, in that order."""
        if analysis.stopped:
            return ()
        changes = []
        if analysis.deleted_logical_cable is not None:
            changes.append(self._delete_change(analysis.deleted_logical_cable))
        for segment in analysis.segments:
            if analysis.is_leg(segment):
                if analysis.harness_pending:
                    changes.append(self._harness_change(analysis.harness))
            elif segment.index not in analysis.proven:
                changes.append(self._create_change(segment, analysis.policies[segment.index]))
        return tuple(changes)

    def _delete_change(self, logical: _ExistingCable) -> PlannedChange:
        """Return the one deletion a Patched Path Replacement ever performs."""
        return PlannedChange(
            identity=_delete_identity(logical.cable.pk),
            target_module=CableModule.key,
            operation="delete",
            payload={"cable_id": logical.cable.pk},
            preconditions={
                "cable_id": logical.cable.pk,
                "terminations": logical.terminations,
                "review_fingerprint": fingerprint_of(self._cable_review_snapshot(logical.cable)),
            },
        )

    def _create_change(self, segment: _DesiredSegment, policy: dict) -> PlannedChange:
        """Return the creation of one physical segment, shared by every trace that states it."""
        return PlannedChange(
            identity=segment.change_identity,
            target_module=CableModule.key,
            operation="create",
            payload={
                "terminations": segment.as_json(),
                "status": CABLE_STATUS,
                "sources": self._sources_by_segment.get(segment.key, []),
                **policy,
            },
            dependencies=self._deletes_by_segment.get(segment.key, ()),
            preconditions={"terminations": segment.as_json()},
        )

    def _harness_change(self, harness: _Harness) -> PlannedChange:
        """Return the one creation every leg unit of a harness carries, identical in each unit."""
        if harness.intent is None:
            raise RuntimeError("A harness that cannot be written has no Planned Change.")
        sources = sorted(
            (_source_record(leg.analysis.trace, leg.segment.index) for leg in harness.legs),
            key=lambda record: (record["trace_identity"], record["segment_index"]),
        )
        return PlannedChange(
            identity=harness.change_identity,
            target_module=CableModule.key,
            operation="create",
            payload={
                "harness": cable_harness.harness_payload(harness.intent),
                "cable_type": harness.cable_type,
                "status": CABLE_STATUS,
                "sources": sources,
            },
            preconditions={"terminations": sorted(end.as_json() for end in harness.ends)},
        )

    @staticmethod
    def _detail(analysis: _TraceAnalysis, disposition: str) -> str:
        """Return the operator wording one unit shows before the trace workspace exists."""
        if disposition == Disposition.NO_OP:
            return "The stated path already exists."
        if disposition != Disposition.ACTIONABLE:
            return ""
        count = len(analysis.pending) + analysis.harness_pending
        if analysis.deleted_logical_cable is not None:
            return f"Would replace the logical cable with {count} physical segment(s)."
        return f"Would create {count} physical segment(s)."


class CableModule:
    """Plan and write the Cables one Source Trace states, as a Patched Path Replacement."""

    key = TargetModuleKey.CABLE
    consumes = frozenset({OutputKind.SOURCE_TRACE})

    def plan(
        self,
        source_batch,
        profile,
        catalog,
        netbox_reader,
        *,
        lock_plan_references: bool = False,
    ) -> list[SynchronizationUnit]:
        """Return one unit per trace and optionally lock rows that prove the plan."""
        del catalog
        if not (self.consumes & source_batch.output_kinds):
            return []
        return _CableBatch(
            source_batch.rows,
            profile,
            netbox_reader,
            lock_plan_references=lock_plan_references,
        ).units()

    def apply(self, planned_change: PlannedChange, execution_context) -> Any:
        """Apply one Cable change, having locked its rows and rechecked its preconditions."""
        if planned_change.operation == "delete":
            return self._delete(planned_change, execution_context)
        if planned_change.operation == "create":
            return self._create(planned_change, execution_context)
        raise PreconditionFailed(f"The Cable Target Module cannot apply operation '{planned_change.operation}'.")

    @staticmethod
    def _delete(planned_change: PlannedChange, execution_context) -> DeletedObject:
        """Remove the one direct Logical Cable this unit replaces."""
        from dcim.models import Cable

        cable_id = planned_change.preconditions["cable_id"]
        cable = Cable.objects.filter(pk=cable_id).select_for_update(of=("self",)).first()
        if cable is None:
            raise PreconditionFailed(f"Cable {cable_id} is gone, so the logical cable cannot be replaced.")
        current = _cable_terminations(cable_id)
        if current != [list(item) for item in planned_change.preconditions["terminations"]]:
            raise PreconditionFailed(f"Cable {cable_id} was re-terminated after the plan was made.")
        ends = [_model_for_label(label).objects.get(pk=object_id) for label, object_id in current]
        if cable_harness.is_breakout(cable, ends):
            raise PreconditionFailed(f"Cable {cable_id} is a breakout Cable, and the import never deletes it.")
        if _deleted_cable_review_fingerprint(cable) != planned_change.preconditions["review_fingerprint"]:
            raise PreconditionFailed(f"Cable {cable_id} changed after the plan was made.")
        enforce_saved_object_permission(cable, execution_context.actor, "delete")
        snapshot = DeletedObject(
            object_type="dcim.cable",
            object_id=cable_id,
        )
        cable.delete()
        return snapshot

    @staticmethod
    def _create(planned_change: PlannedChange, execution_context) -> Any:
        """Create one physical Cable segment or one harness, and the provenance rows its Source Traces earn.

        The write is built from locked live rows through the same constructor planning used.
        """
        from dcim.models import Cable

        payload = planned_change.payload
        if "harness" in payload:
            intent = cable_harness.locked_harness_intent(payload["harness"], CableModule._free_termination)
        else:
            first, second = (CableModule._free_termination(*end) for end in payload["terminations"])
            intent = cable_harness.ordinary_intent(first, second, profile=payload["cable_profile"] or "")
        cable = Cable(
            type=payload["cable_type"],
            profile=intent.profile,
            status=payload["status"],
            a_terminations=list(intent.a_terminations),
            b_terminations=list(intent.b_terminations),
        )
        cable.full_clean()
        cable.save()
        enforce_saved_object_permission(cable, execution_context.actor, "add")
        CableModule._store_provenance(cable, payload, execution_context.profile)
        return cable

    @staticmethod
    def _free_termination(label: str, object_id: int, device_id: int):
        """Return one locked termination that is still free and on its resolved Device."""
        component = _model_for_label(label).objects.filter(pk=object_id).select_for_update(of=("self",)).first()
        if component is None:
            raise PreconditionFailed(f"{label} {object_id} is gone, so the segment cannot be created.")
        if component.device_id != device_id:
            raise PreconditionFailed(f"{label} {object_id} moved to another Device after the plan was made.")
        if component.cable_id is not None:
            raise PreconditionFailed(f"{component} received a cable after the plan was made.")
        return component

    @staticmethod
    def _store_provenance(cable, payload, profile) -> None:
        """Write one provenance row per Source Trace that states this segment."""
        from .models import CableImportSource, index_digest

        for record in payload["sources"]:
            # The unique constraint carries the digest, so the lookup matches it.
            CableImportSource.objects.update_or_create(
                cable=cable,
                profile=profile,
                trace_key=index_digest(record["trace_identity"]),
                defaults={
                    "trace_identity": record["trace_identity"],
                    "segment_index": record["segment_index"],
                    "from_text": record["from_text"],
                    "to_text": record["to_text"],
                    "direction": record["direction"],
                    "workbook_fingerprint": record["workbook_fingerprint"],
                    "sheet": record["sheet"],
                    "block_ordinal": record["block_ordinal"],
                    "row_start": record["row_start"],
                    "row_end": record["row_end"],
                    "export_timestamp": record["export_timestamp"],
                },
            )


def _cable_terminations(cable_id: int) -> list:
    """Return one Cable's termination pairs in the order a precondition records them."""
    from dcim.models import CableTermination

    return sorted(
        [row.termination_type.model_class()._meta.label_lower, row.termination_id]
        for row in CableTermination.objects.filter(cable_id=cable_id).order_by("pk").select_for_update(of=("self",))
    )


__all__ = (
    "AUTOMATICALLY_RESOLVED",
    "CABLE_STATUS",
    "CREATE_SEGMENT",
    "ELIGIBLE_TERMINATION_LIMIT",
    "MANUALLY_RESOLVED",
    "REUSE_SEGMENT",
    "UNRESOLVED",
    "CableModule",
    "EligibleTerminations",
    "eligible_terminations",
)

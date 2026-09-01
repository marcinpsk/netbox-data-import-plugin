# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Cable Target Module: Patched Path Replacement planning and writes.

Section 6 gives one complete Source Trace one Synchronization Unit: it deletes the single direct
Logical Cable when one exists, then creates every physical segment the trace states. It reads the
decisions and the Cable policy the Import Profile holds, and it writes Cables and provenance rows.

It consumes Source Traces by output kind, so it imports no Source Adapter. It reads PortMapping
rows and never writes one, and it never creates or mutates a CablePath.

Live target state that no Planned Change carries, a reused Cable and a PortMapping row the path
relies on, joins the unit through the identities of an `info` diagnostic. Execution replans inside
its transaction, so a change to any of it makes the accepted unit stale and rolls the write back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .catalog import OutputKind, TargetModuleKey
from .field_keys import (
    FRONT_PORT_KIND,
    INTERFACE_KIND,
    MAPPED_PEER_ROLE,
    REAR_PORT_KIND,
    SELECT_TERMINATION_TASK,
    TERMINATION_ROLE,
    claimed_termination_kind,
    same_device_and_cards,
    termination_field_key,
)
from .object_permissions import enforce_saved_object_permission
from .plan import Diagnostic, Disposition, PlannedChange, Severity, SynchronizationUnit
from .target_runtime import DeletedObject, PreconditionFailed
from .values import identity_text, source_text

CABLE_STATUS = "connected"

_KIND_BY_MODEL_NAME = {
    "interface": INTERFACE_KIND,
    "frontport": FRONT_PORT_KIND,
    "rearport": REAR_PORT_KIND,
}
_SUPPORTED_TERMINATION_LABELS = frozenset(f"dcim.{name}" for name in _KIND_BY_MODEL_NAME)
_READER_ACCESSOR_BY_KIND = {
    INTERFACE_KIND: "interfaces",
    FRONT_PORT_KIND: "front_ports",
    REAR_PORT_KIND: "rear_ports",
}


def _object_type_label(obj) -> str:
    """Return the ``app_label.model_name`` key one target object is recorded under."""
    return f"{obj._meta.app_label}.{obj._meta.model_name}"


def _model_for_label(label: str):
    """Return the model class one recorded object-type label names."""
    from django.apps import apps

    app_label, _, model_name = label.partition(".")
    return apps.get_model(app_label, model_name)


def _object_identity(label: str, object_id: int) -> str:
    """Return the plan identity string for one target object."""
    return f"{label}:{object_id}"


@dataclass(frozen=True)
class _Termination:
    """One source Termination Reference bound to a NetBox object."""

    object_type: str
    object_id: int
    kind: str
    device_id: int
    display: str

    @property
    def key(self) -> tuple[str, int]:
        """Return the comparison key that names this object across queries."""
        return self.object_type, self.object_id

    @property
    def identity(self) -> str:
        """Return the plan identity string for this object."""
        return _object_identity(self.object_type, self.object_id)

    def as_json(self) -> list:
        """Return the serializable pair a payload and a precondition carry."""
        return [self.object_type, self.object_id]


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
        """Return the sorted termination pair a payload and a precondition carry."""
        return sorted((self.left.as_json(), self.right.as_json()))


@dataclass(frozen=True)
class _ExistingCable:
    """One existing Cable and the termination sets it holds."""

    cable: Any
    a_side: frozenset
    b_side: frozenset

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


@dataclass
class _TraceAnalysis:
    """What one Source Trace contributes, built in planning order."""

    trace: Any
    identity: str
    display: dict
    diagnostics: list = field(default_factory=list)
    endpoints: tuple = ()
    segments: list = field(default_factory=list)
    proven: dict = field(default_factory=dict)
    policies: dict = field(default_factory=dict)
    logical_cable: Any = None
    blocked: bool = False
    invalid: bool = False

    @property
    def stopped(self) -> bool:
        """Return whether a finding makes further planning for this trace meaningless."""
        return self.blocked or self.invalid

    @property
    def pending(self) -> list:
        """Return the desired segments no existing Cable already proves."""
        return [segment for segment in self.segments if segment.index not in self.proven]

    @property
    def delete_identity(self) -> str | None:
        """Return the Planned Change identity that removes this trace's Logical Cable."""
        return None if self.logical_cable is None else f"cable:delete:{self.logical_cable.cable.pk}"

    def error(self, code: str, display: dict, identities=()) -> None:
        """Record one blocking or invalidating finding."""
        self.diagnostics.append(
            Diagnostic(code=code, severity=Severity.ERROR, identities=tuple(identities), display=display)
        )

    def note(self, code: str, display: dict, identities=()) -> None:
        """Record one review note whose identities keep the unit honest about live state."""
        self.diagnostics.append(
            Diagnostic(code=code, severity=Severity.INFO, identities=tuple(identities), display=display)
        )

    def block(self, code: str, display: dict, identities=()) -> None:
        """Record a finding an operator decision or a NetBox correction can resolve."""
        self.error(code, display, identities)
        self.blocked = True

    def refuse(self, code: str, display: dict, identities=()) -> None:
        """Record a finding no decision inside this plugin can resolve."""
        self.error(code, display, identities)
        self.invalid = True


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


def _visible_devices(netbox_reader, names) -> dict[str, list]:
    """Return the visible Devices at the import target, grouped by comparison name."""
    from django.db.models import Q

    wanted = {identity_text(name) for name in names if identity_text(name)}
    if not wanted:
        return {}
    devices = netbox_reader.devices()
    if netbox_reader.site is not None:
        devices = devices.filter(site=netbox_reader.site)
    lookup = Q()
    for name in sorted(wanted):
        lookup |= Q(name__iexact=name)
    grouped: dict[str, list] = {}
    for device in devices.filter(lookup):
        comparison = identity_text(device.name)
        if comparison in wanted:
            grouped.setdefault(comparison, []).append(device)
    return grouped


def eligible_terminations(field_key: str, netbox_reader) -> list:
    """Return the terminations one open termination field key may be resolved to.

    Section 6.1 restricts a candidate to the claimed kind on the resolved Device, and the reader
    keeps the list inside the actor's view scope. The picker and a proposal request share it.
    """
    try:
        parsed = json.loads(field_key)
        device_name, kind = parsed["device"], parsed["kind"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"'{field_key}' is not a canonical termination field key.") from exc
    accessor = _READER_ACCESSOR_BY_KIND.get(kind)
    if accessor is None:
        raise ValueError(f"Unknown claimed termination kind '{kind}'.")
    devices = _visible_devices(netbox_reader, [device_name]).get(identity_text(device_name), [])
    if len(devices) != 1:
        return []
    return list(getattr(netbox_reader, accessor)().filter(device_id=devices[0].pk).order_by("name", "pk"))


class _CableBatch:
    """Plan every Source Trace in one batch, so an identical shared segment plans once."""

    def __init__(self, traces, profile, netbox_reader):
        self.profile = profile
        self.reader = netbox_reader
        self.actor = netbox_reader.actor
        self.analyses = [self._new_analysis(trace) for trace in traces]
        self._objects: dict[tuple[str, int], Any] = {}
        self._components: dict[tuple[int, str], dict[str, list]] = {}
        self._resolved: dict[str, dict[tuple, _Termination]] = {}
        self._mappings: list = []
        self._existing: dict[int, _ExistingCable] = {}
        self._occupied: dict[tuple[str, int], _ExistingCable] = {}
        self._mapping_rows: dict[str, Any] | None = None
        self._stored = self._stored_resolutions()
        self._devices = self._load_devices()
        self._resolve_terminations()
        self._load_mappings()
        self._build_segments()
        self._load_existing_cables()
        self._classify()
        self._decide()
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

        rows = TerminationResolution.objects.filter(profile=self.profile, task_type=SELECT_TERMINATION_TASK)
        return {row.field_key: row for row in rows}

    def _load_devices(self) -> dict[str, list]:
        """Return the visible Devices every planned trace names, grouped by comparison name."""
        names = [
            reference.device
            for analysis in self.analyses
            if not analysis.stopped
            for reference in self._references(analysis.trace)
        ]
        return _visible_devices(self.reader, names)

    def _components_for(self, device_id: int, kind: str) -> dict[str, list]:
        """Return one Device's terminations of one kind, grouped by comparison name."""
        cached = self._components.get((device_id, kind))
        if cached is None:
            cached = {}
            for component in getattr(self.reader, _READER_ACCESSOR_BY_KIND[kind])().filter(device_id=device_id):
                cached.setdefault(identity_text(component.name), []).append(component)
            self._components[(device_id, kind)] = cached
        return cached

    def _resolve_terminations(self) -> None:
        """Bind every Termination Reference to one NetBox object, or record why it stays open."""
        for analysis in self.analyses:
            if analysis.stopped:
                continue
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
        devices = self._devices.get(identity_text(reference.device), [])
        if len(devices) != 1:
            analysis.block("trace.device_unresolved", {**_reference_display(reference), "matches": len(devices)})
            return None
        device = devices[0]
        stored = self._stored.get(_field_key(reference))
        if stored is not None:
            return self._stored_termination(analysis, reference, device, stored)
        kind = claimed_termination_kind(reference.port_class)
        candidates = self._components_for(device.pk, kind).get(identity_text(reference.port), [])
        if len(candidates) != 1:
            analysis.block(
                "cable.termination_unresolved", {**_reference_display(reference), "matches": len(candidates)}
            )
            return None
        return self._termination(candidates[0])

    def _stored_termination(self, analysis: _TraceAnalysis, reference, device, stored) -> _Termination | None:
        """Return the object one saved decision selected, rechecked against current target state."""
        label = f"{stored.selected_object_type.app_label}.{stored.selected_object_type.model}"
        if label not in _SUPPORTED_TERMINATION_LABELS:
            analysis.refuse(
                "cable.unsupported_termination_kind",
                {**_reference_display(reference), "selected_object_type": label},
            )
            return None
        accessor = _READER_ACCESSOR_BY_KIND[_KIND_BY_MODEL_NAME[label.partition(".")[2]]]
        # A saved selection that left the resolved Device no longer answers the question it was asked.
        selected = getattr(self.reader, accessor)().filter(pk=stored.selected_object_id, device_id=device.pk).first()
        if selected is None:
            analysis.block(
                "cable.termination_unresolved",
                {**_reference_display(reference), "selected_display_name": stored.selected_display_name},
            )
            return None
        return self._termination(selected)

    def _termination(self, component) -> _Termination:
        """Return the plan-side record of one resolved NetBox termination."""
        label = _object_type_label(component)
        self._objects[(label, component.pk)] = component
        return _Termination(
            object_type=label,
            object_id=component.pk,
            kind=_KIND_BY_MODEL_NAME[label.partition(".")[2]],
            device_id=component.device_id,
            display=str(component),
        )

    def _load_mappings(self) -> None:
        """Read every PortMapping row on a Device this batch resolved a pass-through port on."""
        device_ids = {
            termination.device_id
            for resolved in self._resolved.values()
            for termination in resolved.values()
            if termination.kind != INTERFACE_KIND
        }
        if device_ids:
            rows = self.reader.port_mappings().select_related("front_port", "rear_port")
            self._mappings = list(rows.filter(device_id__in=sorted(device_ids)))

    def _peers_of(self, termination: _Termination) -> list:
        """Return the PortMapping rows that link one pass-through port to its opposite side."""
        if termination.kind == FRONT_PORT_KIND:
            return [row for row in self._mappings if row.front_port_id == termination.object_id]
        if termination.kind == REAR_PORT_KIND:
            return [row for row in self._mappings if row.rear_port_id == termination.object_id]
        return []

    def _peer_termination(self, mapping, termination: _Termination) -> _Termination:
        """Return the opposite port one PortMapping row links to *termination*."""
        return self._termination(mapping.rear_port if termination.kind == FRONT_PORT_KIND else mapping.front_port)

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
        analysis.segments = [
            _DesiredSegment(
                index=index,
                left=left_ends[index],
                right=right_ends[index],
                cable_class=source_text(segment.cable_class),
            )
            for index, segment in enumerate(segments)
        ]

    def _continue_path(self, analysis: _TraceAnalysis, reference, exit_end, entry_end) -> _Termination | None:
        """Return the termination the next cable end takes where the path passes through a panel."""
        if exit_end.key != entry_end.key:
            return self._verified_pass_through(analysis, reference, exit_end, entry_end)
        peers: dict[tuple[str, int], Any] = {}
        for row in self._peers_of(exit_end):
            peers.setdefault(self._peer_termination(row, exit_end).key, row)
        if not peers:
            analysis.refuse(
                "cable.pass_through_not_mapped",
                {**_reference_display(reference), "entry": exit_end.display, "exit": entry_end.display, "mapped": []},
                identities=(exit_end.identity,),
            )
            return None
        if len(peers) > 1:
            return self._chosen_peer(analysis, reference, exit_end, peers)
        return self._substituted(analysis, reference, exit_end, next(iter(peers.values())))

    def _substituted(self, analysis: _TraceAnalysis, reference, exit_end, mapping) -> _Termination:
        """Record one same-port continuation and return the mapped peer it substitutes."""
        peer = self._peer_termination(mapping, exit_end)
        analysis.note(
            "cable.same_port_continuation",
            {**_reference_display(reference), "port": exit_end.display, "peer": peer.display},
            identities=(exit_end.identity, peer.identity, _object_identity("dcim.portmapping", mapping.pk)),
        )
        return peer

    def _chosen_peer(self, analysis: _TraceAnalysis, reference, exit_end, peers) -> _Termination | None:
        """Return the mapped peer the operator selected, or block on the several NetBox offers."""
        stored = self._stored.get(_field_key(reference, MAPPED_PEER_ROLE))
        if stored is not None:
            selected = (
                f"{stored.selected_object_type.app_label}.{stored.selected_object_type.model}",
                stored.selected_object_id,
            )
            if selected in peers:
                return self._substituted(analysis, reference, exit_end, peers[selected])
        analysis.block(
            "cable.ambiguous_mapped_peer",
            {
                **_reference_display(reference),
                "port": exit_end.display,
                "peers": sorted(self._peer_termination(row, exit_end).display for row in peers.values()),
            },
            identities=(exit_end.identity, *sorted(_object_identity(*key) for key in peers)),
        )
        return None

    def _verified_pass_through(self, analysis: _TraceAnalysis, reference, exit_end, entry_end) -> _Termination | None:
        """Return the stated entry port once a PortMapping row proves the panel joins the two ports."""
        mapping = None
        if {exit_end.kind, entry_end.kind} == {FRONT_PORT_KIND, REAR_PORT_KIND}:
            front, rear = (exit_end, entry_end) if exit_end.kind == FRONT_PORT_KIND else (entry_end, exit_end)
            mapping = next(
                (
                    row
                    for row in self._mappings
                    if row.front_port_id == front.object_id and row.rear_port_id == rear.object_id
                ),
                None,
            )
        if mapping is None:
            analysis.refuse(
                "cable.pass_through_not_mapped",
                {
                    **_reference_display(reference),
                    "entry": exit_end.display,
                    "exit": entry_end.display,
                    "mapped": sorted(self._peer_termination(row, exit_end).display for row in self._peers_of(exit_end)),
                },
                identities=(exit_end.identity, entry_end.identity),
            )
            return None
        analysis.note(
            "cable.pass_through_verified",
            {**_reference_display(reference), "entry": exit_end.display, "exit": entry_end.display},
            identities=(exit_end.identity, entry_end.identity, _object_identity("dcim.portmapping", mapping.pk)),
        )
        return entry_end

    def _load_existing_cables(self) -> None:
        """Read every Cable that already holds a termination this batch resolved."""
        from dcim.models import Cable, CableTermination

        wanted = {
            termination.key for analysis in self.analyses for termination in self._analysis_terminations(analysis)
        }
        # Occupancy is unscoped: a Cable the actor cannot view still holds the termination.
        cable_ids = {
            component.cable_id for key, component in self._objects.items() if key in wanted and component.cable_id
        }
        if not cable_ids:
            return
        sides: dict[int, dict[str, set]] = {}
        for row in CableTermination.objects.filter(cable_id__in=sorted(cable_ids)):
            label = _object_type_label(row.termination_type.model_class())
            sides.setdefault(row.cable_id, {"A": set(), "B": set()})[row.cable_end].add((label, row.termination_id))
        for cable in Cable.objects.filter(pk__in=sorted(cable_ids)):
            ends = sides.get(cable.pk, {"A": set(), "B": set()})
            existing = _ExistingCable(cable=cable, a_side=frozenset(ends["A"]), b_side=frozenset(ends["B"]))
            self._existing[cable.pk] = existing
            for key in existing.a_side | existing.b_side:
                self._occupied[key] = existing

    @staticmethod
    def _analysis_terminations(analysis: _TraceAnalysis) -> list[_Termination]:
        """Return every resolved termination one trace's plan depends on."""
        terminations = list(analysis.endpoints)
        for segment in analysis.segments:
            terminations.extend(segment.terminations)
        return terminations

    def _classify(self) -> None:
        """Decide which segments already exist, then which Cable is each trace's Logical Cable.

        Every desired segment of the batch is classified before any Logical Cable is chosen, so a
        Cable that proves one trace's segment can never be deleted as another trace's leftover.
        """
        pending = self._writing()
        for analysis in pending:
            for segment in analysis.segments:
                proven = next((item for item in self._existing.values() if item.satisfies(segment)), None)
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
        """Return the direct endpoint-to-endpoint Cable that proves no desired segment."""
        first, second = analysis.endpoints
        for existing in self._existing.values():
            if existing.cable.pk not in proven_ids and existing.connects(first, second):
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
        analysis.note(
            "cable.segment_reused",
            {"segment_index": 0, "cable": str(analysis.logical_cable.cable)},
            identities=(_object_identity("dcim.cable", analysis.logical_cable.cable.pk),),
        )
        analysis.logical_cable = None

    def _note_reuse(self, analysis: _TraceAnalysis, segment: _DesiredSegment, proven: _ExistingCable) -> None:
        """Record the proven physical segment, so its live state joins the unit fingerprint."""
        analysis.note(
            "cable.segment_reused",
            {"segment_index": segment.index, "cable": str(proven.cable)},
            identities=(
                _object_identity("dcim.cable", proven.cable.pk),
                segment.left.identity,
                segment.right.identity,
            ),
        )
        drift = self._attribute_drift(segment, proven.cable)
        if drift:
            analysis.note(
                "cable.attribute_drift",
                {"segment_index": segment.index, "cable": str(proven.cable), **drift},
                identities=(_object_identity("dcim.cable", proven.cable.pk),),
            )

    def _attribute_drift(self, segment: _DesiredSegment, cable) -> dict:
        """Return the reused Cable attributes that differ from what this import would have written."""
        mapping = self._cable_class_mapping(segment.cable_class)
        drift = {}
        if cable.status != CABLE_STATUS:
            drift["status"] = cable.status
        if mapping is not None and mapping.cable_type_resolved and (cable.type or None) != mapping.cable_type:
            drift["type"] = cable.type or ""
        if mapping is not None and mapping.cable_profile_resolved and (cable.profile or None) != mapping.cable_profile:
            drift["profile"] = cable.profile or ""
        if cable.label:
            drift["label"] = cable.label
        return drift

    def _report_conflicts(self, analysis: _TraceAnalysis) -> None:
        """Block the trace when a Cable this import may not touch holds a termination it needs."""
        logical_id = None if analysis.logical_cable is None else analysis.logical_cable.cable.pk
        for segment in analysis.pending:
            for termination in segment.terminations:
                occupying = self._occupied.get(termination.key)
                if occupying is None or occupying.cable.pk == logical_id:
                    continue
                code = (
                    "cable.multi_termination_conflict" if occupying.multi_termination else "cable.termination_occupied"
                )
                analysis.block(
                    code,
                    {"segment_index": segment.index, "port": termination.display, "cable": str(occupying.cable)},
                    identities=(termination.identity, _object_identity("dcim.cable", occupying.cable.pk)),
                )

    def _cable_class_mapping(self, cable_class: str):
        """Return the Cable policy row one CableClass value carries, or None."""
        if self._mapping_rows is None:
            from .models import CableClassMapping

            rows = CableClassMapping.objects.filter(profile=self.profile)
            self._mapping_rows = {row.cable_class: row for row in rows}
        return self._mapping_rows.get(cable_class)

    def _decide(self) -> None:
        """Settle the Cable policy and the write permissions every actionable trace needs."""
        for analysis in self.analyses:
            if analysis.stopped or not analysis.endpoints:
                continue
            for segment in analysis.pending:
                policy = self._cable_policy(analysis, segment)
                if policy is not None:
                    analysis.policies[segment.index] = policy
            self._check_permissions(analysis)

    def _cable_policy(self, analysis: _TraceAnalysis, segment: _DesiredSegment) -> dict | None:
        """Return the Cable Type and Cable Profile one new segment is written with."""
        from .models import cable_class_mapping_choice_errors

        display = {"segment_index": segment.index, "cable_class": segment.cable_class}
        mapping = self._cable_class_mapping(segment.cable_class)
        if mapping is None or not (mapping.cable_type_resolved and mapping.cable_profile_resolved):
            analysis.block("cable.cableclass_unmapped", display)
            return None
        errors = cable_class_mapping_choice_errors(mapping.cable_type, mapping.cable_profile)
        for error in errors.values():
            analysis.block(error.code, {**display, "message": error.messages[0]})
        if errors:
            return None
        return {"cable_type": mapping.cable_type, "cable_profile": mapping.cable_profile}

    def _check_permissions(self, analysis: _TraceAnalysis) -> None:
        """Block the trace when the actor may not make every Cable write it asks for."""
        if self.actor is None:
            return
        if analysis.pending and not self.actor.has_perm("dcim.add_cable"):
            analysis.block("cable.permission_denied", {"permission": "dcim.add_cable"})
            return
        logical = analysis.logical_cable
        if logical is not None and not self.actor.has_perm("dcim.delete_cable", logical.cable):
            analysis.block(
                "cable.permission_denied",
                {"permission": "dcim.delete_cable", "cable": str(logical.cable)},
                identities=(_object_identity("dcim.cable", logical.cable.pk),),
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
        for analysis in self._writing():
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
            display={**analysis.display, "detail": self._detail(analysis, disposition)},
        )

    def _changes(self, analysis: _TraceAnalysis) -> tuple[PlannedChange, ...]:
        """Return the deletion and the creations one actionable trace performs, in that order."""
        if analysis.stopped:
            return ()
        changes = []
        if analysis.logical_cable is not None:
            changes.append(self._delete_change(analysis.logical_cable))
        changes.extend(self._create_change(segment, analysis.policies[segment.index]) for segment in analysis.pending)
        return tuple(changes)

    @staticmethod
    def _delete_change(logical: _ExistingCable) -> PlannedChange:
        """Return the one deletion a Patched Path Replacement ever performs."""
        return PlannedChange(
            identity=f"cable:delete:{logical.cable.pk}",
            target_module=CableModule.key,
            operation="delete",
            payload={"cable_id": logical.cable.pk, "display": str(logical.cable)},
            preconditions={"cable_id": logical.cable.pk, "terminations": logical.terminations},
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
                "cable_class": segment.cable_class,
                "sources": self._sources_by_segment.get(segment.key, []),
                **policy,
            },
            dependencies=self._deletes_by_segment.get(segment.key, ()),
            preconditions={"terminations": segment.as_json()},
        )

    @staticmethod
    def _detail(analysis: _TraceAnalysis, disposition: str) -> str:
        """Return the operator wording one unit shows before the trace workspace exists."""
        if disposition == Disposition.NO_OP:
            return "The stated path already exists."
        if disposition != Disposition.ACTIONABLE:
            return ""
        count = len(analysis.pending)
        if analysis.logical_cable is not None:
            return f"Would replace the logical cable with {count} physical segment(s)."
        return f"Would create {count} physical segment(s)."


class CableModule:
    """Plan and write the Cables one Source Trace states, as a Patched Path Replacement."""

    key = TargetModuleKey.CABLE
    consumes = frozenset({OutputKind.SOURCE_TRACE})

    def plan(self, source_batch, profile, catalog, netbox_reader) -> list[SynchronizationUnit]:
        """Return one Synchronization Unit per Source Trace in the batch."""
        del catalog
        if not (self.consumes & source_batch.output_kinds):
            return []
        return _CableBatch(source_batch.rows, profile, netbox_reader).units()

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
        enforce_saved_object_permission(cable, execution_context.actor, "delete")
        snapshot = DeletedObject(
            object_type="dcim.cable",
            object_id=cable_id,
            display=str(cable),
            detail={"terminations": current},
        )
        cable.delete()
        return snapshot

    @staticmethod
    def _create(planned_change: PlannedChange, execution_context) -> Any:
        """Create one physical Cable segment and the provenance rows its Source Traces earn."""
        from dcim.models import Cable

        payload = planned_change.payload
        ends = [CableModule._free_termination(label, object_id) for label, object_id in payload["terminations"]]
        cable = Cable(
            type=payload["cable_type"],
            profile=payload["cable_profile"] or "",
            status=payload["status"],
            a_terminations=[ends[0]],
            b_terminations=[ends[1]],
        )
        cable.full_clean()
        cable.save()
        enforce_saved_object_permission(cable, execution_context.actor, "add")
        CableModule._store_provenance(cable, payload, execution_context.profile)
        return cable

    @staticmethod
    def _free_termination(label: str, object_id: int):
        """Return one locked termination, refusing the write when it is gone or already cabled."""
        component = _model_for_label(label).objects.filter(pk=object_id).select_for_update(of=("self",)).first()
        if component is None:
            raise PreconditionFailed(f"{label} {object_id} is gone, so the segment cannot be created.")
        if component.cable_id is not None:
            raise PreconditionFailed(f"{component} received a cable after the plan was made.")
        return component

    @staticmethod
    def _store_provenance(cable, payload, profile) -> None:
        """Write one provenance row per Source Trace that states this segment."""
        from .models import CableImportSource

        for record in payload["sources"]:
            CableImportSource.objects.update_or_create(
                cable=cable,
                profile=profile,
                trace_identity=record["trace_identity"],
                defaults={
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
        [_object_type_label(row.termination_type.model_class()), row.termination_id]
        for row in CableTermination.objects.filter(cable_id=cable_id)
    )


__all__ = ("CABLE_STATUS", "CableModule", "eligible_terminations")

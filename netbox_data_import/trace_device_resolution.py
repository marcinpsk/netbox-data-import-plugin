# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve Source Trace Device labels inside one permission-scoped import target."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import reduce
from operator import or_
from typing import Any

from django.db.models import BigIntegerField, Case, F, IntegerField, Q, Value, When

from .identity import CANONICAL_NAME, identity_in, identity_text, matching_search, with_name_identity
from .trace_location_resolution import MAPPED, decided_location_mappings, site_locations, source_location_key
from .values import normalize_for_compare, source_position

AUTOMATICALLY_RESOLVED = "automatically resolved"
MANUALLY_RESOLVED = "manually resolved"
UNRESOLVED = "unresolved"
STALE = "stale"

_SERIALIZED_EVIDENCE_FIELDS = frozenset({"key", "labels", "locations", "racks", "u_positions"})
_DEVICE_QUESTION_PRESENTATION_FIELDS = frozenset(
    {"label", "state", "state_style", "selected", "selectable", "reason", "exact_match_count", "disclosure_source"}
)


@dataclass(frozen=True)
class DeviceEvidence:
    """Aggregate every source fact stated for one canonical Device label."""

    key: str
    labels: tuple[str, ...]
    locations: tuple[str, ...]
    racks: tuple[str, ...]
    u_positions: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DeviceEvidence:
        """Restore evidence carried by an accepted Import Plan."""
        if not isinstance(value, Mapping):
            raise TypeError("Device evidence must be an object.")
        missing = _SERIALIZED_EVIDENCE_FIELDS - value.keys()
        if missing:
            raise ValueError(f"Device evidence is missing fields: {', '.join(sorted(missing))}.")
        unknown = value.keys() - _SERIALIZED_EVIDENCE_FIELDS - _DEVICE_QUESTION_PRESENTATION_FIELDS
        if unknown:
            raise ValueError(f"Device evidence has unknown fields: {', '.join(sorted(unknown))}.")
        raw_key = value["key"]
        if not isinstance(raw_key, str):
            raise TypeError("Device evidence key must be a string.")
        key = source_device_key(raw_key)
        if not key:
            raise ValueError("A Device question needs a source Device key.")
        return cls(
            key=key,
            labels=_serialized_source_values(value, "labels"),
            locations=_serialized_source_values(value, "locations"),
            racks=_serialized_source_values(value, "racks"),
            u_positions=_serialized_source_values(value, "u_positions"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe evidence stored in an Import Plan."""
        return {
            "key": self.key,
            "labels": list(self.labels),
            "locations": list(self.locations),
            "racks": list(self.racks),
            "u_positions": list(self.u_positions),
        }


@dataclass(frozen=True)
class DeviceResolution:
    """State how one source Device label resolves in NetBox."""

    evidence: DeviceEvidence
    state: str
    device: Any | None
    exact_match_count: int
    reason: str = ""

    def to_question(self) -> dict[str, Any]:
        """Return the server-authored Device question carried by a trace plan."""
        state_style = {
            AUTOMATICALLY_RESOLVED: "auto",
            MANUALLY_RESOLVED: "manual",
            UNRESOLVED: "unresolved",
            STALE: "stale",
        }[self.state]
        return {
            **self.evidence.to_dict(),
            "label": self.evidence.labels[0] if self.evidence.labels else self.evidence.key,
            "state": self.state,
            "state_style": state_style,
            "selected": str(self.device) if self.device is not None else "",
            "selectable": self.device is None,
            "reason": self.reason,
            "exact_match_count": self.exact_match_count,
        }


@dataclass(frozen=True)
class CandidateFact:
    """One source fact and the visible NetBox value it was compared with."""

    fact: str
    source: str
    netbox: str
    # Only a Location fact compares through a mapping, so only it names the mapped Location.
    mapped: str = ""

    def to_dict(self) -> dict[str, str]:
        """Return the JSON-safe explanation the Device picker renders."""
        return {"fact": self.fact, "source": self.source, "mapped": self.mapped, "netbox": self.netbox}


@dataclass(frozen=True)
class ImportLocationHint:
    """The import-page Location that holds a candidate's placement Location."""

    location: str
    netbox: str

    def to_dict(self) -> dict[str, str]:
        """Return the JSON-safe hint the Device picker renders apart from source evidence."""
        return {"location": self.location, "netbox": self.netbox}


@dataclass(frozen=True)
class DeviceCandidate:
    """One visible Device and the source evidence that supports or contradicts it."""

    device: Any
    matched: tuple[CandidateFact, ...]
    conflicting: tuple[CandidateFact, ...]
    import_location: ImportLocationHint | None


@dataclass(frozen=True)
class DeviceCandidatePage:
    """One bounded candidate page and its uncapped total."""

    candidates: tuple[DeviceCandidate, ...]
    total: int


def source_device_key(label: str) -> str:
    """Return the profile-wide identity of one source Device label."""
    return identity_text(label)


def _source_values(values: Iterable[str]) -> tuple[str, ...]:
    """Return one source spelling per nonempty identity, in identity order."""
    found: dict[str, str] = {}
    for value in values:
        if key := identity_text(value):
            found.setdefault(key, value)
    return tuple(found[key] for key in sorted(found))


def _serialized_source_values(value: Mapping[str, Any], field: str) -> tuple[str, ...]:
    """Validate and restore one list of source facts from an Import Plan."""
    values = value[field]
    if not isinstance(values, (list, tuple)) or any(not isinstance(item, str) for item in values):
        raise TypeError(f"Device evidence {field} must be a list or tuple of strings.")
    return _source_values(values)


def _trace_references(trace) -> tuple:
    """Return every Termination Reference carried by one Source Trace."""
    summary = trace.endpoint_summary
    references = [summary.from_termination, summary.to_termination]
    for segment in trace.segments:
        references.extend((segment.left, segment.right))
    references.extend(trace.corroboration)
    return tuple(references)


def collect_trace_device_evidence(traces: Iterable[Any]) -> dict[str, DeviceEvidence]:
    """Aggregate Device labels and placement hints across one Source Trace batch."""
    collected: dict[str, dict[str, list[str]]] = {}
    for trace in traces:
        for reference in _trace_references(trace):
            key = source_device_key(reference.device)
            if not key:
                continue
            values = collected.setdefault(
                key,
                {"labels": [], "locations": [], "racks": [], "u_positions": []},
            )
            values["labels"].append(reference.device)
            values["locations"].append(reference.location)
            values["racks"].append(reference.rack)
            values["u_positions"].append(reference.u_position)
    return {
        key: DeviceEvidence(
            key=key,
            labels=_source_values(values["labels"]),
            locations=_source_values(values["locations"]),
            racks=_source_values(values["racks"]),
            u_positions=_source_values(values["u_positions"]),
        )
        for key, values in collected.items()
    }


def _target_devices(reader):
    """Return visible Devices inside the Cable Target Module's selected Site."""
    devices = reader.devices()
    if reader.site is not None:
        devices = devices.filter(site=reader.site)
    return devices


def resolve_trace_devices(
    *,
    profile,
    reader,
    evidence: Mapping[str, DeviceEvidence],
    lock_rows: bool = False,
) -> dict[str, DeviceResolution]:
    """Resolve every source Device key with saved-choice precedence and bulk reads."""
    from .models import TraceDeviceResolution, index_digest

    keys = tuple(evidence)
    if not keys:
        return {}
    stored_rows = TraceDeviceResolution.objects.filter(
        profile=profile,
        source_device_key_digest__in=[index_digest(key) for key in keys],
    )
    stored = {}
    for row in stored_rows:
        if row.source_device_key not in evidence:
            raise ValueError("A Trace Device Resolution digest does not match its source Device key.")
        stored[row.source_device_key] = row

    # A stored key needs its name lookup too, so a stale choice can report the matches it now has.
    devices = with_name_identity(_target_devices(reader)).filter(
        Q(pk__in={row.selected_device_id for row in stored.values()}) | Q(identity_in("name", keys))
    )
    if lock_rows:
        devices = devices.order_by("pk").select_for_update(of=("self",))
    by_id = {}
    by_name: dict[str, list[Any]] = {}
    for device in devices:
        by_id[device.pk] = device
        by_name.setdefault(getattr(device, CANONICAL_NAME), []).append(device)

    outcomes = {}
    for key, facts in evidence.items():
        saved = stored.get(key)
        if saved is not None:
            device = by_id.get(saved.selected_device_id)
            if device is None:
                outcomes[key] = DeviceResolution(
                    evidence=facts,
                    state=STALE,
                    device=None,
                    exact_match_count=len(by_name.get(key, ())),
                    reason="The saved Device is no longer available at this import target. Choose it again.",
                )
            else:
                outcomes[key] = DeviceResolution(
                    evidence=facts,
                    state=MANUALLY_RESOLVED,
                    device=device,
                    exact_match_count=len(by_name.get(key, ())),
                )
            continue
        matches = by_name.get(key, [])
        device = matches[0] if len(matches) == 1 else None
        outcomes[key] = DeviceResolution(
            evidence=facts,
            state=AUTOMATICALLY_RESOLVED if device is not None else UNRESOLVED,
            device=device,
            exact_match_count=len(matches),
            reason=(
                ""
                if device is not None
                else f"The source name matches {len(matches)} Devices at this import target. Choose the Device."
            ),
        )
    return outcomes


def resolved_trace_device(*, profile, reader, key: str, lock_rows: bool = False):
    """Return the Device one source Device key resolves to, or None when its decision is open."""
    if not key:
        return None
    evidence = {key: DeviceEvidence(key=key, labels=(), locations=(), racks=(), u_positions=())}
    return resolve_trace_devices(profile=profile, reader=reader, evidence=evidence, lock_rows=lock_rows)[key].device


def _site_racks(reader):
    """Return the Racks the actor may view inside the selected Site."""
    racks = reader.racks()
    return racks.filter(site=reader.site) if reader.site is not None else racks


def _flag(condition) -> Case:
    """Return 1 for a Device row that meets *condition*, else 0."""
    return Case(When(condition, then=Value(1)), default=Value(0), output_field=IntegerField())


def _placement_location(reader) -> Case:
    """Return a Device's visible placement Location: its own, else its visible Rack's when it has none."""
    visible_locations = site_locations(reader).values("pk")
    return Case(
        When(location_id__in=visible_locations, then=F("location_id")),
        When(
            Q(location_id__isnull=True, rack_id__in=_site_racks(reader).values("pk"))
            & Q(rack__location_id__in=visible_locations),
            then=F("rack__location_id"),
        ),
        default=Value(None),
        output_field=BigIntegerField(),
    )


def _in_subtree(location) -> Q:
    """Match a visible placement inside *location*'s subtree, whatever lies between them."""
    return Q(_ndi_placement_location__in=location.get_descendants(include_self=True).values("pk"))


def _mapped_paths(profile, reader, evidence: DeviceEvidence) -> tuple[tuple[str, Any], ...]:
    """Return each source Location path of *evidence* whose deciding row maps to a visible Location, with it."""
    paths = {source_location_key(path): path for path in evidence.locations}
    mappings = decided_location_mappings(profile=profile, reader=reader, paths=paths.values())
    return tuple((paths[key], mapping.location) for key, mapping in sorted(mappings.items()) if mapping.state == MAPPED)


def _with_evidence(devices, reader, evidence, *, exact_names, matching_racks, mapped_paths):
    """Annotate the per-fact match and conflict flags that rank and explain each candidate."""
    positions = [source_position(value) for value in evidence.u_positions]
    positions = [Decimal(str(value)) for value in positions if value is not None]
    visible_racks = _site_racks(reader).values("pk")
    path_names = tuple(f"_ndi_location_path_{index}" for index in range(len(mapped_paths)))
    devices = with_name_identity(devices).annotate(_ndi_placement_location=_placement_location(reader))
    devices = devices.annotate(
        **{name: _flag(_in_subtree(location)) for name, (_path, location) in zip(path_names, mapped_paths, strict=True)}
    )
    zero = Value(0, output_field=IntegerField())
    location_match = location_conflict = zero
    if path_names:
        # Location scores once when any mapped path matches (section 6.1), and conflicts once when any does not.
        location_match = _flag(reduce(or_, (Q(**{name: 1}) for name in path_names)))
        location_conflict = _flag(
            Q(_ndi_placement_location__isnull=False) & reduce(or_, (Q(**{name: 0}) for name in path_names))
        )
    devices = devices.annotate(
        _ndi_exact_name=_flag(Q(**{f"{CANONICAL_NAME}__in": exact_names})),
        _ndi_rack_match=_flag(Q(rack_id__in=matching_racks)) if matching_racks else zero,
        _ndi_rack_conflict=(
            _flag(Q(rack_id__in=visible_racks) & ~Q(rack_id__in=matching_racks))
            if matching_racks
            else _flag(Q(rack_id__in=visible_racks))
            if evidence.racks
            else zero
        ),
        _ndi_position_match=_flag(Q(position__in=positions)) if positions else zero,
        _ndi_position_conflict=(
            _flag(Q(position__isnull=False) & ~Q(position__in=positions))
            if positions
            else _flag(Q(position__isnull=False))
            if evidence.u_positions
            else zero
        ),
        _ndi_location_match=location_match,
        _ndi_location_conflict=location_conflict,
        _ndi_import_location=_flag(_in_subtree(reader.location)) if reader.location is not None else zero,
    )
    return devices.annotate(
        _ndi_hint_score=F("_ndi_rack_match") + F("_ndi_location_match") + F("_ndi_position_match"),
        _ndi_conflict_score=F("_ndi_rack_conflict") + F("_ndi_location_conflict") + F("_ndi_position_conflict"),
    )


def _explain(device, evidence, *, mapped_paths, rack_names, location_names, import_location) -> DeviceCandidate:
    """Explain one ranked candidate from the same flags that ranked it, naming both values of each fact."""
    matched: list[CandidateFact] = []
    conflicting: list[CandidateFact] = []
    if device._ndi_exact_name:
        matched.append(CandidateFact("name", source=", ".join(evidence.labels), netbox=device.name))
    placement = location_names.get(device._ndi_placement_location, "")
    for index, (path, location) in enumerate(mapped_paths):
        fact = CandidateFact("location", source=path, mapped=location.name, netbox=placement)
        if getattr(device, f"_ndi_location_path_{index}"):
            matched.append(fact)
        elif device._ndi_placement_location is not None:
            conflicting.append(fact)
    rack = CandidateFact("rack", source=", ".join(evidence.racks), netbox=rack_names.get(device.rack_id, ""))
    if device._ndi_rack_match:
        matched.append(rack)
    if device._ndi_rack_conflict:
        conflicting.append(rack)
    position = CandidateFact(
        "U position", source=", ".join(evidence.u_positions), netbox=normalize_for_compare(device.position)
    )
    if device._ndi_position_match:
        matched.append(position)
    if device._ndi_position_conflict:
        conflicting.append(position)
    return DeviceCandidate(
        device=device,
        matched=tuple(matched),
        conflicting=tuple(conflicting),
        import_location=(
            ImportLocationHint(location=import_location.name, netbox=placement) if device._ndi_import_location else None
        ),
    )


def eligible_trace_devices(
    *,
    profile,
    reader,
    evidence: DeviceEvidence,
    search: str = "",
    limit: int,
    offset: int = 0,
    lock_rows: bool = False,
) -> DeviceCandidatePage:
    """Return a deterministic bounded page of visible Device candidates."""
    devices = matching_search(_target_devices(reader), search)
    wanted_racks = {identity_text(value) for value in evidence.racks} - {""}
    matching_racks = set(_site_racks(reader).filter(identity_in("name", wanted_racks)).values_list("pk", flat=True))
    mapped_paths = _mapped_paths(profile, reader, evidence)
    devices = _with_evidence(
        devices,
        reader,
        evidence,
        exact_names={evidence.key},
        matching_racks=matching_racks,
        mapped_paths=mapped_paths,
    ).order_by("-_ndi_exact_name", "-_ndi_hint_score", "_ndi_conflict_score", "-_ndi_import_location", "name", "pk")
    total = devices.count()
    selected: tuple[Any, ...] = ()
    if offset < total and lock_rows:
        ranked_ids = tuple(devices.values_list("pk", flat=True)[offset : offset + limit])
        locked = {
            device.pk: device
            for device in devices.filter(pk__in=ranked_ids).order_by("pk").select_for_update(of=("self",))
        }
        selected = tuple(locked[pk] for pk in ranked_ids if pk in locked)
    elif offset < total:
        selected = tuple(devices[offset : offset + limit])
    rack_names = dict(
        _site_racks(reader).filter(pk__in={device.rack_id for device in selected}).values_list("pk", "name")
    )
    location_names = dict(
        site_locations(reader)
        .filter(pk__in={device._ndi_placement_location for device in selected})
        .values_list("pk", "name")
    )
    return DeviceCandidatePage(
        candidates=tuple(
            _explain(
                device,
                evidence,
                mapped_paths=mapped_paths,
                rack_names=rack_names,
                location_names=location_names,
                import_location=reader.location,
            )
            for device in selected
        ),
        total=total,
    )


__all__ = (
    "AUTOMATICALLY_RESOLVED",
    "MANUALLY_RESOLVED",
    "STALE",
    "UNRESOLVED",
    "CandidateFact",
    "DeviceCandidate",
    "DeviceCandidatePage",
    "DeviceEvidence",
    "DeviceResolution",
    "ImportLocationHint",
    "collect_trace_device_evidence",
    "eligible_trace_devices",
    "resolve_trace_devices",
    "resolved_trace_device",
    "source_device_key",
)

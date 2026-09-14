# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve Source Trace Device labels inside one permission-scoped import target."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from django.db.models import Case, IntegerField, Q, Value, When

from .values import identity_text, normalize_for_compare, source_position, source_text

AUTOMATICALLY_RESOLVED = "automatically resolved"
MANUALLY_RESOLVED = "manually resolved"
UNRESOLVED = "unresolved"
STALE = "stale"


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
        key = source_device_key(value.get("key", ""))
        if not key:
            raise ValueError("A Device question needs a source Device key.")
        return cls(
            key=key,
            labels=_source_values(value.get("labels", ())),
            locations=_source_values(value.get("locations", ())),
            racks=_source_values(value.get("racks", ())),
            u_positions=_source_values(value.get("u_positions", ())),
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
class DeviceCandidate:
    """One visible Device and the source evidence that supports or contradicts it."""

    device: Any
    matched_hints: tuple[str, ...]
    conflicting_hints: tuple[str, ...]


@dataclass(frozen=True)
class DeviceCandidatePage:
    """One bounded candidate page and its uncapped total."""

    candidates: tuple[DeviceCandidate, ...]
    total: int


def source_device_key(label: Any) -> str:
    """Return the profile-wide identity of one source Device label."""
    return identity_text(label)


def _source_values(values: Iterable[Any]) -> tuple[str, ...]:
    """Return distinct nonempty source text in stable comparison order."""
    found: dict[str, str] = {}
    for value in values:
        text = source_text(value)
        if text:
            found.setdefault(identity_text(text), text)
    return tuple(found[key] for key in sorted(found))


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

    lookup = Q(pk__in=[row.selected_device_id for row in stored.values()])
    for key in keys:
        if key not in stored:
            lookup |= Q(name__iexact=key)
    devices = _target_devices(reader).filter(lookup)
    if lock_rows:
        devices = devices.order_by("pk").select_for_update(of=("self",))
    by_id = {}
    by_name: dict[str, list[Any]] = {}
    for device in devices:
        by_id[device.pk] = device
        by_name.setdefault(source_device_key(device.name), []).append(device)

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


def resolved_trace_device(*, profile, reader, source_label: str, lock_rows: bool = False):
    """Return the Device one source label resolves to, or None when its decision is open."""
    key = source_device_key(source_label)
    if not key:
        return None
    evidence = {
        key: DeviceEvidence(key=key, labels=(source_text(source_label),), locations=(), racks=(), u_positions=())
    }
    return resolve_trace_devices(profile=profile, reader=reader, evidence=evidence, lock_rows=lock_rows)[key].device


def _visible_placement(reader, devices):
    """Return visible placement facts for only the bounded candidate page."""
    rack_ids = {device.rack_id for device in devices if device.rack_id is not None}
    direct_location_ids = {device.location_id for device in devices if device.location_id is not None}
    racks = reader.racks().filter(pk__in=rack_ids)
    if reader.site is not None:
        racks = racks.filter(site=reader.site)
    rack_rows = tuple(racks.values_list("pk", "name", "location_id"))
    location_ids = direct_location_ids | {location_id for _pk, _name, location_id in rack_rows if location_id}
    locations = reader.locations().filter(pk__in=location_ids)
    if reader.site is not None:
        locations = locations.filter(site=reader.site)
    location_names = dict(locations.values_list("pk", "name"))
    rack_facts = {pk: (name, location_names.get(location_id, "")) for pk, name, location_id in rack_rows}
    return rack_facts, location_names


def _matching_placement_ids(reader, evidence: DeviceEvidence):
    """Return visible related-object IDs that match source placement evidence."""
    racks = reader.racks()
    locations = reader.locations()
    if reader.site is not None:
        racks = racks.filter(site=reader.site)
        locations = locations.filter(site=reader.site)
    wanted_racks = [identity_text(value) for value in evidence.racks]
    wanted_locations = [identity_text(value) for value in evidence.locations]
    rack_match = Q()
    for value in wanted_racks:
        rack_match |= Q(name__iexact=value)
    location_match = Q()
    for value in wanted_locations:
        location_match |= Q(name__iexact=value)
    matching_racks = set(racks.filter(rack_match).values_list("pk", flat=True)) if wanted_racks else set()
    matching_locations = (
        set(locations.filter(location_match).values_list("pk", flat=True)) if wanted_locations else set()
    )
    racks_in_matching_locations = set(racks.filter(location_id__in=matching_locations).values_list("pk", flat=True))
    return matching_racks, matching_locations, racks_in_matching_locations


def _candidate_hints(device, evidence, rack_facts, location_names) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Explain visible placement facts that match or conflict with the source."""
    matched: list[str] = []
    conflicting: list[str] = []
    if source_device_key(device.name) == evidence.key:
        matched.append("name")
    if evidence.locations:
        location = location_names.get(device.location_id, "")
        if not location and device.rack_id in rack_facts:
            location = rack_facts[device.rack_id][1]
        if location:
            (
                matched
                if identity_text(location) in {identity_text(value) for value in evidence.locations}
                else conflicting
            ).append("location")
    if evidence.racks and device.rack_id in rack_facts:
        rack_name = rack_facts[device.rack_id][0]
        (
            matched if identity_text(rack_name) in {identity_text(value) for value in evidence.racks} else conflicting
        ).append("rack")
    if evidence.u_positions and device.position is not None:
        wanted = {normalize_for_compare(value) for value in evidence.u_positions}
        (matched if normalize_for_compare(device.position) in wanted else conflicting).append("U position")
    return tuple(matched), tuple(conflicting)


def eligible_trace_devices(
    *,
    reader,
    evidence: DeviceEvidence,
    search: str = "",
    limit: int,
    lock_rows: bool = False,
) -> DeviceCandidatePage:
    """Return a deterministic bounded page of visible Device candidates."""
    devices = _target_devices(reader)
    if search:
        devices = devices.filter(name__icontains=search)
    matching_racks, matching_locations, racks_in_matching_locations = _matching_placement_ids(reader, evidence)
    positions = [source_position(value) for value in evidence.u_positions]
    positions = [Decimal(str(value)) for value in positions if value is not None]
    exact = Case(When(name__iexact=evidence.key, then=Value(1)), default=Value(0), output_field=IntegerField())
    rack_score = Case(When(rack_id__in=matching_racks, then=Value(1)), default=Value(0), output_field=IntegerField())
    location_score = Case(
        When(Q(location_id__in=matching_locations) | Q(rack_id__in=racks_in_matching_locations), then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    position_score = Case(When(position__in=positions, then=Value(1)), default=Value(0), output_field=IntegerField())
    devices = devices.annotate(
        _ndi_exact_name=exact,
        _ndi_hint_score=rack_score + location_score + position_score,
    ).order_by("-_ndi_exact_name", "-_ndi_hint_score", "name", "pk")
    total = devices.count()
    if lock_rows:
        devices = devices.select_for_update(of=("self",))
    selected = tuple(devices[:limit])
    rack_facts, location_names = _visible_placement(reader, selected)
    candidates = []
    for device in selected:
        matched, conflicting = _candidate_hints(device, evidence, rack_facts, location_names)
        candidates.append(
            DeviceCandidate(
                device=device,
                matched_hints=matched,
                conflicting_hints=conflicting,
            )
        )
    return DeviceCandidatePage(candidates=tuple(candidates), total=total)


__all__ = (
    "AUTOMATICALLY_RESOLVED",
    "MANUALLY_RESOLVED",
    "STALE",
    "UNRESOLVED",
    "DeviceCandidate",
    "DeviceCandidatePage",
    "DeviceEvidence",
    "DeviceResolution",
    "collect_trace_device_evidence",
    "eligible_trace_devices",
    "resolve_trace_devices",
    "resolved_trace_device",
    "source_device_key",
)

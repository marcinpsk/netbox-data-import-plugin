# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve Source Trace Device labels inside one permission-scoped import target."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import connection
from django.db.models import Case, CharField, F, Func, IntegerField, Q, Value, When

from .values import identity_text, normalize_for_compare, source_position, source_text

AUTOMATICALLY_RESOLVED = "automatically resolved"
MANUALLY_RESOLVED = "manually resolved"
UNRESOLVED = "unresolved"
STALE = "stale"

_SERIALIZED_EVIDENCE_FIELDS = frozenset({"key", "labels", "locations", "racks", "u_positions"})
_DEVICE_QUESTION_PRESENTATION_FIELDS = frozenset(
    {"label", "state", "state_style", "selected", "selectable", "reason", "exact_match_count"}
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


def _database_identity_sql(expression: str) -> tuple[str, tuple[str, str, str]]:
    """Return the one SQL expression used for every target identity comparison."""
    return f"UPPER(TRIM(REGEXP_REPLACE({expression}, %s, %s, %s)))", (r"\s+", " ", "g")


class _DatabaseIdentity(Func):
    """Apply the shared target identity expression to one ORM value."""

    arity = 1

    def __init__(self, expression):
        super().__init__(expression, output_field=CharField())

    def as_sql(self, compiler, connection, **extra_context):
        expression_sql, expression_params = compiler.compile(self.source_expressions[0])
        sql, identity_params = _database_identity_sql(expression_sql)
        return sql, (*expression_params, *identity_params)


def _with_database_identity(queryset):
    """Add the shared target identity to rows that have a name field."""
    return queryset.annotate(_ndi_canonical_name=_DatabaseIdentity(F("name")))


def _database_identity_values(values: Iterable[Any]) -> dict[str, str]:
    """Return PostgreSQL's whitespace-insensitive case key for each source value."""
    unique_values = sorted({source_text(value) for value in values} - {""})
    if not unique_values:
        return {}
    identity_sql, identity_params = _database_identity_sql("source_value")
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT source_value, {identity_sql} FROM unnest(%s::text[]) AS source_value",  # noqa: S608 - The SQL fragment is fixed; values use query parameters.
            [*identity_params, unique_values],
        )
        return dict(cursor.fetchall())


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
    source_labels = {
        key: tuple(source_text(label) for label in facts.labels if source_text(label)) or (key,)
        for key, facts in evidence.items()
    }
    database_values = _database_identity_values(label for labels in source_labels.values() for label in labels)
    source_keys_by_database_name: dict[str, set[str]] = {}
    for key, labels in source_labels.items():
        for label in labels:
            source_keys_by_database_name.setdefault(database_values[label], set()).add(key)

    target_devices = _with_database_identity(_target_devices(reader))
    device_ids = {row.selected_device_id for row in stored.values()}
    lookup = Q(pk__in=device_ids)
    if source_keys_by_database_name:
        lookup |= Q(_ndi_canonical_name__in=source_keys_by_database_name)
    devices = target_devices.filter(lookup)
    if lock_rows:
        devices = devices.order_by("pk").select_for_update(of=("self",))
    by_id = {}
    by_name: dict[str, list[Any]] = {}
    for device in devices:
        by_id[device.pk] = device
        for key in source_keys_by_database_name.get(device._ndi_canonical_name, ()):
            by_name.setdefault(key, []).append(device)

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
    racks = _with_database_identity(reader.racks().filter(pk__in=rack_ids))
    if reader.site is not None:
        racks = racks.filter(site=reader.site)
    rack_rows = tuple(racks.values_list("pk", "_ndi_canonical_name", "location_id"))
    location_ids = direct_location_ids | {location_id for _pk, _name, location_id in rack_rows if location_id}
    locations = _with_database_identity(reader.locations().filter(pk__in=location_ids))
    if reader.site is not None:
        locations = locations.filter(site=reader.site)
    location_names = dict(locations.values_list("pk", "_ndi_canonical_name"))
    rack_facts = {pk: (name, location_names.get(location_id, "")) for pk, name, location_id in rack_rows}
    return rack_facts, location_names


def _matching_placement_ids(reader, wanted_racks: set[str], wanted_locations: set[str]):
    """Return visible related-object IDs that match source placement evidence."""
    racks = reader.racks()
    locations = reader.locations()
    if reader.site is not None:
        racks = racks.filter(site=reader.site)
        locations = locations.filter(site=reader.site)
    matching_racks = set(
        _with_database_identity(racks).filter(_ndi_canonical_name__in=wanted_racks).values_list("pk", flat=True)
    )
    matching_locations = set(
        _with_database_identity(locations).filter(_ndi_canonical_name__in=wanted_locations).values_list("pk", flat=True)
    )
    racks_in_matching_locations = set(racks.filter(location_id__in=matching_locations).values_list("pk", flat=True))
    return matching_racks, matching_locations, racks_in_matching_locations


def _candidate_hints(
    device,
    evidence,
    rack_facts,
    location_names,
    *,
    exact_names: set[str],
    wanted_locations: set[str],
    wanted_racks: set[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Explain visible placement facts that match or conflict with the source."""
    matched: list[str] = []
    conflicting: list[str] = []
    if device._ndi_canonical_name in exact_names:
        matched.append("name")
    if evidence.locations:
        location = location_names.get(device.location_id, "")
        if not location and device.rack_id in rack_facts:
            location = rack_facts[device.rack_id][1]
        if location:
            (matched if location in wanted_locations else conflicting).append("location")
    if evidence.racks and device.rack_id in rack_facts:
        rack_name = rack_facts[device.rack_id][0]
        (matched if rack_name in wanted_racks else conflicting).append("rack")
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
    eligible_devices = devices
    exact_values = tuple(source_text(value) for value in evidence.labels if source_text(value)) or (evidence.key,)
    rack_values = tuple(source_text(value) for value in evidence.racks if source_text(value))
    location_values = tuple(source_text(value) for value in evidence.locations if source_text(value))
    database_values = _database_identity_values((*exact_values, *rack_values, *location_values))
    exact_names = {database_values[value] for value in exact_values}
    wanted_racks = {database_values[value] for value in rack_values}
    wanted_locations = {database_values[value] for value in location_values}
    matching_racks, matching_locations, racks_in_matching_locations = _matching_placement_ids(
        reader,
        wanted_racks,
        wanted_locations,
    )
    devices = _with_database_identity(devices)
    positions = [source_position(value) for value in evidence.u_positions]
    positions = [Decimal(str(value)) for value in positions if value is not None]
    exact = Case(
        When(_ndi_canonical_name__in=exact_names, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
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
        ranked_ids = tuple(devices.values_list("pk", flat=True)[:limit])
        locked = {
            device.pk: device
            for device in _with_database_identity(eligible_devices)
            .filter(pk__in=ranked_ids)
            .order_by("pk")
            .select_for_update(of=("self",))
        }
        selected = tuple(locked[pk] for pk in ranked_ids if pk in locked)
    else:
        selected = tuple(devices[:limit])
    rack_facts, location_names = _visible_placement(reader, selected)
    candidates = []
    for device in selected:
        matched, conflicting = _candidate_hints(
            device,
            evidence,
            rack_facts,
            location_names,
            exact_names=exact_names,
            wanted_locations=wanted_locations,
            wanted_racks=wanted_racks,
        )
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

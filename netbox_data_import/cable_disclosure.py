# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Apply live row permissions to cached Cable presentation data."""

from dataclasses import replace
from types import MappingProxyType

CABLE_ROW = "dcim.cable"
CABLE_CLASS_MAPPING_ROW = "netbox_data_import.cableclassmapping"
CABLE_SEGMENT_OVERRIDE_ROW = "netbox_data_import.cablesegmentoverride"
DISCLOSURE_SOURCE = "disclosure_source"
POLICY_HIDDEN = "a policy you cannot view"

CABLE_DIAGNOSTIC_DISCLOSURES = MappingProxyType(
    {
        "cable.attribute_drift": frozenset({"cable", "status", "type", "profile", "label"}),
        "cable.media_family_mismatch": frozenset({"segments"}),
        "cable.multi_termination_conflict": frozenset({"cable"}),
        "cable.permission_denied": frozenset({"cable"}),
        "cable.segment_reused": frozenset({"cable"}),
        "cable.termination_occupied": frozenset({"cable"}),
    }
)

POLICY_DIAGNOSTIC_DISCLOSURES = MappingProxyType(
    {
        "cable.media_family_mismatch": frozenset({"segments"}),
        "cable.resolved_segment_conflict": frozenset({"cable_type", "cable_profile"}),
        "cable.segment_override_lost": frozenset({"cable_type", "cable_profile"}),
    }
)


def disclosure_source(row_kind: str, row_pk: int) -> dict:
    """Return the plan-side identity for one row that supplied display text."""
    return {"kind": row_kind, "pk": row_pk}


def disclosed_cable(cable) -> dict:
    """Return the fields a planner may record for one visible Cable."""
    return {
        "cable_visible": True,
        "cable": str(cable),
        DISCLOSURE_SOURCE: disclosure_source(CABLE_ROW, cable.pk),
    }


def policy_row_kind(row) -> str:
    """Return the disclosure kind for one supported Cable policy row."""
    from .models import CableClassMapping, CableSegmentOverride

    if isinstance(row, CableClassMapping):
        return CABLE_CLASS_MAPPING_ROW
    if isinstance(row, CableSegmentOverride):
        return CABLE_SEGMENT_OVERRIDE_ROW
    raise TypeError(f"Unsupported Cable policy row: {type(row).__name__}")


def disclosed_policy(row, viewer, display: dict) -> dict:
    """Return policy display values only when the planning viewer may read their row."""
    permission = f"{row._meta.app_label}.view_{row._meta.model_name}"
    if viewer is not None and not viewer.has_perm(permission, row):
        return _redact_policy(display)
    return {**display, DISCLOSURE_SOURCE: disclosure_source(policy_row_kind(row), row.pk)}


def _source_pk(value, row_kind: str) -> int | None:
    if not isinstance(value, dict) or value.get("kind") != row_kind:
        return None
    row_pk = value.get("pk")
    return row_pk if isinstance(row_pk, int) and not isinstance(row_pk, bool) else None


def _row_ids(units) -> dict[str, set[int]]:
    row_ids: dict[str, set[int]] = {
        CABLE_ROW: set(),
        CABLE_CLASS_MAPPING_ROW: set(),
        CABLE_SEGMENT_OVERRIDE_ROW: set(),
    }

    def collect(value) -> None:
        if isinstance(value, dict):
            source = value.get(DISCLOSURE_SOURCE)
            for row_kind, ids in row_ids.items():
                if row_pk := _source_pk(source, row_kind):
                    ids.add(row_pk)
            for child in value.values():
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    for unit in units:
        collect(unit.display)
        for diagnostic in unit.diagnostics:
            collect(diagnostic.display)
    return row_ids


def _redact_cable(display: dict, keys: frozenset[str]) -> dict:
    for key in keys:
        display.pop(key, None)
    display.pop(DISCLOSURE_SOURCE, None)
    display["cable_visible"] = False
    return display


def _redact_policy(display: dict) -> dict:
    display = dict(display)
    for key in ("cable_type", "cable_profile"):
        if key in display:
            display[key] = POLICY_HIDDEN
    if "policy" in display:
        display["policy"] = {}
    display.pop(DISCLOSURE_SOURCE, None)
    return display


def _media_segment(segment: dict, visible_row_ids: dict[str, set[int]]) -> dict:
    source = segment.get(DISCLOSURE_SOURCE)
    row_pk = _source_pk(segment.get(DISCLOSURE_SOURCE), CABLE_ROW)
    if row_pk is not None and row_pk not in visible_row_ids[CABLE_ROW]:
        return {
            "segment_index": segment["segment_index"],
            "retained": segment["retained"],
            "visible": False,
            "origin": "cable",
        }
    for row_kind in (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW):
        row_pk = _source_pk(source, row_kind)
        if row_pk is not None and row_pk not in visible_row_ids[row_kind]:
            return {
                "segment_index": segment["segment_index"],
                "retained": segment["retained"],
                "visible": False,
                "origin": "policy",
            }
    if segment["visible"]:
        return segment
    return {
        "segment_index": segment["segment_index"],
        "retained": segment["retained"],
        "visible": False,
        "origin": segment.get("origin", "cable"),
    }


def _media_message(segments: list[dict]) -> str:
    from .cable_policy import cable_media_family_label, cable_type_label

    statements = []
    for segment in segments:
        position = segment["segment_index"] + 1
        if not segment["visible"]:
            hidden = POLICY_HIDDEN if segment.get("origin") == "policy" else "a Cable you cannot view"
            statements.append(f"segment {position} uses {hidden}")
            continue
        family = cable_media_family_label(segment["family"])
        statement = f"segment {position} is {cable_type_label(segment['cable_type'])} ({family})"
        if segment["retained"]:
            statement += ", on the Cable this import keeps"
        statements.append(statement)
    remedy = (
        "Correct those Cables in NetBox, then re-read."
        if all(segment["retained"] for segment in segments)
        else "Force the segment that states the wrong medium, or correct the source."
    )
    return f"Verified pass-throughs join these segments, and {'; '.join(statements)}. {remedy}"


def _diagnostic(diagnostic, visible_row_ids: dict[str, set[int]]):
    display = diagnostic.to_dict()["display"]
    if diagnostic.code == "cable.media_family_mismatch":
        segments = [_media_segment(segment, visible_row_ids) for segment in display["segments"]]
        display["segments"] = segments
        display["families"] = sorted(
            {_family_label(segment["family"]) for segment in segments if segment["visible"] and segment.get("family")}
        )
        display["message"] = _media_message(segments)
    elif keys := CABLE_DIAGNOSTIC_DISCLOSURES.get(diagnostic.code):
        row_pk = _source_pk(display.get(DISCLOSURE_SOURCE), CABLE_ROW)
        if row_pk is not None and row_pk not in visible_row_ids[CABLE_ROW]:
            display = _redact_cable(display, keys)
    if diagnostic.code in POLICY_DIAGNOSTIC_DISCLOSURES:
        source = display.get(DISCLOSURE_SOURCE)
        for row_kind in (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW):
            row_pk = _source_pk(source, row_kind)
            if row_pk is not None and row_pk not in visible_row_ids[row_kind]:
                display = _redact_policy(display)
    return replace(diagnostic, display=display)


def _family_label(family: str) -> str:
    from .cable_policy import cable_media_family_label

    return cable_media_family_label(family)


def _unit(unit, visible_row_ids: dict[str, set[int]]):
    display = unit.to_dict()["display"]
    trace = display.get("trace")
    if trace is not None:
        logical = trace.get("logical_cable")
        if logical is not None:
            row_pk = _source_pk(logical.get(DISCLOSURE_SOURCE), CABLE_ROW)
            if row_pk is not None and row_pk not in visible_row_ids[CABLE_ROW]:
                trace["logical_cable"] = {"visible": False, "display": "", "description": "", "tags": []}
        for segment in trace.get("segments") or ():
            source = segment.get(DISCLOSURE_SOURCE)
            for row_kind in (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW):
                row_pk = _source_pk(source, row_kind)
                if row_pk is not None and row_pk not in visible_row_ids[row_kind]:
                    segment.update(_redact_policy(segment))
        for policy in trace.get("cable_policies") or ():
            source = policy.get(DISCLOSURE_SOURCE)
            row_pk = _source_pk(source, CABLE_CLASS_MAPPING_ROW)
            if row_pk is not None and row_pk not in visible_row_ids[CABLE_CLASS_MAPPING_ROW]:
                policy.update(_redact_policy(policy))
    diagnostics = tuple(_diagnostic(item, visible_row_ids) for item in unit.diagnostics)
    return replace(unit, diagnostics=diagnostics, display=display)


def present_units(units, viewer) -> tuple:
    """Return presentation copies after one live Cable visibility query."""
    if viewer is None:
        raise TypeError("ReviewWorkspace requires a live viewer.")
    units = tuple(units)
    from dcim.models import Cable

    from .models import CableClassMapping, CableSegmentOverride

    row_ids = _row_ids(units)
    models = {
        CABLE_ROW: Cable,
        CABLE_CLASS_MAPPING_ROW: CableClassMapping,
        CABLE_SEGMENT_OVERRIDE_ROW: CableSegmentOverride,
    }
    visible = {}
    for row_kind, model in models.items():
        ids = row_ids[row_kind]
        visible[row_kind] = (
            set(model.objects.restrict(viewer, "view").filter(pk__in=sorted(ids)).values_list("pk", flat=True))
            if ids
            else set()
        )
    return tuple(_unit(unit, visible) for unit in units)


__all__ = (
    "CABLE_CLASS_MAPPING_ROW",
    "CABLE_DIAGNOSTIC_DISCLOSURES",
    "CABLE_ROW",
    "CABLE_SEGMENT_OVERRIDE_ROW",
    "DISCLOSURE_SOURCE",
    "POLICY_DIAGNOSTIC_DISCLOSURES",
    "POLICY_HIDDEN",
    "disclosed_cable",
    "disclosed_policy",
    "disclosure_source",
    "present_units",
)

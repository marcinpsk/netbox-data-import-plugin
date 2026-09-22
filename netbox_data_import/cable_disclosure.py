# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Apply live row permissions to cached Cable presentation data."""

from dataclasses import replace
from types import MappingProxyType

CABLE_ROW = "dcim.cable"
DISCLOSURE_SOURCE = "disclosure_source"

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


def _source_pk(value, row_kind: str) -> int | None:
    if not isinstance(value, dict) or value.get("kind") != row_kind:
        return None
    row_pk = value.get("pk")
    return row_pk if isinstance(row_pk, int) and not isinstance(row_pk, bool) else None


def _cable_ids(units) -> set[int]:
    cable_ids = set()
    for unit in units:
        trace = unit.display.get("trace") or {}
        logical = trace.get("logical_cable") or {}
        if row_pk := _source_pk(logical.get(DISCLOSURE_SOURCE), CABLE_ROW):
            cable_ids.add(row_pk)
        for diagnostic in unit.diagnostics:
            if row_pk := _source_pk(diagnostic.display.get(DISCLOSURE_SOURCE), CABLE_ROW):
                cable_ids.add(row_pk)
            for segment in diagnostic.display.get("segments") or ():
                if row_pk := _source_pk(segment.get(DISCLOSURE_SOURCE), CABLE_ROW):
                    cable_ids.add(row_pk)
    return cable_ids


def _redact_cable(display: dict, keys: frozenset[str]) -> dict:
    for key in keys:
        display.pop(key, None)
    display.pop(DISCLOSURE_SOURCE, None)
    display["cable_visible"] = False
    return display


def _media_segment(segment: dict, visible_cable_ids: set[int]) -> dict:
    row_pk = _source_pk(segment.get(DISCLOSURE_SOURCE), CABLE_ROW)
    if row_pk is None or row_pk in visible_cable_ids:
        return segment
    return {
        "segment_index": segment["segment_index"],
        "retained": segment["retained"],
        "visible": False,
    }


def _media_message(segments: list[dict]) -> str:
    from .cable_policy import cable_media_family_label, cable_type_label

    statements = []
    for segment in segments:
        position = segment["segment_index"] + 1
        if not segment["visible"]:
            statements.append(f"segment {position} is a Cable you cannot view")
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


def _diagnostic(diagnostic, visible_cable_ids: set[int]):
    display = diagnostic.to_dict()["display"]
    if diagnostic.code == "cable.media_family_mismatch":
        segments = [_media_segment(segment, visible_cable_ids) for segment in display["segments"]]
        display["segments"] = segments
        display["families"] = sorted(
            {_family_label(segment["family"]) for segment in segments if segment["visible"] and segment.get("family")}
        )
        display["message"] = _media_message(segments)
    elif keys := CABLE_DIAGNOSTIC_DISCLOSURES.get(diagnostic.code):
        row_pk = _source_pk(display.get(DISCLOSURE_SOURCE), CABLE_ROW)
        if row_pk is not None and row_pk not in visible_cable_ids:
            display = _redact_cable(display, keys)
    return replace(diagnostic, display=display)


def _family_label(family: str) -> str:
    from .cable_policy import cable_media_family_label

    return cable_media_family_label(family)


def _unit(unit, visible_cable_ids: set[int]):
    display = unit.to_dict()["display"]
    trace = display.get("trace")
    if trace is not None:
        logical = trace.get("logical_cable")
        if logical is not None:
            row_pk = _source_pk(logical.get(DISCLOSURE_SOURCE), CABLE_ROW)
            if row_pk is not None and row_pk not in visible_cable_ids:
                trace["logical_cable"] = {"visible": False, "display": "", "description": "", "tags": []}
    diagnostics = tuple(_diagnostic(item, visible_cable_ids) for item in unit.diagnostics)
    return replace(unit, diagnostics=diagnostics, display=display)


def present_units(units, viewer) -> tuple:
    """Return presentation copies after one live Cable visibility query."""
    if viewer is None:
        raise TypeError("ReviewWorkspace requires a live viewer.")
    units = tuple(units)
    cable_ids = _cable_ids(units)
    visible = set()
    if cable_ids:
        from dcim.models import Cable

        visible = set(
            Cable.objects.restrict(viewer, "view").filter(pk__in=sorted(cable_ids)).values_list("pk", flat=True)
        )
    return tuple(_unit(unit, visible) for unit in units)


__all__ = (
    "CABLE_DIAGNOSTIC_DISCLOSURES",
    "CABLE_ROW",
    "DISCLOSURE_SOURCE",
    "disclosed_cable",
    "disclosure_source",
    "present_units",
)

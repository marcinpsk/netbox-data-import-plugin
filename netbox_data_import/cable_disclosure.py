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
POLICY_VISIBLE = "policy_visible"
POLICY_WRITE_REFUSED = "You cannot change a policy you cannot view."

_REFERENCE_FIELDS = frozenset({"device", "cards", "port", "port_class"})

_DIAGNOSTIC_DISCLOSURE_FIELDS = MappingProxyType(
    {
        "cable.ambiguous_mapped_peer": (_REFERENCE_FIELDS | {"peers"}, frozenset(), frozenset()),
        "cable.attribute_drift": (
            frozenset({"segment_index"}),
            frozenset({"cable", "status", "type", "profile", "label"}),
            frozenset(),
        ),
        "cable.cableclass_unmapped": (frozenset({"segment_index", "cable_class"}), frozenset(), frozenset()),
        "cable.media_family_mismatch": (
            frozenset(),
            frozenset({"segments"}),
            frozenset({"segments"}),
        ),
        "cable.multi_termination_conflict": (
            frozenset({"segment_index", "port"}),
            frozenset({"cable"}),
            frozenset(),
        ),
        "cable.pass_through_not_mapped": (
            _REFERENCE_FIELDS | {"entry", "exit", "mapped"},
            frozenset(),
            frozenset(),
        ),
        "cable.pass_through_verified": (
            _REFERENCE_FIELDS | {"entry", "exit"},
            frozenset(),
            frozenset(),
        ),
        "cable.permission_denied": (
            _REFERENCE_FIELDS | {"permission"},
            frozenset({"cable"}),
            frozenset(),
        ),
        "cable.planned_termination_conflict": (
            frozenset({"segment_index", "termination", "competing_trace"}),
            frozenset(),
            frozenset(),
        ),
        "cable.policy_stale": (frozenset({"segment_index", "cable_class"}), frozenset(), frozenset()),
        "cable.profile_incompatible": (frozenset({"segment_index", "cable_class"}), frozenset(), frozenset()),
        "cable.resolved_segment_conflict": (
            frozenset({"segment_index", "terminations"}),
            frozenset(),
            frozenset({"cable_type", "cable_profile"}),
        ),
        "cable.same_port_continuation": (_REFERENCE_FIELDS | {"peer"}, frozenset(), frozenset()),
        "cable.segment_override_lost": (
            frozenset({"segment_index"}),
            frozenset(),
            frozenset({"cable_type", "cable_profile"}),
        ),
        "cable.segment_reused": (
            frozenset({"segment_index"}),
            frozenset({"cable"}),
            frozenset(),
        ),
        "cable.segment_self_connection": (
            frozenset({"segment_index", "cable_class", "termination"}),
            frozenset(),
            frozenset(),
        ),
        "cable.termination_kind_mismatch": (
            _REFERENCE_FIELDS | {"selected_display_name", "claimed_kind", "selected_kind"},
            frozenset(),
            frozenset(),
        ),
        "cable.termination_occupied": (
            frozenset({"segment_index", "port"}),
            frozenset({"cable"}),
            frozenset(),
        ),
        "cable.termination_unresolved": (
            _REFERENCE_FIELDS | {"matches", "selected_display_name"},
            frozenset(),
            frozenset(),
        ),
        "cable.unsupported_termination_kind": (
            _REFERENCE_FIELDS | {"selected_object_type"},
            frozenset(),
            frozenset(),
        ),
    }
)

CABLE_DIAGNOSTIC_FIELDS = MappingProxyType(
    {code: frozenset().union(*fields) for code, fields in _DIAGNOSTIC_DISCLOSURE_FIELDS.items()}
)

CABLE_DIAGNOSTIC_DISCLOSURES = MappingProxyType(
    {code: fields[1] for code, fields in _DIAGNOSTIC_DISCLOSURE_FIELDS.items() if fields[1]}
)

POLICY_DIAGNOSTIC_DISCLOSURES = MappingProxyType(
    {code: fields[2] for code, fields in _DIAGNOSTIC_DISCLOSURE_FIELDS.items() if fields[2]}
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
    return {
        **display,
        POLICY_VISIBLE: True,
        DISCLOSURE_SOURCE: disclosure_source(policy_row_kind(row), row.pk),
    }


def policy_row_is_disclosed(display: dict, row) -> bool:
    """Return whether a presented value names this policy row as its visible source."""
    return (
        display.get(POLICY_VISIBLE) is True
        and _source_pk(display.get(DISCLOSURE_SOURCE), policy_row_kind(row)) == row.pk
    )


def validate_diagnostic_disclosures(code: str, display: dict) -> None:
    """Reject diagnostic display fields that bypass the shared disclosure vocabulary."""
    if not code.startswith("cable."):
        return
    fields = _DIAGNOSTIC_DISCLOSURE_FIELDS.get(code)
    if fields is None:
        raise ValueError(f"Diagnostic '{code}' has no registered display schema.")
    public, cable, policy = fields
    if code == "cable.media_family_mismatch":
        _validate_media_segments(display)
        return
    metadata = set()
    if cable:
        metadata.update({DISCLOSURE_SOURCE, "cable_visible"})
    if policy:
        metadata.update({DISCLOSURE_SOURCE, POLICY_VISIBLE})
    unknown = set(display) - set(public) - set(cable) - set(policy) - metadata
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Diagnostic '{code}' has unregistered display fields: {names}.")
    source = display.get(DISCLOSURE_SOURCE)
    if set(display) & set(cable) or "cable_visible" in display:
        visible = display.get("cable_visible")
        if not isinstance(visible, bool):
            raise ValueError(f"Diagnostic '{code}' has Cable fields without a visibility flag.")
        if visible and _source_pk(source, CABLE_ROW) is None:
            raise ValueError(f"Diagnostic '{code}' has visible Cable fields without a Cable source.")
        if not visible and source is not None:
            raise ValueError(f"Diagnostic '{code}' has a source for a hidden Cable.")
    if set(display) & set(policy) or POLICY_VISIBLE in display:
        visible = display.get(POLICY_VISIBLE)
        if not isinstance(visible, bool):
            raise ValueError(f"Diagnostic '{code}' has policy fields without a visibility flag.")
        row_kinds = (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW)
        if visible and not any(_source_pk(source, kind) is not None for kind in row_kinds):
            raise ValueError(f"Diagnostic '{code}' has visible policy fields without a policy source.")
        if not visible and source is not None:
            raise ValueError(f"Diagnostic '{code}' has a source for a hidden policy.")


def _validate_media_segments(display: dict) -> None:
    """Reject media observations that omit provenance or add unregistered nested fields."""
    if set(display) != {"segments"} or not isinstance(display["segments"], list):
        raise ValueError("Diagnostic 'cable.media_family_mismatch' has an invalid display schema.")
    base = {"segment_index", "retained", "visible", "origin"}
    disclosed = {"cable_type", "family", DISCLOSURE_SOURCE}
    for segment in display["segments"]:
        if not isinstance(segment, dict) or frozenset(segment) not in {frozenset(base), frozenset(base | disclosed)}:
            raise ValueError("Diagnostic 'cable.media_family_mismatch' has invalid segment fields.")
        origin = segment.get("origin")
        if origin not in {"cable", "policy"} or not isinstance(segment.get("visible"), bool):
            raise ValueError("Diagnostic 'cable.media_family_mismatch' has an invalid segment origin.")
        if segment["visible"] is False:
            if set(segment) != base:
                raise ValueError("Diagnostic 'cable.media_family_mismatch' discloses a hidden segment.")
            continue
        row_kinds = (CABLE_ROW,) if origin == "cable" else (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW)
        if not any(_source_pk(segment.get(DISCLOSURE_SOURCE), kind) is not None for kind in row_kinds):
            raise ValueError("Diagnostic 'cable.media_family_mismatch' has a visible segment without its source.")


def _source_pk(value, row_kind: str) -> int | None:
    if not isinstance(value, dict) or value.get("kind") != row_kind:
        return None
    row_pk = value.get("pk")
    return row_pk if isinstance(row_pk, int) and not isinstance(row_pk, bool) else None


def _source_is_visible(value, row_kinds: tuple[str, ...], visible_row_ids: dict[str, set[int]]) -> bool:
    """Return whether one well-formed source names a row visible in its expected kind."""
    return any(
        (row_pk := _source_pk(value, row_kind)) is not None and row_pk in visible_row_ids[row_kind]
        for row_kind in row_kinds
    )


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
    display[POLICY_VISIBLE] = False
    return display


def _media_segment(segment: dict, visible_row_ids: dict[str, set[int]]) -> dict:
    source = segment.get(DISCLOSURE_SOURCE)
    origin = segment.get("origin")
    row_kinds = (
        (CABLE_ROW,)
        if origin == "cable"
        else (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW)
        if origin == "policy"
        else ()
    )
    if segment.get("visible") is True and _source_is_visible(source, row_kinds, visible_row_ids):
        return segment
    return {
        "segment_index": segment["segment_index"],
        "retained": segment["retained"],
        "visible": False,
        "origin": origin if origin in {"cable", "policy"} else "cable",
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
        if display.get("cable_visible") is True and not _source_is_visible(
            display.get(DISCLOSURE_SOURCE), (CABLE_ROW,), visible_row_ids
        ):
            display = _redact_cable(display, keys)
    if (
        diagnostic.code in POLICY_DIAGNOSTIC_DISCLOSURES
        and display.get(POLICY_VISIBLE) is True
        and not _source_is_visible(
            display.get(DISCLOSURE_SOURCE),
            (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW),
            visible_row_ids,
        )
    ):
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
        if (
            logical is not None
            and logical.get("visible") is True
            and not _source_is_visible(logical.get(DISCLOSURE_SOURCE), (CABLE_ROW,), visible_row_ids)
        ):
            trace["logical_cable"] = {"visible": False, "display": "", "description": "", "tags": []}
        for segment in trace.get("segments") or ():
            if segment.get(POLICY_VISIBLE) is True and not _source_is_visible(
                segment.get(DISCLOSURE_SOURCE),
                (CABLE_CLASS_MAPPING_ROW, CABLE_SEGMENT_OVERRIDE_ROW),
                visible_row_ids,
            ):
                segment.pop(DISCLOSURE_SOURCE, None)
                segment.update(_redact_policy(segment))
        for policy in trace.get("cable_policies") or ():
            if policy.get(POLICY_VISIBLE) is True and not _source_is_visible(
                policy.get(DISCLOSURE_SOURCE), (CABLE_CLASS_MAPPING_ROW,), visible_row_ids
            ):
                policy.pop(DISCLOSURE_SOURCE, None)
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


def redact_deleted_cables(plan_data: dict) -> dict:
    """Return an execution copy whose deleted Cable rows authorize no stored display text."""
    from .plan import ImportPlan

    plan = ImportPlan.from_dict(plan_data)
    visible = _row_ids(plan.units)
    deleted_ids = {
        change.payload["cable_id"]
        for unit in plan.units
        for change in unit.changes
        if change.target_module == "cable" and change.operation == "delete"
    }
    visible[CABLE_ROW].difference_update(deleted_ids)
    units = tuple(_unit(unit, visible) for unit in plan.units)
    return replace(plan, units=units).to_dict()


__all__ = (
    "CABLE_CLASS_MAPPING_ROW",
    "CABLE_DIAGNOSTIC_DISCLOSURES",
    "CABLE_DIAGNOSTIC_FIELDS",
    "CABLE_ROW",
    "CABLE_SEGMENT_OVERRIDE_ROW",
    "DISCLOSURE_SOURCE",
    "POLICY_DIAGNOSTIC_DISCLOSURES",
    "POLICY_HIDDEN",
    "POLICY_VISIBLE",
    "POLICY_WRITE_REFUSED",
    "disclosed_cable",
    "disclosed_policy",
    "disclosure_source",
    "policy_row_is_disclosed",
    "present_units",
    "redact_deleted_cables",
    "validate_diagnostic_disclosures",
)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Apply live row permissions to cached Cable presentation data."""

from dataclasses import replace
from types import MappingProxyType
from typing import NamedTuple

from .field_keys import CABLE_END_KINDS

CABLE_ROW = "dcim.cable"
CABLE_CLASS_MAPPING_ROW = "netbox_data_import.cableclassmapping"
CABLE_SEGMENT_OVERRIDE_ROW = "netbox_data_import.cablesegmentoverride"
DISCLOSURE_SOURCE = "disclosure_source"
POLICY_HIDDEN = "a policy you cannot view"
POLICY_VISIBLE = "policy_visible"
POLICY_WRITE_REFUSED = "You cannot change a policy you cannot view."
TERMINATION_HIDDEN = "a termination you cannot view"
TERMINATION_SOURCES = "termination_sources"

# A segment names its two planned ends apart, so one hidden end leaves the other readable.
SEGMENT_END_SOURCES = MappingProxyType({"left": "left_source", "right": "right_source"})

_REFERENCE_FIELDS = frozenset({"device", "cards", "port", "port_class"})
# These codes replace the source `port` with the NetBox port they resolved.
_RESOLVED_PORT_FIELDS = _REFERENCE_FIELDS - {"port"}


class _DisplaySchema(NamedTuple):
    """The display fields one diagnostic may carry, grouped by the row that authorizes them."""

    public: frozenset
    cable: frozenset = frozenset()
    policy: frozenset = frozenset()
    termination: frozenset = frozenset()


_DIAGNOSTIC_DISCLOSURE_FIELDS = MappingProxyType(
    {
        "cable.ambiguous_mapped_peer": _DisplaySchema(_RESOLVED_PORT_FIELDS, termination=frozenset({"port", "peers"})),
        "cable.attribute_drift": _DisplaySchema(
            frozenset({"segment_index"}), cable=frozenset({"cable", "status", "type", "profile", "label"})
        ),
        "cable.cableclass_unmapped": _DisplaySchema(frozenset({"segment_index", "cable_class"})),
        "cable.incompatible_terminations": _DisplaySchema(
            frozenset({"segment_index", "left_field_key", "right_field_key"}),
            termination=frozenset({"left_model", "right_model"}),
        ),
        "cable.media_family_mismatch": _DisplaySchema(
            frozenset(), cable=frozenset({"segments"}), policy=frozenset({"segments"})
        ),
        "cable.multi_termination_conflict": _DisplaySchema(
            frozenset({"segment_index"}), cable=frozenset({"cable"}), termination=frozenset({"port"})
        ),
        "cable.pass_through_not_mapped": _DisplaySchema(
            _REFERENCE_FIELDS, termination=frozenset({"entry", "exit", "mapped"})
        ),
        "cable.pass_through_verified": _DisplaySchema(_REFERENCE_FIELDS, termination=frozenset({"entry", "exit"})),
        "cable.permission_denied": _DisplaySchema(_REFERENCE_FIELDS | {"permission"}, cable=frozenset({"cable"})),
        "cable.planned_termination_conflict": _DisplaySchema(
            frozenset({"segment_index", "competing_trace"}), termination=frozenset({"termination"})
        ),
        "cable.policy_stale": _DisplaySchema(frozenset({"segment_index", "cable_class"})),
        "cable.profile_incompatible": _DisplaySchema(frozenset({"segment_index", "cable_class"})),
        "cable.resolved_segment_conflict": _DisplaySchema(
            frozenset({"segment_index"}),
            policy=frozenset({"cable_type", "cable_profile"}),
            termination=frozenset({"terminations"}),
        ),
        "cable.same_port_continuation": _DisplaySchema(_RESOLVED_PORT_FIELDS, termination=frozenset({"port", "peer"})),
        "cable.segment_override_lost": _DisplaySchema(
            frozenset({"segment_index"}), policy=frozenset({"cable_type", "cable_profile"})
        ),
        "cable.segment_reused": _DisplaySchema(frozenset({"segment_index"}), cable=frozenset({"cable"})),
        "cable.segment_self_connection": _DisplaySchema(
            frozenset({"segment_index", "cable_class"}), termination=frozenset({"termination"})
        ),
        "cable.termination_kind_mismatch": _DisplaySchema(
            _REFERENCE_FIELDS | {"selected_display_name", "claimed_kind", "selected_object_type"}
        ),
        "cable.termination_occupied": _DisplaySchema(
            frozenset({"segment_index"}), cable=frozenset({"cable"}), termination=frozenset({"port"})
        ),
        "cable.termination_unresolved": _DisplaySchema(_REFERENCE_FIELDS | {"selected_display_name"}),
        "cable.unsupported_termination_kind": _DisplaySchema(_REFERENCE_FIELDS | {"selected_object_type"}),
    }
)

CABLE_DIAGNOSTIC_FIELDS = MappingProxyType(
    {code: frozenset().union(*fields) for code, fields in _DIAGNOSTIC_DISCLOSURE_FIELDS.items()}
)

CABLE_DIAGNOSTIC_DISCLOSURES = MappingProxyType(
    {code: fields.cable for code, fields in _DIAGNOSTIC_DISCLOSURE_FIELDS.items() if fields.cable}
)

POLICY_DIAGNOSTIC_DISCLOSURES = MappingProxyType(
    {code: fields.policy for code, fields in _DIAGNOSTIC_DISCLOSURE_FIELDS.items() if fields.policy}
)

TERMINATION_DIAGNOSTIC_DISCLOSURES = MappingProxyType(
    {code: fields.termination for code, fields in _DIAGNOSTIC_DISCLOSURE_FIELDS.items() if fields.termination}
)


def disclosure_source(row_kind: str, row_pk: int) -> dict:
    """Return the plan-side identity for one row that supplied display text."""
    return {"kind": row_kind, "pk": row_pk}


def termination_sources(*terminations) -> dict:
    """Return the sources a display needs when it names the given resolved terminations."""
    return {TERMINATION_SOURCES: [disclosure_source(label, object_id) for label, object_id in terminations]}


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


def disclosed_policy(row, visible: bool, display: dict) -> dict:
    """Return policy display values only when the planning viewer may read their row."""
    if not visible:
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
    public, cable, policy, termination = fields
    if code == "cable.media_family_mismatch":
        _validate_media_segments(display)
        return
    metadata = set()
    if cable:
        metadata.update({DISCLOSURE_SOURCE, "cable_visible"})
    if policy:
        metadata.update({DISCLOSURE_SOURCE, POLICY_VISIBLE})
    if termination:
        metadata.add(TERMINATION_SOURCES)
        if not _well_formed_termination_sources(display.get(TERMINATION_SOURCES)):
            raise ValueError(f"Diagnostic '{code}' names terminations without their sources.")
    unknown = set(display) - set(public) - set(cable) - set(policy) - set(termination) - metadata
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Diagnostic '{code}' has unregistered display fields: {names}.")
    _validate_row_flags(code, display, cable, policy)


def _validate_row_flags(code: str, display: dict, cable: frozenset, policy: frozenset) -> None:
    """Reject Cable and policy fields whose visibility flag and source disagree."""
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


def _source_is_visible(value, row_kinds, visible_row_ids: dict[str, set[int]]) -> bool:
    """Return whether one well-formed source names a row visible in its expected kind."""
    return any(
        (row_pk := _source_pk(value, row_kind)) is not None and row_pk in visible_row_ids.get(row_kind, ())
        for row_kind in row_kinds
    )


def _well_formed_termination_sources(sources) -> bool:
    """Return whether *sources* is a nonempty list of sources that each name a Cable End Kind row."""
    return (
        isinstance(sources, list)
        and bool(sources)
        and all(any(_source_pk(source, kind) is not None for kind in CABLE_END_KINDS) for source in sources)
    )


def _terminations_are_visible(sources, visible_row_ids: dict[str, set[int]]) -> bool:
    """Return whether every termination one display names is live and visible."""
    return _well_formed_termination_sources(sources) and all(
        _source_is_visible(source, CABLE_END_KINDS, visible_row_ids) for source in sources
    )


def _identity_key(identity) -> tuple[str, int] | None:
    """Return the Cable End Kind row one plan identity names, or None for any other identity."""
    label, _, object_id = str(identity).rpartition(":")
    if label not in CABLE_END_KINDS or not object_id.isdigit():
        return None
    return label, int(object_id)


def _row_sources(value: dict) -> list:
    """Return every row source one display mapping carries."""
    sources = [value.get(DISCLOSURE_SOURCE), *(value.get(key) for key in SEGMENT_END_SOURCES.values())]
    listed = value.get(TERMINATION_SOURCES)
    # A frozen plan holds a list as a tuple.
    if isinstance(listed, (list, tuple)):
        sources.extend(listed)
    return sources


def _row_ids(units) -> dict[str, set[int]]:
    row_ids: dict[str, set[int]] = {
        CABLE_ROW: set(),
        CABLE_CLASS_MAPPING_ROW: set(),
        CABLE_SEGMENT_OVERRIDE_ROW: set(),
        **{kind: set() for kind in sorted(CABLE_END_KINDS)},
    }

    def collect(value) -> None:
        if isinstance(value, dict):
            for source in _row_sources(value):
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
            for identity in diagnostic.identities:
                if key := _identity_key(identity):
                    row_ids[key[0]].add(key[1])
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


def _redact_terminations(display: dict, keys: frozenset[str]) -> dict:
    for key in keys & set(display):
        display[key] = [] if isinstance(display[key], list) else TERMINATION_HIDDEN
    display.pop(TERMINATION_SOURCES, None)
    return display


def _visible_identities(identities, visible_row_ids: dict[str, set[int]]) -> tuple:
    """Return the identities a viewer may read: a hidden or deleted termination drops out."""
    return tuple(
        identity
        for identity in identities
        if (key := _identity_key(identity)) is None or key[1] in visible_row_ids.get(key[0], ())
    )


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
    keys = TERMINATION_DIAGNOSTIC_DISCLOSURES.get(diagnostic.code)
    if keys and not _terminations_are_visible(display.get(TERMINATION_SOURCES), visible_row_ids):
        display = _redact_terminations(display, keys)
    return replace(diagnostic, display=display, identities=_visible_identities(diagnostic.identities, visible_row_ids))


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
        for field in trace.get("terminations") or ():
            if field.get("selected") and not _source_is_visible(
                field.get(DISCLOSURE_SOURCE), CABLE_END_KINDS, visible_row_ids
            ):
                field.pop(DISCLOSURE_SOURCE, None)
                field.update(selected=TERMINATION_HIDDEN, selected_type="")
        for segment in trace.get("segments") or ():
            # Only a planned segment names NetBox ports; an unplanned one repeats the source text.
            if not segment.get("segment_key"):
                continue
            for end, source_key in SEGMENT_END_SOURCES.items():
                if not _source_is_visible(segment.get(source_key), CABLE_END_KINDS, visible_row_ids):
                    segment.pop(source_key, None)
                    segment[end] = TERMINATION_HIDDEN
    diagnostics = tuple(_diagnostic(item, visible_row_ids) for item in unit.diagnostics)
    return replace(unit, diagnostics=diagnostics, display=display)


def present_units(units, viewer) -> tuple:
    """Return presentation copies after one live visibility query per referenced row kind."""
    if viewer is None:
        raise TypeError("ReviewWorkspace requires a live viewer.")
    units = tuple(units)
    from django.apps import apps

    row_ids = _row_ids(units)
    visible = {}
    for row_kind, ids in row_ids.items():
        model = apps.get_model(row_kind)
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
    "SEGMENT_END_SOURCES",
    "TERMINATION_DIAGNOSTIC_DISCLOSURES",
    "TERMINATION_HIDDEN",
    "TERMINATION_SOURCES",
    "disclosed_cable",
    "disclosed_policy",
    "disclosure_source",
    "policy_row_is_disclosed",
    "present_units",
    "redact_deleted_cables",
    "termination_sources",
    "validate_diagnostic_disclosures",
)

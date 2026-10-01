# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Map opaque source Location paths to NetBox Locations inside one permission-scoped import target."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .values import identity_text

UNMAPPED = "unmapped"
MAPPED = "mapped"
STALE = "stale"
HIDDEN = "hidden"

NO_VISIBLE_LOCATION = "No Location in this Site is visible to you."
SAVE_PERMISSION_REFUSED = "You do not have permission to save this Location mapping."
CLEAR_PERMISSION_REFUSED = "You do not have permission to clear this Location mapping."
STALE_REASON = "The mapped Location is no longer available at this import target. Choose it again."


def source_location_key(path: Any) -> str:
    """Return the profile-wide identity of one opaque source Location path."""
    return identity_text(path)


@dataclass(frozen=True)
class LocationMapping:
    """How one source Location path maps for one actor at one import target."""

    key: str
    state: str
    # The stored row, only when the actor may view it.
    row: Any | None
    # The mapped Location, only while it is visible and inside the selected Site.
    location: Any | None


def site_locations(reader):
    """Return the Locations the actor may view inside the selected Site."""
    locations = reader.locations()
    return locations.filter(site=reader.site) if reader.site is not None else locations


def trace_location_mappings(*, profile, reader, keys: Iterable[str]) -> dict[str, LocationMapping]:
    """Return the mapping state of every source Location key, read in bulk inside the actor's scope."""
    from .models import TraceLocationResolution, index_digest

    keys = tuple(dict.fromkeys(key for key in keys if key))
    if not keys:
        return {}
    rows = TraceLocationResolution.objects.filter(
        profile=profile,
        source_location_key_digest__in=[index_digest(key) for key in keys],
    )
    stored = {}
    for row in rows:
        if row.source_location_key not in keys:
            raise ValueError("A Trace Location Resolution digest does not match its source Location key.")
        stored[row.source_location_key] = row
    visible_rows = (
        set(stored)
        if reader.actor is None
        else set(
            TraceLocationResolution.objects.restrict(reader.actor, "view")
            .filter(pk__in=[row.pk for row in stored.values()])
            .values_list("source_location_key", flat=True)
        )
    )
    locations = {
        location.pk: location
        for location in site_locations(reader).filter(pk__in=[row.selected_location_id for row in stored.values()])
    }
    mappings = {}
    for key in keys:
        row = stored.get(key)
        if row is None:
            mappings[key] = LocationMapping(key=key, state=UNMAPPED, row=None, location=None)
        elif key not in visible_rows:
            mappings[key] = LocationMapping(key=key, state=HIDDEN, row=None, location=None)
        elif row.selected_location_id not in locations:
            mappings[key] = LocationMapping(key=key, state=STALE, row=row, location=None)
        else:
            mappings[key] = LocationMapping(
                key=key, state=MAPPED, row=row, location=locations[row.selected_location_id]
            )
    return mappings


@dataclass(frozen=True)
class LocationMappingRow:
    """One source Location path as the workspace lists it, with the reason each action cannot run."""

    key: str
    path: str
    state: str
    state_label: str
    state_style: str
    location: str
    location_id: int | None
    detail: str
    clearable: bool
    save_reason: str
    clear_reason: str


_STATE_PRESENTATION = {
    UNMAPPED: ("unmapped", "unknown"),
    MAPPED: ("mapped", "manual"),
    STALE: ("stale", "stale"),
    HIDDEN: ("a mapping you cannot view", "unknown"),
}


def present_location_mappings(*, profile, viewer, reader, paths: Mapping[str, str], has_locations: bool):
    """Return one workspace row per source Location path, each action permission-checked for *viewer*."""
    from utilities.permissions import get_permission_for_model

    from .cable_disclosure import POLICY_WRITE_REFUSED
    from .models import TraceLocationResolution, index_digest
    from .object_permissions import assess_permission_scoped_save_option

    mappings = trace_location_mappings(profile=profile, reader=reader, keys=paths)
    delete_permission = get_permission_for_model(TraceLocationResolution, "delete")
    rows = []
    for key, path in paths.items():
        mapping = mappings[key]
        label, style = _STATE_PRESENTATION[mapping.state]
        if mapping.state == HIDDEN:
            save_reason = clear_reason = POLICY_WRITE_REFUSED
        else:
            assessment = assess_permission_scoped_save_option(
                viewer,
                TraceLocationResolution,
                {"profile": profile, "source_location_key": key, "source_location_key_digest": index_digest(key)},
                {"selected_location_id": 1, "selected_display_name": "Pending Location mapping"},
                unknown_fields={"selected_location_id", "selected_display_name"},
            )
            save_reason = (
                NO_VISIBLE_LOCATION if not has_locations else "" if assessment.allowed else SAVE_PERMISSION_REFUSED
            )
            clear_reason = (
                CLEAR_PERMISSION_REFUSED
                if mapping.row is not None
                and viewer is not None
                and not viewer.has_perm(delete_permission, mapping.row)
                else ""
            )
        rows.append(
            LocationMappingRow(
                key=key,
                path=path,
                state=mapping.state,
                state_label=label,
                state_style=style,
                location=mapping.location.name if mapping.location is not None else "",
                location_id=mapping.location.pk if mapping.location is not None else None,
                detail=STALE_REASON if mapping.state == STALE else "",
                clearable=mapping.state != UNMAPPED,
                save_reason=save_reason,
                clear_reason=clear_reason,
            )
        )
    return rows


__all__ = (
    "HIDDEN",
    "MAPPED",
    "STALE",
    "UNMAPPED",
    "LocationMapping",
    "LocationMappingRow",
    "present_location_mappings",
    "site_locations",
    "source_location_key",
    "trace_location_mappings",
)

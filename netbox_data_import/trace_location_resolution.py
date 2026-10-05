# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Map source Location paths and their prefixes to NetBox Locations inside one permission-scoped import target."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .identity import identity_text, matching_search

PATH_SEPARATOR = ">>"

UNMAPPED = "unmapped"
MAPPED = "mapped"
STALE = "stale"
HIDDEN = "hidden"

NO_VISIBLE_LOCATION = "No Location in this Site is visible to you."
SAVE_PERMISSION_REFUSED = "You do not have permission to save this Location mapping."
CLEAR_PERMISSION_REFUSED = "You do not have permission to clear this Location mapping."
STALE_REASON = "The mapped Location is no longer available at this import target. Choose it again."
DIGEST_MISMATCH = "A Trace Location Resolution digest does not match its source Location key."


def source_location_key(path: str) -> str:
    """Return the profile-wide identity of one source Location path or prefix."""
    return identity_text(path)


def location_prefixes(path: str) -> tuple[tuple[str, str], ...]:
    """Return each prefix of one source Location path as (key, source text), shortest first."""
    segments = path.split(PATH_SEPARATOR)
    prefixes = [
        (source_location_key(text), text)
        for end in range(1, len(segments))
        if identity_text(segments[end - 1])
        for text in (PATH_SEPARATOR.join(segments[:end]),)
    ]
    # The full path is its own last prefix even when its last segment is blank.
    if key := source_location_key(path):
        prefixes.append((key, path))
    return tuple(prefixes)


def location_prefix_spellings(paths: Mapping[str, str]) -> dict[str, str]:
    """Return every prefix key of the paths, spelled as the first path in key order that carries it."""
    spellings: dict[str, str] = {}
    for path_key in sorted(paths):
        for key, text in location_prefixes(paths[path_key]):
            spellings.setdefault(key, text)
    return spellings


@dataclass(frozen=True)
class LocationMapping:
    """How one stored source Location row maps for one actor at one import target."""

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


@dataclass(frozen=True)
class LocationCandidate:
    """One visible Location the picker offers, with its parent's name when the actor may view it."""

    location: Any
    parent: str


@dataclass(frozen=True)
class LocationCandidatePage:
    """One bounded page of Location candidates and its uncapped total."""

    candidates: tuple[LocationCandidate, ...]
    total: int


def eligible_trace_locations(reader, *, search: str = "", limit: int, offset: int = 0) -> LocationCandidatePage:
    """Return one bounded page of the selected Site's visible Locations, searched by normalized name."""
    locations = matching_search(site_locations(reader), search).order_by("name", "pk")
    total = locations.count()
    page = tuple(locations[offset : offset + limit]) if offset < total else ()
    # Two Locations can share a name under different parents, so the visible parent tells them apart.
    parents = dict(
        site_locations(reader)
        .filter(pk__in={location.parent_id for location in page if location.parent_id})
        .values_list("pk", "name")
    )
    return LocationCandidatePage(
        candidates=tuple(LocationCandidate(location, parents.get(location.parent_id, "")) for location in page),
        total=total,
    )


def trace_location_mappings(*, profile, reader, keys: Iterable[str]) -> dict[str, LocationMapping]:
    """Return the own row state of every source Location key, read in bulk inside the actor's scope."""
    from .models import TraceLocationResolution, index_digest

    keys = tuple(dict.fromkeys(key for key in keys if key))
    if not keys:
        return {}
    requested = frozenset(keys)
    rows = TraceLocationResolution.objects.filter(
        profile=profile,
        source_location_key_digest__in=[index_digest(key) for key in keys],
    )
    stored = {}
    for row in rows:
        # Two requested prefixes can name each other's keys, so the key alone does not prove the digest.
        if (
            index_digest(row.source_location_key) != row.source_location_key_digest
            or row.source_location_key not in requested
        ):
            raise ValueError(DIGEST_MISMATCH)
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


def stored_location_row(profile, key: str):
    """Return the row stored for one source Location key, refusing a row whose digest names another key."""
    from .models import TraceLocationResolution, index_digest

    row = TraceLocationResolution.objects.filter(profile=profile, source_location_key_digest=index_digest(key)).first()
    if row is not None and row.source_location_key != key:
        raise ValueError(DIGEST_MISMATCH)
    return row


def decided_location_mappings(*, profile, reader, paths: Iterable[str]) -> dict[str, LocationMapping]:
    """Return, by path key, the mapping of the longest prefix of each path that has a stored row."""
    prefixes = {key: location_prefixes(path) for path in paths if (key := source_location_key(path))}
    own = trace_location_mappings(
        profile=profile, reader=reader, keys=(key for chain in prefixes.values() for key, _text in chain)
    )
    return {path_key: _deciding(own, [key for key, _text in chain]) for path_key, chain in prefixes.items()}


def _deciding(own: Mapping[str, LocationMapping], chain: list[str]) -> LocationMapping:
    """Return the own mapping of the longest prefix in *chain* that has a stored row, else the last one's."""
    # A stale or hidden row decides too, so the search never passes it to a shorter row.
    return next((own[key] for key in reversed(chain) if own[key].state != UNMAPPED), own[chain[-1]])


@dataclass(frozen=True)
class LocationState:
    """How the workspace shows one mapping state; a hidden row discloses nothing past its existence."""

    state: str
    label: str
    style: str
    location: str
    detail: str


@dataclass(frozen=True)
class LocationSegment:
    """One segment of a tree node's text, which opens the picker for the prefix ending at it."""

    key: str
    text: str
    label: str
    save_reason: str


@dataclass(frozen=True)
class LocationNode:
    """One row of the workspace tree of source Location prefixes, listed flat in page order."""

    index: int
    parent: int | None
    depth: int
    children: tuple[int, ...]
    key: str
    path: str
    segments: tuple[LocationSegment, ...]
    own: LocationState
    # The state of the longest shorter prefix with a stored row, only when this node has no row of its own.
    inherited: LocationState | None
    path_count: int
    expanded: bool
    shown: bool
    clearable: bool
    clear_reason: str
    reasons: tuple[str, ...]

    @property
    def save_reason(self) -> str:
        """Return why the prefix this node ends at cannot be saved, or an empty string."""
        return self.segments[-1].save_reason

    @property
    def inherited_marker(self) -> str:
        """Return the muted text that names the inherited state; a hidden row discloses only that it exists."""
        return _INHERITED_MARKER[self.inherited.state] if self.inherited is not None else ""


_INHERITED_MARKER = {
    MAPPED: "inherited",
    STALE: "inherited stale",
    HIDDEN: "inherited, a mapping you cannot view",
}

_STATE_PRESENTATION = {
    UNMAPPED: ("unmapped", "unknown"),
    MAPPED: ("mapped", "manual"),
    STALE: ("stale", "stale"),
    HIDDEN: ("a mapping you cannot view", "unknown"),
}


def _present_state(mapping: LocationMapping) -> LocationState:
    label, style = _STATE_PRESENTATION[mapping.state]
    return LocationState(
        state=mapping.state,
        label=label,
        style=style,
        location=mapping.location.name if mapping.location is not None else "",
        detail=STALE_REASON if mapping.state == STALE else "",
    )


@dataclass
class _PrefixTree:
    """Every prefix the batch paths carry, keyed and spelled once, with its child prefixes and path count."""

    spellings: dict[str, str]
    segments: dict[str, str]
    children: dict[str | None, dict[str, None]]
    counts: dict[str, int]
    ends: set[str]

    @classmethod
    def of(cls, paths: Mapping[str, str]) -> _PrefixTree:
        tree = cls(spellings={}, segments={}, children={None: {}}, counts={}, ends=set(paths))
        for path_key in sorted(paths):
            parent: str | None = None
            previous: str | None = None
            for key, text in location_prefixes(paths[path_key]):
                if key not in tree.spellings:
                    tree.spellings[key] = text
                    tree.segments[key] = _segment_text(text, previous)
                    tree.children.setdefault(parent, {})[key] = None
                    tree.children[key] = {}
                tree.counts[key] = tree.counts.get(key, 0) + 1
                parent, previous = key, text
        return tree


def _segment_text(text: str, parent_text: str | None) -> str:
    """Return the text a prefix adds to its parent prefix, without the separator between them."""
    if parent_text is None:
        return text.strip()
    added = text[len(parent_text) :]
    return added.removeprefix(PATH_SEPARATOR).strip() or added.strip()


def present_location_tree(
    *, profile, viewer, reader, paths: Mapping[str, str], has_locations: bool
) -> tuple[LocationNode, ...]:
    """Return the workspace tree of the batch paths' prefixes, each action permission-checked for *viewer*."""
    from .models import TraceLocationResolution, index_digest
    from .object_permissions import POLICY_WRITE_REFUSED, assess_loaded_save_option

    tree = _PrefixTree.of(paths)
    own = trace_location_mappings(profile=profile, reader=reader, keys=tree.spellings)
    visible_rows = [mapping.row.pk for mapping in own.values() if mapping.row is not None]
    deletable = set(
        TraceLocationResolution.objects.restrict(viewer, "delete")
        .filter(pk__in=visible_rows)
        .values_list("pk", flat=True)
        if visible_rows
        else ()
    )

    def save_reason(key: str) -> str:
        mapping = own[key]
        if mapping.state == HIDDEN:
            return POLICY_WRITE_REFUSED
        if not has_locations:
            return NO_VISIBLE_LOCATION
        assessment = assess_loaded_save_option(
            viewer,
            TraceLocationResolution,
            {"profile": profile, "source_location_key": key, "source_location_key_digest": index_digest(key)},
            {
                "source_location_path": tree.spellings[key],
                "selected_location_id": 1,
                "selected_display_name": "Pending Location mapping",
            },
            current=mapping.row,
            unknown_fields={"selected_location_id", "selected_display_name"},
        )
        return "" if assessment.allowed else SAVE_PERMISSION_REFUSED

    nodes: list[LocationNode] = []
    children: dict[int, list[int]] = {}
    # (first prefix, depth, parent row, row that decides above, shown), popped in page order.
    pending: list[tuple[str, int, int | None, LocationMapping | None, bool]] = [
        (root, 0, None, None, True) for root in sorted(tree.children[None], reverse=True)
    ]
    while pending:
        start, depth, parent, inherited, shown = pending.pop()
        chain = [start]
        # A node ends at a branch, at a stored row, or at a batch path.
        while len(tree.children[chain[-1]]) == 1 and own[chain[-1]].state == UNMAPPED and chain[-1] not in tree.ends:
            chain.append(next(iter(tree.children[chain[-1]])))
        key = chain[-1]
        mapping = own[key]
        stored = mapping.state != UNMAPPED
        segments = tuple(
            LocationSegment(
                key=item, text=tree.segments[item], label=tree.spellings[item], save_reason=save_reason(item)
            )
            for item in chain
        )
        if mapping.state == HIDDEN:
            clear_reason = POLICY_WRITE_REFUSED
        elif mapping.row is not None and mapping.row.pk not in deletable:
            clear_reason = CLEAR_PERMISSION_REFUSED
        else:
            clear_reason = ""
        index = len(nodes)
        if parent is not None:
            children[parent].append(index)
        children[index] = []
        expanded = bool(tree.children[key]) and depth == 0 and mapping.state != MAPPED
        nodes.append(
            LocationNode(
                index=index,
                parent=parent,
                depth=depth,
                children=(),
                key=key,
                path=tree.spellings[key],
                segments=segments,
                own=_present_state(mapping),
                inherited=None if stored or inherited is None else _present_state(inherited),
                path_count=tree.counts[key],
                expanded=expanded,
                shown=shown,
                clearable=stored,
                clear_reason=clear_reason,
                reasons=tuple(
                    dict.fromkeys(
                        reason for reason in (*(item.save_reason for item in segments), clear_reason) if reason
                    )
                ),
            )
        )
        below = mapping if stored else inherited
        pending.extend(
            (child, depth + 1, index, below, shown and expanded) for child in sorted(tree.children[key], reverse=True)
        )
    return tuple(replace(node, children=tuple(children[node.index])) for node in nodes)


__all__ = (
    "HIDDEN",
    "MAPPED",
    "PATH_SEPARATOR",
    "STALE",
    "UNMAPPED",
    "LocationCandidate",
    "LocationCandidatePage",
    "LocationMapping",
    "LocationNode",
    "LocationSegment",
    "LocationState",
    "decided_location_mappings",
    "eligible_trace_locations",
    "location_prefix_spellings",
    "location_prefixes",
    "present_location_tree",
    "site_locations",
    "source_location_key",
    "stored_location_row",
    "trace_location_mappings",
)

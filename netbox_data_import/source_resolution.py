# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Apply saved Source Resolutions to pristine flat source rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.core.exceptions import ValidationError

from .models import ImportProfile, validate_source_resolution_fields
from .values import source_text as _str_val


def _build_source_to_targets_map(profile: ImportProfile) -> dict[str, list[str]]:
    """Return a source-column keyed map of all target fields that column feeds."""
    source_to_targets: dict[str, list[str]] = {}
    for mapping in profile.column_mappings.all():
        source_to_targets.setdefault(mapping.source_column, []).append(mapping.target_field)
    return source_to_targets


def _clear_resolved_conflicts(row_dict: dict[str, Any], resolved_fields: Mapping) -> None:
    """Remove conflicts for fields overridden by saved resolutions."""
    for resolved_field in resolved_fields:
        row_dict.get("_conflicts", {}).pop(resolved_field, None)
    if not row_dict.get("_conflicts"):
        row_dict.pop("_conflicts", None)


def _apply_one_resolution(
    row_dict: dict,
    resolution,
    source_to_targets: dict[str, list[str]],
    profile: ImportProfile,
) -> None:
    """Apply one saved Source Resolution and clear its ignored mapped values."""
    resolved_fields = resolution.resolved_fields
    try:
        validate_source_resolution_fields(profile, resolution.source_column, resolved_fields)
    except ValidationError:
        return
    row_dict.update(resolved_fields)
    _clear_resolved_conflicts(row_dict, resolved_fields)

    candidate_targets = list(source_to_targets.get(resolution.source_column, []))
    if resolution.source_column not in candidate_targets:
        candidate_targets.append(resolution.source_column)
    for target_field in candidate_targets:
        if target_field in resolved_fields:
            continue
        current = row_dict.get(target_field)
        if current is None:
            continue
        if str(current) == str(resolution.original_value):
            row_dict[target_field] = None


def derive_effective_rows(rows: list[dict], profile) -> list[dict]:
    """Return new effective rows derived from pristine source rows and saved resolutions."""
    resolutions_by_source_id: dict[str, list] = {}
    # Source columns give resolutions with one source ID a stable override order.
    for resolution in profile.source_resolutions.order_by("source_id", "source_column"):
        resolutions_by_source_id.setdefault(str(resolution.source_id), []).append(resolution)

    if not resolutions_by_source_id:
        return rows

    source_to_targets = _build_source_to_targets_map(profile)

    result = []
    for row in rows:
        source_id = _str_val(row.get("source_id"))
        if source_id and source_id in resolutions_by_source_id:
            row = dict(row)
            if "_conflicts" in row:
                row["_conflicts"] = dict(row["_conflicts"])
            for resolution in resolutions_by_source_id[source_id]:
                _apply_one_resolution(row, resolution, source_to_targets, profile)
        result.append(row)
    return result


__all__ = ("derive_effective_rows",)

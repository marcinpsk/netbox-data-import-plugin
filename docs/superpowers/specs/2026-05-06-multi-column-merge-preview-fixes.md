# Multi-Column Merge, Full Preview Fields, Numeric Comparison Fix

**Date:** 2026-05-06
**Branch target:** `feat/per-row-sync`

---

## Overview

Three related improvements to the import preview workflow:

1. **Multi-column merge** — allow multiple source columns to map to the same target field, with conflict detection and manual resolution.
2. **Show all fields in sync modal** — the "Sync to NetBox" confirmation modal only shows 7 hardcoded fields; extend it to show all mapped fields including custom fields.
3. **Numeric comparison normalization** — `_field_diff` compares some numeric values as strings, causing false positives (e.g. `35.0` vs `35` shows as different).

---

## Feature 1: Multi-Column Merge

### Problem

`ColumnMapping` has `UniqueConstraint(fields=["profile", "target_field"])`, so only one source column can map to each target field. Users with data spread across columns (e.g. 'Service Tag' and 'Serial Number' both containing serial-like values) cannot configure a merge.

### Design

#### Model

Remove `UniqueConstraint(["profile", "target_field"])` from `ColumnMapping`. The `(profile, source_column)` combination remains unique (one source column cannot be mapped twice in the same profile). A migration is required.

No new model or priority field is needed.

#### Engine — `parse_file` and `apply_column_mappings`

Refactor `col_map` from `{source_col: target_field}` to `{target_field: [source_col, …]}` (grouped by target).

When building each row dict:

```
for target_field, source_cols in col_map.items():
    values = {col: row[col] for col in source_cols if row[col] is not None and str(row[col]).strip()}
    if len(values) == 0:
        row_dict[target_field] = None
    elif len(set(values.values())) == 1:
        row_dict[target_field] = next(iter(values.values()))  # one value or all identical
    else:
        row_dict[target_field] = None
        row_dict.setdefault("_conflicts", {})[target_field] = values  # {col_name: value}
```

After applying saved `SourceResolution` records (existing `row_dict.update(res.resolved_fields)`), any conflict field covered by the resolution is cleared from `_conflicts`:

```python
for field in res.resolved_fields:
    row_dict.get("_conflicts", {}).pop(field, None)
```

`apply_column_mappings` (used after quick-add column mapping) applies the same grouped logic.

#### Session Storage

`_conflicts` is a plain dict key in the row dict; it serializes naturally with the existing import_rows session storage. No schema change.

#### Preview Display

In `_preview_device_row` and `_preview_rack_row`: pass `row.get("_conflicts", {})` through to `RowResult.extra_data["conflicts"]`.

In `import_preview.html`:
- Rows with non-empty `extra_data.conflicts`: show an inline warning badge `⚠ N conflict(s)` (amber/warning color).
- Clicking the badge opens a conflict resolution modal.

#### Conflict Resolution Modal

A new modal (or reuse of the existing `resolveRowModal` pattern):
- Title: "Resolve field conflict — [device name]"
- One section per conflicting target field showing: field name, all competing source columns and their values, a "Use this" button per candidate.
- Clicking "Use this" POSTs to the existing `SaveResolutionView` (or equivalent) with `source_id`, `target_field`, and chosen `value`. This stores a `SourceResolution` record.
- After saving, the preview refreshes (existing `htmx`/page-reload pattern).

On next preview load, the saved resolution applies automatically and the conflict disappears.

#### UI — Column Mapping Admin

The column mapping admin (profile edit form) should:
- No longer reject saving two mappings with the same `target_field` at the form/view level.
- Optionally: show a visual indicator when multiple mappings share a target field.

---

## Feature 2: Show All Fields in Sync Modal

### Problem

The "Sync to NetBox" confirmation modal (`syncRowModal`) shows only 7 hardcoded fields (Name, Rack, Source ID, Manufacturer, Model, Asset tag, Rack type). Fields like serial, u_position, u_height, face, airflow, status, and any custom fields mapped via `extra_json:` are invisible.

### Design

#### Engine — `_preview_device_row`

Add the currently missing standard fields to `extra_data`:
- `serial` (already available as function arg, stored as `source_serial` — rename or add `serial` key)
- `face` (function arg `device_face`)
- `airflow` (function arg `device_airflow`)
- `status` (function arg `device_status`)
- `u_position` and `u_height` are already in `extra_data`
- `extra_columns`: populate from `row.get("_extra_columns", {})` — the dict of unmapped/custom-field values

#### Template — sync button data attributes

Add to the `ndi-sync-row-btn` button:
```html
data-serial="{{ row.extra_data.serial|default:'' }}"
data-u-position="{{ row.extra_data.u_position|default:'' }}"
data-u-height="{{ row.extra_data.u_height|default:'' }}"
data-face="{{ row.extra_data.face|default:'' }}"
data-airflow="{{ row.extra_data.airflow|default:'' }}"
data-status="{{ row.extra_data.status|default:'' }}"
data-extra-columns='{{ row.extra_data.extra_columns|default:"{}" | tojson }}'
```

For the extra_columns JSON blob, use a template filter or `json_script` tag.

#### JS Modal — `syncRowModal`

Replace the hardcoded 7-item `fieldDefs` array with:

1. A comprehensive standard field list (all standard target fields with human-readable labels):
   ```js
   var FIELD_LABELS = {
     device_name: 'Name', rack_name: 'Rack', source_id: 'Source ID',
     source_make: 'Manufacturer', source_model: 'Model', asset_tag: 'Asset Tag',
     rack_type: 'Rack Type', serial: 'Serial', u_position: 'U Position',
     u_height: 'U Height', face: 'Face', airflow: 'Airflow', status: 'Status',
   };
   ```
2. Build `fieldDefs` from `btn.dataset.*` for all standard fields.
3. After standard fields, iterate over `JSON.parse(btn.dataset.extraColumns || '{}')` and render each key-value pair using the raw key as label.

Fields with empty/falsy values continue to be skipped.

---

## Feature 3: Numeric Comparison Normalization

### Problem

`_field_diff` compares most fields as `str(val)`. This causes false diffs when a value stored in NetBox returns as a float (e.g. `35.0`) while the import file contains an integer (e.g. `35`).

### Design

Add a helper in `engine.py`:

```python
def _normalize_for_compare(val) -> str:
    """Normalize a value for diff comparison.

    Whole-number floats (e.g. 35.0, "35.0") are normalized to their integer
    string form ("35") to avoid false diffs caused by type differences.
    """
    if val is None:
        return ""
    try:
        f = float(val)
        if f == int(f):
            return str(int(f))
        return str(f)
    except (TypeError, ValueError):
        return str(val).strip()
```

Replace all `str(x)` comparisons in `_field_diff` with `_normalize_for_compare(x)`. The `u_height` comparison (which already uses `float()`) is already correct and unchanged.

---

## Testing

- **Multi-column merge**: unit tests for `parse_file` with profiles having two source columns targeting the same field (no conflict, conflict, post-resolution auto-clear).
- **Show all fields**: test that `_preview_device_row` includes serial/face/airflow/status in `extra_data`.
- **Normalization**: unit tests for `_normalize_for_compare` and for `_field_diff` with float/int pairs.
- All 522 existing tests must continue to pass.

---

## Out of Scope

- Priority/default-winner per profile (possible future enhancement).
- Conflict resolution for `extra_json:` custom field values (same mechanism applies, but not explicitly tested in this spec).
- Adding new target fields (e.g. `contact`) — separate spec if needed.

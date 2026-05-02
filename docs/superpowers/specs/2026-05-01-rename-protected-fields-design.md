# Design: Device Rename Override & Protected Fields on Re-import

**Date:** 2026-05-01
**Branch:** fix/misc (or a new feature branch)
**Status:** Approved

---

## Problem

1. **Name = asset tag**: Some source rows have the device's asset tag placed in the Name column (e.g., `05HR35` for a Dell PowerEdge R770). There is no way to override the device name during the import preview without editing the source file.

2. **Silent overwrite on re-import**: When a device is matched by serial or asset tag on a repeat import, all mapped fields are updated — including `serial` and `asset_tag` — even though those fields should never change. There is no per-row way to force an update of a normally-protected field when genuinely needed.

---

## Feature 1: Rename / override device name in preview

### What it does

Every device row in the row-by-row preview gets a small **✎ Rename** icon button in its action column. Clicking opens a compact modal with:

- Current (source) name — read-only label
- Text input for the override name
- **"Use asset tag"** prefill button: copies the row's `asset_tag` value into the input (hidden when no asset_tag is present for that row)
- Save / Cancel
- **"Clear override"** button (only shown when a SourceResolution already exists for this row's device_name)

### Backend

On save the view POSTs to a new `RenameDeviceView` endpoint, which creates or updates a `SourceResolution` record:

```
profile = <profile>
source_id = <row source_id>
source_column = "device_name"
original_value = <original name>
resolved_fields = {"device_name": "<user input>"}
```

This reuses the existing rerere infrastructure. The preview redirect re-runs the engine, which applies the saved resolution before any validation — so the renamed row flows to `create` or `update` normally.

"Clear override" hits a `ClearDeviceRenameView` that deletes the SourceResolution for `source_column="device_name"` and that `source_id`.

### Asset tag case

When Name and Asset Tag columns are both mapped, the engine already sets `asset_tag` correctly even when the values are identical. The "Use asset tag" prefill button in the rename modal allows the user to quickly adopt the asset tag value as the device name without retyping.

For the explicit mapping case (user maps Name → `asset_tag`, no device_name column), the engine emits a "Missing device name" error row, which already shows the Rename button — the user fills in a name there.

---

## Feature 2: Protected fields on update

### Model change

Add to `ImportProfile`:

```python
protected_fields = models.JSONField(
    default=list,
    blank=True,
    help_text="Fields that will never be overwritten when updating an existing device.",
)
```

**Default values** (pre-checked in the form): `["serial", "asset_tag"]`.
All other fields are updatable by default.

### Configurable fields list

The ImportProfile edit form gains a checkbox group showing all updatable device fields:

| Field | Default protected |
|---|---|
| `device_name` | ☐ |
| `rack` | ☐ |
| `u_position` | ☐ |
| `role` | ☐ |
| `device_type` | ☐ |
| `status` | ☐ |
| `serial` | ☑ |
| `asset_tag` | ☑ |
| `face` | ☐ |
| `airflow` | ☐ |
| `tenant` | ☐ |

### Engine behaviour

In `_write_device_row`, when updating an existing device:
- Skip writing any field whose canonical name is in `profile.protected_fields`
- When skipped **and the import value differs from the live value**, append a note to `row.detail`: `"serial unchanged (protected): import='ABC' live='XYZ'"`

This note is visible in the preview table row's detail column so the user can see the mismatch without it silently being applied.

### Per-row force override (session-based)

When a row shows a protected-field conflict note, a **"Force update"** button appears beside the conflict detail. Clicking POSTs to `ForceFieldUpdateView`, which writes into the session:

```
request.session["import_force_overrides"][source_id].append(field_name)
```

On preview re-render:
- `ImportPreviewView.get()` reads `request.session.get("import_force_overrides", {})`
- Passes it into the engine via `ImportContext.force_overrides`
- Engine: for any field listed in `force_overrides[source_id]`, treat it as unprotected

The session entry is cleared for all source_ids when the final import run (`dry_run=False`) completes.

### Migration

One migration: `add_field ImportProfile.protected_fields`. No FK dependencies, no data migration needed.

---

## What is NOT in this spec

- **JIRA ID custom field + clickable link**: separate spec/feature.
- **Rack-level protected fields**: racks currently have fewer updatable fields; can be added later.
- **Bulk force-override**: forcing a field update for all rows at once (not needed yet).

---

## Affected files

| File | Change |
|---|---|
| `models.py` | Add `protected_fields` to `ImportProfile` |
| `forms.py` | `ImportProfileForm` gains checkbox widget for `protected_fields` |
| `engine.py` | `ImportContext`: add `force_overrides` dict; `_write_device_row`: check protected_fields and force_overrides |
| `views.py` | New `RenameDeviceView`, `ClearDeviceRenameView`, `ForceFieldUpdateView`; `ImportPreviewView.get()` reads session force_overrides |
| `urls.py` | Three new routes |
| `templates/import_preview.html` | Rename button on device rows; conflict detail with "Force update" button; rename modal with asset-tag prefill |
| `migrations/` | One new migration for `protected_fields` |
| `tests/` | New tests for engine protected fields logic, rename view, force override view |

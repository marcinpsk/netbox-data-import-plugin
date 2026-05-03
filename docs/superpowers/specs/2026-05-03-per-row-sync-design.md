# Per-Row Sync — Design Spec

**Date:** 2026-05-03
**Status:** Approved
**Scope:** `fix/misc` branch, `netbox_data_import` plugin

---

## Problem

The import preview shows all rows from the source file. The only execution path is "run everything at once". For large initial imports this is impractical: you want to work through devices one by one — inspect a row, sync it to NetBox, watch the preview update, move on.

---

## Solution

Add a labelled **"⚡ Sync to NetBox"** button on every `action=create` row (devices and racks). Clicking opens a confirmation modal showing the full field table for that row. Confirming executes just that one row via a new backend endpoint, then reloads the page — which re-runs the full dry-run preview against the current DB state.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Button style | Text + icon (`⚡ Sync to NetBox`) | User preference: text labels are clearer than icon-only |
| Rows targeted | `action=create` only, both devices and racks | Keeps scope simple; update rows deferred |
| Modal content | Full field table from session `RowResult` | No extra API call needed; opens instantly |
| Backend | Single `SyncSingleRowView` endpoint | Reuses `run_import()` unchanged |
| After success | Full page reload | Re-runs preview against live DB; simplest correct behaviour |
| After failure | Inline error inside modal, no auto-dismiss | User needs to see what failed |

---

## UI Changes

### Button in preview table

```html
<!-- added in controls column for create rows, devices and racks -->
{% if row.action == "create" and row.object_type == "device" or row.action == "create" and row.object_type == "rack" %}
<button type="button" class="btn btn-sm btn-outline-success ms-1"
        data-bs-toggle="modal" data-bs-target="#syncRowModal"
        data-row-number="{{ row.row_number }}"
        data-object-type="{{ row.object_type }}"
        data-name="{{ row.name }}"
        data-source-id="{{ row.source_id }}"
        data-rack-name="{{ row.rack_name|default:'' }}"
        data-detail="{{ row.detail }}"
        data-source-make="{{ row.extra_data.source_make|default:'' }}"
        data-source-model="{{ row.extra_data.source_model|default:'' }}"
        data-asset-tag="{{ row.extra_data.asset_tag|default:'' }}"
        data-rack-type-name="{{ row.extra_data.rack_type_name|default:'' }}"
        title="Create this {{ row.object_type }} in NetBox now">
  <i class="mdi mdi-lightning-bolt"></i> Sync to NetBox
</button>
{% endif %}
```

The modal JS reads these individual `data-*` attributes so no custom template filter is needed.

**Placement:** after the existing Split (✂) and Map DT buttons in the controls column.

### Confirmation modal (`#syncRowModal`)

Single shared modal at the bottom of the page. Structure:

```
┌─────────────────────────────────────────────────────────┐
│ ⚡ Sync to NetBox — <name>                         [✕]   │
├─────────────────────────────────────────────────────────┤
│  [Create device]  Row #N · ID: <source_id>              │
│                                                          │
│  Field          Value                                    │
│  ──────────     ──────────────────────────────           │
│  Name           <name>                                   │
│  Asset tag      65JP27  [from resolution]  ← green       │
│  Device type    Cisco / Catalyst 9300                    │
│  Site           ITC-Lab                                  │
│  Rack           Rack A                                   │
│  Position       U12                                      │
│  Detail         <row.detail full text>                   │
│                                                          │
│  ⚠ This will write to NetBox. Preview reloads after.    │
├─────────────────────────────────────────────────────────┤
│                           [Cancel]  [⚡ Confirm & Sync]  │
└─────────────────────────────────────────────────────────┘
```

**Fields displayed** (in order, skip empty):
1. `name` — primary name
2. `rack_name` — rack (if any)
3. `source_id` — source ID
4. `extra_data.source_make` — manufacturer
5. `extra_data.source_model` — model
6. `extra_data.asset_tag` — asset tag
7. `extra_data.rack_type_name` — rack type (for rack rows)
8. `detail` — full human-readable summary (always last)

**Resolution highlighting:** check `EXISTING_RESOLUTIONS[source_id]` (already in page); any field whose value came from a resolution gets a small `text-bg-success` badge "from resolution".

**Error state:** if the backend returns `{ok: false}`, show a `div.alert.alert-danger` inside the modal body (above the footer). The "Confirm & Sync" button becomes active again. Do not auto-dismiss.

---

## Backend — `SyncSingleRowView`

**URL:** `plugins/data-import/sync-single-row/` (POST only)
**Permission:** `netbox_data_import.change_importprofile` (same as execute)

### Request
```
POST /plugins/data-import/sync-single-row/
Content-Type: application/x-www-form-urlencoded

csrfmiddlewaretoken=...
row_number=42
```

### Logic
1. Load `import_rows` and `import_context` from session. If missing → `{ok: false, error: "No import in progress"}`.
2. Parse `row_number` as int; validate presence → 400 if missing.
3. Re-apply saved resolutions to rows (same call as `ImportPreviewView.get`).
4. Find the row dict where `row["_row_number"] == row_number`. If not found → `{ok: false, error: "Row not found"}`.
5. Reconstruct `context = {site, location, tenant}` from session IDs.
6. Load `ImportProfile` from session `profile_id`.
7. Wrap in `transaction.atomic()`: call `run_import([row], profile, context, dry_run=False, user=request.user)`.
8. If result has errors: rollback; return `{ok: false, errors: [r.detail for r in result.rows if r.action=="error"]}`.
9. On success: optionally persist an `ImportJob` record with `result_rows` containing just the one synced row; return `{ok: true, detail: result.rows[0].detail, url: result.rows[0].netbox_url}`.

### Edge cases
- **Device before its rack exists:** `run_import` will fail with "Rack not found" → returned as an error in the modal. User should sync rack rows first.
- **Row already synced:** on reload, the row will show `action=update` or `action=skip` — the button won't appear (it's Create-only).
- **Concurrent full execute:** no special handling; the full execute will pick up the already-created object as an update.

---

## URL Registration

Add to `urls.py`:
```python
path("sync-single-row/", views.SyncSingleRowView.as_view(), name="sync_single_row"),
```

---

## Data Flow

```
User clicks "⚡ Sync to NetBox"
  → Bootstrap modal opens
  → JS reads data-row-json from button
  → Modal populates field table from RowResult dict
  → Highlights resolution fields from EXISTING_RESOLUTIONS

User clicks "Confirm & Sync"
  → fetch POST /sync-single-row/ {row_number}
  → SyncSingleRowView:
      load session rows + context
      re-apply resolutions
      find row by row_number
      run_import([row], dry_run=False)
  → {ok: true}
      → window.location.reload()
      → ImportPreviewView.get() re-runs full dry-run
      → synced row now shows as update/skip (no button)

  → {ok: false, error: "..."}
      → inline error shown in modal
      → user can cancel or fix and retry
```

---

## Testing

| Test | What to verify |
|---|---|
| `SyncSingleRowView` — success | Creates device in DB, returns `{ok: true}` |
| `SyncSingleRowView` — rack row | Creates rack in DB, returns `{ok: true}` |
| `SyncSingleRowView` — no session | Returns `{ok: false, error: "No import in progress"}` |
| `SyncSingleRowView` — bad row_number | Returns `{ok: false, error: "Row not found"}` |
| `SyncSingleRowView` — engine error | Returns `{ok: false, errors: [...]}`, no DB write |
| Template | Button appears on create rows, absent on update/skip/error/ignore rows |
| Template | Button absent on manufacturer and device_type rows |

---

## Out of Scope (deferred)

- **Update rows:** sync existing devices that need updates — deferred to a follow-up.
- **Batch / checkbox select:** sync multiple rows at once — follow-up feature.
- **Button label improvements across all preview actions** (Split, Map DT, etc.) — the existing icon-only buttons should also get text labels; tracked separately.
- **AJAX row update:** updating just the synced row in-place without a full reload — deferred.

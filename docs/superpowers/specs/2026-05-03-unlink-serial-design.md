# Design: Device Unlink + Serial Mismatch Warning

**Date:** 2026-05-03
**Status:** Approved

## Problem

When a user manually links a source row to an existing NetBox device via the "Link" modal, there is no way to undo that link if the wrong device was selected. Additionally, when searching for a device to link, no serial number information is shown, so users cannot verify they are linking the correct physical device.

## Scope

- Add **Unlink** capability (remove a `DeviceExistingMatch`) via two entry points: a button on the row and a removal option inside the Link modal.
- Add **serial number display** in device search results and a **mismatch warning banner** when the selected device's serial doesn't match the source row's serial.

## Out of scope

- Unlinking auto-matched devices (serial/asset-tag auto-match from `AutoMatchDevicesView`). Only manually-created `DeviceExistingMatch` records get an Unlink button.
- Blocking the link on serial mismatch (warn only, never block).

---

## Architecture

### New: `UnlinkDeviceView`

- **URL:** `plugins/data-import/unlink-device/`
- **Name:** `unlink_device`
- **Method:** POST
- **Permission:** `netbox_data_import.delete_deviceexistingmatch`
- **Inputs:** `profile_id`, `source_id` (POST body)
- **Behaviour:** Deletes the matching `DeviceExistingMatch(profile=profile, source_id=source_id)` if it exists. Redirects to `import_preview`. Shows a success flash message.
- **Error handling:** 404 if profile not found; silently ignores missing match (idempotent).

### Changed: `SearchNetBoxObjectsView` (device branch)

Add `"serial"` to each device result dict:

```python
{"id": dev.pk, "name": dev.name, "site": ..., "serial": dev.serial, "url": ...}
```

`dev.serial` is returned as-is (Python `None` → JSON `null` when the device has no serial). The template uses `escapeHtml(dev.serial || 'Empty')` to display a safe fallback.

### Changed: `ImportPreviewView.get()`

Add two items to the template context:

```python
_matches = list(profile.device_matches.all())

device_match_source_ids = {m.source_id for m in _matches}

device_match_info_json = json.dumps({
    m.source_id: {"device_id": m.netbox_device_id, "device_name": m.device_name}
    for m in _matches
}).translate({ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"})
```

`device_match_source_ids` is used in template `{% if %}` conditionals. `device_match_info_json` is injected as a `DEVICE_MATCH_INFO` JS variable (same pattern as `EXISTING_RESOLUTIONS`).

### Changed: `import_preview.html`

#### 1. Unlink button on `action=update` rows

Shown only when `row.source_id in device_match_source_ids`. Placed after the existing Link/Re-link button:

```html
<form method="post" action="{% url 'plugins:netbox_data_import:unlink_device' %}" class="d-inline ms-1">
  {% csrf_token %}
  <input type="hidden" name="profile_id" value="{{ profile_id }}">
  <input type="hidden" name="source_id" value="{{ row.source_id }}">
  <button type="submit" class="btn btn-sm btn-outline-danger"
          title="Remove link to existing device"
          onclick="return confirm('Remove link between source row and NetBox device?')">
    <i class="mdi mdi-link-off"></i> Unlink
  </button>
</form>
```

#### 2. Link button — additional data attributes

On both `action=update` and `action=create` Link buttons, add `data-source-serial`:

```html
data-source-serial="{{ row.extra_data.serial|default:'' }}"
```

The linked device info (id, name) comes from the `DEVICE_MATCH_INFO` JS variable — the modal JS looks up `DEVICE_MATCH_INFO[sourceId]` rather than reading data attributes for this.

#### 3. Modal — "Currently linked" section

In `deviceMatchModal`, after the modal body opens, JS reads `DEVICE_MATCH_INFO[sourceId]` and, if present, renders:

```html
<div id="dm_current_link_section" class="alert alert-warning py-2 small mb-2" style="display:none">
  <strong>Currently linked:</strong> <span id="dm_current_link_name"></span>
  <form method="post" action="unlink-device/" class="d-inline ms-2" id="dm_unlink_form">
    {% csrf_token %}
    <input type="hidden" name="profile_id" id="dm_unlink_profile_id">
    <input type="hidden" name="source_id" id="dm_unlink_source_id">
    <button type="submit" class="btn btn-sm btn-outline-danger py-0">✕ Remove link</button>
  </form>
</div>
```

JS in `show.bs.modal` populates these fields and toggles visibility.

#### 4. Serial display in search results

`dmSearch()` JS builds result buttons showing serial inline:

```js
var serialInfo = '';
if (sourceSerial && dev.serial) {
  if (dev.serial === sourceSerial) {
    serialInfo = ' <span class="text-success small">✓ SN: ' + dev.serial + '</span>';
  } else {
    serialInfo = ' <span class="text-danger small">⚠ SN: ' + dev.serial + ' (mismatch)</span>';
  }
} else if (dev.serial) {
  serialInfo = ' <span class="text-muted small">SN: ' + dev.serial + '</span>';
}
```

Source serial shown above search box: `"Source serial: X"` (hidden if empty).

#### 5. Warning banner after selection

When user clicks a result button, compare `dev.serial` vs `sourceSerial`. If both non-empty and different:

- Show `#dm_serial_warning` banner: `"⚠ Serial mismatch — source: SN-X · NetBox: SN-Y. Verify before linking."`
- Change submit button text to `"Link anyway"`

If match or either is empty: hide banner, reset button text to `"Link selected device"`.

---

## URL

```python
path("unlink-device/", views.UnlinkDeviceView.as_view(), name="unlink_device"),
```

---

## Tests

- `UnlinkDeviceViewTest` — success (match deleted + redirect), missing match (graceful), unauthenticated (login redirect, not 403), missing profile (404)
- `SearchNetBoxObjectsViewTest` — serial included in device results
- `ImportPreviewViewContextTest` — `device_match_source_ids` and `device_match_info` in context
- Template test — unlink button present on manually-linked update row, absent on non-linked rows

---

## Implementation notes

- `DEVICE_MATCH_INFO` JSON variable injected via `<script>` in template (same pattern as `EXISTING_RESOLUTIONS`)
- The inline confirm dialog (`onclick="return confirm(...)"`) covers the row-level unlink. No confirm needed inside the modal since the user is already in a deliberate modal flow.
- `dm_unlink_form` action uses the Django URL tag, not a hardcoded path: `action="{% url 'plugins:netbox_data_import:unlink_device' %}"`

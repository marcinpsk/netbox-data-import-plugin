# Per-Row Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "⚡ Sync to NetBox" button on every `action=create` row in the import preview that opens a confirmation modal and, on confirm, executes just that one row immediately.

**Architecture:** A new `SyncSingleRowView` (POST-only AJAX) loads session rows, finds the target row by `_row_number`, calls the existing `engine.run_import([row], dry_run=False)` inside `transaction.atomic()`, and returns JSON. The template adds a button per-row that opens a shared Bootstrap modal populated from `data-*` attributes; on confirm JS POSTs and reloads on success.

**Tech Stack:** Django class-based views, `JsonResponse`, `transaction.atomic()`, Bootstrap 5 modal, vanilla JS fetch.

---

## Task 1: `SyncSingleRowView` — backend + URL + tests

**Files:**
- Modify: `netbox_data_import/views.py` (add class at the end of the file, before the module-level helpers)
- Modify: `netbox_data_import/urls.py` (add URL entry)
- Modify: `netbox_data_import/tests/test_views_coverage2.py` (add test class)

### Step 1.1 — Write failing tests

Add this class to the bottom of `netbox_data_import/tests/test_views_coverage2.py`:

```python
class SyncSingleRowViewTest(TestCase):
    """Tests for SyncSingleRowView."""

    def setUp(self):
        from dcim.models import Site

        self.user = _make_superuser("sync_row_user")
        self.client = Client()
        self.client.login(username="sync_row_user", password="testpass")
        self.profile = _make_profile("SyncRowProfile")
        self.site = Site.objects.create(name="SyncRow-Site", slug="syncrow-site")

    def _set_session(self, rows, row_number=1):
        session = self.client.session
        session["import_rows"] = rows
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "test.xlsx",
        }
        session.save()

    def _url(self):
        return reverse("plugins:netbox_data_import:sync_single_row")

    # ---- no session ----

    def test_no_session_returns_ok_false(self):
        resp = self.client.post(self._url(), {"row_number": "1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("No import in progress", data["error"])

    # ---- missing / invalid row_number ----

    def test_missing_row_number_returns_400(self):
        self._set_session([{"_row_number": 1, "source_id": "X"}])
        resp = self.client.post(self._url(), {})
        self.assertEqual(resp.status_code, 400)

    def test_row_not_found_returns_ok_false(self):
        self._set_session([{"_row_number": 1, "source_id": "X"}])
        resp = self.client.post(self._url(), {"row_number": "99"})
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("Row not found", data["error"])

    # ---- engine success (mocked) ----

    @patch("netbox_data_import.views.engine")
    def test_success_returns_ok_true(self, mock_engine):
        from netbox_data_import.engine import ImportResult, RowResult

        mock_result = ImportResult()
        mock_result.rows = [
            RowResult(
                row_number=1,
                source_id="D001",
                name="test-device",
                action="create",
                object_type="device",
                detail="Would create device 'test-device'",
                netbox_url="/dcim/devices/1/",
            )
        ]
        mock_result.has_errors = False
        mock_engine.run_import.return_value = mock_result
        mock_engine.reapply_saved_resolutions.return_value = [{"_row_number": 1, "source_id": "D001"}]

        self._set_session([{"_row_number": 1, "source_id": "D001"}])
        resp = self.client.post(self._url(), {"row_number": "1"})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["detail"], "Would create device 'test-device'")
        self.assertEqual(data["url"], "/dcim/devices/1/")
        mock_engine.run_import.assert_called_once()
        call_kwargs = mock_engine.run_import.call_args
        # Verify dry_run=False was passed
        self.assertFalse(call_kwargs.kwargs.get("dry_run", True))

    # ---- engine returns error rows (mocked) ----

    @patch("netbox_data_import.views.engine")
    def test_engine_error_returns_ok_false(self, mock_engine):
        from netbox_data_import.engine import ImportResult, RowResult

        mock_result = ImportResult()
        mock_result.rows = [
            RowResult(
                row_number=1,
                source_id="D001",
                name="bad-device",
                action="error",
                object_type="device",
                detail="Missing rack",
            )
        ]
        mock_result.has_errors = True
        mock_engine.run_import.return_value = mock_result
        mock_engine.reapply_saved_resolutions.return_value = [{"_row_number": 1, "source_id": "D001"}]

        self._set_session([{"_row_number": 1, "source_id": "D001"}])
        resp = self.client.post(self._url(), {"row_number": "1"})
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("Missing rack", data["errors"])
```

### Step 1.2 — Run tests to verify they fail

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin
# inside devcontainer or with netbox-test alias:
python manage.py test netbox_data_import.tests.test_views_coverage2.SyncSingleRowViewTest -v 2 2>&1 | tail -20
```

Expected: all 5 tests FAIL with `NoReverseMatch` or `AttributeError`.

### Step 1.3 — Add the URL entry

In `netbox_data_import/urls.py`, after the `auto-match-devices` line (line ~103), add:

```python
    path("sync-single-row/", views.SyncSingleRowView.as_view(), name="sync_single_row"),
```

Full block context (replace the comment + existing lines 92-106):

```python
    # AJAX helpers
    path("check-device/", views.CheckDeviceNameView.as_view(), name="check_device"),
    path("search-objects/", views.SearchNetBoxObjectsView.as_view(), name="search_objects"),
    # Quick-resolve views (POST from preview inline fix buttons)
    path("quick-create-manufacturer/", views.QuickCreateManufacturerView.as_view(), name="quick_create_manufacturer"),
    path(
        "quick-resolve-manufacturer/", views.QuickResolveManufacturerView.as_view(), name="quick_resolve_manufacturer"
    ),
    path("quick-resolve-device-type/", views.QuickResolveDeviceTypeView.as_view(), name="quick_resolve_device_type"),
    path("quick-add-class-mapping/", views.QuickAddClassRoleMappingView.as_view(), name="quick_add_class_mapping"),
    path("quick-add-column-mapping/", views.QuickAddColumnMappingView.as_view(), name="quick_add_column_mapping"),
    path("quick-create-role/", views.QuickCreateDeviceRoleView.as_view(), name="quick_create_role"),
    path("match-existing-device/", views.MatchExistingDeviceView.as_view(), name="match_existing_device"),
    path("auto-match-devices/", views.AutoMatchDevicesView.as_view(), name="auto_match_devices"),
    # Per-row sync
    path("sync-single-row/", views.SyncSingleRowView.as_view(), name="sync_single_row"),
    # Import Job history
    path("jobs/", views.ImportJobListView.as_view(), name="importjob_list"),
```

### Step 1.4 — Add `SyncSingleRowView` to views.py

Find the section just before the module-level helper functions near the end of `views.py`. The last class before helpers is `QuickCreateDeviceRoleView`. Insert this new class after it (before `_serialize_rows`):

```python
class SyncSingleRowView(PermissionRequiredMixin, View):
    """AJAX endpoint: execute a single row from the current import session.

    POST body: row_number=<int>
    Returns JSON: {ok: true, detail, url} or {ok: false, error} / {ok: false, errors: [...]}
    """

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        from django.db import transaction
        from django.http import JsonResponse
        from dcim.models import Location, Site
        from tenancy.models import Tenant

        rows = request.session.get("import_rows")
        ctx_data = request.session.get("import_context")
        if not rows or not ctx_data:
            return JsonResponse({"ok": False, "error": "No import in progress"})

        raw_row_number = request.POST.get("row_number")
        if raw_row_number is None:
            return JsonResponse({"ok": False, "error": "row_number is required"}, status=400)
        try:
            row_number = int(raw_row_number)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid row number"}, status=400)

        profile = ImportProfile.objects.filter(pk=ctx_data.get("profile_id")).first()
        if not profile:
            return JsonResponse({"ok": False, "error": "Import profile not found"})

        rows = engine.reapply_saved_resolutions(rows, profile)

        target = next((r for r in rows if r.get("_row_number") == row_number), None)
        if target is None:
            return JsonResponse({"ok": False, "error": "Row not found"})

        site = Site.objects.filter(pk=ctx_data.get("site_id")).first()
        if not site:
            return JsonResponse({"ok": False, "error": "Site not found"})

        location = (
            Location.objects.filter(pk=ctx_data.get("location_id")).first()
            if ctx_data.get("location_id")
            else None
        )
        tenant = (
            Tenant.objects.filter(pk=ctx_data.get("tenant_id")).first()
            if ctx_data.get("tenant_id")
            else None
        )
        context = {"site": site, "location": location, "tenant": tenant}

        try:
            with transaction.atomic():
                result = engine.run_import([target], profile, context, dry_run=False, user=request.user)
                error_rows = [r for r in result.rows if r.action == "error"]
                if error_rows:
                    transaction.set_rollback(True)
                    return JsonResponse({"ok": False, "errors": [r.detail for r in error_rows]})
        except Exception:
            logger.exception("SyncSingleRowView: unexpected error for row_number=%s", row_number)
            return JsonResponse({"ok": False, "error": "An unexpected error occurred — see server logs."})

        row_result = result.rows[0] if result.rows else None
        return JsonResponse(
            {
                "ok": True,
                "detail": row_result.detail if row_result else "",
                "url": row_result.netbox_url if row_result else "",
            }
        )
```

The `engine` import is already used at module scope via `from . import engine` inside view methods; add it in `post()` as shown. Note: `engine` is imported at the method level in several views — do the same here.

**Important:** add `from . import engine` as a local import inside `post()` (the existing pattern in `ImportPreviewView.get()` and `ImportRunView.post()`).

### Step 1.5 — Run tests to verify they pass

```bash
python manage.py test netbox_data_import.tests.test_views_coverage2.SyncSingleRowViewTest -v 2 2>&1 | tail -20
```

Expected: all 5 tests PASS.

### Step 1.6 — Run full test suite to catch regressions

```bash
python manage.py test netbox_data_import -v 1 2>&1 | tail -15
```

Expected: `OK` with 0 failures.

### Step 1.7 — Lint

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin && ruff check . && ruff format --check .
```

Expected: `All checks passed!`

### Step 1.8 — Commit

```bash
git add netbox_data_import/views.py netbox_data_import/urls.py netbox_data_import/tests/test_views_coverage2.py
git commit -m "feat: add SyncSingleRowView for per-row import execution"
```

---

## Task 2: Template — button, modal, and JavaScript

**Files:**
- Modify: `netbox_data_import/templates/netbox_data_import/import_preview.html`

### Step 2.1 — Add the "⚡ Sync to NetBox" button

In `import_preview.html`, locate the Map DT button block (ends around line 521 with `</button>`). After the closing `{% endif %}` of the Map DT block (around line 521), and **before** the closing `</td>` (around line 523), insert:

```html
                {# Per-row sync button: show on create rows for devices and racks only #}
                {% if row.action == 'create' and row.object_type == 'device' or row.action == 'create' and row.object_type == 'rack' %}
                <button type="button" class="btn btn-sm btn-outline-success ms-1 ndi-sync-row-btn"
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

### Step 2.2 — Add the `#syncRowModal` HTML

At the very end of the template, just before `{% endblock %}` (currently the last line), insert the modal HTML:

```html
{# ===== Per-row sync confirmation modal ===== #}
<div class="modal fade" id="syncRowModal" tabindex="-1" aria-labelledby="syncRowModalLabel" aria-hidden="true">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="syncRowModalLabel">
          <i class="mdi mdi-lightning-bolt text-success"></i>
          Sync to NetBox — <span id="syncRowName"></span>
        </h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <p class="text-muted small mb-2">
          <span id="syncRowBadge" class="badge text-bg-primary me-1"></span>
          Row #<span id="syncRowNumber"></span>
          &middot; ID: <span id="syncRowSourceId"></span>
        </p>
        <table class="table table-sm table-bordered">
          <thead>
            <tr class="table-light">
              <th style="width:30%">Field</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody id="syncRowFields"></tbody>
        </table>
        <div class="alert alert-warning mb-0">
          <i class="mdi mdi-alert-outline"></i>
          This will write to NetBox immediately. The preview will reload after syncing.
        </div>
        <div id="syncRowError" class="alert alert-danger mt-2 d-none"></div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button type="button" class="btn btn-success" id="syncRowConfirm">
          <span class="ndi-sync-row-idle"><i class="mdi mdi-lightning-bolt"></i> Confirm &amp; Sync</span>
          <span class="ndi-sync-row-loading d-none"><i class="mdi mdi-loading mdi-spin"></i> Syncing&hellip;</span>
        </button>
      </div>
    </div>
  </div>
</div>
```

### Step 2.3 — Add the JavaScript

After the modal HTML (still before `{% endblock %}`), insert:

```html
<script>
(function () {
  var modal = document.getElementById('syncRowModal');
  if (!modal) return;

  var currentRowNumber = null;

  modal.addEventListener('show.bs.modal', function (e) {
    var btn = e.relatedTarget;
    currentRowNumber = btn.dataset.rowNumber;

    document.getElementById('syncRowName').textContent = btn.dataset.name || '—';
    document.getElementById('syncRowNumber').textContent = currentRowNumber || '—';
    document.getElementById('syncRowSourceId').textContent = btn.dataset.sourceId || '—';
    document.getElementById('syncRowBadge').textContent = 'Create ' + (btn.dataset.objectType || '');

    var resolutions = (typeof EXISTING_RESOLUTIONS !== 'undefined' ? EXISTING_RESOLUTIONS : {});
    var rowRes = resolutions[btn.dataset.sourceId] || {};
    var resolvedFieldKeys = {};
    for (var col in rowRes) {
      var resolved = rowRes[col].resolved_fields || {};
      for (var f in resolved) {
        resolvedFieldKeys[f] = true;
      }
    }

    function makeValueCell(value, fieldKey) {
      var td = document.createElement('td');
      td.appendChild(document.createTextNode(value || '—'));
      if (resolvedFieldKeys[fieldKey]) {
        var badge = document.createElement('span');
        badge.className = 'badge text-bg-success ms-1';
        badge.textContent = 'from resolution';
        td.appendChild(badge);
      }
      return td;
    }

    var tbody = document.getElementById('syncRowFields');
    tbody.innerHTML = '';

    var fieldDefs = [
      ['Name', btn.dataset.name, 'device_name'],
      ['Rack', btn.dataset.rackName, 'rack_name'],
      ['Source ID', btn.dataset.sourceId, 'source_id'],
      ['Manufacturer', btn.dataset.sourceMake, 'source_make'],
      ['Model', btn.dataset.sourceModel, 'source_model'],
      ['Asset tag', btn.dataset.assetTag, 'asset_tag'],
      ['Rack type', btn.dataset.rackTypeName, 'rack_type'],
    ];

    fieldDefs.forEach(function (def) {
      var label = def[0], value = def[1], fieldKey = def[2];
      if (!value) return;
      var tr = document.createElement('tr');
      var th = document.createElement('td');
      th.className = 'fw-semibold';
      th.textContent = label;
      tr.appendChild(th);
      tr.appendChild(makeValueCell(value, fieldKey));
      tbody.appendChild(tr);
    });

    if (btn.dataset.detail) {
      var tr = document.createElement('tr');
      var th = document.createElement('td');
      th.className = 'fw-semibold';
      th.textContent = 'Detail';
      tr.appendChild(th);
      var td = document.createElement('td');
      td.textContent = btn.dataset.detail;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }

    var errorDiv = document.getElementById('syncRowError');
    errorDiv.textContent = '';
    errorDiv.classList.add('d-none');

    var confirmBtn = document.getElementById('syncRowConfirm');
    confirmBtn.disabled = false;
    confirmBtn.querySelector('.ndi-sync-row-idle').classList.remove('d-none');
    confirmBtn.querySelector('.ndi-sync-row-loading').classList.add('d-none');
  });

  document.getElementById('syncRowConfirm').addEventListener('click', function () {
    var confirmBtn = this;
    confirmBtn.disabled = true;
    confirmBtn.querySelector('.ndi-sync-row-idle').classList.add('d-none');
    confirmBtn.querySelector('.ndi-sync-row-loading').classList.remove('d-none');

    var errorDiv = document.getElementById('syncRowError');
    errorDiv.classList.add('d-none');

    var csrfToken = (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';

    fetch('{% url "plugins:netbox_data_import:sync_single_row" %}', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRFToken': csrfToken,
      },
      body: 'row_number=' + encodeURIComponent(currentRowNumber),
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.ok) {
        window.location.reload();
      } else {
        confirmBtn.disabled = false;
        confirmBtn.querySelector('.ndi-sync-row-idle').classList.remove('d-none');
        confirmBtn.querySelector('.ndi-sync-row-loading').classList.add('d-none');
        var msgs = data.errors ? data.errors.join('\n') : (data.error || 'Sync failed');
        errorDiv.textContent = msgs;
        errorDiv.classList.remove('d-none');
      }
    })
    .catch(function () {
      confirmBtn.disabled = false;
      confirmBtn.querySelector('.ndi-sync-row-idle').classList.remove('d-none');
      confirmBtn.querySelector('.ndi-sync-row-loading').classList.add('d-none');
      errorDiv.textContent = 'Network error — sync failed';
      errorDiv.classList.remove('d-none');
    });
  });
}());
</script>
```

### Step 2.4 — Add a template presence test

Add the following test class to `netbox_data_import/tests/test_views_coverage2.py` (after `SyncSingleRowViewTest`):

```python
class SyncRowButtonTemplateTest(TestCase):
    """Verify the Sync to NetBox button appears on create rows and not on others."""

    def setUp(self):
        from dcim.models import Site
        from unittest.mock import patch, MagicMock

        self.user = _make_superuser("sync_btn_user")
        self.client = Client()
        self.client.login(username="sync_btn_user", password="testpass")
        self.profile = _make_profile("SyncBtnProfile")
        self.site = Site.objects.create(name="SyncBtn-Site", slug="syncbtn-site")

    @patch("netbox_data_import.views.engine")
    def test_sync_button_present_on_create_rows(self, mock_engine):
        from netbox_data_import.engine import ImportResult, RowResult

        mock_result = ImportResult()
        mock_result.rows = [
            RowResult(
                row_number=1, source_id="D001", name="new-device",
                action="create", object_type="device",
                detail="Would create device 'new-device'",
            ),
        ]
        mock_result.counts = {}
        mock_result.has_errors = False
        mock_engine.run_import.return_value = mock_result
        mock_engine.reapply_saved_resolutions.return_value = [
            {"_row_number": 1, "source_id": "D001", "device_name": "new-device"}
        ]
        mock_engine.ImportResult = ImportResult

        session = self.client.session
        session["import_rows"] = [{"_row_number": 1, "source_id": "D001", "device_name": "new-device"}]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "test.xlsx",
        }
        session.save()

        resp = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"ndi-sync-row-btn", resp.content)
        self.assertIn(b"Sync to NetBox", resp.content)
        self.assertIn(b"syncRowModal", resp.content)

    @patch("netbox_data_import.views.engine")
    def test_sync_button_absent_on_update_rows(self, mock_engine):
        from netbox_data_import.engine import ImportResult, RowResult

        mock_result = ImportResult()
        mock_result.rows = [
            RowResult(
                row_number=1, source_id="D002", name="existing-device",
                action="update", object_type="device",
                detail="Would update device 'existing-device'",
            ),
        ]
        mock_result.counts = {}
        mock_result.has_errors = False
        mock_engine.run_import.return_value = mock_result
        mock_engine.reapply_saved_resolutions.return_value = [
            {"_row_number": 1, "source_id": "D002", "device_name": "existing-device"}
        ]
        mock_engine.ImportResult = ImportResult

        session = self.client.session
        session["import_rows"] = [{"_row_number": 1, "source_id": "D002", "device_name": "existing-device"}]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "test.xlsx",
        }
        session.save()

        resp = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"ndi-sync-row-btn", resp.content)
```

### Step 2.5 — Run new template tests

```bash
python manage.py test netbox_data_import.tests.test_views_coverage2.SyncRowButtonTemplateTest -v 2 2>&1 | tail -20
```

Expected: both tests PASS.

### Step 2.6 — Run full test suite

```bash
python manage.py test netbox_data_import -v 1 2>&1 | tail -15
```

Expected: `OK`.

### Step 2.7 — Lint

```bash
ruff check . && ruff format --check .
```

Expected: `All checks passed!`

### Step 2.8 — Commit

```bash
git add netbox_data_import/templates/netbox_data_import/import_preview.html \
        netbox_data_import/tests/test_views_coverage2.py
git commit -m "feat: add per-row sync button, modal, and JS to import preview"
```

---

## Self-Review Checklist

- [x] **Spec coverage:**
  - Button on `action=create`, devices + racks only ✓ (Task 2.1)
  - Modal with field table + resolution highlighting ✓ (Task 2.2–2.3)
  - `SyncSingleRowView` — no session, bad row, engine error, success ✓ (Task 1)
  - After success: `window.location.reload()` ✓ (Task 2.3)
  - After failure: inline error, no auto-dismiss ✓ (Task 2.3)
  - URL registered ✓ (Task 1.3)
  - Tests: all 7 table entries covered ✓ (Tasks 1.1, 2.4)

- [x] **No placeholders:** all code blocks are complete.

- [x] **Type consistency:**
  - `engine.run_import` called with `[target]` (list of dict), `profile` (ImportProfile), `context` (dict), `dry_run=False` — matches signature at `engine.py:1335`.
  - `RowResult.action` checked against `"error"` — matches `RowResult` literal type at `engine.py:43`.
  - `engine.reapply_saved_resolutions` called with `(rows, profile)` — matches `engine.py:341`.
  - `r.get("_row_number")` — rows are dicts with `_row_number` key per `engine.py:308`.

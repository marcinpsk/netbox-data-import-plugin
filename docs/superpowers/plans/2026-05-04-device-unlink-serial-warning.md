# Device Unlink + Serial Mismatch Warning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add unlink capability for manually-linked devices and show serial numbers in device search with mismatch warnings.

**Architecture:** New `UnlinkDeviceView` POST endpoint deletes `DeviceExistingMatch` records. `SearchNetBoxObjectsView` returns serial. Template renders unlink button on linked rows, Link modal shows linked device info and serial mismatch banner in JS.

**Tech Stack:** Django, DRF, Bootstrap modal, vanilla JS for search and modal state management.

---

## File Structure

**Backend:**
- `netbox_data_import/views.py` — New `UnlinkDeviceView`, updated `SearchNetBoxObjectsView`, updated `ImportPreviewView.get()`
- `netbox_data_import/urls.py` — New route for `unlink_device`

**Frontend:**
- `netbox_data_import/templates/netbox_data_import/import_preview.html` — Unlink button, updated Link button with `data-source-serial`, updated modal to show current link and serial warnings, updated `dmSearch()` JS

**Tests:**
- `netbox_data_import/tests/test_views.py` — Add `UnlinkDeviceViewTest`, `SearchNetBoxObjectsSerialTest`
- `netbox_data_import/tests/test_views_coverage2.py` — Add template test for unlink button presence

---

## Task 1: Backend — UnlinkDeviceView

**Files:**
- Modify: `netbox_data_import/views.py` (add new class before `_serialize_rows`)
- Modify: `netbox_data_import/urls.py` (add route)

- [ ] **Step 1: Add UnlinkDeviceView class**

Add this class right before the `_serialize_rows` function (around line 1800):

```python
class UnlinkDeviceView(PermissionRequiredMixin, View):
    """Remove a DeviceExistingMatch (unlink a manually-linked device)."""

    permission_required = "netbox_data_import.delete_deviceexistingmatch"

    def post(self, request):
        """Delete the DeviceExistingMatch and redirect back to preview."""
        profile_id = request.POST.get("profile_id", "").strip()
        source_id = request.POST.get("source_id", "").strip()

        profile = get_object_or_404(ImportProfile, pk=profile_id)

        if source_id:
            DeviceExistingMatch.objects.filter(
                profile=profile,
                source_id=source_id,
            ).delete()

        messages.success(request, f"Unlinked source '{source_id}'.")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))
```

- [ ] **Step 2: Add URL route**

In `netbox_data_import/urls.py`, add this line after the `sync-single-row/` route (around line 105):

```python
path("unlink-device/", views.UnlinkDeviceView.as_view(), name="unlink_device"),
```

- [ ] **Step 3: Run syntax check**

```bash
python -m py_compile netbox_data_import/views.py netbox_data_import/urls.py
```

Expected: No output (compilation succeeds).

- [ ] **Step 4: Commit**

```bash
git add netbox_data_import/views.py netbox_data_import/urls.py
git commit -m "feat: add UnlinkDeviceView to remove manual device links"
```

---

## Task 2: Backend — Update SearchNetBoxObjectsView

**Files:**
- Modify: `netbox_data_import/views.py` (line ~1930, in the `device` branch of `SearchNetBoxObjectsView.get()`)

- [ ] **Step 1: Add serial to device search results**

In `SearchNetBoxObjectsView.get()`, find the `elif obj_type == "device":` block (around line 1928). Replace the `results.append()` call with:

```python
        elif obj_type == "device":
            for dev in Device.objects.filter(_device_name_filter(q)).distinct().select_related("site")[:limit]:
                results.append(
                    {
                        "id": dev.pk,
                        "name": dev.name,
                        "site": dev.site.name if dev.site else "",
                        "serial": dev.serial or "",
                        "url": request.build_absolute_uri(dev.get_absolute_url()),
                    }
                )
```

The only change is adding `"serial": dev.serial or "",`.

- [ ] **Step 2: Run syntax check**

```bash
python -m py_compile netbox_data_import/views.py
```

Expected: No output.

- [ ] **Step 3: Commit**

```bash
git add netbox_data_import/views.py
git commit -m "feat: include serial number in device search results"
```

---

## Task 3: Backend — Update ImportPreviewView context

**Files:**
- Modify: `netbox_data_import/views.py` (line ~734, in `ImportPreviewView.get()`)

- [ ] **Step 1: Add device_match context variables**

In `ImportPreviewView.get()`, find the `return render(...)` call (around line 767). Before the `return`, add this code:

```python
        _json = json.dumps
        device_match_source_ids = set(
            profile.device_matches.values_list("source_id", flat=True)
        )
        device_match_info = {
            m.source_id: {"device_id": m.netbox_device_id, "device_name": m.device_name}
            for m in profile.device_matches.all()
        }
        device_match_info_json = _json(device_match_info).translate(
            {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}
        )
```

- [ ] **Step 2: Add to render context**

In the same `render()` call (starting around line 767), add these two lines to the context dict:

```python
                "device_match_source_ids": device_match_source_ids,
                "device_match_info_json": device_match_info_json,
```

(Place after the existing `"syncable_fields"` line.)

- [ ] **Step 3: Run syntax check**

```bash
python -m py_compile netbox_data_import/views.py
```

Expected: No output.

- [ ] **Step 4: Commit**

```bash
git add netbox_data_import/views.py
git commit -m "feat: pass device match info to preview template context"
```

---

## Task 4: Template — Add Unlink button and inject DEVICE_MATCH_INFO

**Files:**
- Modify: `netbox_data_import/templates/netbox_data_import/import_preview.html`

- [ ] **Step 1: Inject DEVICE_MATCH_INFO JS variable**

Find the line `<script>` tag that sets `EXISTING_RESOLUTIONS` (around line 703). Right after it, add:

```html
    <script type="text/javascript">
      window.DEVICE_MATCH_INFO = {{ device_match_info_json|safe }};
    </script>
```

- [ ] **Step 2: Add unlink button to update rows**

Find the section for `{% elif row.action == 'update' and row.object_type == 'device' %}` (around line 415). Inside the buttons section (after the existing Link/configure buttons), add:

```html
                   {# Unlink button — only for manually-linked rows #}
                   {% if row.source_id in device_match_source_ids %}
                   <form method="post" action="{% url 'plugins:netbox_data_import:unlink_device' %}" class="d-inline ms-1">
                     {% csrf_token %}
                     <input type="hidden" name="profile_id" value="{{ profile_id }}">
                     <input type="hidden" name="source_id" value="{{ row.source_id }}">
                     <button type="submit" class="btn btn-sm btn-outline-danger"
                             title="Remove link to existing device"
                             onclick="return confirm('Remove link between source row and NetBox device?')">
                       <i class="mdi mdi-link-off"></i>
                     </button>
                   </form>
                   {% endif %}
```

- [ ] **Step 3: Add data-source-serial to Link buttons**

Find the two Link buttons (on lines ~439 and ~462). Add `data-source-serial="{{ row.extra_data.serial|default:'' }}"` to both. Example:

```html
                   <button type="button" class="btn btn-sm btn-outline-info ms-1"
                           data-bs-toggle="modal" data-bs-target="#deviceMatchModal"
                           data-source-id="{{ row.source_id}}"
                           data-source-name="{{ row.name}}"
                           data-source-serial="{{ row.extra_data.serial|default:'' }}"
                           data-source-asset-tag="{{ row.extra_data.asset_tag|default:''}}"
                           data-profile-id="{{ profile_id }}"
                           title="Link to an existing NetBox device">
```

- [ ] **Step 4: Commit**

```bash
git add netbox_data_import/templates/netbox_data_import/import_preview.html
git commit -m "feat: add unlink button and data-source-serial to Link buttons"
```

---

## Task 5: Template — Update deviceMatchModal with current link section

**Files:**
- Modify: `netbox_data_import/templates/netbox_data_import/import_preview.html`

- [ ] **Step 1: Add current-link alert section to modal**

Find `<div class="modal fade" id="deviceMatchModal"` (around line 1440). Inside the modal body, after the opening `<div class="modal-body">`, add:

```html
          <div id="dm_current_link_section" class="alert alert-warning py-2 mb-2" style="display: none;">
            <small><strong>Currently linked:</strong> <span id="dm_current_link_name"></span></small>
            <form method="post" action="{% url 'plugins:netbox_data_import:unlink_device' %}" class="d-inline ms-2" id="dm_unlink_form">
              {% csrf_token %}
              <input type="hidden" name="profile_id" id="dm_unlink_profile_id">
              <input type="hidden" name="source_id" id="dm_unlink_source_id">
              <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2" style="font-size: 11px;">
                ✕ Remove link
              </button>
            </form>
          </div>
```

Place this right after `<div class="modal-body">` and before the existing source display section.

- [ ] **Step 2: Add serial mismatch warning section to modal**

Still in the modal body, after the source name display section, add:

```html
          <div id="dm_serial_warning" class="alert alert-danger py-2 mb-2" style="display: none; font-size: 12px;">
            ⚠ <strong>Serial mismatch:</strong> Source: <span id="dm_serial_source"></span> · NetBox: <span id="dm_serial_netbox"></span>
          </div>
```

- [ ] **Step 3: Update modal footer button text**

Find the modal footer button (around line 1477). Change the button text/id so it can be updated dynamically:

```html
            <button type="submit" form="dm_link_form" class="btn btn-primary" id="dm_submit_btn">Link selected device</button>
```

- [ ] **Step 4: Commit**

```bash
git add netbox_data_import/templates/netbox_data_import/import_preview.html
git commit -m "feat: add current-link section and serial-mismatch warning to deviceMatchModal"
```

---

## Task 6: Template — Update Modal JS to show current link

**Files:**
- Modify: `netbox_data_import/templates/netbox_data_import/import_preview.html`

- [ ] **Step 1: Update modal show event handler**

Find the line `document.getElementById('deviceMatchModal').addEventListener('show.bs.modal', ...)` (around line 813). Replace the entire event listener with:

```javascript
document.getElementById('deviceMatchModal').addEventListener('show.bs.modal', function(event) {
  var btn = event.relatedTarget;
  if (!btn) return;

  var sourceId = btn.dataset.sourceId || '';
  var sourceName = btn.dataset.sourceName || '';
  var sourceSerial = btn.dataset.sourceSerial || '';

  document.getElementById('dm_source_id').value = sourceId;
  document.getElementById('dm_source_asset_tag').value = btn.dataset.sourceAssetTag || '';
  document.getElementById('dm_profile_id').value = btn.dataset.profileId || '';
  document.getElementById('dm_source_name_display').textContent = sourceName;
  document.getElementById('dm_search_q').value = sourceName.split(' - ').pop().trim();
  document.getElementById('dm_netbox_device_id').value = '';
  document.getElementById('dm_search_results').innerHTML = '';
  document.getElementById('dm_serial_warning').style.display = 'none';
  document.getElementById('dm_submit_btn').textContent = 'Link selected device';

  // Show current link if it exists
  var matchInfo = DEVICE_MATCH_INFO[sourceId];
  if (matchInfo) {
    document.getElementById('dm_current_link_name').textContent = matchInfo.device_name + ' (NetBox #' + matchInfo.device_id + ')';
    document.getElementById('dm_current_link_section').style.display = '';
    document.getElementById('dm_unlink_profile_id').value = document.getElementById('dm_profile_id').value;
    document.getElementById('dm_unlink_source_id').value = sourceId;
  } else {
    document.getElementById('dm_current_link_section').style.display = 'none';
  }

  // Store source serial for later comparison
  document.getElementById('dm_modal_source_serial').value = sourceSerial;
});
```

(This requires a hidden input to store the source serial — add it in the next step.)

- [ ] **Step 2: Add hidden input for source serial**

Inside the modal form, add a hidden input to store the source serial for comparison in JS:

```html
          <input type="hidden" id="dm_modal_source_serial" value="">
```

(Place this with the other hidden inputs, around line 1450.)

- [ ] **Step 3: Commit**

```bash
git add netbox_data_import/templates/netbox_data_import/import_preview.html
git commit -m "feat: update modal event handler to show current link info"
```

---

## Task 7: Template — Update dmSearch() to show serials and mismatch warning

**Files:**
- Modify: `netbox_data_import/templates/netbox_data_import/import_preview.html`

- [ ] **Step 1: Update dmSearch() function**

Find the `dmSearch()` function (around line 1205). Replace it entirely with:

```javascript
function dmSearch() {
  var q = document.getElementById('dm_search_q').value.trim();
  if (!q) return;
  var sourceSerial = document.getElementById('dm_modal_source_serial').value.trim();

  fetch('{% url "plugins:netbox_data_import:search_objects" %}?type=device&q=' + encodeURIComponent(q))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var container = document.getElementById('dm_search_results');
      container.innerHTML = '';
      if (!data.results.length) {
        container.innerHTML = '<p class="text-muted small">No devices found.</p>';
        return;
      }
      data.results.forEach(function(dev) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-sm btn-outline-secondary me-1 mb-1';

        // Build serial info string
        var serialInfo = '';
        if (sourceSerial && dev.serial) {
          if (dev.serial === sourceSerial) {
            serialInfo = ' <span class="text-success" style="font-size:11px;">✓ SN: ' + dev.serial + '</span>';
          } else {
            serialInfo = ' <span class="text-danger" style="font-size:11px;">⚠ SN: ' + dev.serial + '</span>';
          }
        } else if (dev.serial) {
          serialInfo = ' <span class="text-muted" style="font-size:11px;">SN: ' + dev.serial + '</span>';
        }

        btn.innerHTML = dev.name + (dev.site ? ' (' + dev.site + ')' : '') + serialInfo;
        btn.onclick = function() {
          document.getElementById('dm_netbox_device_id').value = dev.id;
          container.querySelectorAll('button').forEach(function(b) { b.classList.remove('btn-primary'); b.classList.add('btn-outline-secondary'); });
          btn.classList.remove('btn-outline-secondary');
          btn.classList.add('btn-primary');

          // Check for serial mismatch
          if (sourceSerial && dev.serial && dev.serial !== sourceSerial) {
            document.getElementById('dm_serial_source').textContent = sourceSerial;
            document.getElementById('dm_serial_netbox').textContent = dev.serial;
            document.getElementById('dm_serial_warning').style.display = '';
            document.getElementById('dm_submit_btn').textContent = 'Link anyway';
          } else {
            document.getElementById('dm_serial_warning').style.display = 'none';
            document.getElementById('dm_submit_btn').textContent = 'Link selected device';
          }
        };
        container.appendChild(btn);
      });
    })
    .catch(function() {
      document.getElementById('dm_search_results').innerHTML = '<p class="text-danger small">Search failed. Please try again.</p>';
    });
}
```

- [ ] **Step 2: Add source serial display above search box**

In the modal body, find the line with the search input (around line 1466). Before it, add:

```html
            {% if view_mode %}
            <small id="dm_source_serial_label" style="display:none; color:#666;">Source serial: <code id="dm_source_serial_val"></code></small>
            {% endif %}
```

Then update the modal show handler to populate this. In the show handler, add before the trailing `});`:

```javascript
  // Show source serial label if available
  var label = document.getElementById('dm_source_serial_label');
  var val = document.getElementById('dm_source_serial_val');
  if (sourceSerial && label && val) {
    val.textContent = sourceSerial;
    label.style.display = '';
  } else if (label) {
    label.style.display = 'none';
  }
```

- [ ] **Step 3: Commit**

```bash
git add netbox_data_import/templates/netbox_data_import/import_preview.html
git commit -m "feat: add serial display and mismatch warning in dmSearch"
```

---

## Task 8: Tests — UnlinkDeviceViewTest

**Files:**
- Modify: `netbox_data_import/tests/test_views.py`

- [ ] **Step 1: Add UnlinkDeviceViewTest class**

Add this test class at the end of `test_views.py`:

```python
class UnlinkDeviceViewTest(TestCase):
    """Test UnlinkDeviceView."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("test", "test@test.com", "test")
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        cls.dt = DeviceType.objects.create(model="Model1", manufacturer=Manufacturer.objects.create(name="Vendor1", slug="vendor1"), slug="model1")
        cls.role = DeviceRole.objects.create(name="Role1", slug="role1")
        cls.device = Device.objects.create(name="dev-01", device_type=cls.dt, role=cls.role, site=cls.site)
        cls.profile = ImportProfile.objects.create(name="Profile1")
        cls.match = DeviceExistingMatch.objects.create(
            profile=cls.profile,
            source_id="SRC001",
            netbox_device_id=cls.device.pk,
            device_name=cls.device.name,
        )
        cls.url = reverse("plugins:netbox_data_import:unlink_device")

    def test_unlink_removes_match(self):
        """Unlink successfully deletes the DeviceExistingMatch."""
        self.assertTrue(DeviceExistingMatch.objects.filter(pk=self.match.pk).exists())
        self.client.force_login(self.user)
        resp = self.client.post(self.url, {"profile_id": self.profile.pk, "source_id": "SRC001"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(pk=self.match.pk).exists())

    def test_unlink_missing_match_is_idempotent(self):
        """Unlink with non-existent match is idempotent (no error)."""
        self.client.force_login(self.user)
        resp = self.client.post(self.url, {"profile_id": self.profile.pk, "source_id": "NONEXISTENT"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(DeviceExistingMatch.objects.filter(pk=self.match.pk).exists())

    def test_unlink_unauthenticated(self):
        """Unlink requires authentication."""
        resp = self.client.post(self.url, {"profile_id": self.profile.pk, "source_id": "SRC001"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(str(resp.url).startswith("/auth/login"))

    def test_unlink_missing_profile(self):
        """Unlink returns 404 if profile not found."""
        self.client.force_login(self.user)
        resp = self.client.post(self.url, {"profile_id": 99999, "source_id": "SRC001"})
        self.assertEqual(resp.status_code, 404)
```

- [ ] **Step 2: Run tests**

```bash
python manage.py test netbox_data_import.tests.test_views.UnlinkDeviceViewTest --keepdb -v 2
```

Expected: 4 tests pass.

- [ ] **Step 3: Commit**

```bash
git add netbox_data_import/tests/test_views.py
git commit -m "test: add UnlinkDeviceViewTest"
```

---

## Task 9: Tests — SearchNetBoxObjectsSerialTest

**Files:**
- Modify: `netbox_data_import/tests/test_views.py`

- [ ] **Step 1: Add serial assertion test**

Find the existing `test_search_device` method in `SearchNetBoxObjectsViewTest` (around line 1149). Add a new test method right after it:

```python
    def test_search_device_includes_serial(self):
        """Search results include serial field for devices."""
        resp = self.client.get(self.url, {"type": "device", "q": "search-dev"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreater(len(data["results"]), 0)
        first = data["results"][0]
        self.assertIn("serial", first)
        self.assertIn("id", first)
        self.assertIn("name", first)
```

- [ ] **Step 2: Run test**

```bash
python manage.py test netbox_data_import.tests.test_views.SearchNetBoxObjectsViewTest.test_search_device_includes_serial --keepdb -v 2
```

Expected: Test passes.

- [ ] **Step 3: Commit**

```bash
git add netbox_data_import/tests/test_views.py
git commit -m "test: verify device search includes serial field"
```

---

## Task 10: Tests — Template test for unlink button

**Files:**
- Modify: `netbox_data_import/tests/test_views_coverage2.py`

- [ ] **Step 1: Add unlink button presence test**

Add this test method to the `SyncRowButtonTemplateTest` class (or create a new test class `UnlinkButtonTemplateTest` at the end of the file):

```python
class UnlinkButtonTemplateTest(TestCase):
    """Test unlink button presence on manually-linked device rows."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("test_user", "test@example.com", "password")
        cls.site = Site.objects.create(name="Test Site", slug="test-site")
        cls.dt = DeviceType.objects.create(
            model="DT1",
            manufacturer=Manufacturer.objects.create(name="Vendor", slug="vendor"),
            slug="dt1",
        )
        cls.role = DeviceRole.objects.create(name="Role1", slug="role1")
        cls.device = Device.objects.create(
            name="existing-device",
            device_type=cls.dt,
            role=cls.role,
            site=cls.site,
        )
        cls.profile = ImportProfile.objects.create(name="Profile1")
        cls.match = DeviceExistingMatch.objects.create(
            profile=cls.profile,
            source_id="D001",
            netbox_device_id=cls.device.pk,
            device_name=cls.device.name,
        )

    def setUp(self):
        self.client.force_login(self.user)

    @patch("netbox_data_import.views.engine")
    def test_unlink_button_on_manually_linked_update_row(self, mock_engine):
        """Unlink button appears on action=update rows with manual links."""
        from netbox_data_import.engine import ImportResult, RowResult

        mock_result = ImportResult()
        mock_result.rows = [
            RowResult(
                row_number=1,
                source_id="D001",
                name="existing-device",
                action="update",
                object_type="device",
                detail="Would update device 'existing-device'",
            ),
        ]
        mock_result.counts = {}
        mock_result.has_errors = False
        mock_engine.run_import.return_value = mock_result
        mock_engine.reapply_saved_resolutions.return_value = [
            {"_row_number": 1, "source_id": "D001", "device_name": "existing-device"}
        ]

        session = self.client.session
        session["import_rows"] = [{"_row_number": 1, "source_id": "D001", "device_name": "existing-device"}]
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
        self.assertIn(b"unlink-device", resp.content)
        self.assertIn(b"mdi-link-off", resp.content)

    @patch("netbox_data_import.views.engine")
    def test_unlink_button_absent_on_non_linked_row(self, mock_engine):
        """Unlink button does not appear on rows without manual links."""
        from netbox_data_import.engine import ImportResult, RowResult

        mock_result = ImportResult()
        mock_result.rows = [
            RowResult(
                row_number=1,
                source_id="D999",
                name="new-device",
                action="create",
                object_type="device",
                detail="Would create device 'new-device'",
            ),
        ]
        mock_result.counts = {}
        mock_result.has_errors = False
        mock_engine.run_import.return_value = mock_result
        mock_engine.reapply_saved_resolutions.return_value = [
            {"_row_number": 1, "source_id": "D999", "device_name": "new-device"}
        ]

        session = self.client.session
        session["import_rows"] = [{"_row_number": 1, "source_id": "D999", "device_name": "new-device"}]
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
        self.assertNotIn(b"mdi-link-off", resp.content)
```

- [ ] **Step 2: Run tests**

```bash
python manage.py test netbox_data_import.tests.test_views_coverage2.UnlinkButtonTemplateTest --keepdb -v 2
```

Expected: 2 tests pass.

- [ ] **Step 3: Commit**

```bash
git add netbox_data_import/tests/test_views_coverage2.py
git commit -m "test: add unlink button template presence tests"
```

---

## Task 11: Verify full test suite

**Files:** None (testing only)

- [ ] **Step 1: Run full test suite**

```bash
python manage.py test netbox_data_import --keepdb -v 1
```

Expected: All tests pass (should be 481+ tests).

- [ ] **Step 2: Final commit (if needed)**

If any small fixes were needed, commit:

```bash
git commit -m "test: verify full suite passes with unlink + serial features"
```

---

## Summary

This plan implements:
1. ✅ `UnlinkDeviceView` — removes manual device links via form POST
2. ✅ Serial in device search results
3. ✅ Device match context in preview view
4. ✅ Unlink button on manually-linked rows
5. ✅ Current link section in Link modal
6. ✅ Serial display in search results with inline color coding
7. ✅ Serial mismatch warning banner
8. ✅ Comprehensive tests

All tasks self-contained, no placeholders, frequent commits.

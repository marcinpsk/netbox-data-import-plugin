# Multi-Column Merge, Preview Field Display, and Numeric Comparison Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **NEVER add Co-authored-by lines to commits.**

**Goal:** Allow multiple source columns to map to the same NetBox target field (with conflict detection and manual resolution); show all mapped fields in the sync confirmation modal; and fix false field-diff positives caused by float/int representation differences.

**Architecture:** Three loosely coupled changes: (1) remove a DB unique constraint and refactor the engine's column-mapping logic to support N→1 merges; (2) enrich the `_preview_device_row` `extra_data` dict and pass it to new template data attributes; (3) add a `_normalize_for_compare` helper used by `_field_diff`. All three share the same branch.

**Tech Stack:** Python 3.12, Django ORM, openpyxl, Bootstrap 5 modals (template JS)

---

## File Map

| File | What changes |
|---|---|
| `netbox_data_import/engine.py` | Add `_normalize_for_compare`, `_build_grouped_col_map`; refactor `parse_file`, `apply_column_mappings`, `reapply_saved_resolutions`, `_field_diff`, `_preview_device_row` |
| `netbox_data_import/models.py` | Remove `UniqueConstraint` on `ColumnMapping(profile, target_field)` |
| `netbox_data_import/migrations/0013_remove_columnmapping_unique_target.py` | Generated migration |
| `netbox_data_import/views.py` | Add `conflicts_by_row` and `extra_columns_by_row` to `ImportPreviewView` context |
| `netbox_data_import/templates/netbox_data_import/import_preview.html` | Add conflict badge, conflict modal, expand sync button data attrs, update sync modal JS |
| `netbox_data_import/tests/test_engine.py` | New test classes for multi-column merge, `_normalize_for_compare`, field_diff normalization, and extra_data extension |

---

## Task 1: Add `_normalize_for_compare` helper and fix `_field_diff`

**Files:**
- Modify: `netbox_data_import/engine.py` (around line 806–840)
- Test: `netbox_data_import/tests/test_engine.py`

- [ ] **Step 1: Write failing tests**

Add a new test class at the bottom of `netbox_data_import/tests/test_engine.py` (after the last existing class):

```python
from netbox_data_import.engine import _normalize_for_compare


class NormalizeForCompareTest(TestCase):
    """Tests for _normalize_for_compare helper."""

    def test_integer_string_unchanged(self):
        self.assertEqual(_normalize_for_compare("35"), "35")

    def test_float_whole_number_normalized(self):
        """35.0 → '35'"""
        self.assertEqual(_normalize_for_compare("35.0"), "35")

    def test_float_whole_number_direct(self):
        """float(35.0) → '35'"""
        self.assertEqual(_normalize_for_compare(35.0), "35")

    def test_float_with_fraction_unchanged(self):
        """1.5 stays '1.5'"""
        self.assertEqual(_normalize_for_compare(1.5), "1.5")

    def test_none_returns_empty(self):
        self.assertEqual(_normalize_for_compare(None), "")

    def test_non_numeric_string_unchanged(self):
        self.assertEqual(_normalize_for_compare("ABC-123"), "ABC-123")

    def test_zero(self):
        self.assertEqual(_normalize_for_compare(0), "0")

    def test_zero_float(self):
        self.assertEqual(_normalize_for_compare(0.0), "0")
```

Also add a test to the existing `RunImportDryRunTest` class (look for `test_field_diff_no_u_height_when_matches` around line 730, add after it):

```python
    def test_field_diff_u_position_float_vs_int(self):
        """u_position 35.0 from NetBox vs 35 from file must NOT appear in diff."""
        # Arrange: set device to position=35 in NetBox (stored as int), file has 35 (int)
        self._make_existing_device(serial="SN-POS", asset_tag="AT-POS")
        # The existing device has position=None by default; set it:
        from dcim.models import Device
        dev = Device.objects.get(name="existing-server")
        dev.position = 35
        dev.save()

        result = self._call_preview("existing-server", serial="SN-POS", asset_tag="AT-POS",
                                    u_position=35)
        diff = result.extra_data.get("field_diff", {})
        self.assertNotIn("u_position", diff, "35 vs 35 must not appear as a diff")

    def test_field_diff_generic_float_normalization(self):
        """Any field that returns 35.0 from NetBox and 35 from file should not diff."""
        self._make_existing_device(serial="SN-NORM", asset_tag="AT-NORM")
        # serial: both set to same value — no diff expected
        result = self._call_preview("existing-server", serial="SN-NORM", asset_tag="AT-NORM")
        diff = result.extra_data.get("field_diff", {})
        self.assertNotIn("serial", diff)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py test netbox_data_import.tests.test_engine.NormalizeForCompareTest netbox_data_import.tests.test_engine.RunImportDryRunTest.test_field_diff_u_position_float_vs_int -v 2 2>&1 | tail -30"
```

Expected: `ImportError: cannot import name '_normalize_for_compare'` or `AttributeError`.

- [ ] **Step 3: Implement `_normalize_for_compare` and update `_field_diff`**

In `engine.py`, add the helper just before the `_compute_field_diff` function (around line 806):

```python
def _normalize_for_compare(val) -> str:
    """Normalize a value for field-diff comparison.

    Whole-number floats (e.g. 35.0, "35.0") are normalized to their integer
    string form ("35") to avoid false diffs caused by type differences between
    the source file and what NetBox returns.
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

Then update `_field_diff` (the function that currently uses `str()` for comparisons). Replace the whole `candidates` list and the loop that follows it — the relevant section in `_compute_field_diff` starting from `def _compute_field_diff(` at around line 808:

```python
def _compute_field_diff(
    matched_device, device_name, serial, asset_tag, device_face, device_airflow, device_status, u_height, u_position
):
    """Return a dict of fields that differ between the XLS row and the existing NetBox device."""
    diff = {}
    candidates = [
        ("device_name", device_name, matched_device.name),
        ("status", device_status, matched_device.status),
        ("serial", serial or "", matched_device.serial or ""),
        ("asset_tag", asset_tag or "", matched_device.asset_tag or ""),
    ]
    if device_face is not None:
        candidates.append(("face", device_face, matched_device.face if matched_device.face else ""))
    if device_airflow is not None:
        candidates.append(
            ("airflow", device_airflow, matched_device.airflow if matched_device.airflow else "")
        )
    for fname, xls_val, nb_val in candidates:
        if _normalize_for_compare(xls_val) != _normalize_for_compare(nb_val):
            diff[fname] = {"netbox": _normalize_for_compare(nb_val), "file": _normalize_for_compare(xls_val)}
    nb_u_height = matched_device.device_type.u_height if matched_device.device_type_id else None
    if nb_u_height is not None:
        try:
            if float(u_height) != float(nb_u_height):
                diff["u_height"] = {"netbox": str(nb_u_height), "file": str(u_height)}
        except (TypeError, ValueError):
            pass
    if _normalize_for_compare(u_position) != _normalize_for_compare(matched_device.position):
        diff["u_position"] = {
            "netbox": _normalize_for_compare(matched_device.position),
            "file": _normalize_for_compare(u_position),
        }
    return diff
```

Also update the import at the top of `test_engine.py` to include `_normalize_for_compare`:

```python
from netbox_data_import.engine import (
    ImportContext,
    ImportResult,
    ParseError,
    RowResult,
    _ensure_device_type,
    _normalize_for_compare,
    _preview_device_row,
    parse_file,
    run_import,
)
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py test netbox_data_import.tests.test_engine.NormalizeForCompareTest netbox_data_import.tests.test_engine.RunImportDryRunTest.test_field_diff_u_position_float_vs_int netbox_data_import.tests.test_engine.RunImportDryRunTest.test_field_diff_generic_float_normalization -v 2 2>&1 | tail -20"
```

Expected: `OK` for all three test cases.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && netbox-test 2>&1 | tail -10"
```

Expected: all tests pass.

- [ ] **Step 6: Lint**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin && ruff check . && ruff format --check .
```

- [ ] **Step 7: Commit**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin
git add netbox_data_import/engine.py netbox_data_import/tests/test_engine.py
git commit -m "fix(engine): normalize float/int values before field diff comparison"
```

---

## Task 2: Remove ColumnMapping unique constraint (migration)

**Files:**
- Modify: `netbox_data_import/models.py` (line 108–114)
- Create: `netbox_data_import/migrations/0013_remove_columnmapping_unique_target.py`

- [ ] **Step 1: Remove the constraint from `models.py`**

In `netbox_data_import/models.py`, find the `ColumnMapping.Meta` class (around line 108). Change it from:

```python
    class Meta:
        ordering = ["profile", "target_field"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "target_field"], name="ndi_columnmapping_profile_target"),
        ]
        verbose_name = "Column Mapping"
        verbose_name_plural = "Column Mappings"
```

To:

```python
    class Meta:
        ordering = ["profile", "target_field"]
        verbose_name = "Column Mapping"
        verbose_name_plural = "Column Mappings"
```

- [ ] **Step 2: Generate the migration**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py makemigrations netbox_data_import --name remove_columnmapping_unique_target 2>&1"
```

Expected output: `Migrations for 'netbox_data_import': netbox_data_import/migrations/0013_remove_columnmapping_unique_target.py`

- [ ] **Step 3: Review the generated migration**

Open the generated file (path: `netbox_data_import/migrations/0013_remove_columnmapping_unique_target.py`) and verify it contains a `RemoveConstraint` operation for `ndi_columnmapping_profile_target`. It should look roughly like:

```python
class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0012_classrolemapping_rack_type_related_name"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="columnmapping",
            name="ndi_columnmapping_profile_target",
        ),
    ]
```

- [ ] **Step 4: Apply and verify**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py migrate netbox_data_import 2>&1 | tail -5"
```

Expected: `Applying netbox_data_import.0013_remove_columnmapping_unique_target... OK`

- [ ] **Step 5: Run full test suite**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && netbox-test 2>&1 | tail -10"
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin
git add netbox_data_import/models.py netbox_data_import/migrations/0013_remove_columnmapping_unique_target.py
git commit -m "feat(models): allow multiple source columns to map to the same target field"
```

---

## Task 3: Refactor engine for multi-column merge

**Files:**
- Modify: `netbox_data_import/engine.py` (functions: `parse_file`, `apply_column_mappings`, `reapply_saved_resolutions`)
- Test: `netbox_data_import/tests/test_engine.py`

- [ ] **Step 1: Write failing tests**

Add a new test class after the existing `ParseFileTest` class in `test_engine.py`:

```python
class MultiColumnMergeTest(TestCase):
    """Tests for multi-source column merging in parse_file."""

    def _make_merge_profile(self) -> ImportProfile:
        """Profile with 'Serial Number' and 'Service Tag' both mapping to 'serial'."""
        profile = ImportProfile.objects.create(
            name="MergeTest",
            sheet_name="Data",
            update_existing=True,
            create_missing_device_types=True,
        )
        # Minimal mappings to parse the fixture
        for src, tgt in [
            ("Id", "source_id"),
            ("Rack", "rack_name"),
            ("Name", "device_name"),
            ("Class", "device_class"),
            ("Make", "make"),
            ("Model", "model"),
            ("Serial Number", "serial"),
            ("Service Tag", "serial"),  # second source for serial
        ]:
            ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)
        return profile

    def _make_single_row_workbook(
        self,
        serial_number: str | None,
        service_tag: str | None,
    ) -> BytesIO:
        """Build an in-memory Excel file with one data row."""
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(["Id", "Rack", "Name", "Class", "Make", "Model", "Serial Number", "Service Tag"])
        ws.append(["100", "Rack-01", "Dev-01", "Server", "Cisco", "C9300", serial_number, service_tag])
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    def test_single_source_no_conflict(self):
        """When only one source column has a value, it is used with no conflict."""
        profile = self._make_merge_profile()
        rows = parse_file(self._make_single_row_workbook("SN-001", None), profile)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["serial"], "SN-001")
        self.assertNotIn("_conflicts", rows[0])

    def test_both_sources_same_value_no_conflict(self):
        """When both sources have identical values, the value is used with no conflict."""
        profile = self._make_merge_profile()
        rows = parse_file(self._make_single_row_workbook("SAME-42", "SAME-42"), profile)
        self.assertEqual(rows[0]["serial"], "SAME-42")
        self.assertNotIn("_conflicts", rows[0])

    def test_both_sources_different_values_conflict(self):
        """When both sources have different non-empty values, _conflicts is populated."""
        profile = self._make_merge_profile()
        rows = parse_file(self._make_single_row_workbook("ABC-111", "XYZ-999"), profile)
        self.assertIsNone(rows[0].get("serial"))
        self.assertIn("_conflicts", rows[0])
        conflict = rows[0]["_conflicts"]["serial"]
        self.assertIn("Serial Number", conflict)
        self.assertIn("Service Tag", conflict)
        self.assertEqual(conflict["Serial Number"], "ABC-111")
        self.assertEqual(conflict["Service Tag"], "XYZ-999")

    def test_conflict_cleared_by_saved_resolution(self):
        """A saved SourceResolution for the target field clears the conflict."""
        from netbox_data_import.models import SourceResolution
        profile = self._make_merge_profile()

        # Save a resolution for source_id=100, choosing 'ABC-111' for serial
        SourceResolution.objects.create(
            profile=profile,
            source_id="100",
            source_column="_merge_serial",
            original_value="",
            resolved_fields={"serial": "ABC-111"},
        )

        rows = parse_file(self._make_single_row_workbook("ABC-111", "XYZ-999"), profile)
        # The resolution should have cleared the conflict and applied the chosen value
        self.assertEqual(rows[0]["serial"], "ABC-111")
        self.assertFalse(rows[0].get("_conflicts", {}).get("serial"))
```

- [ ] **Step 2: Run failing tests**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py test netbox_data_import.tests.test_engine.MultiColumnMergeTest -v 2 2>&1 | tail -30"
```

Expected: `IntegrityError` or wrong behavior (tests fail).

- [ ] **Step 3: Add `_build_grouped_col_map` helper in `engine.py`**

Add this function near `apply_column_mappings` (around line 244, before `apply_column_mappings`):

```python
def _build_grouped_col_map(profile: ImportProfile) -> dict[str, list[str]]:
    """Return {target_field: [source_col, ...]} mapping for the profile.

    Multiple source columns that target the same field all appear in the list.
    Single-source targets have a single-element list.
    """
    grouped: dict[str, list[str]] = {}
    for cm in profile.column_mappings.all():
        grouped.setdefault(cm.target_field, []).append(cm.source_column)
    return grouped
```

- [ ] **Step 4: Refactor `parse_file` to use grouped col_map**

In `parse_file`, replace the section that builds and uses `col_map` (currently lines 286–316) with the grouped version. The replacement starts just after `ws = wb[profile.sheet_name]` and `raw_headers = _build_header_index_map(ws)`:

```python
    # Build grouped source_column→target_field map
    col_map = _build_grouped_col_map(profile)

    # Unmapped columns: present in the sheet but not in any mapping
    all_mapped_sources = {src for srcs in col_map.values() for src in srcs}
    unmapped_cols = [col for col in raw_headers if col not in all_mapped_sources]

    # Pre-fetch transform rules for efficiency
    transform_rules = list(profile.column_transform_rules.all())

    # Pre-fetch all saved resolutions for this profile (avoids N+1 queries)
    resolutions_by_source_id: dict[str, list] = {}
    for res in profile.source_resolutions.all():
        resolutions_by_source_id.setdefault(str(res.source_id), []).append(res)

    unused_stats: dict[str, dict] = {}
    capture_extra = profile.capture_extra_data

    rows = []
    for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if all(v is None for v in row):
            continue

        row_dict: dict[str, object] = {"_row_number": row_num}
        for target_field, source_cols in col_map.items():
            values: dict[str, object] = {}
            for source_col in source_cols:
                idx = raw_headers.get(source_col)
                if idx is None:
                    continue
                value = row[idx] if idx < len(row) else None
                if isinstance(value, str):
                    value = value.strip()
                if value is not None and str(value).strip():
                    values[source_col] = value

            if not values:
                pass  # Leave key absent; downstream uses .get() with None default
            elif len(set(str(v) for v in values.values())) == 1:
                row_dict[target_field] = next(iter(values.values()))
            else:
                row_dict[target_field] = None
                row_dict.setdefault("_conflicts", {})[target_field] = {
                    k: str(v) for k, v in values.items()
                }

        # Promote explicit extra_json: mappings into _extra_columns
        _promote_extra_json_fields(row_dict)

        _apply_transform_rules(row_dict, row, raw_headers, transform_rules)

        # Apply saved resolutions (rerere) and clear any resolved conflicts
        source_id = row_dict.get("source_id", "")
        if source_id:
            for res in resolutions_by_source_id.get(str(source_id), []):
                row_dict.update(res.resolved_fields)
                for field in res.resolved_fields:
                    row_dict.get("_conflicts", {}).pop(field, None)

        if return_stats or capture_extra:
            extra = _collect_unmapped_values(row, raw_headers, unmapped_cols, unused_stats, return_stats, capture_extra)
            if capture_extra and extra:
                row_dict.setdefault("_extra_columns", {}).update(extra)

        rows.append(row_dict)

    if return_stats:
        return rows, unused_stats
    return rows
```

- [ ] **Step 5: Refactor `apply_column_mappings` to handle multi-source merge**

Replace the entire body of `apply_column_mappings` with:

```python
def apply_column_mappings(rows: list[dict], profile: ImportProfile) -> list[dict]:
    """Re-apply the profile's column mappings to already-parsed session rows.

    Used after a quick-add column mapping so that in-session row dicts reflect
    the new mapping without requiring the source file to be re-uploaded.
    Handles multi-source merge: if a newly-mapped source column conflicts with an
    already-mapped value for the same target field, a _conflicts entry is recorded.
    """
    grouped = _build_grouped_col_map(profile)

    for row in rows:
        for target_field, source_cols in grouped.items():
            # Only process source columns that are still unmapped (sitting as source-name keys)
            unmapped = {}
            for sc in source_cols:
                if sc in row:
                    val = row.pop(sc)
                    if val is not None and str(val).strip():
                        unmapped[sc] = val

            if not unmapped:
                continue  # No newly-mapped source columns present; nothing to do

            existing = row.get(target_field)
            existing_nonempty = existing is not None and str(existing).strip() != ""

            if not existing_nonempty:
                unique = set(str(v) for v in unmapped.values())
                if len(unique) == 1:
                    row[target_field] = next(iter(unmapped.values()))
                else:
                    row[target_field] = None
                    row.setdefault("_conflicts", {})[target_field] = {
                        k: str(v) for k, v in unmapped.items()
                    }
            else:
                all_candidates = {target_field: str(existing), **{k: str(v) for k, v in unmapped.items()}}
                unique = set(all_candidates.values())
                if len(unique) > 1:
                    row[target_field] = None
                    row.setdefault("_conflicts", {})[target_field] = all_candidates
                # else: all agree, keep existing value

        _promote_extra_json_fields(row)

    return rows
```

- [ ] **Step 6: Update `reapply_saved_resolutions` to clear resolved conflicts**

Find `reapply_saved_resolutions` (around line 341) and update the inner loop:

```python
    result = []
    for row in rows:
        source_id = str(row.get("source_id", ""))
        if source_id and source_id in resolutions_by_source_id:
            row = dict(row)  # shallow copy — don't mutate the session dict
            for res in resolutions_by_source_id[source_id]:
                row.update(res.resolved_fields)
                for field in res.resolved_fields:
                    row.get("_conflicts", {}).pop(field, None)
        result.append(row)
    return result
```

- [ ] **Step 7: Run multi-column merge tests**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py test netbox_data_import.tests.test_engine.MultiColumnMergeTest -v 2 2>&1 | tail -20"
```

Expected: all 4 tests pass.

- [ ] **Step 8: Run full test suite**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && netbox-test 2>&1 | tail -10"
```

Expected: all tests pass.

- [ ] **Step 9: Lint**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin && ruff check . && ruff format --check .
```

- [ ] **Step 10: Commit**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin
git add netbox_data_import/engine.py netbox_data_import/tests/test_engine.py
git commit -m "feat(engine): support multiple source columns mapping to the same target field"
```

---

## Task 4: Extend `_preview_device_row` extra_data with full field set and conflicts

**Files:**
- Modify: `netbox_data_import/engine.py` (function `_preview_device_row`, around line 843)
- Test: `netbox_data_import/tests/test_engine.py`

- [ ] **Step 1: Write failing tests**

In `RunImportDryRunTest` (or a new test class after it), add these tests:

```python
    def test_extra_data_includes_face_airflow_status(self):
        """_preview_device_row must include face, airflow, status in extra_data."""
        result = self._call_preview(
            "new-device",
            serial="SN-FIELDS",
            asset_tag="AT-FIELDS",
            device_face="front",
            device_airflow="front-to-rear",
            device_status="staged",
        )
        self.assertEqual(result.action, "create")
        self.assertEqual(result.extra_data.get("face"), "front")
        self.assertEqual(result.extra_data.get("airflow"), "front-to-rear")
        self.assertEqual(result.extra_data.get("status"), "staged")

    def test_extra_data_includes_extra_columns(self):
        """_preview_device_row must pass through _extra_columns from the row."""
        result = self._call_preview_with_row(
            "new-device",
            extra_row_fields={"_extra_columns": {"cf_location": "DC1"}},
        )
        self.assertEqual(result.action, "create")
        self.assertEqual(result.extra_data.get("extra_columns"), {"cf_location": "DC1"})

    def test_extra_data_includes_conflicts(self):
        """_preview_device_row must pass through _conflicts from the row."""
        result = self._call_preview_with_row(
            "new-device",
            extra_row_fields={
                "_conflicts": {"serial": {"Serial Number": "AAA", "Service Tag": "BBB"}}
            },
        )
        self.assertEqual(result.action, "create")
        self.assertIn("conflicts", result.extra_data)
        self.assertIn("serial", result.extra_data["conflicts"])
```

You also need a `_call_preview_with_row` helper in the same test class. Add it after `_call_preview`:

```python
    def _call_preview_with_row(self, device_name, extra_row_fields=None, **kwargs):
        """Like _call_preview but allows injecting extra row dict keys."""
        row = {
            "_row_number": 1,
            "source_id": "test-id",
            "device_name": device_name,
            "rack_name": None,
            "make": "Cisco",
            "model": "C9300",
            "u_height": 1,
            "serial": None,
            "asset_tag": None,
        }
        if extra_row_fields:
            row.update(extra_row_fields)
        from netbox_data_import.engine import _preview_device_row, ImportContext
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            dry_run=True,
        )
        from dcim.models import DeviceType, Device, Rack
        return _preview_device_row(
            row, ctx,
            make="Cisco", model="C9300",
            mfg_slug="cisco", dt_slug="c9300",
            source_id="test-id",
            device_name=device_name,
            serial=row.get("serial"),
            asset_tag=row.get("asset_tag"),
            DeviceType=DeviceType, Device=Device, Rack=Rack,
            **kwargs,
        )
```

Also update `_call_preview` in the same test class to accept `device_face`, `device_airflow`, `device_status` and pass them through to `_preview_device_row`:

```python
    def _call_preview(self, device_name, serial=None, asset_tag=None,
                      ip_fields=None, u_position=None,
                      device_face=None, device_airflow=None, device_status="active"):
        row = {
            "_row_number": 1,
            "source_id": "test-id",
            "device_name": device_name,
            "rack_name": None,
            "make": "Cisco",
            "model": "C9300",
            "u_height": 1,
            "serial": serial,
            "asset_tag": asset_tag,
        }
        from netbox_data_import.engine import _preview_device_row, ImportContext
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            dry_run=True,
        )
        from dcim.models import DeviceType, Device, Rack
        return _preview_device_row(
            row, ctx,
            make="Cisco", model="C9300",
            mfg_slug="cisco", dt_slug="c9300",
            source_id="test-id",
            device_name=device_name,
            serial=serial,
            asset_tag=asset_tag,
            DeviceType=DeviceType, Device=Device, Rack=Rack,
            ip_fields=ip_fields,
            u_position=u_position,
            device_face=device_face,
            device_airflow=device_airflow,
            device_status=device_status,
        )
```

Check how `_call_preview` is currently implemented (around line 610) and update to match the above signature. Use the same body with the added parameters.

- [ ] **Step 2: Run failing tests**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py test netbox_data_import.tests.test_engine.RunImportDryRunTest.test_extra_data_includes_face_airflow_status netbox_data_import.tests.test_engine.RunImportDryRunTest.test_extra_data_includes_extra_columns netbox_data_import.tests.test_engine.RunImportDryRunTest.test_extra_data_includes_conflicts -v 2 2>&1 | tail -20"
```

Expected: tests fail (fields missing from `extra_data`).

- [ ] **Step 3: Update `_preview_device_row` to include all fields in `extra_data`**

In `_preview_device_row`, find the two `return RowResult(...)` calls that build `extra_data` dicts — one for the error case (around line 881) and one for the normal case (around line 962). Update both to include the new fields.

For the **error-return case** (no rack/DT found), add:

```python
            extra_data={
                "source_make": make,
                "source_model": model,
                "mfg_slug": mfg_slug,
                "dt_slug": dt_slug,
                "u_height": u_height,
                "asset_tag": asset_tag or "",
                "source_serial": serial or "",
                "face": device_face or "",
                "airflow": device_airflow or "",
                "status": device_status,
                "extra_columns": row.get("_extra_columns", {}),
                "conflicts": row.get("_conflicts", {}),
                "is_explicit_mapping": is_explicit_mapping,
                "dt_exists": dt_exists,
                **({"_ip": ip_fields} if ip_fields else {}),
            },
```

For the **normal return case** (around line 962), replace the `extra_data` dict:

```python
        extra_data={
            "source_make": make,
            "source_model": model,
            "mfg_slug": mfg_slug,
            "dt_slug": dt_slug,
            "u_height": u_height,
            "u_position": position,
            "asset_tag": asset_tag or "",
            "source_serial": serial or "",
            "face": device_face or "",
            "airflow": device_airflow or "",
            "status": device_status,
            "extra_columns": row.get("_extra_columns", {}),
            "conflicts": row.get("_conflicts", {}),
            "is_explicit_mapping": is_explicit_mapping,
            "dt_exists": dt_exists,
            **({"_ip": ip_fields} if ip_fields else {}),
            **({"field_diff": field_diff} if field_diff is not None else {}),
            **({"netbox_device_id": matched_device.pk} if action == "update" else {}),
        },
```

- [ ] **Step 4: Run tests**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && python manage.py test netbox_data_import.tests.test_engine.RunImportDryRunTest.test_extra_data_includes_face_airflow_status netbox_data_import.tests.test_engine.RunImportDryRunTest.test_extra_data_includes_extra_columns netbox_data_import.tests.test_engine.RunImportDryRunTest.test_extra_data_includes_conflicts -v 2 2>&1 | tail -20"
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && netbox-test 2>&1 | tail -10"
```

- [ ] **Step 6: Lint**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin && ruff check . && ruff format --check .
```

- [ ] **Step 7: Commit**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin
git add netbox_data_import/engine.py netbox_data_import/tests/test_engine.py
git commit -m "feat(engine): include face, airflow, status, extra_columns, and conflicts in preview extra_data"
```

---

## Task 5: Add `conflicts_by_row` to view context

**Files:**
- Modify: `netbox_data_import/views.py` (function `ImportPreviewView.get`, around line 782–802)

- [ ] **Step 1: Add `conflicts_by_row` and `extra_columns_by_row` to the context dict**

In `ImportPreviewView.get`, just before the `return render(...)` call, add:

```python
        conflicts_by_row = {
            str(r.row_number): r.extra_data.get("conflicts", {})
            for r in result.rows
            if r.extra_data.get("conflicts")
        }
        extra_columns_by_row = {
            str(r.row_number): r.extra_data.get("extra_columns", {})
            for r in result.rows
            if r.extra_data.get("extra_columns")
        }
```

Then add both to the context dict inside `return render(...)`:

```python
                "conflicts_by_row": conflicts_by_row,
```

The full `return render(...)` block should become (add the line near the end):

```python
        return render(
            request,
            "netbox_data_import/import_preview.html",
            {
                "result": result,
                "filename": ctx.get("filename", ""),
                "profile_id": ctx.get("profile_id"),
                "profile": profile,
                "view_mode": view_mode,
                "existing_resolutions_json": _json.dumps(existing_resolutions).translate(
                    {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}
                ),
                "existing_resolutions": existing_resolutions,
                "can_create_role": request.user.has_perm("dcim.add_devicerole"),
                "unused_columns": unused_columns,
                "target_field_choices": TARGET_FIELD_CHOICES,
                "syncable_fields": SyncDeviceFieldView._ALLOWED_FIELDS,
                "device_match_source_ids": device_match_source_ids,
                "device_match_info": device_match_info,
                "conflicts_by_row": conflicts_by_row,
                "extra_columns_by_row": extra_columns_by_row,
            },
        )
```

- [ ] **Step 2: Run full suite**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && netbox-test 2>&1 | tail -10"
```

- [ ] **Step 3: Lint**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin && ruff check . && ruff format --check .
```

- [ ] **Step 4: Commit**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin
git add netbox_data_import/views.py
git commit -m "feat(views): add conflicts_by_row and extra_columns_by_row to preview template context"
```

---

## Task 6: Template — conflict indicator, conflict modal, and full fields in sync modal

**Files:**
- Modify: `netbox_data_import/templates/netbox_data_import/import_preview.html`

This task has no unit tests (it is UI behavior); the changes are verified by running the dev server and inspecting the output.

### Part A: Pass `conflicts_by_row` as JSON to the page

- [ ] **Step 1: Add json_script tags for conflicts and extra_columns data**

Near the existing `json_script` tags (around line 689–690, after `{{ device_match_info|json_script:"ndi-device-match-info" }}`), add:

```html
{{ conflicts_by_row|json_script:"ndi-conflicts-by-row" }}
{{ extra_columns_by_row|json_script:"ndi-extra-columns-by-row" }}
```

### Part B: Conflict indicator badge on preview rows

- [ ] **Step 2: Add conflict badge in the row action column**

Find the section where the unlink/link buttons are rendered (around line 540–565, before `{% if row.action == 'create' and ... %}`). Add the conflict badge right before the sync button `{% if %}` block:

```html
                {# Conflict badge: shown when two source columns disagree on the same target field #}
                {% if row.extra_data.conflicts %}
                <span class="badge text-bg-warning ms-1 ndi-conflict-badge"
                      style="cursor:pointer;"
                      data-bs-toggle="modal"
                      data-bs-target="#conflictModal"
                      data-row-number="{{ row.row_number }}"
                      data-source-id="{{ row.source_id }}"
                      title="This row has field conflicts that require manual resolution before import">
                  <i class="mdi mdi-alert"></i> {{ row.extra_data.conflicts|length }} conflict{{ row.extra_data.conflicts|length|pluralize }}
                </span>
                {% endif %}
```

### Part C: Conflict resolution modal

- [ ] **Step 3: Add the conflict resolution modal HTML**

Find the end of the existing `splitModal` block (around line 687, just before `{{ existing_resolutions|json_script:... }}`). Add the new modal just before it:

```html
{# ── Conflict Resolution Modal ─────────────────────────────────────────── #}
<div class="modal fade" id="conflictModal" tabindex="-1" aria-labelledby="conflictModalLabel" aria-hidden="true">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="conflictModalLabel">
          <i class="mdi mdi-alert-circle-outline"></i> Resolve Field Conflict
        </h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <form method="post" action="{% url 'plugins:netbox_data_import:save_resolution' %}" id="conflictForm">
        {% csrf_token %}
        <input type="hidden" name="profile_id" value="{{ profile_id }}">
        <input type="hidden" name="source_id" id="conf_source_id">
        <input type="hidden" name="source_column" id="conf_source_column">
        <input type="hidden" name="original_value" id="conf_original_value" value="">
        <input type="hidden" name="next" value="{{ request.get_full_path }}">
        <input type="hidden" name="resolved_fields" id="conf_resolved_fields">
        <div class="modal-body">
          <p class="text-muted small mb-3">
            Multiple source columns provide different values for the same field.
            Select which value to use. Your choice will be saved and applied automatically on future imports.
          </p>
          <div id="conflictModalBody">
            {# Populated by JS #}
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        </div>
      </form>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Add conflict modal JavaScript**

Near the bottom of the template, before the closing `</script>` of the last script block or in a new `<script>` block, add:

```html
<script>
(function () {
  var CONFLICTS_BY_ROW = JSON.parse(
    (document.getElementById('ndi-conflicts-by-row') || {textContent: '{}'}).textContent
  );

  var conflictModal = document.getElementById('conflictModal');
  if (!conflictModal) return;

  conflictModal.addEventListener('show.bs.modal', function (e) {
    var trigger = e.relatedTarget;
    if (!trigger) return;

    var rowNumber = trigger.dataset.rowNumber;
    var sourceId = trigger.dataset.sourceId;
    document.getElementById('conf_source_id').value = sourceId || '';

    var conflicts = CONFLICTS_BY_ROW[rowNumber] || {};
    var body = document.getElementById('conflictModalBody');
    body.innerHTML = '';

    var FIELD_LABELS = {
      serial: 'Serial Number', asset_tag: 'Asset Tag', device_name: 'Device Name',
      rack_name: 'Rack', u_position: 'U Position', u_height: 'U Height',
      face: 'Face', airflow: 'Airflow', status: 'Status', make: 'Make', model: 'Model',
    };

    for (var fieldName in conflicts) {
      var candidates = conflicts[fieldName];
      var section = document.createElement('div');
      section.className = 'mb-3';

      var heading = document.createElement('h6');
      heading.textContent = FIELD_LABELS[fieldName] || fieldName;
      section.appendChild(heading);

      var table = document.createElement('table');
      table.className = 'table table-sm table-bordered mb-0';

      for (var sourceName in candidates) {
        var value = candidates[sourceName];
        var tr = document.createElement('tr');

        var tdSource = document.createElement('td');
        tdSource.className = 'text-muted';
        tdSource.textContent = sourceName;

        var tdValue = document.createElement('td');
        tdValue.textContent = value;

        var tdBtn = document.createElement('td');
        tdBtn.style.width = '100px';
        var useBtn = document.createElement('button');
        useBtn.type = 'button';
        useBtn.className = 'btn btn-sm btn-outline-primary ndi-conflict-resolve-btn';
        useBtn.textContent = 'Use this';
        useBtn.dataset.fieldName = fieldName;
        useBtn.dataset.value = value;
        tdBtn.appendChild(useBtn);

        tr.appendChild(tdSource);
        tr.appendChild(tdValue);
        tr.appendChild(tdBtn);
        table.appendChild(tr);
      }

      section.appendChild(table);
      body.appendChild(section);
    }
  });

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.ndi-conflict-resolve-btn');
    if (!btn) return;

    var fieldName = btn.dataset.fieldName;
    var value = btn.dataset.value;
    var resolved = {};
    resolved[fieldName] = value;

    document.getElementById('conf_source_column').value = '_merge_' + fieldName;
    document.getElementById('conf_original_value').value = '';
    document.getElementById('conf_resolved_fields').value = JSON.stringify(resolved);
    document.getElementById('conflictForm').submit();
  });
})();
</script>
```

### Part D: Extend sync modal to show all fields

- [ ] **Step 5: Add new data attributes to the sync button**

Find the `ndi-sync-row-btn` button element (around line 550). It currently ends at `title="Create this {{ row.object_type }} in NetBox now">`. Add these data attributes:

```html
                        data-serial="{{ row.extra_data.source_serial|default:'' }}"
                        data-u-position="{{ row.extra_data.u_position|default:'' }}"
                        data-u-height="{{ row.extra_data.u_height|default:'' }}"
                        data-face="{{ row.extra_data.face|default:'' }}"
                        data-airflow="{{ row.extra_data.airflow|default:'' }}"
                        data-status="{{ row.extra_data.status|default:'' }}"
```

After this change the full button element should look like:

```html
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
                        data-serial="{{ row.extra_data.source_serial|default:'' }}"
                        data-u-position="{{ row.extra_data.u_position|default:'' }}"
                        data-u-height="{{ row.extra_data.u_height|default:'' }}"
                        data-face="{{ row.extra_data.face|default:'' }}"
                        data-airflow="{{ row.extra_data.airflow|default:'' }}"
                        data-status="{{ row.extra_data.status|default:'' }}"
                        title="Create this {{ row.object_type }} in NetBox now">
                  <i class="mdi mdi-lightning-bolt"></i> Sync to NetBox
                </button>
```

- [ ] **Step 6: Update sync modal `fieldDefs` to include all standard fields**

Find the `var fieldDefs = [` array in the sync modal JS (around line 2005). Replace the entire `fieldDefs` array with:

```javascript
    var fieldDefs = [
      ['Name', btn.dataset.name, 'device_name'],
      ['Rack', btn.dataset.rackName, 'rack_name'],
      ['Source ID', btn.dataset.sourceId, 'source_id'],
      ['Manufacturer', btn.dataset.sourceMake, 'source_make'],
      ['Model', btn.dataset.sourceModel, 'source_model'],
      ['Asset Tag', btn.dataset.assetTag, 'asset_tag'],
      ['Rack Type', btn.dataset.rackTypeName, 'rack_type'],
      ['Serial', btn.dataset.serial, 'serial'],
      ['U Position', btn.dataset.uPosition, 'u_position'],
      ['U Height', btn.dataset.uHeight, 'u_height'],
      ['Face', btn.dataset.face, 'face'],
      ['Airflow', btn.dataset.airflow, 'airflow'],
      ['Status', btn.dataset.status, 'status'],
    ];
```

- [ ] **Step 7: Show `extra_columns` (custom fields) in sync modal**

In the sync modal JS handler (the `show.bs.modal` listener for `#syncRowModal`), after the `fieldDefs` loop that fills the table body, add code to read extra_columns from the json_script data and append them. Find the section that loops over `fieldDefs` (it ends with something like `tbody.appendChild(tr);` in a for loop). After that loop, add:

```javascript
    // Append extra_columns (custom fields / unmapped columns) below standard fields
    var EXTRA_COLUMNS_BY_ROW = JSON.parse(
      (document.getElementById('ndi-extra-columns-by-row') || {textContent: '{}'}).textContent
    );
    var rowNumber = btn.dataset.rowNumber;
    var extraCols = EXTRA_COLUMNS_BY_ROW[rowNumber] || {};
    for (var ecKey in extraCols) {
      var ecVal = String(extraCols[ecKey]);
      if (!ecVal) continue;
      var ecTr = document.createElement('tr');
      var ecTd1 = document.createElement('td');
      ecTd1.className = 'text-muted small';
      ecTd1.textContent = ecKey;
      var ecTd2 = document.createElement('td');
      ecTd2.textContent = ecVal;
      ecTr.appendChild(ecTd1);
      ecTr.appendChild(ecTd2);
      tbody.appendChild(ecTr);
    }
```

- [ ] **Step 8: Run full test suite**

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && netbox-test 2>&1 | tail -10"
```

- [ ] **Step 9: Lint**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin && ruff check . && ruff format --check .
```

- [ ] **Step 10: Commit**

```bash
cd /home/mzieba/workspace/netbox-data-import-plugin
git add netbox_data_import/templates/netbox_data_import/import_preview.html
git commit -m "feat(preview): add conflict resolution UI and show all fields in sync modal"
```

---

## Verification

After all tasks are complete, run the full test suite one final time:

```bash
docker exec netbox-interfacenamerules-plugin_devcontainer-devcontainer-1 bash -c "cd /workspace && netbox-test 2>&1 | tail -15"
```

Expected output: `Ran N tests in Xs` / `OK` with no failures.

Then check push eligibility:

```bash
git --no-pager log --format="%ai" origin/feat/per-row-sync -1
```

If the last push was more than 1 hour ago, push:

```bash
git push origin feat/per-row-sync
```

# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for parse_file return_stats / capture_extra_data and _store_source_id extra_columns."""

import os
from unittest.mock import MagicMock

from django.test import TestCase

from netbox_data_import.engine import _collect_unmapped_values, _promote_extra_json_fields, _store_source_id, parse_file
from netbox_data_import.models import ColumnMapping, ImportProfile


FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")


def _make_profile(name="ExtraTest", capture_extra=False) -> ImportProfile:
    profile = ImportProfile.objects.create(
        name=name,
        sheet_name="Data",
        source_id_column="Id",
        custom_field_name="",
        update_existing=False,
        create_missing_device_types=False,
        capture_extra_data=capture_extra,
    )
    for src, tgt in {
        "Id": "source_id",
        "Rack": "rack_name",
        "Name": "device_name",
        "Make": "make",
        "Model": "model",
    }.items():
        ColumnMapping.objects.create(profile=profile, source_column=src, target_field=tgt)
    return profile


class ParseFileReturnStatsTest(TestCase):
    """parse_file with return_stats=True returns (rows, unused_stats)."""

    def test_returns_tuple_when_return_stats_true(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile()
            result = parse_file(fh, profile, return_stats=True)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        rows, stats = result
        self.assertIsInstance(rows, list)
        self.assertIsInstance(stats, dict)

    def test_returns_list_when_return_stats_false(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("ListTest")
            result = parse_file(fh, profile, return_stats=False)
        self.assertIsInstance(result, list)

    def test_stats_only_has_unmapped_columns(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("StatsTest")
            _, stats = parse_file(fh, profile, return_stats=True)
        # Mapped columns should NOT appear in stats
        for mapped_col in ("Id", "Rack", "Name", "Make", "Model"):
            self.assertNotIn(mapped_col, stats)

    def test_stats_have_count_and_samples(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("SamplesTest")
            _, stats = parse_file(fh, profile, return_stats=True)
        for col, entry in stats.items():
            self.assertIn("count", entry)
            self.assertIn("samples", entry)
            self.assertGreater(entry["count"], 0)
            self.assertIsInstance(entry["samples"], list)
            self.assertLessEqual(len(entry["samples"]), 5)

    def test_stats_counts_only_nonempty_values(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("NonEmptyTest")
            _, stats = parse_file(fh, profile, return_stats=True)
        for col, entry in stats.items():
            # Every sample must be a non-empty string
            for s in entry["samples"]:
                self.assertTrue(s.strip(), f"Empty sample in column '{col}'")


class ParseFileExtraColumnsTest(TestCase):
    """parse_file populates _extra_columns on each row when capture_extra_data=True."""

    def test_no_extra_columns_when_capture_disabled(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("NoExtraTest", capture_extra=False)
            rows = parse_file(fh, profile)
        for row in rows:
            self.assertNotIn("_extra_columns", row)

    def test_extra_columns_present_when_capture_enabled(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("WithExtraTest", capture_extra=True)
            rows = parse_file(fh, profile)
        # At least some rows should have _extra_columns (fixture has unmapped columns)
        has_extra = any("_extra_columns" in row for row in rows)
        self.assertTrue(has_extra, "Expected at least one row with _extra_columns")

    def test_extra_columns_exclude_mapped_fields(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("ExcludeTest", capture_extra=True)
            rows = parse_file(fh, profile)
        mapped = {"Id", "Rack", "Name", "Make", "Model"}
        for row in rows:
            extra = row.get("_extra_columns", {})
            for col in extra:
                self.assertNotIn(col, mapped, f"Mapped column '{col}' leaked into _extra_columns")

    def test_extra_columns_no_empty_values(self):
        with open(FIXTURE_PATH, "rb") as fh:
            profile = _make_profile("EmptyFilterTest", capture_extra=True)
            rows = parse_file(fh, profile)
        for row in rows:
            for col, val in row.get("_extra_columns", {}).items():
                self.assertTrue(str(val).strip(), f"Empty value in _extra_columns['{col}']")


class StoreSourceIdExtraTest(TestCase):
    """_store_source_id writes extra_columns to data_import_source['extra']."""

    def _make_mock_obj(self):
        obj = MagicMock()
        obj.custom_field_data = {}
        return obj

    def _make_profile_obj(self):
        return ImportProfile(
            name="StoreTest",
            pk=99,
            custom_field_name="",
        )

    def test_extra_columns_written_to_data_import_source(self):
        obj = self._make_mock_obj()
        profile = self._make_profile_obj()
        extra = {"JiraID": "JIRA-123", "Location": "DC1"}
        _store_source_id(obj, profile, "SRC-1", extra_columns=extra)
        stored = obj.custom_field_data.get("data_import_source", {})
        self.assertEqual(stored.get("extra"), extra)
        self.assertEqual(stored.get("source_id"), "SRC-1")

    def test_no_extra_key_when_extra_is_none(self):
        obj = self._make_mock_obj()
        profile = self._make_profile_obj()
        _store_source_id(obj, profile, "SRC-2", extra_columns=None)
        stored = obj.custom_field_data.get("data_import_source", {})
        self.assertNotIn("extra", stored)

    def test_no_extra_key_when_extra_is_empty_dict(self):
        obj = self._make_mock_obj()
        profile = self._make_profile_obj()
        _store_source_id(obj, profile, "SRC-3", extra_columns={})
        stored = obj.custom_field_data.get("data_import_source", {})
        self.assertNotIn("extra", stored)


class PromoteExtraJsonFieldsTest(TestCase):
    """Unit tests for _promote_extra_json_fields."""

    def test_moves_non_empty_value_to_extra_columns(self):
        """extra_json:<key> with a value is moved into _extra_columns."""
        row = {"device_name": "host", "extra_json:jira_id": "JIRA-123"}
        _promote_extra_json_fields(row)
        self.assertNotIn("extra_json:jira_id", row)
        self.assertEqual(row["_extra_columns"]["jira_id"], "JIRA-123")

    def test_multiple_extra_json_fields(self):
        """Multiple extra_json: keys are all promoted."""
        row = {"extra_json:jira_id": "JIRA-1", "extra_json:ticket": "T-99"}
        _promote_extra_json_fields(row)
        self.assertEqual(row["_extra_columns"]["jira_id"], "JIRA-1")
        self.assertEqual(row["_extra_columns"]["ticket"], "T-99")

    def test_none_value_not_added_to_extra_columns(self):
        """extra_json: key with None value is removed but not written to _extra_columns."""
        row = {"device_name": "host", "extra_json:jira_id": None}
        _promote_extra_json_fields(row)
        self.assertNotIn("extra_json:jira_id", row)
        self.assertNotIn("_extra_columns", row)

    def test_empty_string_value_not_added_to_extra_columns(self):
        """extra_json: key with empty-string value is removed but not written to _extra_columns."""
        row = {"device_name": "host", "extra_json:jira_id": ""}
        _promote_extra_json_fields(row)
        self.assertNotIn("extra_json:jira_id", row)
        self.assertNotIn("_extra_columns", row)

    def test_non_extra_json_keys_untouched(self):
        """Keys not starting with extra_json: are not affected."""
        row = {"device_name": "host", "serial": "ABC123"}
        _promote_extra_json_fields(row)
        self.assertEqual(row["device_name"], "host")
        self.assertEqual(row["serial"], "ABC123")


class CollectUnmappedValuesTest(TestCase):
    """Unit tests for _collect_unmapped_values."""

    def test_empty_string_value_is_skipped(self):
        """Cells containing only whitespace are excluded (line 194: continue)."""
        # row is a list, raw_headers maps column name to index
        row = ["  ", "\t"]
        raw_headers = {"col1": 0, "col2": 1}
        unmapped_cols = ["col1", "col2"]
        result = _collect_unmapped_values(row, raw_headers, unmapped_cols, {}, False, True)
        self.assertEqual(result, {})

    def test_non_empty_values_included(self):
        """Cells with real values are collected."""
        row = ["value1", "  "]
        raw_headers = {"col1": 0, "col2": 1}
        unmapped_cols = ["col1", "col2"]
        result = _collect_unmapped_values(row, raw_headers, unmapped_cols, {}, False, True)
        self.assertEqual(result, {"col1": "value1"})

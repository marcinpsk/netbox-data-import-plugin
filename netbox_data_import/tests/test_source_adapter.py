# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Use SimpleTestCase to enforce the Source Adapter's no-database boundary."""

from io import BytesIO

import openpyxl
from django.test import SimpleTestCase
from openpyxl.worksheet.worksheet import Worksheet

from netbox_data_import.adapters import FlatWorkbookAdapter, SourceBatch, SourceUnreadable
from netbox_data_import.flat_workbook import FlatWorkbookConfig, TransformRule


def _workbook(sheet_name, header, *rows):
    """Build workbook bytes in memory so SimpleTestCase keeps database access disabled."""
    book = openpyxl.Workbook()
    sheet = book.active
    if not isinstance(sheet, Worksheet):
        sheet = book.create_sheet()
    sheet.title = sheet_name
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class FlatWorkbookInterpretTest(SimpleTestCase):
    """The flat adapter turns a workbook plus its configuration into a Source Batch."""

    def test_it_reads_rows_without_a_database(self):
        """The whole point of the seam: parsing needs the configuration, never the ORM."""
        content = _workbook("Data", ("Id", "Name"), ("SRC-1", "device-a"), ("SRC-2", "device-b"))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={"source_id": ("Id",), "device_name": ("Name",)},
        )

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertIsInstance(batch, SourceBatch)
        self.assertEqual(
            [(row["source_id"], row["device_name"]) for row in batch.rows],
            [("SRC-1", "device-a"), ("SRC-2", "device-b")],
        )
        self.assertEqual([row["_row_number"] for row in batch.rows], [2, 3])

    def test_it_skips_a_fully_empty_row(self):
        """A blank spreadsheet row is not a source item."""
        content = _workbook("Data", ("Id",), ("SRC-1",), (None,), ("SRC-2",))
        config = FlatWorkbookConfig(sheet_name="Data", column_map={"source_id": ("Id",)})

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertEqual([row["source_id"] for row in batch.rows], ["SRC-1", "SRC-2"])

    def test_two_columns_feeding_one_field_agree_or_conflict(self):
        """Multi-source merge is a source-format fact, so it belongs to the adapter."""
        content = _workbook("Data", ("A", "B"), ("same", "same"), ("left", "right"))
        config = FlatWorkbookConfig(sheet_name="Data", column_map={"serial": ("A", "B")})

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertEqual(batch.rows[0]["serial"], "same")
        self.assertIsNone(batch.rows[1]["serial"])
        self.assertEqual(batch.rows[1]["_conflicts"]["serial"], {"A": "left", "B": "right"})

    def test_a_transform_rule_splits_one_column_into_two_fields(self):
        """Transform rules are configuration the adapter applies, not policy it looks up."""
        content = _workbook("Data", ("Combined",), ("rack-7/U12",))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={},
            transform_rules=(
                TransformRule(
                    source_column="Combined",
                    pattern=r"(.+)/U(\d+)",
                    group_1_target="rack_name",
                    group_2_target="u_position",
                ),
            ),
        )

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertEqual(batch.rows[0]["rack_name"], "rack-7")
        self.assertEqual(batch.rows[0]["u_position"], "12")

    def test_a_transform_input_is_not_reported_or_captured_as_unused(self):
        """A transform consumes its source column even when no direct mapping names it."""
        content = _workbook("Data", ("Combined", "Notes"), ("rack-7/U12", "on loan"))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={},
            transform_rules=(
                TransformRule(
                    source_column="Combined",
                    pattern=r"(.+)/U(\d+)",
                    group_1_target="rack_name",
                    group_2_target="u_position",
                ),
            ),
            capture_extra_data=True,
        )

        batch = FlatWorkbookAdapter.interpret(content, config, collect_unused=True)

        self.assertEqual(batch.rows[0]["_extra_columns"], {"Notes": "on loan"})
        self.assertEqual(batch.unused_columns, {"Notes": {"count": 1, "samples": ["on loan"]}})

    def test_capture_extra_data_keeps_the_unmapped_columns(self):
        """The adapter reports what it did not map, so nothing is lost silently."""
        content = _workbook("Data", ("Id", "Notes"), ("SRC-1", "on loan"))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={"source_id": ("Id",)},
            capture_extra_data=True,
        )

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertEqual(batch.rows[0]["_extra_columns"], {"Notes": "on loan"})

    def test_an_unmapped_column_is_dropped_when_capture_is_off(self):
        """Capture is opt-in, so the default keeps the row to its mapped fields."""
        content = _workbook("Data", ("Id", "Notes"), ("SRC-1", "on loan"))
        config = FlatWorkbookConfig(sheet_name="Data", column_map={"source_id": ("Id",)})

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertNotIn("_extra_columns", batch.rows[0])

    def test_a_missing_sheet_names_the_ones_the_file_has(self):
        """The operator picked the wrong sheet, so the message has to list the real ones."""
        content = _workbook("Data", ("Id",), ("SRC-1",))
        config = FlatWorkbookConfig(sheet_name="Absent", column_map={"source_id": ("Id",)})

        with self.assertRaises(SourceUnreadable) as caught:
            FlatWorkbookAdapter.interpret(content, config)

        self.assertIn("Absent", str(caught.exception))
        self.assertIn("Data", str(caught.exception))

    def test_unreadable_bytes_are_refused_as_a_source_error(self):
        """A caller gets one adapter error, not whatever openpyxl raised underneath."""
        config = FlatWorkbookConfig(sheet_name="Data", column_map={})

        with self.assertRaises(SourceUnreadable):
            FlatWorkbookAdapter.interpret(b"not a workbook", config)

    def test_an_invalid_transform_pattern_names_its_column(self):
        """A bad regex is configuration the operator has to find and fix."""
        content = _workbook("Data", ("Combined",), ("anything",))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={},
            transform_rules=(TransformRule(source_column="Combined", pattern="(unclosed", group_1_target="rack_name"),),
        )

        with self.assertRaises(SourceUnreadable) as caught:
            FlatWorkbookAdapter.interpret(content, config)

        self.assertIn("Combined", str(caught.exception))

    def test_a_pathological_transform_pattern_is_time_bounded(self):
        """One configured regex cannot hold the import worker indefinitely."""
        content = _workbook("Data", ("Combined",), ("a" * 64 + "!",))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={},
            transform_rules=(
                TransformRule(source_column="Combined", pattern=r"^(a|aa)+$", group_1_target="rack_name"),
            ),
        )

        with self.assertRaises(SourceUnreadable) as caught:
            FlatWorkbookAdapter.interpret(content, config)

        self.assertIn("timed out", str(caught.exception).lower())

    def test_a_nontrivial_transform_has_a_practical_time_budget(self):
        """A valid workbook must not fail because its regex needs more than a scheduler tick."""
        content = _workbook("Data", ("Combined",), ("a" * 26 + "!",))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={},
            transform_rules=(
                TransformRule(source_column="Combined", pattern=r"^(a|aa)+$", group_1_target="rack_name"),
            ),
        )

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertNotIn("rack_name", batch.rows[0])

    def test_transform_time_budget_is_shared_by_the_workbook(self):
        """Many individually bounded matches must not hold one import worker indefinitely."""
        content = _workbook("Data", ("Combined",), *(("a" * 26 + "!",) for _index in range(32)))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={},
            transform_rules=(
                TransformRule(source_column="Combined", pattern=r"^(a|aa)+$", group_1_target="rack_name"),
            ),
        )

        with self.assertRaises(SourceUnreadable) as caught:
            FlatWorkbookAdapter.interpret(content, config)

        self.assertIn("timed out", str(caught.exception).lower())

    def test_a_candidate_column_is_kept_for_review_rather_than_written(self):
        """A candidate target offers review choices, so its values stay grouped by source column."""
        content = _workbook("Data", ("Owner", "Backup"), ("ada", "grace"))
        config = FlatWorkbookConfig(sheet_name="Data", column_map={"candidate:contact": ("Owner", "Backup")})

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertEqual(batch.rows[0]["_candidate_values"]["contact"], {"Owner": "ada", "Backup": "grace"})

    def test_the_unmapped_tally_counts_rows_and_keeps_a_few_samples(self):
        """The setup step reports what the profile does not map, so the operator can map it."""
        content = _workbook("Data", ("Id", "Notes"), ("SRC-1", "first"), ("SRC-2", "second"))
        config = FlatWorkbookConfig(sheet_name="Data", column_map={"source_id": ("Id",)})

        batch = FlatWorkbookAdapter.interpret(content, config, collect_unused=True)

        self.assertEqual(batch.unused_columns["Notes"]["count"], 2)
        self.assertEqual(batch.unused_columns["Notes"]["samples"], ["first", "second"])

    def test_an_extra_json_mapping_becomes_a_captured_column(self):
        """`extra_json:<name>` is a declared passthrough, not a Target Field the writer knows."""
        content = _workbook("Data", ("Owner",), ("ada",))
        config = FlatWorkbookConfig(sheet_name="Data", column_map={"extra_json:owner": ("Owner",)})

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertEqual(batch.rows[0]["_extra_columns"], {"owner": "ada"})

    def test_an_extra_json_transform_becomes_a_captured_column(self):
        """A transformed passthrough follows the same row shape as a directly mapped one."""
        content = _workbook("Data", ("Owner",), ("owner=ada",))
        config = FlatWorkbookConfig(
            sheet_name="Data",
            column_map={},
            transform_rules=(
                TransformRule(
                    source_column="Owner",
                    pattern=r"owner=(.+)",
                    group_1_target="extra_json:owner",
                ),
            ),
        )

        batch = FlatWorkbookAdapter.interpret(content, config)

        self.assertEqual(batch.rows[0]["_extra_columns"], {"owner": "ada"})
        self.assertNotIn("extra_json:owner", batch.rows[0])


class SourceAdapterContractTest(SimpleTestCase):
    """The base declares the seam; a subclass that forgets it must fail loudly."""

    def test_the_base_refuses_to_interpret(self):
        """`interpret` is the contract, so the base cannot silently return nothing."""
        from netbox_data_import.adapters import SourceAdapter

        with self.assertRaises(NotImplementedError):
            SourceAdapter.interpret(b"", None)

    def test_the_base_refuses_to_name_a_configuration_form(self):
        """Every adapter validates its own `adapter_config` at the boundary."""
        from netbox_data_import.adapters import SourceAdapter

        with self.assertRaises(NotImplementedError):
            SourceAdapter.config_form_class()

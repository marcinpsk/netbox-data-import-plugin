# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A Source Adapter interprets one file format and reaches nothing else.

`SimpleTestCase` is the assertion, not a convenience: it forbids database access, so an adapter that
grew an ORM read would fail these tests rather than pass them quietly.
"""

from io import BytesIO

import openpyxl
from django.test import SimpleTestCase

from netbox_data_import.adapters import FlatWorkbookAdapter, SourceBatch, SourceUnreadable
from netbox_data_import.flat_workbook import FlatWorkbookConfig, TransformRule


def _workbook(sheet_name, header, *rows):
    """Return the bytes of a one-sheet workbook."""
    book = openpyxl.Workbook()
    sheet = book.active
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

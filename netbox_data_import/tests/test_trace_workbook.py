# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Exercise the trace Source Adapter with no database access."""

from io import BytesIO
import json
from pathlib import Path

from django.test import SimpleTestCase
import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

from netbox_data_import.adapters import SourceBatch, SourceUnreadable, TraceWorkbookAdapter
from netbox_data_import.catalog import OutputKind
from netbox_data_import.trace_workbook import parse_endpoint_line

FIXTURES = Path(__file__).parent / "fixtures"

PATH_HEADER = (
    "Port",
    "PortClass",
    "Cards",
    "Device",
    "UPos",
    "Rack",
    "Location",
    "CableClass",
    "Port",
    "PortClass",
    "Cards",
    "Device",
    "UPos",
    "Rack",
    "Location",
)
LIST_HEADER = ("Location", "Rack", "UPos", "Device", "Cards", "Port", "PortClass", "Cable")


def _termination(device, cards, port, port_class):
    """Return one compact termination tuple for an in-memory workbook."""
    return device, cards, port, port_class


def _endpoint_line(termination):
    """Render one endpoint line in the source format."""
    device, cards, port, port_class = termination
    parts = [device]
    if cards:
        parts.append(cards)
    parts.append(f"{port} ({port_class})")
    return " > ".join(parts)


def _segment(left, cable_class, right):
    """Render one Segment Evidence row in the source column order."""
    left_device, left_cards, left_port, left_class = left
    right_device, right_cards, right_port, right_class = right
    return (
        left_port,
        left_class,
        left_cards,
        left_device,
        "",
        "",
        "",
        cable_class,
        right_port,
        right_class,
        right_cards,
        right_device,
        "",
        "",
        "",
    )


def _visit(termination):
    """Render one Trace List visit row."""
    device, cards, port, port_class = termination
    return "", "", "", device, cards, port, port_class, "Ignored"


def _add_sheet(book, name, header, blocks):
    """Add one trace sheet with the supplied source blocks."""
    sheet = book.create_sheet(name)
    sheet.append(("Executed", "2026-08-31 12:00:00"))
    sheet.append(())
    for from_line, to_line, rows in blocks:
        sheet.append(("From", from_line))
        sheet.append(("To", to_line))
        sheet.append(header)
        for row in rows:
            sheet.append(row)
    return sheet


def _workbook(*, path_blocks=(), list_blocks=(), include_path=True, include_list=False):
    """Build workbook bytes with fixed trace sheet names."""
    book = openpyxl.Workbook()
    active = book.active
    if isinstance(active, Worksheet):
        book.remove(active)
    if include_path:
        _add_sheet(book, "Trace From To", PATH_HEADER, path_blocks)
    if include_list:
        _add_sheet(book, "Trace List", LIST_HEADER, list_blocks)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _interpret(content):
    """Run the public Source Adapter seam with its empty configuration."""
    return TraceWorkbookAdapter.interpret(content, {})


def _codes(batch):
    """Return all batch diagnostic codes in source order."""
    return [diagnostic.code for diagnostic in batch.diagnostics]


class TraceWorkbookFixtureTest(SimpleTestCase):
    """The committed workbooks define the real trace format."""

    def test_copper_fixture_collapses_duplicate_blocks(self):
        """The copper corpus produces ten valid three-segment Source Traces."""
        content = (FIXTURES / "trace_copper.xlsx").read_bytes()

        batch = _interpret(content)

        self.assertIsInstance(batch, SourceBatch)
        self.assertEqual(batch.output_kinds, frozenset({OutputKind.SOURCE_TRACE}))
        self.assertEqual(len(batch.rows), 10)
        self.assertTrue(all(trace.valid for trace in batch.rows))
        self.assertEqual({len(trace.segments) for trace in batch.rows}, {3})
        self.assertEqual({len(trace.provenance) for trace in batch.rows}, {4})
        self.assertEqual(batch.diagnostics, ())

    def test_fiber_fixture_preserves_valid_and_invalid_traces(self):
        """The fiber corpus keeps its two structural failures and every other trace."""
        content = (FIXTURES / "trace_fiber.xlsx").read_bytes()

        batch = _interpret(content)

        valid = [trace for trace in batch.rows if trace.valid]
        self.assertEqual(len(batch.rows), 10)
        self.assertEqual(len(valid), 8)
        self.assertTrue(all(4 <= len(trace.segments) <= 9 for trace in valid))
        self.assertEqual(sum(trace.ends_at_rear_port for trace in valid), 4)
        self.assertEqual(_codes(batch).count("trace.non_linear_path"), 1)
        self.assertEqual(_codes(batch).count("trace.pass_through_at_interface"), 1)
        self.assertNotIn("trace.corroboration_mismatch", _codes(batch))
        self.assertNotIn("trace.duplicate_conflict", _codes(batch))
        self.assertNotIn("trace.cross_trace_conflict", _codes(batch))
        self.assertEqual(batch.diagnostics, tuple(error for trace in batch.rows for error in trace.errors))
        interface_trace = next(
            trace
            for trace in batch.rows
            if any(error.code == "trace.pass_through_at_interface" for error in trace.errors)
        )
        self.assertTrue(
            any(claim.entry_port == claim.exit_port == "R01" for claim in interface_trace.pass_through_claims)
        )
        self.assertEqual(interface_trace.corroboration[-1].device, "DEV-07")

    def test_fixture_diagnostics_never_cross_the_target_boundary(self):
        """The Source Adapter never emits a diagnostic that needs NetBox state."""
        forbidden = {
            "cable.unsupported_termination_kind",
            "trace.device_unresolved",
            "trace.endpoint_evidence_only",
        }

        for fixture in ("trace_copper.xlsx", "trace_fiber.xlsx"):
            batch = _interpret((FIXTURES / fixture).read_bytes())
            self.assertTrue(forbidden.isdisjoint(_codes(batch)))

    def test_fixture_provenance_captures_the_workbook_and_both_sheets(self):
        """Collapsed occurrences retain their source positions and export timestamp."""
        batch = _interpret((FIXTURES / "trace_copper.xlsx").read_bytes())
        trace = batch.rows[0]

        self.assertEqual({item.sheet for item in trace.provenance}, {"Trace From To", "Trace List"})
        self.assertEqual({item.export_timestamp for item in trace.provenance}, {"2026-08-16 21:28:51"})
        self.assertEqual({len(item.workbook_fingerprint) for item in trace.provenance}, {64})
        self.assertTrue(all(item.from_text and item.to_text for item in trace.provenance))


class TraceWorkbookIdentityTest(SimpleTestCase):
    """Trace identity and content use direction-independent canonical forms."""

    def test_endpoint_lines_keep_cards_and_port_classes_distinct(self):
        """The endpoint grammar accepts every source shape used by the fixtures."""
        examples = {
            "DEV-02 > 08 (Switch Port)": ("DEV-02", "", "08", "Switch Port"),
            "DEV-01 > Slot 0 > 2/1 (Port)": ("DEV-01", "Slot 0", "2/1", "Port"),
            "DEV-10 > Switch A - MOD-01 > Eth 01 (NIC)": ("DEV-10", "Switch A - MOD-01", "Eth 01", "NIC"),
            "H1-Q10-52-F > H1-Q10-52-1-F - H1-Q7-2-1-F > R03 (Fiber Pair Back)": (
                "H1-Q10-52-F",
                "H1-Q10-52-1-F - H1-Q7-2-1-F",
                "R03",
                "Fiber Pair Back",
            ),
            "DEV-12 > SFP - 4 (Switch Port)": ("DEV-12", "", "SFP - 4", "Switch Port"),
        }

        for line, expected in examples.items():
            with self.subTest(line=line):
                termination = parse_endpoint_line(line)
                self.assertEqual(
                    (termination.device, termination.cards, termination.port, termination.port_class),
                    expected,
                )

    def test_a_reversed_restatement_collapses_without_fingerprint_churn(self):
        """Direction changes provenance but not trace identity or content."""
        endpoint_a = _termination("DEVICE-A", "CARD-A", "PORT-A", "Port")
        panel_entry = _termination("PANEL-A", "CARD-P", "FRONT-A", "Position Front")
        panel_exit = _termination("PANEL-A", "CARD-P", "REAR-A", "Punch-Down")
        endpoint_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        forward = (
            _endpoint_line(endpoint_a),
            _endpoint_line(endpoint_b),
            (_segment(endpoint_a, "Cable A", panel_entry), _segment(panel_exit, "Cable B", endpoint_b)),
        )
        reverse = (
            _endpoint_line(endpoint_b),
            _endpoint_line(endpoint_a),
            (_segment(endpoint_b, "Cable B", panel_exit), _segment(panel_entry, "Cable A", endpoint_a)),
        )

        forward_trace = _interpret(_workbook(path_blocks=(forward,))).rows[0]
        reverse_trace = _interpret(_workbook(path_blocks=(reverse,))).rows[0]
        combined = _interpret(_workbook(path_blocks=(forward, reverse)))

        self.assertEqual(forward_trace.identity, reverse_trace.identity)
        self.assertEqual(forward_trace.content_fingerprint, reverse_trace.content_fingerprint)
        self.assertEqual(len(combined.rows), 1)
        self.assertEqual(len(combined.rows[0].provenance), 2)
        self.assertEqual({item.direction for item in combined.rows[0].provenance}, {"canonical", "reversed"})
        self.assertEqual(combined.diagnostics, ())

    def test_json_identity_does_not_collide_on_separator_characters(self):
        """JSON preserves field boundaries that a separator-joined identity would lose."""
        first_a = _termination("A|B", "C", "D", "Port")
        first_b = _termination("Z|Y", "X", "W", "NIC")
        second_a = _termination("A", "B|C", "D", "Port")
        second_b = _termination("Z", "Y|X", "W", "NIC")
        blocks = (
            (_endpoint_line(first_a), _endpoint_line(first_b), (_segment(first_a, "Cable", first_b),)),
            (_endpoint_line(second_a), _endpoint_line(second_b), (_segment(second_a, "Cable", second_b),)),
        )

        batch = _interpret(_workbook(path_blocks=blocks))

        identities = {trace.identity for trace in batch.rows}
        self.assertEqual(len(identities), 2)
        self.assertTrue(all(isinstance(json.loads(identity), list) for identity in identities))


class TraceWorkbookTaxonomyTest(SimpleTestCase):
    """Small workbooks cover source-only validation conditions absent from the corpus."""

    def test_no_recognized_sheet_is_a_batch_error(self):
        """An unrelated workbook returns its diagnostic code without creating a unit."""
        book = openpyxl.Workbook()
        sheet = book.active
        if isinstance(sheet, Worksheet):
            sheet.title = "Other"
            sheet.append(("not", "a trace"))
        buffer = BytesIO()
        book.save(buffer)

        batch = _interpret(buffer.getvalue())

        self.assertEqual(batch.rows, ())
        self.assertEqual(_codes(batch), ["trace.no_recognized_sheet"])

    def test_unreadable_bytes_raise_the_adapter_error(self):
        """A broken archive keeps the same unreadable-source contract as the flat adapter."""
        with self.assertRaises(SourceUnreadable):
            _interpret(b"not a workbook")

    def test_an_incomplete_trace_does_not_stop_a_valid_trace(self):
        """A missing header invalidates one trace while the next block remains usable."""
        invalid_a = _termination("DEVICE-A", "", "PORT-A", "Port")
        invalid_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        valid_a = _termination("DEVICE-C", "", "PORT-C", "Port")
        valid_b = _termination("DEVICE-D", "", "PORT-D", "NIC")
        book = openpyxl.Workbook()
        sheet = book.active
        if not isinstance(sheet, Worksheet):
            sheet = book.create_sheet()
        sheet.title = "Trace From To"
        sheet.append(("Executed", "2026-08-31 12:00:00"))
        sheet.append(())
        sheet.append(("From", _endpoint_line(invalid_a)))
        sheet.append(("To", _endpoint_line(invalid_b)))
        sheet.append(("Wrong", "Header"))
        sheet.append(("From", _endpoint_line(valid_a)))
        sheet.append(("To", _endpoint_line(valid_b)))
        sheet.append(PATH_HEADER)
        sheet.append(_segment(valid_a, "Cable", valid_b))
        buffer = BytesIO()
        book.save(buffer)

        batch = _interpret(buffer.getvalue())

        self.assertEqual(len(batch.rows), 2)
        self.assertEqual(sum(trace.valid for trace in batch.rows), 1)
        self.assertEqual(_codes(batch), ["trace.incomplete_block"])

    def test_trace_list_mismatch_invalidates_only_its_trace(self):
        """A wrong ordered device visit emits the corroboration diagnostic."""
        endpoint_a = _termination("DEVICE-A", "", "PORT-A", "Port")
        panel_in = _termination("PANEL-A", "CARD-A", "FRONT", "Position Front")
        panel_out = _termination("PANEL-A", "CARD-A", "REAR", "Punch-Down")
        endpoint_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        lines = _endpoint_line(endpoint_a), _endpoint_line(endpoint_b)
        path_block = (*lines, (_segment(endpoint_a, "Cable A", panel_in), _segment(panel_out, "Cable B", endpoint_b)))
        list_block = (*lines, (_visit(endpoint_a), _visit(_termination("WRONG", "", "P", "Port")), _visit(endpoint_b)))

        batch = _interpret(_workbook(path_blocks=(path_block,), list_blocks=(list_block,), include_list=True))

        self.assertFalse(batch.rows[0].valid)
        self.assertEqual(_codes(batch), ["trace.corroboration_mismatch"])

    def test_differing_duplicate_evidence_becomes_one_invalid_trace(self):
        """One identity with two CableClass claims emits a duplicate conflict."""
        endpoint_a = _termination("DEVICE-A", "", "PORT-A", "Port")
        endpoint_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        lines = _endpoint_line(endpoint_a), _endpoint_line(endpoint_b)
        blocks = (
            (*lines, (_segment(endpoint_a, "Cable A", endpoint_b),)),
            (*lines, (_segment(endpoint_a, "Cable B", endpoint_b),)),
        )

        batch = _interpret(_workbook(path_blocks=blocks))

        self.assertEqual(len(batch.rows), 1)
        self.assertFalse(batch.rows[0].valid)
        self.assertEqual(_codes(batch), ["trace.duplicate_conflict"])
        self.assertIn("block 1", batch.diagnostics[0].message)
        self.assertIn("block 2", batch.diagnostics[0].message)

    def test_unknown_port_class_is_detected_without_target_state(self):
        """A class outside the fixed PortClass vocabulary is a source error."""
        endpoint_a = _termination("DEVICE-A", "", "PORT-A", "Unknown Class")
        endpoint_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        block = (
            _endpoint_line(endpoint_a),
            _endpoint_line(endpoint_b),
            (_segment(endpoint_a, "Cable", endpoint_b),),
        )

        batch = _interpret(_workbook(path_blocks=(block,)))

        self.assertFalse(batch.rows[0].valid)
        self.assertEqual(_codes(batch), ["trace.unknown_port_class"])

    def test_cross_trace_occupancy_and_cable_class_conflicts_invalidate_every_trace(self):
        """Distinct traces cannot share terminations or disagree about one segment class."""
        endpoint_a = _termination("DEVICE-A", "", "PORT-A", "Port")
        endpoint_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        endpoint_c = _termination("DEVICE-C", "", "PORT-C", "Port")
        endpoint_d = _termination("DEVICE-D", "", "PORT-D", "NIC")
        panel_p_in = _termination("PANEL-P", "CARD-P", "FRONT", "Position Front")
        panel_p_out = _termination("PANEL-P", "CARD-P", "REAR", "Punch-Down")
        panel_q_in = _termination("PANEL-Q", "CARD-Q", "FRONT", "Position Front")
        panel_q_out = _termination("PANEL-Q", "CARD-Q", "REAR", "Punch-Down")
        first = (
            _endpoint_line(endpoint_a),
            _endpoint_line(endpoint_b),
            (
                _segment(endpoint_a, "Cable A", panel_p_in),
                _segment(panel_p_out, "Shared A", panel_q_in),
                _segment(panel_q_out, "Cable B", endpoint_b),
            ),
        )
        second = (
            _endpoint_line(endpoint_c),
            _endpoint_line(endpoint_d),
            (
                _segment(endpoint_c, "Cable C", panel_p_in),
                _segment(panel_p_out, "Shared B", panel_q_in),
                _segment(panel_q_out, "Cable D", endpoint_d),
            ),
        )

        batch = _interpret(_workbook(path_blocks=(first, second)))

        self.assertEqual(len(batch.rows), 2)
        self.assertTrue(all(not trace.valid for trace in batch.rows))
        self.assertEqual(_codes(batch), ["trace.cross_trace_conflict", "trace.cross_trace_conflict"])

    def test_an_identical_shared_segment_is_allowed(self):
        """Two traces may contribute the same segment with the same CableClass."""
        endpoint_a = _termination("DEVICE-A", "", "PORT-A", "Port")
        endpoint_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        endpoint_c = _termination("DEVICE-C", "", "PORT-C", "Port")
        endpoint_d = _termination("DEVICE-D", "", "PORT-D", "NIC")
        panel_p_first = _termination("PANEL-P", "CARD-P", "FRONT-1", "Position Front")
        panel_p_second = _termination("PANEL-P", "CARD-P", "FRONT-2", "Position Front")
        panel_p_shared = _termination("PANEL-P", "CARD-P", "REAR", "Punch-Down")
        panel_q_shared = _termination("PANEL-Q", "CARD-Q", "REAR", "Punch-Down")
        panel_q_first = _termination("PANEL-Q", "CARD-Q", "FRONT-1", "Position Front")
        panel_q_second = _termination("PANEL-Q", "CARD-Q", "FRONT-2", "Position Front")
        first = (
            _endpoint_line(endpoint_a),
            _endpoint_line(endpoint_b),
            (
                _segment(endpoint_a, "Cable A", panel_p_first),
                _segment(panel_p_shared, "Shared", panel_q_shared),
                _segment(panel_q_first, "Cable B", endpoint_b),
            ),
        )
        second = (
            _endpoint_line(endpoint_c),
            _endpoint_line(endpoint_d),
            (
                _segment(endpoint_c, "Cable C", panel_p_second),
                _segment(panel_p_shared, "Shared", panel_q_shared),
                _segment(panel_q_second, "Cable D", endpoint_d),
            ),
        )

        batch = _interpret(_workbook(path_blocks=(first, second)))

        self.assertEqual(len(batch.rows), 2)
        self.assertTrue(all(trace.valid for trace in batch.rows))
        self.assertNotIn("trace.cross_trace_conflict", _codes(batch))

    def test_an_unpaired_trace_list_block_becomes_endpoint_summary_evidence(self):
        """Trace List data can produce a valid Source Trace without Segment Evidence."""
        endpoint_a = _termination("DEVICE-A", "", "PORT-A", "Port")
        endpoint_b = _termination("DEVICE-B", "", "PORT-B", "NIC")
        block = (
            _endpoint_line(endpoint_a),
            _endpoint_line(endpoint_b),
            (_visit(endpoint_a), _visit(endpoint_b)),
        )

        batch = _interpret(_workbook(list_blocks=(block,), include_path=False, include_list=True))

        self.assertEqual(len(batch.rows), 1)
        self.assertTrue(batch.rows[0].valid)
        self.assertEqual(batch.rows[0].segments, ())
        self.assertEqual(batch.rows[0].pass_through_claims, ())
        self.assertEqual(batch.rows[0].provenance[0].sheet, "Trace List")

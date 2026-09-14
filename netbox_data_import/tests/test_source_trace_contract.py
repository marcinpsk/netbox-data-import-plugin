# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The adapter-neutral Source Trace contract."""

from django.test import SimpleTestCase

from netbox_data_import.adapters import SourceAdapter, SourceBatch
from netbox_data_import.catalog import OutputKind
from netbox_data_import.source_trace import (
    EndpointSummary,
    SourceTrace,
    TerminationReference,
    TraceProvenance,
)


class IndependentTraceAdapter(SourceAdapter):
    """Emit one Source Trace without depending on the fixed workbook parser."""

    key = "independent_trace"
    label = "Independent trace"
    output_kinds = frozenset({OutputKind.SOURCE_TRACE})

    @classmethod
    def interpret(cls, content, adapter_config, *, collect_unused=False):
        first = TerminationReference(device="SOURCE-A", cards="", port="eth0", port_class="Port")
        second = TerminationReference(device="SOURCE-B", cards="", port="eth1", port_class="Port")
        trace = SourceTrace(
            endpoint_summary=EndpointSummary(first, second, "SOURCE-A > eth0 (Port)", "SOURCE-B > eth1 (Port)"),
            segments=(),
            pass_through_claims=(),
            corroboration=(),
            identity="independent",
            content_fingerprint="0" * 64,
            provenance=(
                TraceProvenance(
                    workbook_fingerprint="1" * 64,
                    sheet="Independent",
                    block_ordinal=1,
                    row_start=1,
                    row_end=2,
                    export_timestamp="",
                    from_text="SOURCE-A > eth0 (Port)",
                    to_text="SOURCE-B > eth1 (Port)",
                    direction="forward",
                ),
            ),
        )
        return SourceBatch(output_kinds=cls.output_kinds, rows=(trace,))


class SourceTraceContractTest(SimpleTestCase):
    def test_an_independent_adapter_emits_the_contract_without_the_workbook_parser(self):
        batch = IndependentTraceAdapter.interpret(b"", {})

        self.assertIsInstance(batch.rows[0], SourceTrace)
        self.assertEqual(SourceTrace.__module__, "netbox_data_import.source_trace")

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Trace Review Workspace: its summary strip, its trace list, and its per-trace actions."""

from dcim.models import Interface
from django.test import TestCase

from netbox_data_import.review_workspace import ReviewWorkspace
from netbox_data_import.tests.test_cable_module import (
    CableTopologyMixin,
    direct_path,
    patched_path,
)
from netbox_data_import.tests.helpers import trace_termination


class TraceWorkspaceTest(CableTopologyMixin, TestCase):
    """One workspace entry per Source Trace, with every action visible."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def traces(self, *blocks):
        """Return the workspace trace entries one set of path blocks produces."""
        return ReviewWorkspace(self.plan(*blocks)).traces

    def action(self, trace, key):
        """Return one named action from a workspace trace entry."""
        return next(item for item in trace.actions if item.key == key)

    def separate_blocked_path(self, suffix):
        """Return a path whose source port is absent, on devices no other trace touches."""
        Interface.objects.create(device=self.make_device(f"SRC-{suffix}"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device(f"DST-{suffix}"), name="eth0", type="1000base-t")
        return direct_path(
            from_end=trace_termination(f"SRC-{suffix}", "", "absent-port", "Port"),
            to_end=trace_termination(f"DST-{suffix}", "", "eth0", "Port"),
        )

    def test_one_entry_per_source_trace_carries_its_panels(self):
        """The trace list shows every trace, and each entry carries the three panels."""
        traces = self.traces(patched_path())

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].endpoints["from"], "DEV-A eth0")
        self.assertEqual([segment["status"] for segment in traces[0].segments], ["create", "create", "create"])
        self.assertEqual(traces[0].disposition, "actionable")

    def test_an_actionable_trace_can_be_synchronized(self):
        """A trace whose every dependency is settled offers its synchronization enabled."""
        trace = self.traces(patched_path())[0]

        sync = self.action(trace, "sync")
        self.assertTrue(sync.enabled)
        self.assertEqual(sync.reason, "")

    def test_synchronizing_a_blocked_trace_is_disabled_with_its_reason(self):
        """An illegal action stays visible and states why it cannot run."""
        missing = trace_termination("DEV-A", "", "absent-port", "Port")

        trace = self.traces(direct_path(from_end=missing))[0]

        sync = self.action(trace, "sync")
        self.assertEqual(trace.disposition, "blocked")
        self.assertFalse(sync.enabled)
        self.assertIn("termination", sync.reason.lower())

    def test_a_trace_that_needs_no_change_is_disabled_with_its_reason(self):
        """A path NetBox already holds has nothing to synchronize, and says so."""
        self.connect(self.eth0, self.eth1)

        trace = self.traces(direct_path())[0]

        sync = self.action(trace, "sync")
        self.assertEqual(trace.disposition, "no-op")
        self.assertFalse(sync.enabled)
        self.assertEqual(sync.reason, "The stated path already exists.")

    def test_every_action_is_visible_on_every_trace(self):
        """The operator sees the same actions everywhere; only their reasons differ."""
        blocked, actionable = self.traces(self.separate_blocked_path("V"), patched_path())

        self.assertEqual(blocked.disposition, "blocked")
        self.assertEqual(actionable.disposition, "actionable")
        self.assertEqual([item.key for item in blocked.actions], [item.key for item in actionable.actions])
        self.assertNotEqual(
            [item.enabled for item in blocked.actions],
            [item.enabled for item in actionable.actions],
        )

    def test_the_summary_strip_counts_terminations_and_dispositions(self):
        """The strip states what the reviewer has to work through, not one number."""
        summary = ReviewWorkspace(self.plan(patched_path(), self.separate_blocked_path("S"))).trace_summary

        self.assertEqual(summary["traces"], 2)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["actionable"], 1)
        self.assertEqual(summary["unresolved_terminations"], 1)
        self.assertEqual(summary["resolved_terminations"], 7)

    def test_an_unresolved_termination_offers_its_picker(self):
        """The picker is the decision seam, so an open termination has to point at one."""
        missing = trace_termination("DEV-A", "", "absent-port", "Port")

        trace = self.traces(direct_path(from_end=missing))[0]

        open_terminations = [item for item in trace.terminations if item["state"] == "unresolved"]
        self.assertEqual([item["label"] for item in open_terminations], ["DEV-A absent-port"])
        self.assertTrue(open_terminations[0]["field_key"])

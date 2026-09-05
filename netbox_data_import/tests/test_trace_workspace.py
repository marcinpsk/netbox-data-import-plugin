# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Trace Review Workspace: its summary strip, its trace list, and its per-trace actions."""

from io import BytesIO

from dcim.models import Interface
from django.test import TestCase
from django.urls import reverse

from netbox_data_import.review_workspace import ReviewWorkspace
from netbox_data_import.tests.test_cable_module import (
    CableTopologyMixin,
    direct_path,
    patched_path,
)
from netbox_data_import.tests.helpers import trace_termination, trace_workbook_bytes


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


class TraceWorkspacePageTest(CableTopologyMixin, TestCase):
    """The workspace page, reached through the real wizard for a trace profile."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def open_workspace(self, *blocks):
        """Upload the given path blocks and return the rendered workspace response."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(setup.status_code, 200)
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def test_the_page_lists_every_trace_with_its_panels(self):
        """One page per preview: the strip, the list, and the panels of the selected trace."""
        response = self.open_workspace(patched_path())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["traces"]), 1)
        self.assertEqual(response.context["summary"]["traces"], 1)
        self.assertContains(response, "DEV-A eth0 to DEV-B eth1")
        self.assertContains(response, "reuse existing", count=0, status_code=200)
        self.assertContains(response, "automatically resolved")

    def test_a_blocked_trace_renders_its_sync_action_disabled_with_its_reason(self):
        """An illegal action stays on screen, disabled, with the reason underneath."""
        Interface.objects.create(device=self.make_device("SRC-P"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("DST-P"), name="eth0", type="1000base-t")
        blocked = direct_path(
            from_end=trace_termination("SRC-P", "", "absent-port", "Port"),
            to_end=trace_termination("DST-P", "", "eth0", "Port"),
        )

        response = self.open_workspace(blocked)

        trace = response.context["traces"][0]
        self.assertFalse(trace.actions[0].enabled)
        self.assertContains(response, "disabled")
        self.assertContains(response, trace.actions[0].reason)

    def test_the_workspace_reports_no_drift_for_a_freshly_read_preview(self):
        """The strip appears on a difference, so a preview just read must not show one."""
        response = self.open_workspace(patched_path())

        self.assertFalse(response.context["drift"])

    def test_the_workspace_reports_drift_when_netbox_moved_under_the_preview(self):
        """A live change since the reviewed plan is exactly what the strip is for."""
        self.open_workspace(patched_path())

        self.connect(self.panel_1_rear, self.panel_2_rear)
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertTrue(response.context["drift"])

    def test_re_reading_clears_the_drift_strip(self):
        """The re-read action adopts the live plan, so the difference it reported is gone."""
        self.open_workspace(patched_path())
        self.connect(self.panel_1_rear, self.panel_2_rear)

        self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session["import_preview_revision"]},
        )
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertFalse(response.context["drift"])
        statuses = [segment["status"] for segment in response.context["traces"][0].segments]
        self.assertIn("reuse existing", statuses)

    def test_the_workspace_refuses_a_session_that_holds_no_preview(self):
        """Without a materialized preview there is nothing to review, so it sends the operator back."""
        self.client.force_login(self.actor)

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertEqual(response.status_code, 302)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Trace Review Workspace: its summary strip, its trace list, and its per-trace actions."""

import copy
import re
from io import BytesIO

from dcim.models import Cable, Device, FrontPort, Interface, RearPort, Site
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape
from extras.models import Tag

from netbox_data_import.cable_policy import cable_type_label
from netbox_data_import.cable_target import ELIGIBLE_TERMINATION_LIMIT
from netbox_data_import import adapters as adapter_registry
from netbox_data_import.adapters import TraceWorkbookAdapter
from netbox_data_import.catalog import OutputKind
from netbox_data_import.field_keys import termination_field_key
from netbox_data_import.models import CableClassMapping, CableSegmentOverride, ImportProfile, TerminationResolution
from netbox_data_import.plan import Disposition, ImportPlan, PlannedChange, SynchronizationUnit
from netbox_data_import.preview_row_actions import (
    PREVIEW_DIRTY_SESSION_KEY,
    PREVIEW_PLAN_SESSION_KEY,
    PREVIEW_REVISION_SESSION_KEY,
)
from netbox_data_import.review_workspace import _SUMMARY_KEYS, ReviewWorkspace
from netbox_data_import.tests.test_cable_module import (
    CableTopologyMixin,
    direct_path,
    patched_path,
)
from netbox_data_import.tests.helpers import (
    assert_absent_from,
    cables_on,
    competing_write_during,
    trace_endpoint_line,
    trace_segment,
    trace_termination,
    trace_workbook_bytes,
    user_with_object_permission,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.views import _review_workspace_url, _trace_workspace_url


class _MixedOutputTestAdapter(TraceWorkbookAdapter):
    """A registered test adapter whose complete output needs the generic workspace."""

    key = "mixed_output_test"
    output_kinds = frozenset({OutputKind.SOURCE_TRACE, OutputKind.DEVICE_SOURCE_ROW})


class ReviewWorkspaceRouteTest(TestCase):
    def test_a_mixed_output_profile_keeps_the_generic_workspace(self):
        adapter_registry._ADAPTERS_BY_KEY[_MixedOutputTestAdapter.key] = _MixedOutputTestAdapter
        self.addCleanup(adapter_registry._ADAPTERS_BY_KEY.pop, _MixedOutputTestAdapter.key)
        profile = ImportProfile.objects.create(
            name="Mixed output workspace",
            source_adapter=_MixedOutputTestAdapter.key,
            adapter_config={},
        )

        self.assertEqual(
            _review_workspace_url(profile),
            reverse("plugins:netbox_data_import:import_preview"),
        )


class TraceWorkspaceTest(CableTopologyMixin, TestCase):
    """One workspace entry per Source Trace, with every action visible."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def traces(self, *blocks):
        """Return the workspace trace entries one set of path blocks produces."""
        return ReviewWorkspace(self.plan(*blocks), self.actor).traces

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
        """An illegal action stays visible and states why it cannot run, in operator wording."""
        missing = trace_termination("DEV-A", "", "absent-port", "Port")

        trace = self.traces(direct_path(from_end=missing))[0]

        sync = self.action(trace, "sync")
        self.assertEqual(trace.disposition, "blocked")
        self.assertFalse(sync.enabled)
        self.assertEqual(
            sync.reason,
            "No single port on the resolved Device matches this name. Choose the termination for it.",
        )

    def test_no_finding_reads_back_its_own_diagnostic_code(self):
        """A code is an internal name. Every finding the workspace renders has to be an instruction."""
        traces = self.traces(
            patched_path(),
            self.separate_blocked_path("W"),
            direct_path(
                from_end=trace_termination("NO-SUCH-DEVICE", "", "eth0", "Port"),
                to_end=trace_termination("DEV-B", "", "eth1", "Port"),
            ),
        )

        findings = [finding for trace in traces for finding in trace.findings]
        self.assertNotEqual(findings, [])
        for finding in findings:
            with self.subTest(code=finding["code"]):
                self.assertNotEqual(finding["message"], finding["code"])
                self.assertIn(" ", finding["message"])

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

    def test_the_workspace_answers_whether_it_holds_a_trace_without_building_one(self):
        """The preview page asks this on every render, so it must not serialize the whole plan."""
        workspace = ReviewWorkspace(self.plan(patched_path()), self.actor)

        self.assertTrue(workspace.has_traces)
        self.assertEqual(len(workspace.traces), 1)
        self.assertFalse(ReviewWorkspace(ImportPlan(units=()), self.actor).has_traces)

    def test_the_workspace_builds_its_trace_entries_once_per_instance(self):
        """One page reads `traces` and `trace_summary`, and each build reserializes every change."""
        workspace = ReviewWorkspace(self.plan(patched_path()), self.actor)

        first = workspace.traces
        workspace.trace_summary

        self.assertIs(workspace.traces, first)

    def test_a_units_copy_builds_its_own_trace_entries(self):
        """`with_units` bypasses `__init__`, so the copy must carry its own cache, not share one."""
        workspace = ReviewWorkspace(self.plan(patched_path()), self.actor)
        original = workspace.traces

        copy = workspace.with_units(workspace.units)

        self.assertIsNot(copy, workspace)
        self.assertIsNot(copy.traces, original)
        self.assertEqual([trace.identity for trace in copy.traces], [trace.identity for trace in original])

    def test_the_summary_strip_counts_terminations_and_dispositions(self):
        """The strip states what the reviewer has to work through, not one number."""
        summary = ReviewWorkspace(self.plan(patched_path(), self.separate_blocked_path("S")), self.actor).trace_summary

        self.assertEqual(summary["traces"], 2)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["actionable"], 1)
        self.assertEqual(summary["unresolved_terminations"], 1)
        self.assertEqual(summary["resolved_terminations"], 7)

    def test_every_trace_is_counted_under_exactly_one_disposition(self):
        """A disposition absent from `_SUMMARY_KEYS` would drop its traces from the strip silently."""
        summary = ReviewWorkspace(self.plan(patched_path(), self.separate_blocked_path("S")), self.actor).trace_summary

        self.assertEqual(sum(summary[key] for key in _SUMMARY_KEYS.values()), summary["traces"])

    def test_the_summary_names_every_disposition_a_trace_can_carry(self):
        """Only `cable_target` states a trace, and it never assigns EXCLUDED; a new member must decide."""
        self.assertEqual(set(_SUMMARY_KEYS), set(Disposition.ALL) - {Disposition.EXCLUDED})

    def test_a_restored_termination_without_state_counts_as_unresolved(self):
        data = self.plan(direct_path()).to_dict()
        terminations = data["units"][0]["display"]["trace"]["terminations"]
        del terminations[0]["state"]

        summary = ReviewWorkspace.from_dict(data, self.actor).trace_summary

        self.assertEqual(summary["traces"], 1)
        self.assertEqual(summary["unresolved_terminations"], 1)
        self.assertEqual(summary["resolved_terminations"], len(terminations) - 1)

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

    def test_workspace_disables_browser_history_snapshot(self):
        """The browser must not retain a trace preview after another operator opens it."""
        response = self.open_workspace(patched_path())

        self.assertEqual(response.content.count(b'hx-history="false"'), 1)

    def test_trace_setup_opens_the_trace_workspace_directly(self):
        """A trace-only profile starts on its review surface instead of the flat preview."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(patched_path(),)))
        upload.name = "traces.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:trace_workspace"),
            fetch_redirect_response=False,
        )

    def test_an_empty_trace_workbook_still_opens_the_trace_workspace(self):
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes())
        upload.name = "empty-traces.xlsx"

        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:trace_workspace"),
            fetch_redirect_response=False,
        )

    def test_the_page_lists_every_trace_with_its_panels(self):
        """One page per preview: the strip, the list, and the panels of the selected trace."""
        response = self.open_workspace(patched_path())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["traces"]), 1)
        self.assertEqual(response.context["summary"]["traces"], 1)
        self.assertContains(response, "DEV-A eth0 to DEV-B eth1")
        self.assertContains(response, "reuse existing", count=0, status_code=200)
        self.assertContains(response, "automatically resolved")

    def test_the_proposed_topology_names_both_devices_for_each_segment(self):
        response = self.open_workspace(patched_path())

        page = response.content.decode()
        proposed = page[
            page.index("Proposed physical topology") : page.index("</ol>", page.index("Proposed physical topology"))
        ]
        self.assertIn("DEV-A eth0 &rarr; PANEL-1 F1", proposed)
        self.assertIn("PANEL-1 R1 &rarr; PANEL-2 R1", proposed)
        self.assertIn("PANEL-2 F1 &rarr; DEV-B eth1", proposed)

    def test_a_longer_proposed_topology_does_not_add_device_reads(self):
        def rendered_device_reads(block):
            self.open_workspace(block)
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
            self.assertEqual(response.status_code, 200)
            table = Device._meta.db_table
            return [
                query["sql"]
                for query in captured.captured_queries
                if f'FROM "{table}"' in query["sql"] and f'WHERE "{table}"."id" =' in query["sql"]
            ]

        short_reads = rendered_device_reads(direct_path())
        long_reads = rendered_device_reads(patched_path())

        self.assertEqual(
            (len(short_reads), len(long_reads)),
            (0, 0),
            {"short": short_reads, "long": long_reads},
        )

    def test_repeated_findings_render_one_message(self):
        response = self.open_workspace(patched_path())

        self.assertContains(response, "A PortMapping proves the stated pass-through.", count=1)

    def test_the_list_shows_every_trace_but_the_panels_show_one(self):
        """Section 10.2 is a trace list plus the three panels of the selected trace."""
        Interface.objects.create(device=self.make_device("SEL-A"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("SEL-B"), name="eth0", type="1000base-t")
        second = direct_path(
            from_end=trace_termination("SEL-A", "", "eth0", "Port"),
            to_end=trace_termination("SEL-B", "", "eth0", "Port"),
        )

        response = self.open_workspace(patched_path(), second)

        self.assertEqual(len(response.context["traces"]), 2)
        self.assertEqual(response.context["selected_trace"].endpoints["from"], "DEV-A eth0")
        # Both traces are listed, and only the selected one contributes a proposed-topology panel.
        self.assertContains(response, "SEL-A eth0")
        self.assertContains(response, "Proposed physical topology", count=1)

    def test_the_list_selects_the_trace_the_query_names(self):
        """The operator moves through the list, so the page has to follow the one they picked."""
        Interface.objects.create(device=self.make_device("SEL-C"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("SEL-D"), name="eth0", type="1000base-t")
        second = direct_path(
            from_end=trace_termination("SEL-C", "", "eth0", "Port"),
            to_end=trace_termination("SEL-D", "", "eth0", "Port"),
        )
        self.open_workspace(patched_path(), second)
        listing = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        wanted = next(t for t in listing.context["traces"] if t.endpoints["from"] == "SEL-C eth0")

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), {"trace": wanted.identity})

        self.assertEqual(response.context["selected_trace"].identity, wanted.identity)

    def test_a_re_read_returns_to_the_trace_it_was_issued_from(self):
        """The re-read is issued from one trace's page, so it must not move the operator."""
        Interface.objects.create(device=self.make_device("SEL-G"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("SEL-H"), name="eth0", type="1000base-t")
        second = direct_path(
            from_end=trace_termination("SEL-G", "", "eth0", "Port"),
            to_end=trace_termination("SEL-H", "", "eth0", "Port"),
        )
        opened = self.open_workspace(patched_path(), second)
        wanted = opened.context["traces"][1]
        self.assertNotEqual(opened.context["selected_trace"].identity, wanted.identity)

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": opened.context["preview_revision"], "trace": wanted.identity},
            follow=True,
        )

        self.assertEqual(response.context["selected_trace"].identity, wanted.identity)

    def test_every_workspace_command_form_names_the_trace_it_was_issued_from(self):
        """Each command replans and returns to the workspace, so each has to name its own trace."""
        opened = self.open_workspace(patched_path())
        page = opened.content.decode()
        identity = escape(opened.context["selected_trace"].identity)
        commands = [form for form in re.findall(r"<form\b.*?</form>", page, re.DOTALL) if "/trace-workspace/" in form]

        # Naming the set, not a count: a new command that skips the check below shows up here.
        self.assertEqual(
            sorted(re.search(r'action="([^"]+)"', form).group(1) for form in commands),
            sorted(
                [
                    reverse("plugins:netbox_data_import:trace_workspace_reread"),
                    reverse("plugins:netbox_data_import:trace_sync"),
                    reverse("plugins:netbox_data_import:trace_resolve_device"),
                    reverse("plugins:netbox_data_import:trace_resolve_termination"),
                    # This path states two CableClass values, and each offers its own policy form.
                    *[reverse("plugins:netbox_data_import:trace_cable_policy")] * 2,
                    # It states three segments, and each offers its own override form.
                    *[reverse("plugins:netbox_data_import:trace_segment_policy")] * 3,
                ]
            ),
        )
        for form in commands:
            action = re.search(r'action="([^"]+)"', form).group(1)
            with self.subTest(action=action):
                # The sync command already names the trace it synchronizes.
                field = "identity" if action.endswith("/sync/") else "trace"
                self.assertIn(f'name="{field}" value="{identity}"', form)

    def test_a_workspace_command_swaps_the_page_content_in_place(self):
        """A full page load loses the scroll position the operator was reading at."""
        page = self.open_workspace(patched_path()).content.decode()

        for form_id in ("traceWorkspaceRereadForm", "traceDeviceForm", "traceTerminationForm"):
            with self.subTest(form=form_id):
                tag = re.search(rf'<form[^>]*id="{form_id}"[^>]*>', page)
                self.assertIsNotNone(tag)
                self.assertIn('hx-target="#page-content"', tag.group(0))
                self.assertIn('hx-select="#page-content"', tag.group(0))
                self.assertIn('hx-swap="outerHTML"', tag.group(0))

    def test_the_summary_states_the_saved_decisions_and_the_preview_state(self):
        """Section 10.2 names both, and neither can be read off the plan alone."""
        response = self.open_workspace(patched_path())

        self.assertEqual(response.context["summary"]["saved_decisions"], 0)
        self.assertEqual(response.context["summary"]["preview_state"], "current")
        self.assertContains(response, "current")

    def test_the_summary_strip_groups_each_tile_by_its_meaning(self):
        response = self.open_workspace(patched_path())

        groups = re.findall(
            r'<section\b[^>]*data-trace-summary-group="([^"]+)"[^>]*>(.*?)</section>',
            response.content.decode(),
            re.DOTALL,
        )
        self.assertEqual([name for name, _content in groups], ["traces", "terminations", "proposals", "review"])
        expected_groups = {
            "traces": ("Traces", ("Traces", "Actionable", "Blocked", "Invalid", "No change")),
            "terminations": ("Terminations", ("Terminations resolved", "Terminations open")),
            "proposals": ("Proposals", ("Active proposals",)),
            "review": ("Review state", ("Saved decisions", "Preview")),
        }
        for name, content in groups:
            with self.subTest(group=name):
                heading, labels = expected_groups[name]
                self.assertRegex(content, rf"<h2\b[^>]*>{heading}</h2>")
                for label in labels:
                    self.assertIn(f">{label}</div>", content)

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
        self.assertRegex(response.content.decode(), r'<button\b[^>]*data-trace-action="sync"[^>]*\sdisabled(?=[\s>])')
        self.assertContains(response, trace.actions[0].reason)

    def test_an_invalid_pass_through_explains_why_resolution_did_not_run(self):
        """An early source failure must not look like successful Device and port resolution."""
        source = trace_termination("DEV-A", "", "eth0", "Port")
        destination = trace_termination("DEV-B", "", "eth1", "NIC")
        interface_entry = trace_termination("PANEL-1", "", "F1", "Port")
        panel_exit = trace_termination("PANEL-1", "", "R1", "Punch-Down")
        invalid = (
            trace_endpoint_line(source),
            trace_endpoint_line(destination),
            (
                trace_segment(source, "Patch", interface_entry),
                trace_segment(panel_exit, "Patch", destination),
            ),
        )

        response = self.open_workspace(invalid)

        self.assertContains(response, "Why this trace is invalid")
        self.assertContains(
            response,
            "The path continues through PANEL-1 from F1 (Port) to R1 (Punch-Down). "
            "An interface PortClass can terminate a trace, but it cannot join two cable segments.",
        )
        self.assertContains(response, "Planning stopped before it resolved source Devices.")
        self.assertContains(response, "Planning stopped before it resolved source terminations.")
        self.assertNotContains(response, "Every source Device on this trace resolves in NetBox.")
        self.assertNotContains(response, "Every termination on this trace resolves to a NetBox port.")

    def test_the_re_read_action_is_visible_even_without_drift(self):
        """Every action is always visible, so a quiet workspace still offers its re-read."""
        response = self.open_workspace(patched_path())

        self.assertFalse(response.context["drift"])
        self.assertContains(response, reverse("plugins:netbox_data_import:trace_workspace_reread"))
        self.assertContains(response, "Re-read from NetBox")

    def test_a_termination_no_picker_can_settle_is_disabled_with_its_reason(self):
        """With no resolved Device there is no port list, so the picker states why it cannot help."""
        unknown = direct_path(
            from_end=trace_termination("NO-SUCH-DEVICE", "", "eth0", "Port"),
            to_end=trace_termination("DEV-B", "", "eth1", "Port"),
        )

        response = self.open_workspace(unknown)

        trace = response.context["traces"][0]
        blocked = next(item for item in trace.terminations if item["label"] == "NO-SUCH-DEVICE eth0")
        self.assertFalse(blocked["selectable"])
        self.assertIn("Resolve the source Device", blocked["reason"])
        self.assertContains(response, blocked["reason"])

    def test_the_termination_search_carries_an_accessible_name(self):
        """A screen reader has to name the search box, and a placeholder is not a name."""
        response = self.open_workspace(patched_path())

        tag = re.search(r'<input[^>]*id="traceTerminationSearch"[^>]*>', response.content.decode())
        self.assertIsNotNone(tag)
        self.assertRegex(tag.group(0), r'aria-label="[^"]+"')

    def test_the_termination_picker_modal_is_named_by_its_own_title(self):
        """A dialog needs an accessible name, and the name has to resolve to an element that exists."""
        page = self.open_workspace(patched_path()).content.decode()

        modal = re.search(r'<div[^>]*id="traceTerminationPicker"[^>]*>', page)
        self.assertIsNotNone(modal)
        labelled_by = re.search(r'aria-labelledby="([^"]+)"', modal.group(0))
        self.assertIsNotNone(labelled_by, modal.group(0))
        self.assertRegex(page, rf'<h5[^>]*id="{re.escape(labelled_by.group(1))}"')

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

    def test_a_refused_sync_returns_to_the_trace_it_was_issued_from(self):
        """A refusal has to leave the operator on the trace whose button they pressed."""
        Interface.objects.create(device=self.make_device("SEL-K"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("SEL-L"), name="eth0", type="1000base-t")
        second = direct_path(
            from_end=trace_termination("SEL-K", "", "eth0", "Port"),
            to_end=trace_termination("SEL-L", "", "eth0", "Port"),
        )
        opened = self.open_workspace(patched_path(), second)
        wanted = opened.context["traces"][1]
        self.assertNotEqual(opened.context["selected_trace"].identity, wanted.identity)
        # A live topology change under the reviewed plan is what the sync command refuses.
        self.connect(self.panel_1_rear, self.panel_2_rear)

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": wanted.identity, "preview_revision": opened.context["preview_revision"]},
            follow=True,
        )

        self.assertEqual(response.context["selected_trace"].identity, wanted.identity)

    def test_sync_refuses_topology_drift_after_the_workspace_was_rendered(self):
        """A live topology change requires another review before a trace can be queued."""
        from core.models import Job

        response = self.open_workspace(patched_path())
        chosen = response.context["traces"][0]
        self.connect(self.panel_1_rear, self.panel_2_rear)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            follow=True,
        )

        self.assertFalse(Job.objects.filter(data__job_type="netbox_data_import.import").exists())
        self.assertRedirects(refused, _trace_workspace_url(chosen.identity))
        self.assertContains(refused, "NetBox has changed. Re-read the preview before synchronizing.")

    def test_topology_drift_disables_the_sync_button_with_its_reason(self):
        """The workspace must explain why the reviewed trace cannot be synchronized."""
        self.open_workspace(patched_path())
        self.connect(self.panel_1_rear, self.panel_2_rear)

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertRegex(response.content.decode(), r'<button\b[^>]*data-trace-action="sync"[^>]*\sdisabled(?=[\s>])')
        self.assertContains(response, "NetBox has changed. Re-read the preview before synchronizing.")

    def test_sync_ends_the_preview_when_its_target_went_after_the_render(self):
        """The replan the sync now makes reads the planning target, which can go while it is reviewed."""
        from core.models import Job
        from dcim.models import Location

        location = Location.objects.create(name="Room 9", slug="room-9", site=self.site)
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(patched_path(),)))
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "location": location.pk, "excel_file": upload},
            follow=True,
        )
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        chosen = workspace.context["traces"][0]
        Location.objects.filter(pk=location.pk).delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            follow=True,
        )

        self.assertFalse(Job.objects.filter(data__job_type="netbox_data_import.import").exists())
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(response, "The saved import target is no longer available.")
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_the_workspace_ends_the_preview_when_its_target_goes_after_the_live_plan(self):
        """The proposal display resolves the target again, so loss after planning must still be contained."""
        from dcim.models import Location

        location = Location.objects.create(name="Room 10", slug="room-10", site=self.site)
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(patched_path(),)))
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "location": location.pk, "excel_file": upload},
            follow=True,
        )
        target_reads = 0
        deleting = False

        def delete_target_before_second_read(execute, sql, params, many, context):
            nonlocal deleting, target_reads
            if not deleting and 'FROM "dcim_location"' in sql and params and location.pk in params:
                target_reads += 1
                if target_reads == 2:
                    deleting = True
                    Location.objects.filter(pk=location.pk).delete()
                    deleting = False
            return execute(sql, params, many, context)

        with connection.execute_wrapper(delete_target_before_second_read):
            response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), follow=True)

        self.assertEqual(target_reads, 2)
        self.assertFalse(Location.objects.filter(pk=location.pk).exists())
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(response, "The saved import target is no longer available.")
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_re_reading_clears_the_drift_strip(self):
        """The re-read action adopts the live plan, so the difference it reported is gone."""
        self.open_workspace(patched_path())
        self.connect(self.panel_1_rear, self.panel_2_rear)

        self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertFalse(response.context["drift"])
        statuses = [segment["status"] for segment in response.context["traces"][0].segments]
        self.assertIn("reuse existing", statuses)

    def queue_one_sync(self):
        """Synchronize the first trace and return the queued Job, leaving it unstarted."""
        from core.models import Job

        response = self.open_workspace(patched_path())
        chosen = response.context["traces"][0]
        self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )
        return Job.objects.get(data__job_type="netbox_data_import.import")

    def test_a_re_read_is_refused_while_the_queued_synchronization_still_runs(self):
        """The queued job has not written yet, so a re-read would adopt the state it is about to replace."""
        self.queue_one_sync()

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            follow=True,
        )

        self.assertContains(refused, "A trace synchronization is still running.")
        self.assertTrue(self.client.session[PREVIEW_DIRTY_SESSION_KEY])

    def test_a_second_synchronization_cannot_be_queued_by_re_reading_first(self):
        """The re-read cleared the guard the first queue set, which let a second plan pre-write state."""
        from core.models import Job

        self.queue_one_sync()
        self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {
                "identity": workspace.context["traces"][0].identity,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
            follow=True,
        )

        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), 1)
        self.assertContains(refused, "A trace synchronization is still running.")

    def test_the_queued_synchronization_disables_the_workspace_controls_with_its_reason(self):
        """The page cannot offer a command the POST refuses, so both controls state the same reason."""
        self.queue_one_sync()

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        body = response.content.decode()
        self.assertRegex(body, r'<button\b[^>]*data-trace-action="sync"[^>]*\sdisabled(?=[\s>])')
        self.assertRegex(body, r'<button\b[^>]*id="traceWorkspaceReread"[^>]*\sdisabled(?=[\s>])')
        self.assertContains(response, "A trace synchronization is still running.")

    def test_the_workspace_re_reads_again_once_the_queued_synchronization_ends(self):
        """The block lasts exactly as long as the job, so a terminal job restores every command."""
        from core.choices import JobStatusChoices
        from core.models import Job

        job = self.queue_one_sync()
        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_COMPLETED)

        accepted = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            follow=True,
        )

        self.assertContains(accepted, "The workspace was re-read from NetBox.")
        self.assertFalse(self.client.session[PREVIEW_DIRTY_SESSION_KEY])

    def test_the_sync_command_refuses_the_retained_job_without_help_from_the_dirty_guard(self):
        """The dirty guard hides the sync guard, so this clears it and leaves the retained job alone."""
        from core.models import Job

        self.queue_one_sync()
        session = self.client.session
        session[PREVIEW_DIRTY_SESSION_KEY] = False
        session.save()
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {
                "identity": workspace.context["traces"][0].identity,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
        )

        self.assertEqual(refused.status_code, 302)
        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), 1)

    def test_the_block_holds_for_every_non_terminal_job_status(self):
        """The writes are outstanding until the Job is terminal, not until the worker picks it up."""
        from core.choices import JobStatusChoices
        from core.models import Job

        job = self.queue_one_sync()
        for status in JobStatusChoices.ENQUEUED_STATE_CHOICES:
            with self.subTest(status=status):
                Job.objects.filter(pk=job.pk).update(status=status)

                refused = self.client.post(
                    reverse("plugins:netbox_data_import:trace_workspace_reread"),
                    {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
                    follow=True,
                )

                self.assertContains(refused, "A trace synchronization is still running.")
                self.assertTrue(self.client.session[PREVIEW_DIRTY_SESSION_KEY])

    def test_a_stale_form_post_is_refused_by_the_sync_command(self):
        """Two tabs share one session, so a command from the older one must not queue its plan."""
        from core.models import Job

        response = self.open_workspace(patched_path())
        chosen = response.context["traces"][0]

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": "an-older-tab"},
        )

        self.assertEqual(refused.status_code, 302)
        self.assertFalse(Job.objects.filter(data__job_type="netbox_data_import.import").exists())

    def test_the_workspace_refuses_a_session_that_holds_no_preview(self):
        """Without a materialized preview there is nothing to review, so it sends the operator back."""
        self.client.force_login(self.actor)

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertEqual(response.status_code, 302)

    def open_endpoint_evidence_workspace(self):
        """Upload a Trace List block that states two endpoints and no physical path."""
        self.client.force_login(self.actor)
        upload = BytesIO(
            trace_workbook_bytes(
                include_path=False,
                include_list=True,
                list_blocks=(
                    (
                        trace_endpoint_line(trace_termination("DEV-A", "", "eth0", "Port")),
                        trace_endpoint_line(trace_termination("DEV-B", "", "eth1", "NIC")),
                        (("", "", "", "DEV-A", "", "eth0", "Port", "Ignored"),),
                    ),
                ),
            )
        )
        upload.name = "traces.xlsx"
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(setup.status_code, 200)
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def test_the_topology_panel_names_the_cable_that_satisfies_endpoint_evidence(self):
        """The panel states what NetBox holds. A Cable the import keeps is still a Cable it holds."""
        existing = self.connect(self.eth0, self.eth1)

        response = self.open_endpoint_evidence_workspace()

        trace = response.context["traces"][0]
        self.assertEqual(trace.disposition, "no-op")
        self.assertIsNotNone(trace.logical_cable)
        self.assertTrue(trace.logical_cable["visible"])
        self.assertEqual(trace.logical_cable["display"], str(existing))
        # The Cable is kept, so the proposed panel must not offer to remove it.
        self.assertFalse(trace.deletes_logical_cable)
        self.assertNotContains(response, "No direct Logical Cable joins these endpoints.")

    def test_a_re_read_from_a_stale_tab_is_refused(self):
        """Every other workspace command checks the revision it is sent, so this one has to too."""
        self.open_workspace(patched_path())
        current = self.client.session[PREVIEW_REVISION_SESSION_KEY]

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": "stale"},
            follow=True,
        )

        self.assertEqual(self.client.session[PREVIEW_REVISION_SESSION_KEY], current)
        self.assertContains(response, "This preview is no longer the current one.")

    def test_a_sync_is_refused_when_this_release_dropped_the_source_adapter(self):
        """Queueing a plan for an adapter this release does not register writes nothing but a failure."""
        from core.models import Job

        workspace = self.open_workspace(patched_path())
        chosen = workspace.context["traces"][0]
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired-adapter")

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            follow=True,
        )

        self.assertFalse(Job.objects.filter(data__job_type="netbox_data_import.import").exists())
        self.assertContains(response, "retired-adapter")
        # The preview cannot be planned again in this release, so it is not left to be retried.
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_the_workspace_page_refuses_an_adapter_with_no_target_module(self):
        """Planning raises the same error for an unimplemented Target Module, so the gate must cover it."""
        import dataclasses

        from netbox_data_import import catalog as catalog_module
        from netbox_data_import.catalog import TargetModuleKey

        self.open_workspace(patched_path())
        without_cable = tuple(
            dataclasses.replace(module, implemented=False) if module.key == TargetModuleKey.CABLE else module
            for module in catalog_module.TARGET_MODULES
        )

        with catalog_module.declared_modules_override(without_cable):
            response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "trace_workbook")

    def test_the_preview_refuses_an_adapter_with_no_target_module(self):
        """Preview reload refuses an unavailable Target Module before replanning."""
        import dataclasses

        from netbox_data_import import catalog as catalog_module
        from netbox_data_import.catalog import TargetModuleKey

        self.open_workspace(patched_path())
        without_cable = tuple(
            dataclasses.replace(module, implemented=False) if module.key == TargetModuleKey.CABLE else module
            for module in catalog_module.TARGET_MODULES
        )

        with catalog_module.declared_modules_override(without_cable):
            response = self.client.get(reverse("plugins:netbox_data_import:import_preview"), follow=True)

        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(response, "trace_workbook")
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_the_workspace_page_refuses_an_adapter_this_release_dropped(self):
        """Planning raises for an unregistered adapter, so the page has to refuse before it plans."""
        self.open_workspace(patched_path())
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired-adapter")

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "retired-adapter")


class TraceActionRoutingTest(CableTopologyMixin, TestCase):
    """Each review command posts to its own endpoint, so a new one cannot inherit another's."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_every_offered_action_names_the_endpoint_it_posts_to(self):
        """The template routes on `action.url_name`, so an action cannot reach a command by default."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(direct_path(),)))
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        offered = [action for trace in response.context["traces"] for action in trace.actions]
        self.assertTrue(offered)
        for action in offered:
            self.assertTrue(reverse(action.url_name), f"{action.key} names no endpoint")
        self.assertContains(response, f'action="{reverse("plugins:netbox_data_import:trace_sync")}"')


class RetainedTraceSyncTest(CableTopologyMixin, TestCase):
    """A per-trace sync keeps the preview, so every door onto that preview has to respect its Job."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def upload(self, *blocks):
        """Upload the given path blocks and leave the wizard on a materialized preview."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

    def queue_one_sync(self):
        """Synchronize the first trace and return the queued Job, leaving it unstarted."""
        from core.models import Job

        self.upload(patched_path())
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {
                "identity": workspace.context["traces"][0].identity,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
        )
        return Job.objects.get(data__job_type="netbox_data_import.import")

    def test_the_writer_itself_refuses_a_recalculation_while_the_retained_sync_runs(self):
        """The guard lives in the writer, so a caller that never checks still cannot clear it."""
        from netbox_data_import.plan import ImportPlan
        from netbox_data_import.preview_row_actions import (
            PREVIEW_PLAN_SESSION_KEY,
            PreviewLocked,
            record_recalculated_preview,
        )

        self.queue_one_sync()
        session = self.client.session
        plan = ImportPlan.from_dict(session[PREVIEW_PLAN_SESSION_KEY])

        with self.assertRaises(PreviewLocked):
            record_recalculated_preview(session, plan, user=self.actor)

        self.assertTrue(session[PREVIEW_DIRTY_SESSION_KEY])

    def test_the_writer_records_again_once_the_retained_sync_is_terminal(self):
        """The refusal lasts exactly as long as the Job, so the writer is not simply disabled."""
        from core.choices import JobStatusChoices
        from core.models import Job

        from netbox_data_import.plan import ImportPlan
        from netbox_data_import.preview_row_actions import (
            PREVIEW_PLAN_SESSION_KEY,
            record_recalculated_preview,
        )

        job = self.queue_one_sync()
        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_COMPLETED)
        session = self.client.session
        plan = ImportPlan.from_dict(session[PREVIEW_PLAN_SESSION_KEY])

        record_recalculated_preview(session, plan, user=self.actor)

        self.assertFalse(session[PREVIEW_DIRTY_SESSION_KEY])

    def test_the_ordinary_preview_does_not_recalculate_while_the_retained_sync_runs(self):
        """The wizard preview is another door onto the same preview, and it clears the same guard."""
        self.queue_one_sync()

        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"), follow=True)

        self.assertTrue(self.client.session[PREVIEW_DIRTY_SESSION_KEY])
        self.assertContains(response, "A trace synchronization is still running.")

    def test_a_full_import_cannot_be_queued_while_the_retained_sync_runs(self):
        """Recalculating through the wizard preview would otherwise unblock the whole-plan import."""
        from core.models import Job

        self.queue_one_sync()
        self.client.get(reverse("plugins:netbox_data_import:import_preview"), follow=True)

        refused = self.client.post(reverse("plugins:netbox_data_import:import_run"), follow=True)

        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), 1)
        self.assertContains(refused, "A trace synchronization is still running.")

    def test_a_running_whole_plan_import_does_not_block_a_later_workspace(self):
        """The wizard leaves its own Job id behind, and that Job is not a retained trace sync."""
        from core.models import Job

        self.upload(patched_path())
        self.client.post(reverse("plugins:netbox_data_import:import_run"), follow=True)
        wizard_job = Job.objects.get(data__job_type="netbox_data_import.import")
        self.assertEqual(self.client.session["import_background_job_id"], wizard_job.pk)

        self.upload(patched_path())
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertNotContains(response, "A trace synchronization is still running.")
        sync = next(action for action in response.context["traces"][0].actions if action.key == "sync")
        self.assertTrue(sync.enabled)

    def _rival_sync_job(self, first):
        """Enqueue the Job a second concurrent sync would create against this same preview."""
        from core.choices import JobNotificationChoices

        from netbox_data_import.jobs import ImportJobRunner

        rival = ImportJobRunner.enqueue(
            name=ImportJobRunner.name,
            user=self.actor,
            notifications=JobNotificationChoices.NOTIFICATION_NEVER,
            job_timeout=3600,
            profile_id=self.profile.pk,
            source_document_id=first.data["source_document_id"],
            accepted_plan=first.data["accepted_plan"],
            selection=[],
            idempotency_key="rival-selection",
        )
        rival.data = dict(first.data)
        rival.save(update_fields=["data"])
        return rival

    def test_a_sync_that_finishes_first_does_not_unlock_one_that_is_still_running(self):
        """Two syncs hold one preview, and the first to finish is not the last to write.

        Nothing orders the two, so the Job a request happens to know about can reach a terminal state
        while its rival is still writing. One of them holding the preview is not enough.
        """
        from core.choices import JobStatusChoices
        from core.models import Job

        from netbox_data_import.plan import ImportPlan
        from netbox_data_import.preview_row_actions import (
            PREVIEW_PLAN_SESSION_KEY,
            PreviewLocked,
            record_recalculated_preview,
        )

        queued = self.queue_one_sync()
        rival = self._rival_sync_job(queued)
        Job.objects.filter(pk=queued.pk).update(status=JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(Job.objects.get(pk=rival.pk).status, JobStatusChoices.STATUS_PENDING)
        session = self.client.session

        plan = ImportPlan.from_dict(session[PREVIEW_PLAN_SESSION_KEY])
        with self.assertRaises(PreviewLocked):
            record_recalculated_preview(session, plan, user=self.actor)

    def test_a_fresh_session_is_refused_by_the_sync_it_never_queued(self):
        """The Job holds the preview, so a session that was never told about it is refused too."""
        from netbox_data_import.plan import ImportPlan
        from netbox_data_import.preview_row_actions import (
            PREVIEW_PLAN_SESSION_KEY,
            PreviewLocked,
            record_recalculated_preview,
        )

        self.queue_one_sync()
        context = dict(self.client.session["import_context"])
        plan_data = self.client.session[PREVIEW_PLAN_SESSION_KEY]

        self.client.logout()
        self.client.force_login(self.actor)
        fresh = self.client.session
        fresh["import_context"] = context
        fresh.save()

        with self.assertRaises(PreviewLocked):
            record_recalculated_preview(fresh, ImportPlan.from_dict(plan_data), user=self.actor)

    def test_a_reread_is_refused_before_it_reads_while_the_sync_runs(self):
        """The view reads NetBox and stores the result, and the sync can end between the two.

        The read would then see NetBox as it was *before* the sync wrote, and the store would pass
        because the Job has since gone terminal. The workspace would report a successful re-read and
        mark that stale plan clean. The guard has to refuse before anything is read.

        The Job is completed from a query wrapper on the live connection, so the sync lands exactly
        when the planning read begins. Nothing is patched: the real view, ORM and planner all run.
        """
        from core.choices import JobStatusChoices
        from core.models import Job
        from django.db import connection

        job = self.queue_one_sync()
        stored_before = self.client.session[PREVIEW_PLAN_SESSION_KEY]
        completed: list[str] = []

        def complete_the_sync_once_the_read_starts(execute, sql, params, many, context):
            """End the retained sync at the planner's first read of live NetBox state."""
            result = execute(sql, params, many, context)
            if not completed and "dcim_" in sql:
                completed.append(sql)
                Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_COMPLETED)
            return result

        # Only the re-read request is watched; following its redirect would read NetBox legitimately.
        with connection.execute_wrapper(complete_the_sync_once_the_read_starts):
            response = self.client.post(
                reverse("plugins:netbox_data_import:trace_workspace_reread"),
                {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            )

        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response.url), "A trace synchronization is still running.")
        # The guard refused first, so the planner never read and the sync is still the live one.
        self.assertEqual(completed, [])
        self.assertTrue(self.client.session[PREVIEW_DIRTY_SESSION_KEY])
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], stored_before)

    def test_the_wizard_preview_answers_a_sync_that_starts_after_its_own_check(self):
        """The preview checks the guard, then replans and stores, and a sync can arrive between.

        The writer then refuses the store, and nothing caught that, so the operator met a 500 on an
        ordinary page load. The sync is made live from a query wrapper at the planning read, which is
        after the page's own check and before the store.
        """
        from core.choices import JobStatusChoices
        from core.models import Job
        from django.db import connection

        job = self.queue_one_sync()
        # Terminal at the page's check, so the page replans instead of adopting the stored plan.
        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_COMPLETED)
        session = self.client.session
        session[PREVIEW_DIRTY_SESSION_KEY] = False
        session.save()
        started: list[str] = []

        def start_the_sync_once_the_replan_begins(execute, sql, params, many, context):
            """Make the retained sync live again while the page is planning."""
            result = execute(sql, params, many, context)
            if not started and "dcim_" in sql:
                started.append(sql)
                Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_PENDING)
            return result

        with connection.execute_wrapper(start_the_sync_once_the_replan_begins):
            response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertTrue(started, "the page never replanned, so the race was not reached")
        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:trace_workspace"),
            fetch_redirect_response=False,
        )
        self.assertContains(self.client.get(response.url), "A trace synchronization is still running.")

    def test_a_new_upload_frees_the_workspace_of_the_previous_retained_sync(self):
        """A new preview owns no earlier sync, so the old Job must not refuse its commands."""
        self.queue_one_sync()

        self.upload(patched_path())
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertNotContains(response, "A trace synchronization is still running.")
        sync = next(action for action in response.context["traces"][0].actions if action.key == "sync")
        self.assertTrue(sync.enabled)


class RetainedSyncEnqueueSerializationTest(IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """The guard is read under the profile row, so two syncs cannot both pass it and queue."""

    def setUp(self):
        """Build the shared topology this transactional case cannot inherit from class data."""
        super().setUp()
        self.build_topology()

    @staticmethod
    def _backend_pid():
        """Return the PostgreSQL backend PID this connection is using."""
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    @staticmethod
    def _is_blocked(pid):
        """Return whether that one backend is waiting on a lock.

        A row wait shows up as an ungranted `transactionid` lock with no relation, so this keys on
        the backend rather than on the table: any other waiter would otherwise pass for ours.
        """
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE NOT granted AND pid = %s", [pid])
            return cursor.fetchone()[0] > 0

    def test_a_sync_queued_under_the_lock_refuses_the_one_waiting_behind_it(self):
        """The competing Job commits as the row is released, and the waiting request has to see it."""
        import threading
        import time

        from core.choices import JobNotificationChoices
        from core.models import Job

        from netbox_data_import.jobs import ImportJobRunner
        from netbox_data_import.models import locked_profile_policy

        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(direct_path(),)))
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        chosen = workspace.context["traces"][0]
        context = self.client.session["import_context"]
        profile_pk, document_pk = self.profile.pk, context["source_document_id"]
        # The test client runs the view on this connection, so this is the PID that will block.
        target_pid = self._backend_pid()
        holding, waited = threading.Event(), []

        def queue_the_competing_sync():
            """Hold the profile row, then commit the Job the rival request would have queued."""
            from django.db import connection

            try:
                with locked_profile_policy(profile_pk):
                    holding.set()
                    # Let the request under test reach the row and block on it before committing.
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not self._is_blocked(target_pid):
                        time.sleep(0.05)
                    waited.append(self._is_blocked(target_pid))
                    if not waited[-1]:
                        # Queueing anyway would let the request meet a Job it never waited for.
                        return
                    rival = ImportJobRunner.enqueue(
                        name=ImportJobRunner.name,
                        user=self.actor,
                        notifications=JobNotificationChoices.NOTIFICATION_NEVER,
                        job_timeout=3600,
                        profile_id=profile_pk,
                        source_document_id=document_pk,
                        accepted_plan={},
                        selection=[],
                        idempotency_key="rival-selection",
                    )
                    rival.data = {
                        "job_type": ImportJobRunner.job_type,
                        "profile_id": profile_pk,
                        "source_document_id": document_pk,
                        "keeps_preview": True,
                    }
                    rival.save(update_fields=["data"])
            finally:
                connection.close()

        holder = threading.Thread(target=queue_the_competing_sync)
        holder.start()
        try:
            self.assertTrue(holding.wait(10), "the competing connection never took the profile row")
            response = self.client.post(
                reverse("plugins:netbox_data_import:trace_sync"),
                {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
                follow=True,
            )
        finally:
            holder.join(20)

        self.assertEqual(waited, [True], "the request never waited on the profile row the enqueue holds")
        self.assertContains(response, "A trace synchronization is still running.")
        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), 1)


class TraceSyncDispatchFailureTest(IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """The Job row commits before the queue push, so a push that fails must not hold the preview."""

    def setUp(self):
        """Build the shared topology this transactional case cannot inherit from class data."""
        super().setUp()
        self.build_topology()

    def _upload_and_choose(self):
        """Leave the wizard on a materialized preview and return the first trace."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(direct_path(),)))
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        return workspace.context["traces"][0]

    def test_a_queue_push_that_fails_leaves_no_job_holding_the_preview(self):
        """NetBox pushes from `on_commit`, so the row outlives a refused push and would block."""
        from unittest.mock import patch

        from core.choices import JobStatusChoices
        from core.models import Job
        from django_rq.queues import DjangoRQ
        from redis.exceptions import ConnectionError as RedisConnectionError

        from netbox_data_import.preview_row_actions import retained_sync_block_reason

        chosen = self._upload_and_choose()
        revision = self.client.session[PREVIEW_REVISION_SESSION_KEY]

        with patch.object(DjangoRQ, "enqueue_call", autospec=True, side_effect=RedisConnectionError("queue down")):
            with self.assertRaises(RedisConnectionError):
                self.client.post(
                    reverse("plugins:netbox_data_import:trace_sync"),
                    {"identity": chosen.identity, "preview_revision": revision},
                )

        stranded = Job.objects.get(data__job_type="netbox_data_import.import")
        self.assertEqual(stranded.status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual(retained_sync_block_reason(self.client.session, self.actor), "")


class TraceTerminationPickerTest(CableTopologyMixin, TestCase):
    """The picker offers eligible candidates only, and its choice replans the preview."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def open_workspace(self, *blocks):
        """Upload the given path blocks and leave the wizard on a materialized preview."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

    def open_blocked_workspace(self):
        """Leave the wizard on a preview whose one trace waits on a termination decision."""
        self.open_workspace(
            direct_path(
                from_end=trace_termination("DEV-A", "", "absent-port", "Port"),
                to_end=trace_termination("DEV-B", "", "eth1", "Port"),
            )
        )
        return termination_field_key(device="DEV-A", cards="", port="absent-port", kind="interface")

    def candidates(self, field_key, **params):
        """Ask the picker endpoint the way the picker itself asks: JSON, with the revision."""
        params.setdefault("preview_revision", self.client.session[PREVIEW_REVISION_SESSION_KEY])
        return self.client.get(
            reverse("plugins:netbox_data_import:trace_termination_candidates"),
            {"field_key": field_key, **params},
            headers={"accept": "application/json"},
        )

    def test_the_picker_is_answered_when_it_asks_for_json(self):
        """The picker sends Accept: application/json, which the revision check has to accept."""
        field_key = self.open_blocked_workspace()

        response = self.candidates(field_key)

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertTrue(response.json()["ok"])

    def test_a_stale_revision_is_refused(self):
        """A picker left open across a recalculation must not read the preview it no longer shows."""
        field_key = self.open_blocked_workspace()

        response = self.candidates(field_key, preview_revision="stale")

        self.assertEqual(response.status_code, 409)

    def test_the_picker_offers_the_claimed_kind_on_the_resolved_device(self):
        """The picker never offers a port of another kind, nor one on another Device."""
        field_key = self.open_blocked_workspace()
        Interface.objects.create(device=self.device_a, name="eth5", type="1000base-t")
        Interface.objects.create(device=self.device_b, name="elsewhere", type="1000base-t")

        payload = self.candidates(field_key).json()

        self.assertEqual([item["name"] for item in payload["candidates"]], ["eth0", "eth5"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["shown"], 2)

    def test_the_picker_states_how_many_of_the_eligible_candidates_it_shows(self):
        """The visible count is "N of M eligible", so a capped page has to report both."""
        field_key = self.open_blocked_workspace()
        for number in range(6):
            Interface.objects.create(device=self.device_a, name=f"eth1{number}", type="1000base-t")

        payload = self.candidates(field_key, limit=3).json()

        self.assertEqual(payload["shown"], 3)
        self.assertEqual(payload["total"], 7)

    def test_the_picker_rejects_invalid_limits(self):
        """A malformed or out-of-range limit is an invalid request."""
        field_key = self.open_blocked_workspace()
        Interface.objects.create(device=self.device_a, name="eth5", type="1000base-t")

        for limit in ("not-an-integer", "-1", "0", str(ELIGIBLE_TERMINATION_LIMIT + 1)):
            with self.subTest(limit=limit):
                response = self.candidates(field_key, limit=limit)

                self.assertEqual(response.status_code, 400)
                # A bare 400 would still pass if a later regression refused the request elsewhere.
                self.assertEqual(
                    response.json()["error"],
                    f"Candidate limit must be an integer from 1 to {ELIGIBLE_TERMINATION_LIMIT}.",
                )

    def test_the_picker_searches_by_name(self):
        """A searchable picker narrows the same eligible set, and never widens it."""
        field_key = self.open_blocked_workspace()
        Interface.objects.create(device=self.device_a, name="mgmt0", type="1000base-t")

        payload = self.candidates(field_key, search="mgmt").json()

        self.assertEqual([item["name"] for item in payload["candidates"]], ["mgmt0"])
        self.assertEqual(payload["total"], 1)

    def test_choosing_a_candidate_saves_the_decision_and_replans(self):
        """The decision is a TerminationResolution row, and the plan is asked for again."""
        field_key = self.open_blocked_workspace()

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": field_key,
                "object_type": "dcim.interface",
                "object_id": self.eth0.pk,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
        )

        self.assertEqual(response.status_code, 302)
        stored = TerminationResolution.objects.get(profile=self.profile, field_key=field_key)
        self.assertEqual(stored.selected_object_id, self.eth0.pk)
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        trace = workspace.context["traces"][0]
        self.assertEqual(trace.disposition, "actionable")
        states = {item["label"]: item["state"] for item in trace.terminations}
        self.assertEqual(states["DEV-A absent-port"], "manually resolved")

    def test_a_termination_decision_returns_to_the_trace_it_was_made_on(self):
        """The picker is opened from one trace, so the page after the save has to show that trace."""
        Interface.objects.create(device=self.make_device("SEL-I"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("SEL-J"), name="eth0", type="1000base-t")
        self.open_workspace(
            direct_path(
                from_end=trace_termination("SEL-I", "", "eth0", "Port"),
                to_end=trace_termination("SEL-J", "", "eth0", "Port"),
            ),
            direct_path(
                from_end=trace_termination("DEV-A", "", "absent-port", "Port"),
                to_end=trace_termination("DEV-B", "", "eth1", "Port"),
            ),
        )
        field_key = termination_field_key(device="DEV-A", cards="", port="absent-port", kind="interface")
        opened = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        wanted = next(
            trace
            for trace in opened.context["traces"]
            if any(item["field_key"] == field_key for item in trace.terminations)
        )
        self.assertNotEqual(opened.context["selected_trace"].identity, wanted.identity)

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": field_key,
                "object_type": "dcim.interface",
                "object_id": self.eth0.pk,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                "trace": wanted.identity,
            },
            follow=True,
        )

        self.assertEqual(response.context["selected_trace"].identity, wanted.identity)

    def test_a_decision_is_refused_while_a_queued_synchronization_still_runs(self):
        """The replan a decision makes reads NetBox, which the queued job is about to write."""
        from core.models import Job

        Interface.objects.create(device=self.make_device("SRC-open"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("DST-open"), name="eth0", type="1000base-t")
        blocked = direct_path(
            from_end=trace_termination("SRC-open", "", "absent-port", "Port"),
            to_end=trace_termination("DST-open", "", "eth0", "Port"),
        )
        self.open_workspace(patched_path(), blocked)
        field_key = termination_field_key(device="SRC-open", cards="", port="absent-port", kind="interface")
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        syncable = next(trace for trace in workspace.context["traces"] if trace.disposition == "actionable")
        self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": syncable.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )
        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), 1)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": field_key,
                "object_type": "dcim.interface",
                "object_id": Interface.objects.get(device__name="SRC-open", name="eth0").pk,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
            headers={"accept": "application/json"},
        )

        self.assertEqual(refused.status_code, 409, refused.content[:300])
        self.assertIn("A trace synchronization is still running.", refused.json()["error"])
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile, field_key=field_key).exists())

    def test_a_searched_candidate_beyond_the_first_page_can_be_saved(self):
        """The offer is what the picker showed, so the recheck has to reproduce that query."""
        field_key = self.open_blocked_workspace()
        for number in range(ELIGIBLE_TERMINATION_LIMIT + 1):
            Interface.objects.create(device=self.device_a, name=f"aa{number:03d}", type="1000base-t")
        target = Interface.objects.create(device=self.device_a, name="zz-target", type="1000base-t")
        offered = self.candidates(field_key, search="zz-target").json()
        self.assertEqual([item["id"] for item in offered["candidates"]], [target.pk])

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": field_key,
                "object_type": "dcim.interface",
                "object_id": target.pk,
                "search": "zz-target",
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            TerminationResolution.objects.get(profile=self.profile, field_key=field_key).selected_object_id,
            target.pk,
        )

    def test_a_field_key_the_workspace_never_asked_about_is_refused(self):
        """A review command answers a question this preview asked, not one the caller invented."""
        self.open_blocked_workspace()
        # PANEL-1 exists in NetBox, but this workbook never names it.
        elsewhere = termination_field_key(device="PANEL-1", cards="", port="F1", kind="front_port")

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": elsewhere,
                "object_type": "dcim.frontport",
                "object_id": self.panel_1_fronts[0].pk,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
            headers={"accept": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(TerminationResolution.objects.filter(field_key=elsewhere).exists())

    def test_a_stale_form_post_is_refused_by_the_resolve_command(self):
        """A decision taken against a preview that has moved on is not the decision it looks like."""
        field_key = self.open_blocked_workspace()

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": field_key,
                "object_type": "dcim.interface",
                "object_id": self.eth0.pk,
                "preview_revision": "an-older-tab",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_a_candidate_outside_the_eligible_set_is_refused(self):
        """The picker is the only legal source of a choice, so the endpoint rechecks it."""
        field_key = self.open_blocked_workspace()

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": field_key,
                "object_type": "dcim.interface",
                "object_id": self.eth1.pk,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
            headers={"accept": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("eligible", response.json()["error"])
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())

    def test_a_preview_lock_rolls_back_the_termination_resolution(self):
        """The saved decision and the replacement preview form one database outcome."""
        import uuid

        from core.choices import JobStatusChoices
        from core.models import Job

        from netbox_data_import.jobs import ImportJobRunner

        field_key = self.open_blocked_workspace()
        import_context = self.client.session["import_context"]
        retained = []
        decision_writes = []

        def retain_preview_after_initial_guard(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "netbox_data_import_terminationresolution" in sql.lower() and sql.lstrip().upper().startswith(
                ("INSERT", "UPDATE")
            ):
                decision_writes.append(sql)
            if not retained and 'FROM "core_job"' in sql:
                retained.append(sql)
                Job.objects.create(
                    name=ImportJobRunner.name,
                    user=self.actor,
                    job_id=uuid.uuid4(),
                    status=JobStatusChoices.STATUS_PENDING,
                    data={
                        "job_type": ImportJobRunner.job_type,
                        "keeps_preview": True,
                        "profile_id": self.profile.pk,
                        "source_document_id": import_context["source_document_id"],
                    },
                )
            return result

        with connection.execute_wrapper(retain_preview_after_initial_guard):
            response = self.client.post(
                reverse("plugins:netbox_data_import:trace_resolve_termination"),
                {
                    "field_key": field_key,
                    "object_type": "dcim.interface",
                    "object_id": self.eth0.pk,
                    "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                },
                headers={"accept": "application/json"},
            )

        self.assertTrue(retained)
        self.assertTrue(decision_writes)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())


class TraceSyncSelectionTest(CableTopologyMixin, TestCase):
    """`Sync with dependencies` selects the trace and every unit whose change it needs."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_a_trace_that_depends_on_nothing_selects_itself(self):
        """A self-contained trace needs no other unit, and must not drag one in."""
        workspace = ReviewWorkspace(self.plan(patched_path()), self.actor)

        selection = workspace.sync_selection(workspace.traces[0].identity)

        self.assertEqual(selection, (workspace.traces[0].identity,))

    def test_a_trace_selects_the_unit_that_owns_the_change_it_depends_on(self):
        """A create that waits on another unit's delete cannot execute without it."""
        plan = ImportPlan(
            units=(
                SynchronizationUnit(
                    identity="cable:trace:first",
                    disposition=Disposition.ACTIONABLE,
                    changes=(
                        PlannedChange(
                            identity="cable:create:shared",
                            target_module="cable",
                            operation="create",
                            payload={},
                            dependencies=("cable:delete:7",),
                        ),
                    ),
                    display={"trace": {"cable_policies": []}},
                ),
                SynchronizationUnit(
                    identity="cable:trace:second",
                    disposition=Disposition.ACTIONABLE,
                    changes=(
                        PlannedChange(
                            identity="cable:delete:7",
                            target_module="cable",
                            operation="delete",
                            payload={},
                        ),
                    ),
                    display={"trace": {"cable_policies": []}},
                ),
            )
        )

        selection = ReviewWorkspace(plan, self.actor).sync_selection("cable:trace:first")

        self.assertEqual(sorted(selection), ["cable:trace:first", "cable:trace:second"])

    def test_a_trace_that_is_not_actionable_selects_nothing(self):
        """A blocked trace has no work to select, so the command has nothing to send."""
        Interface.objects.create(device=self.make_device("SRC-Y"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("DST-Y"), name="eth0", type="1000base-t")
        blocked = direct_path(
            from_end=trace_termination("SRC-Y", "", "absent-port", "Port"),
            to_end=trace_termination("DST-Y", "", "eth0", "Port"),
        )
        workspace = ReviewWorkspace(self.plan(blocked), self.actor)

        self.assertEqual(workspace.sync_selection(workspace.traces[0].identity), ())


class TraceSyncExecutionTest(IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """Synchronizing one trace writes that trace's Cables and leaves the others alone."""

    def setUp(self):
        """Build the shared topology this transactional case cannot inherit from class data."""
        super().setUp()
        self.build_topology()

    def test_synchronizing_one_trace_writes_only_its_own_segments(self):
        """A per-trace command is a selection, so it must not queue the whole plan."""
        from core.models import Job

        second = Interface.objects.create(device=self.make_device("DEV-E"), name="eth0", type="1000base-t")
        other = Interface.objects.create(device=self.make_device("DEV-F"), name="eth0", type="1000base-t")
        untouched = direct_path(
            from_end=trace_termination("DEV-E", "", "eth0", "Port"),
            to_end=trace_termination("DEV-F", "", "eth0", "Port"),
        )
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(direct_path(), untouched)))
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        chosen = next(trace for trace in workspace.context["traces"] if trace.endpoints["from"] == "DEV-A eth0")

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )

        job = Job.objects.get(data__job_type="netbox_data_import.import")
        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}),
            fetch_redirect_response=False,
        )
        self.run_rq_jobs()
        self.assertTrue(cables_on(self.eth0).exists())
        self.assertFalse(cables_on(second).exists())
        self.assertFalse(cables_on(other).exists())

    def test_a_second_trace_can_be_synchronized_after_the_first(self):
        """A per-trace command is repeatable, so it must not spend the whole preview on one trace."""
        from core.models import Job

        second = Interface.objects.create(device=self.make_device("DEV-G"), name="eth0", type="1000base-t")
        other = Interface.objects.create(device=self.make_device("DEV-H"), name="eth0", type="1000base-t")
        independent = direct_path(
            from_end=trace_termination("DEV-G", "", "eth0", "Port"),
            to_end=trace_termination("DEV-H", "", "eth0", "Port"),
        )
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(direct_path(), independent)))
        upload.name = "traces.xlsx"
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )

        self.assertEqual(setup.status_code, 200, "the setup POST did not render")
        self.assertTrue(self.client.session.get("import_preview_pending"), "setup stored no preview")

        for step, endpoint in enumerate(("DEV-A eth0", "DEV-G eth0"), start=1):
            workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
            self.assertEqual(workspace.status_code, 200, "the workspace has to survive a per-trace sync")
            chosen = next(trace for trace in workspace.context["traces"] if trace.endpoints["from"] == endpoint)
            response = self.client.post(
                reverse("plugins:netbox_data_import:trace_sync"),
                {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            )
            queued = Job.objects.filter(data__job_type="netbox_data_import.import").order_by("pk")
            self.assertEqual(queued.count(), step, f"step {step}: the sync queued no new job")
            self.assertRedirects(
                response,
                reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": queued.last().pk}),
                fetch_redirect_response=False,
                msg_prefix=f"step {step}: synchronize",
            )
            self.run_rq_jobs()
            reread = self.client.post(
                reverse("plugins:netbox_data_import:trace_workspace_reread"),
                {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            )

            self.assertRedirects(
                reread,
                reverse("plugins:netbox_data_import:trace_workspace"),
                fetch_redirect_response=False,
                msg_prefix=f"step {step}: re-read",
            )
            self.assertContains(
                self.client.get(reread.url),
                "The workspace was re-read from NetBox.",
                msg_prefix=f"step {step}: re-read",
            )

        self.assertTrue(cables_on(self.eth0).exists())
        self.assertTrue(cables_on(second).exists())
        self.assertTrue(cables_on(other).exists())

    def test_a_replanned_trace_is_executed_again_rather_than_reported_done(self):
        """One trace identity spans two workbooks, so the execution key cannot be the selection alone."""
        from core.models import Job
        from dcim.models import Cable

        from netbox_data_import.models import CableImportSource, ExecutionOutcome, ImportExecution

        self.client.force_login(self.actor)
        keys = []
        # The loop ignored every response, so a silent refusal anywhere arrived as one final failure.
        for step, blocks in enumerate((direct_path(), patched_path()), start=1):
            upload = BytesIO(trace_workbook_bytes(path_blocks=(blocks,)))
            upload.name = "traces.xlsx"
            setup = self.client.post(
                reverse("plugins:netbox_data_import:import_setup"),
                {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
                follow=True,
            )
            self.assertEqual(setup.status_code, 200, f"step {step}: the setup POST did not render")
            self.assertTrue(self.client.session.get("import_preview_pending"), f"step {step}: setup stored no preview")
            workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
            self.assertEqual(workspace.status_code, 200, f"step {step}: the workspace did not render")
            self.assertFalse(
                workspace.context["drift"], f"step {step}: the fresh preview already disagreed with NetBox"
            )
            self.assertTrue(workspace.context["traces"], f"step {step}: the preview planned no trace")
            chosen = workspace.context["traces"][0]
            response = self.client.post(
                reverse("plugins:netbox_data_import:trace_sync"),
                {"identity": chosen.identity, "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
            )
            queued = Job.objects.filter(data__job_type="netbox_data_import.import").order_by("pk")
            self.assertEqual(queued.count(), step, f"step {step}: the sync queued no new job")
            self.assertRedirects(
                response,
                reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": queued.last().pk}),
                fetch_redirect_response=False,
                msg_prefix=f"step {step}",
            )
            self.run_rq_jobs()
            execution = ImportExecution.objects.order_by("pk").last()
            self.assertIsNotNone(execution, f"step {step}: the queued job recorded no execution")
            job = queued.last()
            job.refresh_from_db()
            # The job swallows its own failure into a row, so the outcome is what says it ran.
            self.assertEqual(
                execution.outcome,
                ExecutionOutcome.SUCCEEDED,
                f"step {step}: outcome={execution.outcome} failure={execution.failure_detail} "
                f"counts={execution.result_counts} job={job.status} data={job.data}",
            )
            keys.append(execution.idempotency_key)

        self.assertNotEqual(keys[0], keys[1], "both steps built one execution key, so the second never ran")

        # The patched path replaces the direct Cable with its three physical segments.
        self.assertFalse(cables_on(self.eth0, self.eth1).exists())
        self.assertTrue(cables_on(self.eth0, self.panel_1_fronts[0]).exists())
        self.assertTrue(cables_on(self.panel_1_rear, self.panel_2_rear).exists())
        self.assertTrue(cables_on(self.panel_2_fronts[0], self.eth1).exists())
        self.assertEqual(Cable.objects.count(), 3)
        self.assertEqual(CableImportSource.objects.count(), 3)
        self.assertEqual(
            set(CableImportSource.objects.values_list("cable_id", flat=True)),
            set(Cable.objects.values_list("pk", flat=True)),
        )
        self.assertEqual(
            sorted(CableImportSource.objects.values_list("segment_index", flat=True)),
            [0, 1, 2],
        )
        self.assertEqual({cable.status for cable in Cable.objects.all()}, {"connected"})
        self.assertEqual({cable.type for cable in Cable.objects.all()}, {"cat6"})


class TraceResolveTargetLossTest(CableTopologyMixin, TransactionTestCase):
    """The replan a decision asks for reads the planning target, which can go while it is deciding."""

    def setUp(self):
        """Build the shared topology this transactional case cannot inherit from class data."""
        super().setUp()
        self.build_topology()

    def test_a_target_deleted_while_the_decision_saves_ends_the_preview_with_its_reason(self):
        """The saved decision replans, so a target removed under it must not answer a 500."""
        from dcim.models import Location
        from django.db.models.signals import post_save

        location = Location.objects.create(name="Room 1", slug="room-1", site=self.site)
        self.client.force_login(self.actor)
        upload = BytesIO(
            trace_workbook_bytes(
                path_blocks=(
                    direct_path(
                        from_end=trace_termination("DEV-A", "", "absent-port", "Port"),
                        to_end=trace_termination("DEV-B", "", "eth1", "Port"),
                    ),
                )
            )
        )
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "location": location.pk, "excel_file": upload},
            follow=True,
        )
        field_key = termination_field_key(device="DEV-A", cards="", port="absent-port", kind="interface")

        # The location goes on another connection between the eligibility recheck and the replan.
        with competing_write_during(
            post_save, TerminationResolution, lambda: Location.objects.filter(pk=location.pk).delete()
        ) as (observed, blocked):
            response = self.client.post(
                reverse("plugins:netbox_data_import:trace_resolve_termination"),
                {
                    "field_key": field_key,
                    "object_type": "dcim.interface",
                    "object_id": self.eth0.pk,
                    "search": "",
                    "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                },
                follow=True,
            )

        self.assertTrue(observed, "the decision never reached its TerminationResolution write")
        self.assertFalse(blocked, "the target deletion must complete before the replan")
        self.assertFalse(Location.objects.filter(pk=location.pk).exists())
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(response, "The saved import target is no longer available.")
        self.assertFalse(self.client.session["import_preview_pending"])


class TraceWorkspaceCableDisclosureTest(CableTopologyMixin, TransactionTestCase):
    """Recheck cached Cable display values against the viewer of each workspace render."""

    def setUp(self):
        self.build_topology()
        self.viewer = user_with_object_permission(
            "trace-cable-viewer",
            [
                (ImportProfile, ("view", "change"), {}),
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("view", "add", "delete"), {}),
            ],
        )
        self.client.force_login(self.viewer)

    def open_workspace(self, *blocks):
        """Upload the path blocks through the real setup flow and render the workspace."""
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(setup.status_code, 200)
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def revoke_cable_view(self):
        """Keep Cable writes permitted while removing the viewer's Cable read grant."""
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.get(name__startswith="trace-cable-viewer Cable ")
        permission.actions = ["add", "delete"]
        permission.save(update_fields=("actions",))

    def grant_cable_view(self):
        """Restore the Cable read grant without changing the accepted cached plan."""
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.get(name__startswith="trace-cable-viewer Cable ")
        permission.actions = ["view", "add", "delete"]
        permission.save(update_fields=("actions",))

    def test_revoking_cable_view_makes_the_cached_page_match_a_fresh_hidden_plan(self):
        """A cached Cable name must disappear on the first render after its view grant is revoked."""
        logical = self.connect(self.eth0, self.eth1, label="Reviewed link")
        visible = self.open_workspace(patched_path())
        self.assertContains(visible, str(logical))
        self.revoke_cable_view()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertNotContains(cached, str(logical))
        self.assertContains(cached, "A Logical Cable exists that you may not view.")
        cached_trace = cached.context["selected_trace"]
        reread = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )
        fresh = self.client.get(reread.url)
        self.assertNotContains(fresh, str(logical))
        self.assertContains(fresh, "A Logical Cable exists that you may not view.")
        self.assertEqual(cached_trace, fresh.context["selected_trace"])

    def test_a_later_cable_view_grant_does_not_reveal_a_planner_redaction(self):
        """A plan with no Cable row identity has no cached value a later grant can reveal."""
        logical = self.connect(self.eth0, self.eth1, label="Initially hidden link")
        self.revoke_cable_view()
        hidden = self.open_workspace(patched_path())
        self.assertNotContains(hidden, str(logical))
        self.grant_cable_view()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertNotContains(cached, str(logical))
        self.assertContains(cached, "A Logical Cable exists that you may not view.")

    def test_a_deleted_cable_redacts_instead_of_falling_back_to_cached_text(self):
        """A row that no longer exists cannot authorize its cached display value."""
        logical = self.connect(self.eth0, self.eth1, label="Deleted reviewed link")
        self.open_workspace(patched_path())
        logical.delete()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertNotContains(cached, "Deleted reviewed link")
        self.assertContains(cached, "A Logical Cable exists that you may not view.")

    def test_cached_media_wording_matches_a_fresh_plan_after_cable_view_is_revoked(self):
        """The renderer recomposes a media warning after it removes one retained Cable's facts."""
        self.connect(self.eth0, self.panel_1_fronts[0], type="cat6")
        self.connect(self.panel_1_rear, self.panel_2_rear, type="mmf-om4")
        self.connect(self.panel_2_fronts[0], self.eth1, type="cat6")
        visible = self.open_workspace(patched_path())
        visible_message = visible.context["selected_trace"].findings[-1]["message"]
        self.assertIn(cable_type_label("mmf-om4"), visible_message)
        self.revoke_cable_view()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        cached_finding = cached.context["selected_trace"].findings[-1]
        self.assertIn("a Cable you cannot view", cached_finding["message"])
        self.assertNotIn(cable_type_label("mmf-om4"), cached_finding["message"])
        reread = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY]},
        )
        fresh = self.client.get(reread.url)
        self.assertEqual(cached_finding, fresh.context["selected_trace"].findings[-1])

    def test_one_query_resolves_every_referenced_cable_for_one_workspace(self):
        """Presentation batches Cable visibility for the whole accepted plan."""
        self.connect(self.eth0, self.panel_1_fronts[0], label="First retained link")
        self.connect(self.panel_1_rear, self.panel_2_rear, label="Middle retained link", type="mmf-om4")
        self.connect(self.panel_2_fronts[0], self.eth1, label="Last retained link")
        plan = self.plan(patched_path(), actor=self.viewer)

        with CaptureQueriesContext(connection) as queries:
            workspace = ReviewWorkspace(plan, self.viewer)
            tuple(workspace.traces)

        cable_queries = [query["sql"] for query in queries if 'FROM "dcim_cable"' in query["sql"]]
        self.assertEqual(len(cable_queries), 1, cable_queries)

    def test_rendering_under_different_live_permissions_does_not_change_the_accepted_fingerprint(self):
        """Live presentation removes text without mutating any accepted decision input."""
        self.connect(self.eth0, self.eth1, label="Fingerprint link")
        plan = self.plan(patched_path(), actor=self.viewer)
        accepted = plan.fingerprint
        without_sources = plan.to_dict()

        def remove_sources(value):
            if isinstance(value, dict):
                value.pop("disclosure_source", None)
                for child in value.values():
                    remove_sources(child)
            elif isinstance(value, list):
                for child in value:
                    remove_sources(child)

        remove_sources(without_sources)
        ReviewWorkspace(plan, self.viewer)
        self.revoke_cable_view()

        ReviewWorkspace(plan, self.viewer)

        self.assertEqual(plan.fingerprint, accepted)
        self.assertEqual(ImportPlan.from_dict(without_sources).fingerprint, accepted)

    def test_a_visible_cable_claim_without_an_authorizable_source_redacts_on_render(self):
        """The workspace does not trust cached Cable text whose source shape is invalid."""
        logical = self.connect(self.eth0, self.eth1, label="Malformed source cable")
        self.open_workspace(patched_path())
        original = self.client.session[PREVIEW_PLAN_SESSION_KEY]
        invalid_sources = (None, "1", {"kind": "unknown.row", "pk": logical.pk})

        for source in invalid_sources:
            with self.subTest(source=source):
                data = copy.deepcopy(original)
                logical_display = data["units"][0]["display"]["trace"]["logical_cable"]
                logical_display["disclosure_source"] = source
                session = self.client.session
                session[PREVIEW_PLAN_SESSION_KEY] = data
                session.save()

                response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

                self.assertNotContains(response, "Malformed source cable")

        data = copy.deepcopy(original)
        data["units"][0]["display"]["trace"]["logical_cable"].pop("disclosure_source")
        session = self.client.session
        session[PREVIEW_PLAN_SESSION_KEY] = data
        session.save()
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertNotContains(response, "Malformed source cable")

    def test_the_queued_execution_plan_does_not_store_deleted_cable_metadata(self):
        """Native Job readers receive no metadata that only the deleted Cable could authorize."""
        from core.models import Job

        label = 'Hidden "job\\\tcable'
        description = 'Hidden "job\\\tdetail'
        tag_name = 'Hidden "job\\\ttag'
        logical = self.connect(self.eth0, self.eth1, label=label, description=description)
        tag = Tag.objects.create(name=tag_name, slug="hidden-job-tag")
        logical.tags.add(tag)
        logical.refresh_from_db()
        tag.refresh_from_db()
        self.assertEqual(logical.label, label)
        self.assertEqual(logical.description, description)
        self.assertEqual(tag.name, tag_name)
        opened = self.open_workspace(patched_path())
        trace = opened.context["selected_trace"]

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {
                "identity": trace.identity,
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            },
        )

        self.assertEqual(response.status_code, 302)
        stored = Job.objects.latest("pk").data["accepted_plan"]
        assert_absent_from(self, stored, label)
        assert_absent_from(self, stored, description)
        assert_absent_from(self, stored, tag_name)


class TraceWorkspacePolicyDisclosureTest(CableTopologyMixin, TransactionTestCase):
    """Recheck cached policy display values against the viewer of each render."""

    def setUp(self):
        self.build_topology()
        self.viewer = user_with_object_permission(
            "trace-policy-viewer",
            [
                (ImportProfile, ("view", "change"), {}),
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("view", "add", "delete"), {}),
                (CableClassMapping, ("view", "add", "change", "delete"), {}),
                (CableSegmentOverride, ("view", "add", "change", "delete"), {}),
            ],
        )
        self.client.force_login(self.viewer)

    def open_workspace(self, *blocks):
        """Upload the patched path through the real setup flow."""
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks or (patched_path(),)))
        upload.name = "traces.xlsx"
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(setup.status_code, 200)
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def revoke_mapping_view(self):
        """Keep mapping writes permitted while removing the viewer's read grant."""
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.get(name__startswith="trace-policy-viewer CableClassMapping ")
        permission.actions = ["add", "change", "delete"]
        permission.save(update_fields=("actions",))

    def grant_mapping_view(self):
        """Restore mapping read access without changing the accepted preview."""
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.get(name__startswith="trace-policy-viewer CableClassMapping ")
        permission.actions = ["view", "add", "change", "delete"]
        permission.save(update_fields=("actions",))

    def set_mapping_actions(self, actions):
        """Replace the live CableClass Mapping actions for this viewer."""
        from users.models import ObjectPermission

        from netbox_data_import.object_permissions import clear_user_permission_caches

        permission = ObjectPermission.objects.get(name__startswith="trace-policy-viewer CableClassMapping ")
        permission.actions = actions
        permission.save(update_fields=("actions",))
        clear_user_permission_caches(self.viewer)

    def revoke_override_view(self):
        """Keep override writes permitted while removing the viewer's read grant."""
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.get(name__startswith="trace-policy-viewer CableSegmentOverride ")
        permission.actions = ["add", "change", "delete"]
        permission.save(update_fields=("actions",))

    def set_override_actions(self, actions):
        """Replace the live Cable Segment Override actions for this viewer."""
        from users.models import ObjectPermission

        from netbox_data_import.object_permissions import clear_user_permission_caches

        permission = ObjectPermission.objects.get(name__startswith="trace-policy-viewer CableSegmentOverride ")
        permission.actions = actions
        permission.save(update_fields=("actions",))
        clear_user_permission_caches(self.viewer)

    def test_revoking_mapping_view_redacts_and_disables_all_policy_surfaces(self):
        """A cached decision cannot expose values or accept a blind overwrite after revocation."""
        visible = self.open_workspace()
        self.assertContains(visible, cable_type_label("cat6"))
        self.revoke_mapping_view()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        hidden = "a policy you cannot view"
        refusal = "You cannot change a policy you cannot view."
        self.assertContains(cached, hidden)
        policy = next(item for item in cached.context["cable_policy_forms"] if item["cable_class"] == "Patch")
        segment = cached.context["segment_policy_forms"][0]
        self.assertEqual((policy["cable_type"], policy["cable_profile"]), (hidden, hidden))
        self.assertEqual((segment["cable_type"], segment["cable_profile"]), (hidden, hidden))
        self.assertFalse(policy["form"].initial.get("cable_type"))
        self.assertFalse(segment["form"].initial.get("cable_type"))
        self.assertTrue(policy["form"].fields["cable_type"].disabled)
        self.assertTrue(segment["form"].fields["cable_type"].disabled)
        self.assertEqual(policy["reason"], refusal)
        self.assertEqual(segment["reason"], refusal)

        detail = self.client.get(reverse("plugins:netbox_data_import:importprofile", kwargs={"pk": self.profile.pk}))
        self.assertContains(detail, hidden)
        self.assertNotContains(detail, cable_type_label("cat6"))

        mapping = CableClassMapping.objects.get(profile=self.profile, cable_class="Patch")
        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_cable_policy"),
            {
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                "trace": cached.context["selected_trace"].identity,
                "cable_class": "Patch",
                "cable_type": "mmf-om4",
                "cable_profile": "single-1c1p",
            },
            follow=True,
        )
        self.assertContains(refused, refusal)
        mapping.refresh_from_db()
        self.assertEqual(mapping.cable_type, "cat6")

    def test_revoking_override_view_removes_prefill_and_refuses_force_and_clear(self):
        """Both override writes fail closed when the deciding row is no longer viewable."""
        opened = self.open_workspace()
        trace = opened.context["selected_trace"]
        forced = self.client.post(
            reverse("plugins:netbox_data_import:trace_segment_policy"),
            {
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                "trace": trace.identity,
                "segment": 0,
                "cable_type": "mmf-om4",
                "cable_profile": "single-1c1p",
            },
            follow=True,
        )
        self.assertEqual(forced.status_code, 200)
        override = CableSegmentOverride.objects.get()
        self.revoke_override_view()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        segment = cached.context["segment_policy_forms"][0]
        hidden = "a policy you cannot view"
        refusal = "You cannot change a policy you cannot view."
        self.assertEqual((segment["cable_type"], segment["cable_profile"]), (hidden, hidden))
        self.assertFalse(segment["form"].initial.get("cable_type"))
        self.assertTrue(segment["form"].fields["cable_type"].disabled)
        self.assertEqual(segment["reason"], refusal)

        common = {
            "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
            "trace": cached.context["selected_trace"].identity,
            "segment": 0,
        }
        cleared = self.client.post(
            reverse("plugins:netbox_data_import:trace_segment_policy"), {**common, "clear": "1"}, follow=True
        )
        self.assertContains(cleared, refusal)
        self.assertTrue(CableSegmentOverride.objects.filter(pk=override.pk).exists())
        overwritten = self.client.post(
            reverse("plugins:netbox_data_import:trace_segment_policy"),
            {**common, "cable_type": "cat6", "cable_profile": "single-1c1p"},
            follow=True,
        )
        self.assertContains(overwritten, refusal)
        override.refresh_from_db()
        self.assertEqual(override.cable_type, "mmf-om4")

    def test_a_hidden_mapping_created_after_preview_cannot_populate_or_accept_the_form(self):
        """A live row absent from the cached plan cannot borrow authority from Unresolved text."""
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Patch").delete()
        self.open_workspace()
        self.revoke_mapping_view()
        mapping = CableClassMapping.objects.create(
            profile=self.profile,
            cable_class="Patch",
            cable_type_resolved=True,
            cable_type="mmf-om4",
            cable_profile_resolved=True,
            cable_profile="single-1c1p",
        )

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        policy = next(item for item in cached.context["cable_policy_forms"] if item["cable_class"] == "Patch")
        segment = cached.context["segment_policy_forms"][0]
        refusal = "You cannot change a policy you cannot view."
        self.assertEqual((policy["cable_type"], policy["cable_profile"]), ("a policy you cannot view",) * 2)
        self.assertEqual((segment["cable_type"], segment["cable_profile"]), ("a policy you cannot view",) * 2)
        self.assertFalse(policy["form"].initial.get("cable_type"))
        self.assertFalse(segment["form"].initial.get("cable_type"))
        self.assertTrue(policy["form"].fields["cable_type"].disabled)
        self.assertTrue(segment["form"].fields["cable_type"].disabled)
        self.assertEqual(policy["reason"], refusal)
        self.assertEqual(segment["reason"], refusal)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_cable_policy"),
            {
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                "trace": cached.context["selected_trace"].identity,
                "cable_class": "Patch",
                "cable_type": "cat6",
                "cable_profile": "single-1c1p",
            },
            follow=True,
        )
        self.assertContains(refused, refusal)
        mapping.refresh_from_db()
        self.assertEqual(mapping.cable_type, "mmf-om4")

    def test_a_hidden_override_created_after_preview_cannot_populate_or_accept_the_form(self):
        """A live override absent from the cached plan cannot borrow authority from its mapping."""
        opened = self.open_workspace()
        trace = opened.context["selected_trace"]
        segment = trace.segments[0]
        self.revoke_override_view()
        override = CableSegmentOverride.objects.create(
            profile=self.profile,
            segment_key=segment["segment_key"],
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
            source_trace_identity=trace.trace_identity,
            segment_index=segment["index"],
        )

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        form_row = cached.context["segment_policy_forms"][0]
        refusal = "You cannot change a policy you cannot view."
        self.assertEqual((form_row["cable_type"], form_row["cable_profile"]), ("a policy you cannot view",) * 2)
        self.assertFalse(form_row["form"].initial.get("cable_type"))
        self.assertTrue(form_row["form"].fields["cable_type"].disabled)
        self.assertEqual(form_row["reason"], refusal)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_segment_policy"),
            {
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                "trace": trace.identity,
                "segment": segment["index"],
                "cable_type": "cat6",
                "cable_profile": "single-1c1p",
            },
            follow=True,
        )
        self.assertContains(refused, refusal)
        override.refresh_from_db()
        self.assertEqual(override.cable_type, "mmf-om4")

    def test_a_visible_override_created_after_preview_cannot_bind_the_cached_mapping(self):
        """A new deciding row cannot borrow the cached mapping's disclosure or form values."""
        opened = self.open_workspace()
        trace = opened.context["selected_trace"]
        segment = trace.segments[0]
        CableSegmentOverride.objects.create(
            profile=self.profile,
            segment_key=segment["segment_key"],
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
            source_trace_identity=trace.trace_identity,
            segment_index=segment["index"],
        )

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        form_row = cached.context["segment_policy_forms"][0]
        moved = (
            "This profile's policy changed since this preview was planned. "
            "Re-read from NetBox, then make the decision again."
        )
        self.assertFalse(form_row["form"].initial.get("cable_type"))
        self.assertTrue(form_row["form"].fields["cable_type"].disabled)
        self.assertEqual(form_row["reason"], moved)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_segment_policy"),
            {
                "preview_revision": self.client.session[PREVIEW_REVISION_SESSION_KEY],
                "trace": trace.identity,
                "segment": segment["index"],
                "cable_type": "cat6",
                "cable_profile": "single-1c1p",
            },
            follow=True,
        )
        from django.contrib.messages import get_messages

        self.assertIn(moved, [str(message) for message in get_messages(refused.wsgi_request)])

    def test_a_visible_policy_claim_without_an_authorizable_source_redacts_on_render(self):
        """The workspace does not trust cached policy text whose source shape is invalid."""
        self.open_workspace()
        original = self.client.session[PREVIEW_PLAN_SESSION_KEY]
        invalid_sources = (None, "1", {"kind": "unknown.row", "pk": 1})

        for source in invalid_sources:
            with self.subTest(source=source):
                data = copy.deepcopy(original)
                policy = data["units"][0]["display"]["trace"]["cable_policies"][0]
                policy["disclosure_source"] = source
                session = self.client.session
                session[PREVIEW_PLAN_SESSION_KEY] = data
                session.save()

                response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

                policy_form = response.context["cable_policy_forms"][0]
                self.assertEqual(policy_form["cable_type"], "a policy you cannot view")
                self.assertTrue(policy_form["form"].fields["cable_type"].disabled)
                self.assertEqual(policy_form["reason"], "You cannot change a policy you cannot view.")

        data = copy.deepcopy(original)
        data["units"][0]["display"]["trace"]["cable_policies"][0].pop("disclosure_source")
        session = self.client.session
        session[PREVIEW_PLAN_SESSION_KEY] = data
        session.save()
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        policy_form = response.context["cable_policy_forms"][0]
        self.assertEqual(policy_form["cable_type"], "a policy you cannot view")
        self.assertTrue(policy_form["form"].fields["cable_type"].disabled)
        self.assertEqual(policy_form["reason"], "You cannot change a policy you cannot view.")

    def test_a_cable_media_observation_cannot_use_a_policy_row_as_its_source(self):
        """A viewable row of the wrong kind cannot authorize a retained Cable's cached type."""
        self.connect(self.panel_1_rear, self.panel_2_rear, type="mmf-om4")
        self.open_workspace()
        data = self.client.session[PREVIEW_PLAN_SESSION_KEY]
        diagnostic = next(
            item for item in data["units"][0]["diagnostics"] if item["code"] == "cable.media_family_mismatch"
        )
        cable_segment = next(item for item in diagnostic["display"]["segments"] if item["origin"] == "cable")
        mapping = CableClassMapping.objects.get(profile=self.profile, cable_class="Trunk")
        cable_segment["disclosure_source"] = {
            "kind": "netbox_data_import.cableclassmapping",
            "pk": mapping.pk,
        }
        session = self.client.session
        session[PREVIEW_PLAN_SESSION_KEY] = data
        session.save()

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        finding = next(
            item
            for item in response.context["selected_trace"].findings
            if item["code"] == "cable.media_family_mismatch"
        )
        self.assertIn("a Cable you cannot view", finding["message"])
        self.assertNotIn(cable_type_label("mmf-om4"), finding["message"])

    def test_policy_view_changes_only_presentation_not_the_plan_decision(self):
        """The unrestricted policy decision and fingerprint do not depend on policy view access."""
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(cable_type="mmf-om4")
        visible = self.plan(patched_path(), actor=self.viewer)
        visible_unit = visible.units[0]
        self.revoke_mapping_view()
        from netbox_data_import.object_permissions import clear_user_permission_caches

        clear_user_permission_caches(self.viewer)
        hidden = self.plan(patched_path(), actor=self.viewer)
        hidden_unit = hidden.units[0]

        self.assertEqual(hidden_unit.disposition, visible_unit.disposition)
        self.assertEqual(hidden_unit.changes, visible_unit.changes)
        self.assertEqual(
            [item.evidence for item in hidden_unit.diagnostics],
            [item.evidence for item in visible_unit.diagnostics],
        )
        self.assertEqual(hidden.fingerprint, visible.fingerprint)
        self.assertEqual(hidden_unit.display["trace"]["segments"][0]["cable_type"], "a policy you cannot view")

    def test_a_later_mapping_grant_does_not_reveal_a_planner_redaction(self):
        """A hidden plan stores no row identity that a later mapping grant can reveal."""
        self.revoke_mapping_view()
        hidden = self.open_workspace()
        self.assertContains(hidden, "a policy you cannot view")
        self.grant_mapping_view()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        policy = next(item for item in cached.context["cable_policy_forms"] if item["cable_class"] == "Patch")
        segment = cached.context["segment_policy_forms"][0]
        self.assertEqual(policy["cable_type"], "a policy you cannot view")
        self.assertEqual(segment["cable_type"], "a policy you cannot view")

    def test_a_deleted_mapping_redacts_the_cached_decision(self):
        """A deleted policy row cannot authorize cached display text."""
        self.open_workspace()
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Patch").delete()

        cached = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        policy = next(item for item in cached.context["cable_policy_forms"] if item["cable_class"] == "Patch")
        segment = cached.context["segment_policy_forms"][0]
        self.assertEqual(policy["cable_type"], "a policy you cannot view")
        self.assertEqual(segment["cable_type"], "a policy you cannot view")

    def test_hidden_policy_media_wording_uses_the_shared_redaction(self):
        """Media wording identifies a hidden policy without exposing its stored values."""
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(cable_type="mmf-om4")
        self.revoke_mapping_view()

        response = self.open_workspace()

        finding = next(
            item
            for item in response.context["selected_trace"].findings
            if item["code"] == "cable.media_family_mismatch"
        )
        self.assertIn("uses a policy you cannot view", finding["message"])
        self.assertNotIn(cable_type_label("mmf-om4"), finding["message"])
        plan = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        diagnostic = next(item for item in plan.units[0].diagnostics if item.code == "cable.media_family_mismatch")
        self.assertEqual({segment["family"] for segment in diagnostic.evidence["segments"]}, {"cat3", "mmf"})

    def test_mapping_child_views_require_view_access_in_addition_to_write_access(self):
        """Change and delete grants cannot expose a policy row through their forms."""
        mapping = CableClassMapping.objects.get(profile=self.profile, cable_class="Patch")
        self.revoke_mapping_view()

        edit = self.client.get(reverse("plugins:netbox_data_import:cableclassmapping_edit", kwargs={"pk": mapping.pk}))
        delete = self.client.get(
            reverse("plugins:netbox_data_import:cableclassmapping_delete", kwargs={"pk": mapping.pk})
        )

        self.assertEqual(edit.status_code, 403)
        self.assertEqual(delete.status_code, 403)

    def test_mapping_controls_distinguish_add_from_change_permission(self):
        """Each policy row offers Save only for the write action that row needs."""
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").delete()
        self.open_workspace()
        refusal = "You do not have permission to save this CableClass policy."

        self.set_mapping_actions(["view", "change"])
        change_only = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        existing = next(item for item in change_only.context["cable_policy_forms"] if item["cable_class"] == "Patch")
        missing = next(item for item in change_only.context["cable_policy_forms"] if item["cable_class"] == "Trunk")
        self.assertFalse(existing["form"].fields["cable_type"].disabled)
        self.assertEqual(existing["reason"], "")
        self.assertTrue(missing["form"].fields["cable_type"].disabled)
        self.assertEqual(missing["reason"], refusal)

        self.set_mapping_actions(["view", "add"])
        add_only = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        existing = next(item for item in add_only.context["cable_policy_forms"] if item["cable_class"] == "Patch")
        missing = next(item for item in add_only.context["cable_policy_forms"] if item["cable_class"] == "Trunk")
        self.assertTrue(existing["form"].fields["cable_type"].disabled)
        self.assertEqual(existing["reason"], refusal)
        self.assertFalse(missing["form"].fields["cable_type"].disabled)
        self.assertEqual(missing["reason"], "")

    def test_a_new_segment_override_requires_add_permission(self):
        """The workspace does not offer Force when its child row cannot be created."""
        self.set_override_actions(["view"])

        response = self.open_workspace()

        segment = response.context["segment_policy_forms"][0]
        self.assertTrue(segment["form"].fields["cable_type"].disabled)
        self.assertEqual(segment["reason"], "You do not have permission to force this segment policy.")

    def test_existing_override_controls_distinguish_change_from_delete_permission(self):
        """Force and Clear follow their independent child-row permissions."""
        identity = self.identity_of(patched_path())
        self.force(
            self.eth0,
            self.panel_1_fronts[0],
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
            trace_identity=identity,
        )
        self.open_workspace()

        self.set_override_actions(["view", "change"])
        change_only = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        segment = change_only.context["segment_policy_forms"][0]
        self.assertTrue(segment["overridden"])
        self.assertFalse(segment["form"].fields["cable_type"].disabled)
        self.assertEqual(segment["reason"], "")
        self.assertEqual(segment["clear_reason"], "You do not have permission to clear this segment policy.")
        self.assertContains(change_only, "data-segment-policy-clear disabled")

        self.set_override_actions(["view", "delete"])
        delete_only = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        segment = delete_only.context["segment_policy_forms"][0]
        self.assertTrue(segment["form"].fields["cable_type"].disabled)
        self.assertEqual(segment["reason"], "You do not have permission to force this segment policy.")
        self.assertEqual(segment["clear_reason"], "")
        self.assertNotContains(delete_only, "data-segment-policy-clear disabled")


class TraceWorkspaceCablePolicyTest(CableTopologyMixin, TransactionTestCase):
    """Set the Cable policy for an unmapped CableClass without leaving the workspace."""

    def setUp(self):
        self.build_topology()
        self.client.force_login(self.actor)

    def unmapped_path(self):
        """Return a one-segment path whose CableClass the profile does not map."""
        from netbox_data_import.tests.test_cable_module import DEVICE_A, DEVICE_B

        return (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (trace_segment(DEVICE_A, "Fiber Cable", DEVICE_B),),
        )

    def open_workspace(self, *blocks):
        """Upload the given path blocks and return the rendered workspace response."""
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(setup.status_code, 200)
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def save_policy(self, **data):
        """Post one CableClass policy decision through the workspace endpoint."""
        data.setdefault("preview_revision", self.client.session[PREVIEW_REVISION_SESSION_KEY])
        return self.client.post(reverse("plugins:netbox_data_import:trace_cable_policy"), data, follow=True)

    def test_the_workspace_maps_an_unmapped_cableclass_and_the_import_writes_it(self):
        """The operator unblocks the trace from the workspace, and execution writes those values."""
        import uuid

        from dcim.models import Cable

        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.models import CableClassMapping, SourceDocument

        blocked = self.open_workspace(self.unmapped_path())
        self.assertEqual(blocked.context["traces"][0].disposition, Disposition.BLOCKED)
        self.assertEqual(
            [finding["code"] for finding in blocked.context["traces"][0].findings],
            ["cable.cableclass_unmapped"],
        )
        before = self.client.session[PREVIEW_REVISION_SESSION_KEY]

        saved = self.save_policy(
            trace=blocked.context["traces"][0].identity,
            cable_class="Fiber Cable",
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertEqual(saved.status_code, 200)
        row = CableClassMapping.objects.get(profile=self.profile, cable_class="Fiber Cable")
        self.assertEqual((row.cable_type, row.cable_profile), ("mmf-om4", "single-1c1p"))
        self.assertTrue(row.cable_type_resolved and row.cable_profile_resolved)
        self.assertNotEqual(self.client.session[PREVIEW_REVISION_SESSION_KEY], before)

        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        trace = workspace.context["traces"][0]
        self.assertEqual(trace.disposition, Disposition.ACTIONABLE)

        document = SourceDocument.objects.get(profile=self.profile)
        plan = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        execution = ImportEngine.execute(
            self.profile,
            document,
            plan.to_dict(),
            [unit.identity for unit in plan.units if unit.disposition == Disposition.ACTIONABLE],
            str(uuid.uuid4()),
            self.actor,
        )

        self.assertEqual(execution.outcome, "succeeded")
        written = Cable.objects.get()
        self.assertEqual((written.type, written.profile), ("mmf-om4", "single-1c1p"))

    def test_a_cableclass_this_preview_never_asked_about_is_refused(self):
        """A review command answers a question the preview asked, never one the caller invented."""
        from netbox_data_import.models import CableClassMapping

        trace = self.open_workspace(self.unmapped_path()).context["traces"][0]

        refused = self.save_policy(
            trace=trace.identity,
            cable_class="Invented Class",
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertContains(refused, "This preview states no segment with that CableClass.")
        self.assertFalse(CableClassMapping.objects.filter(cable_class="Invented Class").exists())

    def test_a_policy_that_moved_under_this_preview_refuses_the_save(self):
        """Two operators on one profile: the revision is per session, the fingerprint is not."""
        from netbox_data_import.models import CableClassMapping

        trace = self.open_workspace(self.unmapped_path()).context["traces"][0]
        CableClassMapping.objects.create(profile=self.profile, cable_class="Other Class")

        refused = self.save_policy(
            trace=trace.identity,
            cable_class="Fiber Cable",
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertContains(refused, "policy changed since this preview was planned")
        self.assertFalse(CableClassMapping.objects.filter(cable_class="Fiber Cable").exists())

    def test_a_stale_preview_revision_writes_nothing(self):
        """The decision and its replan commit together, so a stale command must not write."""
        from netbox_data_import.models import CableClassMapping

        trace = self.open_workspace(self.unmapped_path()).context["traces"][0]

        refused = self.save_policy(
            trace=trace.identity,
            cable_class="Fiber Cable",
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
            preview_revision="obsolete",
        )

        self.assertEqual(refused.status_code, 200)
        self.assertFalse(CableClassMapping.objects.filter(cable_class="Fiber Cable").exists())


class TraceWorkspaceSegmentOverrideTest(CableTopologyMixin, TransactionTestCase):
    """Force one segment of a trace to its own Cable policy, from the trace under review."""

    def setUp(self):
        self.build_topology()
        self.client.force_login(self.actor)

    def open_workspace(self, *blocks):
        """Upload the given path blocks and return the rendered workspace response."""
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        setup = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(setup.status_code, 200)
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

    def force_segment(self, **data):
        """Post one segment override through the workspace endpoint."""
        data.setdefault("preview_revision", self.client.session[PREVIEW_REVISION_SESSION_KEY])
        return self.client.post(reverse("plugins:netbox_data_import:trace_segment_policy"), data, follow=True)

    def execute_selected(self):
        """Run the real execution over every actionable unit of the reviewed plan."""
        import uuid

        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.models import SourceDocument

        document = SourceDocument.objects.get(profile=self.profile)
        plan = ImportPlan.from_dict(self.client.session[PREVIEW_PLAN_SESSION_KEY])
        return ImportEngine.execute(
            self.profile,
            document,
            plan.to_dict(),
            [unit.identity for unit in plan.units if unit.disposition == Disposition.ACTIONABLE],
            str(uuid.uuid4()),
            self.actor,
        )

    def test_one_cableclass_writes_two_media_once_a_segment_is_forced(self):
        """The reported case: one label names multimode on this path and something else elsewhere."""
        opened = self.open_workspace(patched_path())
        trace = opened.context["selected_trace"]
        self.assertEqual([segment["policy"]["cable_type"] for segment in trace.segments], ["cat6"] * 3)

        forced = self.force_segment(
            trace=trace.identity,
            segment=0,
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertEqual(forced.status_code, 200)
        segments = forced.context["selected_trace"].segments
        self.assertEqual([segment["policy"]["cable_type"] for segment in segments], ["mmf-om4", "cat6", "cat6"])
        self.assertEqual([segment["overridden"] for segment in segments], [True, False, False])
        # The panel names the policy the way the running instance does, never a value of our own.
        self.assertEqual(segments[0]["cable_type"], cable_type_label("mmf-om4"))
        self.assertEqual(forced.context["selected_trace"].disposition, Disposition.ACTIONABLE)

        self.assertEqual(self.execute_selected().outcome, "succeeded")
        self.assertEqual(cables_on(self.eth0, self.panel_1_fronts[0]).get().type, "mmf-om4")
        self.assertEqual(cables_on(self.panel_2_fronts[0], self.eth1).get().type, "cat6")

    def test_clearing_the_override_returns_the_segment_to_its_cableclass_policy(self):
        """The override is a decision, so the operator can take it back without editing the profile."""
        from netbox_data_import.models import CableSegmentOverride

        trace = self.open_workspace(patched_path()).context["selected_trace"]
        self.force_segment(trace=trace.identity, segment=0, cable_type="mmf-om4", cable_profile="single-1c1p")

        cleared = self.force_segment(trace=trace.identity, segment=0, clear="1")

        segments = cleared.context["selected_trace"].segments
        self.assertEqual([segment["policy"]["cable_type"] for segment in segments], ["cat6"] * 3)
        self.assertEqual([segment["overridden"] for segment in segments], [False, False, False])
        self.assertFalse(CableSegmentOverride.objects.exists())

    def test_a_retained_segment_can_clear_the_override_that_created_its_cable(self):
        """A completed sync must not strand the decision that selected the Cable policy."""
        from netbox_data_import.models import CableSegmentOverride

        trace = self.open_workspace(patched_path()).context["selected_trace"]
        self.force_segment(trace=trace.identity, segment=0, cable_type="mmf-om4", cable_profile="single-1c1p")
        self.assertEqual(self.execute_selected().outcome, "succeeded")

        retained = self.open_workspace(patched_path())
        trace = retained.context["selected_trace"]
        self.assertTrue(trace.segments[0]["retained"])
        self.assertTrue(trace.segments[0]["overridden"])
        self.assertNotContains(retained, "data-segment-policy-clear disabled")

        cleared = self.force_segment(trace=trace.identity, segment=0, clear="1")

        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(CableSegmentOverride.objects.exists())
        self.assertEqual(cables_on(self.eth0, self.panel_1_fronts[0]).get().type, "mmf-om4")

    def test_a_retained_segment_says_the_override_cannot_change_its_cable(self):
        """An override decides what the import writes, and this segment is one the import keeps."""
        from netbox_data_import.models import CableSegmentOverride

        self.connect(self.eth0, self.panel_1_fronts[0])

        opened = self.open_workspace(patched_path())
        trace = opened.context["selected_trace"]
        self.assertTrue(trace.segments[0]["retained"])
        self.assertContains(opened, "Correct that Cable in NetBox, then re-read.")

        refused = self.force_segment(
            trace=trace.identity,
            segment=0,
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertContains(refused, "Correct that Cable in NetBox, then re-read.")
        self.assertFalse(CableSegmentOverride.objects.exists())

    def test_a_segment_this_preview_never_stated_is_refused(self):
        """A review command answers a question the preview asked, never one the caller invented."""
        from netbox_data_import.models import CableSegmentOverride

        trace = self.open_workspace(patched_path()).context["selected_trace"]

        refused = self.force_segment(
            trace=trace.identity,
            segment=9,
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertContains(refused, "This preview resolved no segment there.")
        self.assertFalse(CableSegmentOverride.objects.exists())

    def test_an_incomplete_override_is_refused_with_the_form_message(self):
        """An override decides both dimensions, so a half-made decision cannot be stored."""
        from netbox_data_import.models import CableSegmentOverride

        trace = self.open_workspace(patched_path()).context["selected_trace"]

        refused = self.force_segment(trace=trace.identity, segment=0, cable_type="mmf-om4", cable_profile="")

        self.assertContains(refused, "This field is required.")
        self.assertFalse(CableSegmentOverride.objects.exists())

    def test_the_page_says_the_run_states_two_media_and_who_can_correct_it(self):
        """A warning the operator cannot see is the same as no warning at all."""
        trace = self.open_workspace(patched_path()).context["selected_trace"]

        forced = self.force_segment(
            trace=trace.identity,
            segment=0,
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertContains(forced, "data-finding-severity")
        self.assertContains(forced, "Force the segment that states the wrong medium")
        # A warning decides nothing, so the trace stays synchronizable.
        self.assertEqual(forced.context["selected_trace"].disposition, Disposition.ACTIONABLE)

    def test_a_decision_made_against_an_older_policy_is_refused(self):
        """A session revision cannot see another operator's policy edit, so the lock has to."""
        from netbox_data_import.models import CableClassMapping, CableSegmentOverride

        trace = self.open_workspace(patched_path()).context["selected_trace"]
        # What a second operator's save would leave behind between the read and this command.
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(cable_type="mmf-om4")

        refused = self.force_segment(
            trace=trace.identity,
            segment=0,
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
        )

        self.assertContains(refused, "policy changed since this preview was planned")
        self.assertFalse(CableSegmentOverride.objects.exists())

    def test_schema_recovery_stands_aside_when_the_profile_is_gone(self):
        """The recovery reads a Source Document its profile no longer owns, so it cannot assume one."""
        self.open_workspace(patched_path())
        session = self.client.session
        session[PREVIEW_PLAN_SESSION_KEY] = {**session[PREVIEW_PLAN_SESSION_KEY], "schema_version": 3}
        session.save()
        self.profile.delete()

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No import preview in progress")

    def test_a_sync_that_starts_while_the_recovery_replans_leaves_the_stale_plan(self):
        """The guard the recovery reads and the guard the write takes are two moments."""
        import uuid

        from core.choices import JobStatusChoices
        from core.models import Job
        from django.db import connection

        from netbox_data_import.jobs import ImportJobRunner

        self.open_workspace(patched_path())
        context = self.client.session["import_context"]
        session = self.client.session
        stale = {**session[PREVIEW_PLAN_SESSION_KEY], "schema_version": 1}
        session[PREVIEW_PLAN_SESSION_KEY] = stale
        session.save()
        queued: list[str] = []

        def queue_the_sync_after_the_first_guard(execute, sql, params, many, context_):
            result = execute(sql, params, many, context_)
            if not queued and 'FROM "core_job"' in sql:
                queued.append(sql)
                Job.objects.create(
                    name=ImportJobRunner.name,
                    user=self.actor,
                    job_id=uuid.uuid4(),
                    status=JobStatusChoices.STATUS_PENDING,
                    data={
                        "job_type": ImportJobRunner.job_type,
                        "keeps_preview": True,
                        "profile_id": self.profile.pk,
                        "source_document_id": context["source_document_id"],
                    },
                )
            return result

        with connection.execute_wrapper(queue_the_sync_after_the_first_guard):
            response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), follow=True)

        self.assertTrue(queued, "the recovery never read the retained sync guard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session[PREVIEW_PLAN_SESSION_KEY], stale)

    def test_a_cached_plan_this_release_cannot_read_is_rebuilt_from_the_stored_source(self):
        """A plan schema change must not send an operator mid-review back to setup."""
        opened = self.open_workspace(patched_path())
        before = self.client.session[PREVIEW_REVISION_SESSION_KEY]
        session = self.client.session
        session[PREVIEW_PLAN_SESSION_KEY] = {**session[PREVIEW_PLAN_SESSION_KEY], "schema_version": 3}
        session.save()

        reopened = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(
            [trace.identity for trace in reopened.context["traces"]],
            [trace.identity for trace in opened.context["traces"]],
        )
        self.assertNotEqual(self.client.session[PREVIEW_REVISION_SESSION_KEY], before)

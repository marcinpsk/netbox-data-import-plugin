# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Trace Review Workspace: its summary strip, its trace list, and its per-trace actions."""

import re
from io import BytesIO

from dcim.models import Cable, Interface
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.cable_target import ELIGIBLE_TERMINATION_LIMIT
from netbox_data_import.field_keys import termination_field_key
from netbox_data_import.models import ImportProfile, TerminationResolution
from netbox_data_import.plan import Disposition, ImportPlan, PlannedChange, SynchronizationUnit
from netbox_data_import.preview_row_actions import PREVIEW_DIRTY_SESSION_KEY
from netbox_data_import.review_workspace import ReviewWorkspace
from netbox_data_import.tests.test_cable_module import (
    CableTopologyMixin,
    direct_path,
    patched_path,
)
from netbox_data_import.tests.helpers import (
    competing_write_during,
    trace_endpoint_line,
    trace_termination,
    trace_workbook_bytes,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin


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
        workspace = ReviewWorkspace(self.plan(patched_path()))

        self.assertTrue(workspace.has_traces)
        self.assertEqual(len(workspace.traces), 1)
        self.assertFalse(ReviewWorkspace(ImportPlan(units=())).has_traces)

    def test_the_workspace_builds_its_trace_entries_once_per_instance(self):
        """One page reads `traces` and `trace_summary`, and each build reserializes every change."""
        workspace = ReviewWorkspace(self.plan(patched_path()))

        first = workspace.traces
        workspace.trace_summary

        self.assertIs(workspace.traces, first)

    def test_a_units_copy_builds_its_own_trace_entries(self):
        """`with_units` bypasses `__init__`, so the copy must carry its own cache, not share one."""
        workspace = ReviewWorkspace(self.plan(patched_path()))
        original = workspace.traces

        copy = workspace.with_units(workspace.units)

        self.assertIsNot(copy, workspace)
        self.assertIsNot(copy.traces, original)
        self.assertEqual([trace.identity for trace in copy.traces], [trace.identity for trace in original])

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

    def test_the_preview_offers_the_workspace_for_a_trace_profile(self):
        """The workspace has to be reachable, and only from a preview that planned traces."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(patched_path(),)))
        upload.name = "traces.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )

        self.assertTrue(response.context["trace_workspace_available"])
        self.assertContains(response, reverse("plugins:netbox_data_import:trace_workspace"))

    def test_the_page_lists_every_trace_with_its_panels(self):
        """One page per preview: the strip, the list, and the panels of the selected trace."""
        response = self.open_workspace(patched_path())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["traces"]), 1)
        self.assertEqual(response.context["summary"]["traces"], 1)
        self.assertContains(response, "DEV-A eth0 to DEV-B eth1")
        self.assertContains(response, "reuse existing", count=0, status_code=200)
        self.assertContains(response, "automatically resolved")

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

    def test_the_summary_states_the_saved_decisions_and_the_preview_state(self):
        """Section 10.2 names both, and neither can be read off the plan alone."""
        response = self.open_workspace(patched_path())

        self.assertEqual(response.context["summary"]["saved_decisions"], 0)
        self.assertEqual(response.context["summary"]["preview_state"], "current")
        self.assertContains(response, "current")

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
        self.assertIn("matching Devices", blocked["reason"])
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

    def test_sync_refuses_topology_drift_after_the_workspace_was_rendered(self):
        """A live topology change requires another review before a trace can be queued."""
        from core.models import Job

        response = self.open_workspace(patched_path())
        chosen = response.context["traces"][0]
        self.connect(self.panel_1_rear, self.panel_2_rear)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
            follow=True,
        )

        self.assertFalse(Job.objects.filter(data__job_type="netbox_data_import.import").exists())
        self.assertRedirects(refused, reverse("plugins:netbox_data_import:trace_workspace"))
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
            {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
            follow=True,
        )

        self.assertFalse(Job.objects.filter(data__job_type="netbox_data_import.import").exists())
        self.assertRedirects(response, reverse("plugins:netbox_data_import:import_setup"))
        self.assertContains(response, "The saved import target is no longer available.")
        self.assertFalse(self.client.session["import_preview_pending"])

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

    def queue_one_sync(self):
        """Synchronize the first trace and return the queued Job, leaving it unstarted."""
        from core.models import Job

        response = self.open_workspace(patched_path())
        chosen = response.context["traces"][0]
        self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
        )
        return Job.objects.get(data__job_type="netbox_data_import.import")

    def test_a_re_read_is_refused_while_the_queued_synchronization_still_runs(self):
        """The queued job has not written yet, so a re-read would adopt the state it is about to replace."""
        self.queue_one_sync()

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": self.client.session["import_preview_revision"]},
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
            {"preview_revision": self.client.session["import_preview_revision"]},
        )
        workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {
                "identity": workspace.context["traces"][0].identity,
                "preview_revision": self.client.session["import_preview_revision"],
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
            {"preview_revision": self.client.session["import_preview_revision"]},
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
                "preview_revision": self.client.session["import_preview_revision"],
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
                    {"preview_revision": self.client.session["import_preview_revision"]},
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
        current = self.client.session["import_preview_revision"]

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_workspace_reread"),
            {"preview_revision": "stale"},
            follow=True,
        )

        self.assertEqual(self.client.session["import_preview_revision"], current)
        self.assertContains(response, "This preview is no longer the current one.")

    def test_a_sync_is_refused_when_this_release_dropped_the_source_adapter(self):
        """Queueing a plan for an adapter this release does not register writes nothing but a failure."""
        from core.models import Job

        workspace = self.open_workspace(patched_path())
        chosen = workspace.context["traces"][0]
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired-adapter")

        response = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
            follow=True,
        )

        self.assertFalse(Job.objects.filter(data__job_type="netbox_data_import.import").exists())
        self.assertContains(response, "retired-adapter")
        # The preview cannot be planned again in this release, so it is not left to be retried.
        self.assertFalse(self.client.session["import_preview_pending"])

    def test_the_workspace_page_refuses_an_adapter_this_release_dropped(self):
        """Planning raises for an unregistered adapter, so the page has to refuse before it plans."""
        self.open_workspace(patched_path())
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired-adapter")

        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "retired-adapter")


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
                "preview_revision": self.client.session["import_preview_revision"],
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
        holding = threading.Event()

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
                {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
                follow=True,
            )
        finally:
            holder.join(20)

        self.assertContains(response, "A trace synchronization is still running.")
        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), 1)


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
        params.setdefault("preview_revision", self.client.session["import_preview_revision"])
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

    def test_the_picker_clamps_a_limit_below_one(self):
        """The limit is a QuerySet slice stop, so a value under one has to be clamped, not passed on."""
        field_key = self.open_blocked_workspace()
        Interface.objects.create(device=self.device_a, name="eth5", type="1000base-t")

        payload = self.candidates(field_key, limit=-1).json()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["shown"], 1)
        self.assertEqual(payload["total"], 2)

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
                "preview_revision": self.client.session["import_preview_revision"],
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
            {"identity": syncable.identity, "preview_revision": self.client.session["import_preview_revision"]},
        )
        self.assertEqual(Job.objects.filter(data__job_type="netbox_data_import.import").count(), 1)

        refused = self.client.post(
            reverse("plugins:netbox_data_import:trace_resolve_termination"),
            {
                "field_key": field_key,
                "object_type": "dcim.interface",
                "object_id": Interface.objects.get(device__name="SRC-open", name="eth0").pk,
                "preview_revision": self.client.session["import_preview_revision"],
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
                "preview_revision": self.client.session["import_preview_revision"],
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
                "preview_revision": self.client.session["import_preview_revision"],
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
                "preview_revision": self.client.session["import_preview_revision"],
            },
            headers={"accept": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("eligible", response.json()["error"])
        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile).exists())


class TraceSyncSelectionTest(CableTopologyMixin, TestCase):
    """`Sync with dependencies` selects the trace and every unit whose change it needs."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_a_trace_that_depends_on_nothing_selects_itself(self):
        """A self-contained trace needs no other unit, and must not drag one in."""
        workspace = ReviewWorkspace(self.plan(patched_path()))

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
                    display={"trace": {}},
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
                    display={"trace": {}},
                ),
            )
        )

        selection = ReviewWorkspace(plan).sync_selection("cable:trace:first")

        self.assertEqual(sorted(selection), ["cable:trace:first", "cable:trace:second"])

    def test_a_trace_that_is_not_actionable_selects_nothing(self):
        """A blocked trace has no work to select, so the command has nothing to send."""
        Interface.objects.create(device=self.make_device("SRC-Y"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("DST-Y"), name="eth0", type="1000base-t")
        blocked = direct_path(
            from_end=trace_termination("SRC-Y", "", "absent-port", "Port"),
            to_end=trace_termination("DST-Y", "", "eth0", "Port"),
        )
        workspace = ReviewWorkspace(self.plan(blocked))

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
            {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
        )

        job = Job.objects.get(data__job_type="netbox_data_import.import")
        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}),
            fetch_redirect_response=False,
        )
        self.run_rq_jobs()
        self.assertTrue(Cable.objects.filter(terminations__termination_id=self.eth0.pk).exists())
        self.assertFalse(Cable.objects.filter(terminations__termination_id=second.pk).exists())
        self.assertFalse(Cable.objects.filter(terminations__termination_id=other.pk).exists())

    def test_a_second_trace_can_be_synchronized_after_the_first(self):
        """A per-trace command is repeatable, so it must not spend the whole preview on one trace."""
        second = Interface.objects.create(device=self.make_device("DEV-G"), name="eth0", type="1000base-t")
        other = Interface.objects.create(device=self.make_device("DEV-H"), name="eth0", type="1000base-t")
        independent = direct_path(
            from_end=trace_termination("DEV-G", "", "eth0", "Port"),
            to_end=trace_termination("DEV-H", "", "eth0", "Port"),
        )
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(direct_path(), independent)))
        upload.name = "traces.xlsx"
        self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )

        for endpoint in ("DEV-A eth0", "DEV-G eth0"):
            workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
            self.assertEqual(workspace.status_code, 200, "the workspace has to survive a per-trace sync")
            chosen = next(trace for trace in workspace.context["traces"] if trace.endpoints["from"] == endpoint)
            self.client.post(
                reverse("plugins:netbox_data_import:trace_sync"),
                {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
            )
            self.run_rq_jobs()
            self.client.post(
                reverse("plugins:netbox_data_import:trace_workspace_reread"),
                {"preview_revision": self.client.session["import_preview_revision"]},
            )

        self.assertTrue(Cable.objects.filter(terminations__termination_id=self.eth0.pk).exists())
        self.assertTrue(Cable.objects.filter(terminations__termination_id=second.pk).exists())
        self.assertTrue(Cable.objects.filter(terminations__termination_id=other.pk).exists())

    def test_a_replanned_trace_is_executed_again_rather_than_reported_done(self):
        """One trace identity spans two workbooks, so the execution key cannot be the selection alone."""
        from core.models import Job

        from netbox_data_import.models import ExecutionOutcome, ImportExecution

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
                {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
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
        self.assertFalse(
            Cable.objects.filter(terminations__termination_id=self.eth0.pk)
            .filter(terminations__termination_id=self.eth1.pk)
            .exists()
        )
        self.assertTrue(Cable.objects.filter(terminations__termination_id=self.panel_1_rear.pk).exists())


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
                    "preview_revision": self.client.session["import_preview_revision"],
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

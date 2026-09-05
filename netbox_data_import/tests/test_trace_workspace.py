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
from netbox_data_import.models import TerminationResolution
from netbox_data_import.plan import Disposition, ImportPlan, PlannedChange, SynchronizationUnit
from netbox_data_import.review_workspace import ReviewWorkspace
from netbox_data_import.tests.test_cable_module import (
    CableTopologyMixin,
    direct_path,
    patched_path,
)
from netbox_data_import.tests.helpers import trace_termination, trace_workbook_bytes
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
        self.assertContains(response, "disabled")
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
        self.client.force_login(self.actor)
        for blocks in (direct_path(), patched_path()):
            upload = BytesIO(trace_workbook_bytes(path_blocks=(blocks,)))
            upload.name = "traces.xlsx"
            self.client.post(
                reverse("plugins:netbox_data_import:import_setup"),
                {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
                follow=True,
            )
            workspace = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
            chosen = workspace.context["traces"][0]
            self.client.post(
                reverse("plugins:netbox_data_import:trace_sync"),
                {"identity": chosen.identity, "preview_revision": self.client.session["import_preview_revision"]},
            )
            self.run_rq_jobs()

        # The patched path replaces the direct Cable with its three physical segments.
        self.assertFalse(
            Cable.objects.filter(terminations__termination_id=self.eth0.pk)
            .filter(terminations__termination_id=self.eth1.pk)
            .exists()
        )
        self.assertTrue(Cable.objects.filter(terminations__termination_id=self.panel_1_rear.pk).exists())

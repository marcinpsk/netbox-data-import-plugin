# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Sync all actionable traces: one workspace command queues every trace that can sync, and keeps the preview."""

from contextlib import nullcontext
from dataclasses import replace
from io import BytesIO
from itertools import combinations
from random import Random

from core.choices import JobStatusChoices
from core.models import Job
from dcim.models import Cable, Device, FrontPort, Interface, RearPort, Site
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.jobs import ImportJobRunner
from netbox_data_import.models import ExecutionOutcome, ImportExecution, ImportProfile, PreviewState, SourceDocument
from netbox_data_import.plan import Disposition, ImportPlan, PlannedChange, SynchronizationUnit
from netbox_data_import.preview_coordinator import SYNC_FINISHED, SYNC_QUEUED
from netbox_data_import.review_workspace import SYNC_ALL_NOTHING, SYNC_DEPENDENCY_HELD, ReviewWorkspace
from netbox_data_import.tests.helpers import (
    cables_on,
    preview_claim,
    preview_coordinator,
    retired_claim,
    seed_preview,
    trace_endpoint_line,
    trace_segment,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
    user_with_object_permission,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, direct_path

DRIFT = "NetBox has changed. Re-read the preview before synchronizing."
JSON = {"HTTP_ACCEPT": "application/json"}


def _trace_unit(identity, *changes, disposition=Disposition.ACTIONABLE):
    """Return one trace unit as the workspace reads it, carrying the given changes."""
    return SynchronizationUnit(
        identity=identity, disposition=disposition, changes=changes, display={"trace": {"cable_policies": []}}
    )


def _change(identity, *dependencies):
    return PlannedChange(
        identity=identity, target_module="cable", operation="create", payload={}, dependencies=dependencies
    )


HELD = ("cable:trace:waits", "cable:trace:dangling")


def _held_plan():
    """Return a trace waiting on a blocked owner, a trace with a dangling dependency, and a free trace."""
    return ImportPlan(
        units=(
            _trace_unit("cable:trace:waits", _change("cable:create:w", "cable:delete:9")),
            _trace_unit("cable:trace:owner", _change("cable:delete:9"), disposition=Disposition.BLOCKED),
            _trace_unit("cable:trace:dangling", _change("cable:create:d", "cable:delete:404")),
            _trace_unit("cable:trace:free", _change("cable:create:f")),
        )
    )


class _CountingUnits(tuple):
    """A plan's units that count each full read of the plan."""

    iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


class SyncAllSelectionTest(CableTopologyMixin, TestCase):
    """The selection is explicit: each trace that can sync, and every unit its changes wait on."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_it_selects_every_actionable_trace_with_its_dependencies_in_plan_order(self):
        plan = ImportPlan(
            units=(
                _trace_unit("cable:trace:c", _change("cable:create:c", "cable:delete:7")),
                _trace_unit("cable:trace:blocked", disposition=Disposition.BLOCKED),
                _trace_unit("cable:trace:a", _change("cable:delete:7")),
                _trace_unit("cable:trace:invalid", disposition=Disposition.INVALID),
                _trace_unit("cable:trace:b", _change("cable:create:b")),
                _trace_unit("cable:trace:same", disposition=Disposition.NO_OP),
            )
        )

        selection = ReviewWorkspace(plan, self.actor).sync_all

        self.assertEqual(selection.traces, ("cable:trace:c", "cable:trace:a", "cable:trace:b"))
        self.assertEqual(selection.units, ("cable:trace:c", "cable:trace:a", "cable:trace:b"))
        self.assertEqual((selection.blocked, selection.invalid, selection.held_back), (1, 1, 0))
        self.assertEqual(selection.label, "Sync 3 actionable traces")
        self.assertEqual(selection.unsynced_note, "2 traces stay unsynced: 1 blocked, 1 invalid.")

    def test_a_trace_whose_dependency_cannot_sync_is_left_out_and_counted(self):
        """The engine refuses a selection with a unit that is not actionable, so the trace cannot join it."""
        selection = ReviewWorkspace(_held_plan(), self.actor).sync_all

        self.assertEqual(selection.traces, ("cable:trace:free",))
        self.assertEqual(selection.units, ("cable:trace:free",))
        self.assertEqual((selection.blocked, selection.invalid, selection.held_back), (1, 0, 2))
        self.assertEqual(selection.label, "Sync 1 actionable trace")
        self.assertEqual(
            selection.unsynced_note,
            "3 traces stay unsynced: 1 blocked, 2 with a dependency that cannot sync.",
        )

    def test_a_trace_whose_dependency_cannot_sync_shows_its_own_sync_disabled(self):
        """The per-trace command uses the same check as sync all, so it cannot offer what the engine refuses."""
        actions = {trace.identity: trace.actions for trace in ReviewWorkspace(_held_plan(), self.actor).traces}

        for identity in HELD:
            (sync,) = actions[identity]
            self.assertEqual((sync.key, sync.enabled, sync.reason), ("sync", False, SYNC_DEPENDENCY_HELD))
        (free,) = actions["cable:trace:free"]
        self.assertEqual((free.key, free.enabled, free.reason), ("sync", True, ""))

    def test_a_dependency_the_trace_carries_itself_adds_no_other_trace(self):
        """Identical changes are shared (section 4.4), so another owner of a carried change is not a dependency."""
        plan = ImportPlan(
            units=(
                _trace_unit("cable:trace:a", _change("cable:delete:7"), _change("cable:create:a", "cable:delete:7")),
                _trace_unit("cable:trace:b", _change("cable:delete:7"), _change("cable:create:b")),
            )
        )

        self.assertEqual(ReviewWorkspace(plan, self.actor).sync_selection("cable:trace:a"), ("cable:trace:a",))

    def test_a_shared_dependency_is_taken_from_an_owner_that_can_sync(self):
        plan = ImportPlan(
            units=(
                _trace_unit("cable:trace:owner", _change("cable:delete:7")),
                _trace_unit("cable:trace:blocked", _change("cable:delete:7"), disposition=Disposition.BLOCKED),
                _trace_unit("cable:trace:waits", _change("cable:create:w", "cable:delete:7")),
            )
        )
        workspace = ReviewWorkspace(plan, self.actor)

        self.assertEqual(workspace.sync_selection("cable:trace:waits"), ("cable:trace:waits", "cable:trace:owner"))
        (sync,) = next(trace.actions for trace in workspace.traces if trace.identity == "cable:trace:waits")
        self.assertEqual((sync.enabled, sync.reason), (True, ""))
        self.assertEqual(workspace.sync_all.traces, ("cable:trace:owner", "cable:trace:waits"))

    def test_a_shared_dependency_skips_an_actionable_owner_whose_own_dependency_cannot_sync(self):
        plan = ImportPlan(
            units=(
                _trace_unit(
                    "cable:trace:first", _change("cable:delete:7"), _change("cable:create:f", "cable:delete:9")
                ),
                _trace_unit("cable:trace:held", _change("cable:delete:9"), disposition=Disposition.BLOCKED),
                _trace_unit("cable:trace:second", _change("cable:delete:7")),
                _trace_unit("cable:trace:waits", _change("cable:create:w", "cable:delete:7")),
            )
        )
        workspace = ReviewWorkspace(plan, self.actor)

        self.assertEqual(workspace.sync_selection("cable:trace:waits"), ("cable:trace:waits", "cable:trace:second"))
        actions = {trace.identity: trace.actions for trace in workspace.traces}
        (sync,) = actions["cable:trace:waits"]
        self.assertEqual((sync.enabled, sync.reason), (True, ""))
        (first,) = actions["cable:trace:first"]
        self.assertEqual((first.enabled, first.reason), (False, SYNC_DEPENDENCY_HELD))
        self.assertEqual(workspace.sync_all.traces, ("cable:trace:second", "cable:trace:waits"))
        self.assertEqual(workspace.sync_all.held_back, 1)

    def test_a_selection_syncs_whenever_some_selection_with_the_unit_can_sync(self):
        """Compare each closure with every subset of small random plans, so no owner choice hides a valid one."""
        rng = Random(220)
        dispositions = (Disposition.ACTIONABLE,) * 3 + (Disposition.BLOCKED,)
        for _ in range(300):
            pool = [f"cable:create:{n}" for n in range(5)]
            dependencies = {
                change: tuple(rng.sample([*pool[:n], "cable:delete:404"], rng.randint(0, min(2, n + 1))))
                for n, change in enumerate(pool)
            }
            plan = ImportPlan(
                units=tuple(
                    _trace_unit(
                        f"cable:trace:{n}",
                        *(_change(c, *dependencies[c]) for c in rng.sample(pool, rng.randint(1, 2))),
                        disposition=rng.choice(dispositions),
                    )
                    for n in range(5)
                )
            )
            workspace = ReviewWorkspace(plan, self.actor)
            identities = [unit.identity for unit in plan.units]
            for unit in plan.units:
                if unit.disposition != Disposition.ACTIONABLE:
                    continue
                others = [identity for identity in identities if identity != unit.identity]
                possible = any(
                    not workspace.cannot_sync((unit.identity, *subset))
                    for size in range(len(others) + 1)
                    for subset in combinations(others, size)
                )
                with self.subTest(plan=plan.to_dict(), unit=unit.identity):
                    self.assertEqual(not workspace.cannot_sync(workspace.sync_selection(unit.identity)), possible)

    def _plan_reads(self, count):
        """Return how often one workspace load reads a plan of *count* traces that share one dependency."""
        plan = ImportPlan(
            units=(
                _trace_unit("cable:trace:0", _change("cable:delete:0")),
                *(
                    _trace_unit(f"cable:trace:{n}", _change(f"cable:create:{n}", "cable:delete:0"))
                    for n in range(1, count)
                ),
            )
        )
        units = _CountingUnits(plan.units)
        # ImportPlan copies the tuple it receives, so the counter replaces the copy.
        object.__setattr__(plan, "units", units)
        workspace = ReviewWorkspace(plan, self.actor)

        self.assertEqual(len(workspace.sync_all.traces), count)
        self.assertEqual(len(workspace.traces), count)
        return units.iterations

    def test_a_workspace_load_reads_the_plan_a_fixed_number_of_times(self):
        """Each trace's sync check reuses one index of the plan instead of rebuilding it per trace."""
        self.assertEqual(self._plan_reads(30), self._plan_reads(3))

    def test_nothing_to_select_names_no_unsynced_trace(self):
        plan = ImportPlan(units=(_trace_unit("cable:trace:same", disposition=Disposition.NO_OP),))

        selection = ReviewWorkspace(plan, self.actor).sync_all

        self.assertEqual((selection.traces, selection.units), ((), ()))
        self.assertEqual(selection.label, "Sync 0 actionable traces")
        self.assertEqual(selection.unsynced_note, "")
        self.assertFalse(selection.action.enabled)
        self.assertEqual(selection.action.reason, SYNC_ALL_NOTHING)


class _SyncAllMixin:
    """Two actionable traces, one blocked, one invalid and one already in NetBox."""

    def build_mixed(self):
        self.build_topology()
        self.dev_e = Interface.objects.create(device=self.make_device("DEV-E"), name="eth0", type="1000base-t")
        self.dev_f = Interface.objects.create(device=self.make_device("DEV-F"), name="eth0", type="1000base-t")
        self.dev_c = Interface.objects.create(device=self.make_device("DEV-C"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("DEV-D"), name="eth0", type="1000base-t")
        self.dev_g = Interface.objects.create(device=self.make_device("DEV-G"), name="eth0", type="1000base-t")
        self.dev_h = Interface.objects.create(device=self.make_device("DEV-H"), name="eth0", type="1000base-t")
        self.existing = self.connect(self.dev_g, self.dev_h)

    def mixed_blocks(self):
        dev_c, dev_d = trace_termination("DEV-C", "", "eth0", "Port"), trace_termination("DEV-D", "", "eth0", "Port")
        return (
            direct_path(),
            direct_path(trace_termination("DEV-E", "", "eth0", "Port"), trace_termination("DEV-F", "", "eth0", "Port")),
            direct_path(
                from_end=trace_termination("DEV-A", "", "absent-port", "Port"),
                to_end=trace_termination("DEV-B", "", "absent-b", "Port"),
            ),
            (
                trace_endpoint_line(dev_c),
                trace_endpoint_line(dev_d),
                (trace_segment(dev_c, "Patch", trace_termination("PANEL-1", "", "F1", "Bogus Class")),),
            ),
            direct_path(trace_termination("DEV-G", "", "eth0", "Port"), trace_termination("DEV-H", "", "eth0", "Port")),
        )

    def upload(self, *blocks, client=None):
        client = client or self.client
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks or self.mixed_blocks()))
        upload.name = "traces.xlsx"
        response = upload_preview(
            client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload}, follow=True
        )
        self.assertEqual(response.status_code, 200)

    def workspace(self):
        response = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"))
        self.assertEqual(response.status_code, 200)
        return response

    def sync_all(self, client=None, claim=None, **extra):
        client = client or self.client
        # A transactional case commits for real, so only a TestCase needs the after-commit pushes run.
        capture = getattr(self, "captureOnCommitCallbacks", None)
        with capture(execute=True) if capture else nullcontext():
            return client.post(
                reverse("plugins:netbox_data_import:trace_sync_all"),
                {**(claim or preview_claim(client)), "trace": ""},
                **extra,
            )

    def import_jobs(self):
        return Job.objects.filter(data__job_type=ImportJobRunner.job_type)


class HeldTraceSyncTest(_SyncAllMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    """A trace whose dependency cannot sync shows its sync disabled, and its POST queues no Job."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)
        self.upload(direct_path())
        uploaded = preview_coordinator(self.client)
        planned = ImportPlan.from_dict(uploaded.plan)
        # The coordinator refuses a plan made for another preview, so the synthetic plan keeps these inputs.
        held = replace(
            _held_plan(),
            actor=planned.actor,
            source_fingerprint=planned.source_fingerprint,
            profile_fingerprint=planned.profile_fingerprint,
            planning_context=planned.planning_context,
        )
        seed_preview(
            self.client,
            profile=self.profile,
            document=SourceDocument.objects.get(pk=uploaded.source_document_id),
            plan=held,
            context=uploaded.context,
        )

    def test_the_page_disables_the_held_sync_and_states_why(self):
        for identity in HELD:
            page = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), {"trace": identity})

            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.context["selected_trace"].identity, identity)
            self.assertRegex(page.content.decode(), r'data-trace-action="sync"\s+disabled')
            self.assertContains(page, SYNC_DEPENDENCY_HELD)

    def test_the_post_is_refused_before_a_job_exists(self):
        for identity in HELD:
            refused = self.client.post(
                reverse("plugins:netbox_data_import:trace_sync"),
                {**preview_claim(self.client), "identity": identity},
                **JSON,
            )

            self.assertEqual((refused.status_code, refused.json()["error"]), (400, SYNC_DEPENDENCY_HELD))
        self.assertFalse(self.import_jobs().exists())
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)


class SyncAllExecutionTest(_SyncAllMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase):
    """The button queues one Job that writes every actionable trace and nothing else."""

    def setUp(self):
        super().setUp()
        self.build_mixed()
        self.client.force_login(self.actor)

    def test_a_mixed_workbook_syncs_every_actionable_trace_and_keeps_the_workspace(self):
        self.upload()
        page = self.workspace()
        summary = page.context["summary"]
        self.assertEqual(
            (summary["actionable"], summary["blocked"], summary["invalid"], summary["no_change"]), (2, 1, 1, 1)
        )
        self.assertContains(page, "Sync 2 actionable traces")
        self.assertContains(page, "2 traces stay unsynced: 1 blocked, 1 invalid.")
        self.assertTrue(page.context["sync_all"].enabled)
        self.assertContains(page, reverse("plugins:netbox_data_import:trace_sync_all"))

        response = self.sync_all()

        job = self.import_jobs().get()
        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}),
            fetch_redirect_response=False,
        )
        self.assertIs(job.data["keeps_preview"], True)
        pending = preview_coordinator(self.client)
        self.assertEqual((pending.state, pending.job_id), (PreviewState.SYNC_PENDING, job.pk))
        self.assertEqual(self.client.get(response.url).status_code, 200)

        self.run_rq_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.error)
        execution = ImportExecution.objects.get(job=job)
        self.assertEqual(execution.outcome, ExecutionOutcome.SUCCEEDED, execution.failure_detail)
        traces = {trace.endpoints["from"]: trace.identity for trace in page.context["traces"]}
        self.assertEqual(execution.selected_units, [traces["DEV-A eth0"], traces["DEV-E eth0"]])
        self.assertTrue(cables_on(self.eth0, self.eth1).exists())
        self.assertTrue(cables_on(self.dev_e, self.dev_f).exists())
        self.assertFalse(cables_on(self.dev_c).exists())
        self.assertFalse(cables_on(self.panel_1_fronts[0]).exists())
        self.assertEqual(list(cables_on(self.dev_g, self.dev_h)), [self.existing])
        self.assertEqual(Cable.objects.count(), 3)

        finished = self.workspace()
        self.assertFalse(finished.context["sync_all"].enabled)
        self.assertEqual(finished.context["sync_all"].reason, SYNC_FINISHED)
        reread = self.client.post(
            reverse("plugins:netbox_data_import:preview_reread"),
            {**preview_claim(self.client), "next": reverse("plugins:netbox_data_import:trace_workspace")},
        )
        self.assertEqual(reread.status_code, 302)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)

        after = self.workspace()
        self.assertContains(after, "Sync 0 actionable traces")
        self.assertContains(after, SYNC_ALL_NOTHING)
        self.assertContains(after, "2 traces stay unsynced: 1 blocked, 1 invalid.")
        self.assertFalse(after.context["sync_all"].enabled)

    def test_nothing_actionable_renders_disabled_and_the_post_is_refused(self):
        self.upload(self.mixed_blocks()[2], self.mixed_blocks()[4])
        page = self.workspace()
        self.assertContains(page, "Sync 0 actionable traces")
        self.assertContains(page, SYNC_ALL_NOTHING)
        self.assertRegex(page.content.decode(), r"data-trace-sync-all\s+disabled")
        before = preview_claim(self.client)

        refused = self.sync_all(**JSON)

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"], SYNC_ALL_NOTHING)
        self.assertFalse(self.import_jobs().exists())
        self.assertEqual(preview_claim(self.client), before)
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)

    def test_drift_disables_the_button_and_refuses_the_post(self):
        self.upload()
        spare = Interface.objects.create(device=Device.objects.get(name="DEV-E"), name="spare", type="1000base-t")
        self.connect(self.dev_f, spare)

        page = self.workspace()
        self.assertFalse(page.context["sync_all"].enabled)
        self.assertEqual(page.context["sync_all"].reason, DRIFT)
        self.assertContains(page, DRIFT)

        refused = self.sync_all(**JSON)

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"], DRIFT)
        self.assertFalse(self.import_jobs().exists())
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)

    def test_an_active_sync_disables_the_button_and_refuses_another_session(self):
        self.upload()
        page = self.workspace()
        actionable = next(trace for trace in page.context["traces"] if trace.disposition == Disposition.ACTIONABLE)
        queued = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {**preview_claim(self.client), "identity": actionable.identity},
        )
        self.assertEqual(queued.status_code, 302)
        held = preview_coordinator(self.client)

        pending = self.workspace()
        self.assertFalse(pending.context["sync_all"].enabled)
        self.assertEqual(pending.context["sync_all"].reason, SYNC_QUEUED)
        own = self.sync_all(**JSON)
        self.assertEqual(own.status_code, 409)
        self.assertEqual(own.json()["error"], SYNC_QUEUED)

        other = self.client_class()
        other.force_login(self.actor)
        seed_preview(
            other,
            profile=self.profile,
            document=SourceDocument.objects.get(pk=held.source_document_id),
            plan=ImportPlan.from_dict(held.plan),
            context=held.context,
        )
        rival = self.sync_all(client=other, **JSON)

        self.assertEqual(rival.status_code, 409)
        self.assertEqual(rival.json()["error"], SYNC_QUEUED)
        self.assertEqual(self.import_jobs().count(), 1)
        self.assertEqual(preview_coordinator(other).state, PreviewState.READY)

    def test_a_replaced_claim_is_refused(self):
        self.upload()
        stale = retired_claim(self.client)

        refused = self.sync_all(claim=stale, **JSON)

        self.assertEqual(refused.status_code, 409)
        self.assertFalse(self.import_jobs().exists())


class SyncAllPermissionTest(_SyncAllMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TestCase):
    """An operator who lost change permission on the profile cannot queue the sync."""

    def setUp(self):
        super().setUp()
        self.build_mixed()
        self.operator = user_with_object_permission(
            "sync-all-operator",
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
        self.client.force_login(self.operator)

    def test_the_post_is_refused_without_change_permission(self):
        from users.models import ObjectPermission

        from netbox_data_import.object_permissions import clear_user_permission_caches

        self.upload()
        self.assertContains(self.workspace(), "Sync 2 actionable traces")
        permission = ObjectPermission.objects.get(name="sync-all-operator ImportProfile view-change")
        permission.actions = ["view"]
        permission.save(update_fields=("actions",))
        clear_user_permission_caches(self.operator)

        refused = self.sync_all()

        self.assertEqual(refused.status_code, 403)
        self.assertFalse(self.import_jobs().exists())
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)

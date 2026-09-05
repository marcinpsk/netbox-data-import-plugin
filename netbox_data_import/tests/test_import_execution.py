# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Import Engine executes accepted Synchronization Units as one audited transaction."""

from io import BytesIO

import openpyxl
from django.test import TransactionTestCase

from netbox_data_import.adapters import SourceUnreadable
from netbox_data_import.import_engine import ImportEngine, SelectionError, StalePlan
from netbox_data_import.netbox_reader import PlanningTargetUnavailable
from netbox_data_import.models import (
    DeviceImportSource,
    ExecutionOutcome,
    IgnoredDevice,
    ImportExecution,
    SourceDocument,
)
from netbox_data_import.plan import Disposition
from netbox_data_import.target_modules import PreconditionFailed
from netbox_data_import.tests.helpers import run_on_separate_connection
from netbox_data_import.tests.test_import_engine import ImportEngineTestDataMixin, _workbook


def _workbook_with_unmapped_column(*rows) -> bytes:
    """Return one workbook whose last column no Column Mapping consumes."""
    book = openpyxl.Workbook()
    sheet = book.worksheets[0]
    sheet.title = "Data"
    sheet.append(["Source ID", "Class", "Name", "Rack", "Make", "Model", "Height", "Depth"])
    for row in rows:
        sheet.append(list(row))
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class ImportEngineExecutionTest(ImportEngineTestDataMixin, TransactionTestCase):
    """Execution writes through real Target Modules and records the result."""

    @staticmethod
    def _planning_grants(overrides=None):
        """Return the read and write permissions the execution actor needs."""
        from dcim.models import Device, Rack, Site

        grants = {
            Site: (("view",), {}),
            Rack: (("view", "add", "change"), {}),
            Device: (("view", "add", "change"), {}),
        }
        for model, constraints in (overrides or {}).items():
            actions = grants.get(model, (("view",), {}))[0]
            grants[model] = (actions, constraints)
        return [(model, actions, constraints) for model, (actions, constraints) in grants.items()]

    def test_one_selected_create_writes_the_device_and_succeeds(self):
        """One accepted create writes its Device and names its Planned Change in the audit."""
        from dcim.models import Device

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")

        execution = ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "create-device",
            self.actor,
        )

        device = Device.objects.get(name="server-a", site=self.site)
        self.assertEqual(device.rack_id, self.rack.pk)
        self.assertEqual(execution.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertEqual(
            execution.applied_changes,
            {"changes": [unit.changes[0].identity], "deleted": []},
        )
        self.assertEqual(execution.actor, self.actor)
        self.assertEqual(execution.source_document, self.document)
        self.assertEqual(execution.plan_schema_version, accepted.schema_version)
        self.assertEqual(execution.accepted_plan_fingerprint, accepted.fingerprint)
        self.assertEqual(execution.selected_units, [unit.identity])
        self.assertEqual(execution.input_filename, self.document.filename)
        self.assertEqual(execution.site_name, self.site.name)
        self.assertEqual(execution.result_counts, {"created": {"device": 1}, "errors": 0})

    def test_execution_holds_the_device_type_that_sized_the_placement(self):
        """The replan reads u_height for placement, so it cannot move before Device.full_clean()."""
        from threading import current_thread

        from dcim.models import Device, DeviceType
        from django.db import OperationalError, connection
        from django.db.models.signals import pre_save

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        execution_thread = current_thread()
        observed = []
        blocked = []

        def contend_during_write(sender, instance, **kwargs):
            if observed or current_thread() is not execution_thread:
                return
            observed.append(True)

            def contend():
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout TO '750ms'")
                try:
                    DeviceType.objects.filter(pk=self.device_type.pk).update(u_height=42)
                except OperationalError:
                    blocked.append(True)

            with run_on_separate_connection(contend):
                pass

        pre_save.connect(contend_during_write, sender=Device, weak=False)
        try:
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                [unit.identity],
                "hold-device-type",
                self.actor,
            )
        finally:
            pre_save.disconnect(contend_during_write, sender=Device)

        self.assertTrue(observed, "the execution reached no Device write")
        self.assertEqual(blocked, [True], "the competing Device Type change did not wait for the execution")
        self.assertEqual(DeviceType.objects.get(pk=self.device_type.pk).u_height, 1)

    def test_a_captured_extra_column_reaches_the_stored_provenance(self):
        """A created device stores the captured extra columns its plan carries."""
        from dcim.models import Device

        self.profile.adapter_config = {**self.profile.adapter_config, "capture_extra_data": True}
        self.profile.save(update_fields=["adapter_config"])
        document = SourceDocument.store(
            profile=self.profile,
            content=_workbook_with_unmapped_column(
                (
                    "D-1",
                    "Server",
                    "server-a",
                    self.rack.name,
                    self.manufacturer.name,
                    self.device_type.model,
                    1,
                    "508",
                )
            ),
            filename="extra-columns.xlsx",
        )
        accepted = self._plan(document)
        unit = accepted.unit("device:source:D-1")

        ImportEngine.execute(
            self.profile,
            document,
            accepted.to_dict(),
            [unit.identity],
            "create-device-with-extra-columns",
            self.actor,
        )

        device = Device.objects.get(name="server-a", site=self.site)
        self.assertEqual(DeviceImportSource.objects.get(device=device).extra_columns, {"Depth": "508"})

    def test_execution_permission_overrides_constrain_the_saved_device(self):
        """Execution grants use the same model-to-constraint override contract as planning grants."""
        from dcim.models import Device

        from netbox_data_import.tests.helpers import user_with_object_permission

        actor = user_with_object_permission(
            "constrained-execution-operator",
            self._planning_grants({Device: {"name": "server-a"}}),
        )
        accepted = ImportEngine.plan(self.profile, self.document, actor, self.planning_context)
        unit = accepted.unit("device:source:D-1")

        execution = ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "constrained-device-create",
            actor,
        )

        self.assertEqual(execution.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertTrue(Device.objects.filter(name="server-a", site=self.site).exists())

    def test_rack_and_device_creates_share_dependency_order(self):
        """One selection creates a Rack before the Device that depends on it."""
        from dcim.models import Device, Rack

        self.rack.delete()
        accepted = self._plan()
        device_unit = accepted.unit("device:source:D-1")
        rack_unit = accepted.unit("rack:source:R-1")

        execution = ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [device_unit.identity, rack_unit.identity],
            "create-rack-and-device",
            self.actor,
        )

        rack = Rack.objects.get(name="rack-a", site=self.site)
        device = Device.objects.get(name="server-a", site=self.site)
        self.assertEqual(device.rack_id, rack.pk)
        self.assertEqual(
            execution.applied_changes["changes"],
            [rack_unit.changes[0].identity, device_unit.changes[0].identity],
        )

    def test_a_precondition_failure_rolls_back_the_complete_selection(self):
        """A vanished update target rolls back an earlier Rack create and records the failed change."""
        from django.db.models.signals import post_save

        from dcim.models import Device, Rack

        stored = Device.objects.create(
            name="server-a",
            site=self.site,
            rack=self.rack,
            device_type=self.device_type,
            role=self.role,
        )
        self.profile.device_matches.create(
            source_id="D-1",
            netbox_device_id=stored.pk,
            device_name=stored.name,
        )
        document = SourceDocument.store(
            profile=self.profile,
            content=_workbook(
                ("R-NEW", "Cabinet", "", "new-rack", "", "", 42),
                (
                    "D-1",
                    "Server",
                    "server-a",
                    "new-rack",
                    self.manufacturer.name,
                    self.device_type.model,
                    1,
                ),
                (
                    "D-2",
                    "Server",
                    "server-b",
                    "new-rack",
                    self.manufacturer.name,
                    self.device_type.model,
                    1,
                ),
            ),
            filename="rollback.xlsx",
        )
        accepted = self._plan(document)
        rack_unit = accepted.unit("rack:source:R-NEW")
        device_unit = accepted.unit("device:source:D-1")
        later_unit = accepted.unit("device:source:D-2")
        self.assertEqual(device_unit.disposition, Disposition.ACTIONABLE)

        def delete_matched_device(sender, instance, created, **kwargs):
            if created and instance.name == "new-rack":
                Device.objects.filter(pk=stored.pk).delete()

        post_save.connect(delete_matched_device, sender=Rack, weak=False)
        self.addCleanup(post_save.disconnect, delete_matched_device, sender=Rack)

        with self.assertRaises(PreconditionFailed):
            ImportEngine.execute(
                self.profile,
                document,
                accepted.to_dict(),
                [rack_unit.identity, device_unit.identity, later_unit.identity],
                "rollback-complete-selection",
                self.actor,
            )

        self.assertFalse(Rack.objects.filter(name="new-rack", site=self.site).exists())
        self.assertTrue(Device.objects.filter(pk=stored.pk).exists())
        self.assertFalse(Device.objects.filter(name="server-b", site=self.site).exists())
        execution = ImportExecution.objects.get(idempotency_key="rollback-complete-selection")
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(
            execution.failure_detail,
            {
                "failed_change": device_unit.changes[0].identity,
                "rolled_back": [rack_unit.changes[0].identity],
                "not_attempted": [later_unit.changes[0].identity],
                "reason": "precondition",
            },
        )
        self.assertEqual(execution.input_filename, "rollback.xlsx")
        self.assertEqual(execution.site_name, self.site.name)
        self.assertEqual(execution.result_counts, {"created": {}, "errors": 1})

    def test_a_finished_idempotency_key_returns_the_same_row_without_writing_again(self):
        """A duplicate delivery returns its succeeded audit row before it replans or writes."""
        from dcim.models import Device

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        first = ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "finished-duplicate",
            self.actor,
        )

        written_at = Device.objects.get(name="server-a", site=self.site).last_updated

        second = ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "finished-duplicate",
            self.actor,
        )

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertEqual(Device.objects.filter(name="server-a", site=self.site).count(), 1)
        self.assertEqual(ImportExecution.objects.filter(idempotency_key="finished-duplicate").count(), 1)
        # A second write would touch the row, so the stored timestamp proves nothing ran again.
        self.assertEqual(Device.objects.get(name="server-a", site=self.site).last_updated, written_at)

    def test_a_pending_idempotency_key_returns_without_writing(self):
        """A duplicate delivery returns the live pending row without entering the target transaction."""
        from dcim.models import Device

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        pending, _ = ImportExecution.reserve(
            profile=self.profile,
            source_document=self.document,
            actor=self.actor,
            idempotency_key="pending-duplicate",
            plan_schema_version=accepted.schema_version,
            accepted_plan_fingerprint=accepted.fingerprint,
            selected_units=[unit.identity],
        )

        returned = ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "pending-duplicate",
            self.actor,
        )

        self.assertEqual(returned.pk, pending.pk)
        self.assertEqual(returned.outcome, ExecutionOutcome.PENDING)
        self.assertFalse(Device.objects.filter(name="server-a", site=self.site).exists())

    def test_a_failure_after_the_reservation_never_leaves_the_row_pending(self):
        """A replan that fails once the audit row exists still records an outcome."""
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Site
        from users.models import ObjectPermission

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        # The actor loses the target site between accepting the plan and executing it.
        ObjectPermission.objects.filter(users=self.actor, object_types=ContentType.objects.get_for_model(Site)).delete()
        # Django caches permissions on the user instance, so the revoked actor is read again.
        actor = get_user_model().objects.get(pk=self.actor.pk)

        with self.assertRaises(PlanningTargetUnavailable):
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                [unit.identity],
                "planning-failure",
                actor,
            )

        execution = ImportExecution.objects.get(idempotency_key="planning-failure")
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(execution.failure_detail["reason"], "planning")

    def test_the_profile_policy_is_held_for_the_whole_execution(self):
        """A policy write cannot commit between the comparison and the writes."""
        from django.db import connection

        from netbox_data_import.models import ImportProfile

        def policy_lock_is_held():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_locks
                        WHERE pid = pg_backend_pid()
                          AND relation = to_regclass(%s)
                          AND mode = 'RowShareLock'
                          AND granted
                    )
                    """,
                    [ImportProfile._meta.db_table],
                )
                return cursor.fetchone()[0]

        lock_observations = []

        class LockObservedEngine(ImportEngine):
            @classmethod
            def plan(cls, *args, **kwargs):
                lock_observations.append(("comparison", policy_lock_is_held()))
                return super().plan(*args, **kwargs)

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        LockObservedEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "policy-lock",
            self.actor,
            progress_callback=lambda _processed, _total: lock_observations.append(("write", policy_lock_is_held())),
        )

        self.assertTrue(lock_observations)
        self.assertIn(("comparison", True), lock_observations)
        self.assertIn(("write", True), lock_observations)
        self.assertTrue(all(held for _stage, held in lock_observations))
        self.assertFalse(policy_lock_is_held())
        self.assertTrue(ImportProfile.objects.filter(pk=self.profile.pk).exists())

    def test_execution_reloads_policy_after_acquiring_its_lock(self):
        """Replanning uses a policy write that committed after the caller loaded its profile."""
        from dcim.models import Device

        from netbox_data_import.models import ImportProfile

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        ImportProfile.objects.filter(pk=self.profile.pk).update(
            adapter_config={**self.profile.adapter_config, "sheet_name": "Missing"}
        )

        with self.assertRaises(SourceUnreadable):
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                [unit.identity],
                "fresh-locked-policy",
                self.actor,
            )

        self.assertFalse(Device.objects.filter(name="server-a", site=self.site).exists())

    def test_an_empty_selection_is_refused_before_any_row_exists(self):
        """An execution names the units it applies, so an empty selection is a mistake."""
        accepted = self._plan()

        with self.assertRaises(SelectionError):
            ImportEngine.execute(self.profile, self.document, accepted.to_dict(), [], "empty-selection", self.actor)

        self.assertFalse(ImportExecution.objects.filter(idempotency_key="empty-selection").exists())

    def test_a_selected_unit_that_moved_is_refused_as_stale(self):
        """A target-state change invalidates its accepted unit before any target write."""
        from dcim.models import Device

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        stored = Device.objects.create(
            name="server-a",
            site=self.site,
            rack=self.rack,
            device_type=self.device_type,
            role=self.role,
        )

        with self.assertRaises(StalePlan):
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                [unit.identity],
                "stale-selected-unit",
                self.actor,
            )

        self.assertEqual(Device.objects.filter(name="server-a", site=self.site).count(), 1)
        self.assertFalse(hasattr(stored, "data_import_source"))
        execution = ImportExecution.objects.get(idempotency_key="stale-selected-unit")
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(execution.failure_detail["reason"], "stale_plan")

    def test_a_selected_unit_that_became_a_no_op_is_stale(self):
        """An earlier execution can reconcile a unit, so the old actionable plan cannot run again."""
        from dcim.models import Device

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "reconcile-selected-unit",
            self.actor,
        )

        with self.assertRaises(StalePlan):
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                [unit.identity],
                "selected-unit-no-longer-actionable",
                self.actor,
            )

        self.assertEqual(Device.objects.filter(name="server-a", site=self.site).count(), 1)
        stale = ImportExecution.objects.get(idempotency_key="selected-unit-no-longer-actionable")
        self.assertEqual(stale.failure_detail["reason"], "stale_plan")

    def test_an_unrelated_unit_change_does_not_block_a_safe_selection(self):
        """Only selected unit fingerprints take part in selective execution comparison."""
        from dcim.models import Device

        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")
        self.rack.u_height = 41
        self.rack.save(update_fields=["u_height"])

        execution = ImportEngine.execute(
            self.profile,
            self.document,
            accepted.to_dict(),
            [unit.identity],
            "unrelated-unit-moved",
            self.actor,
        )

        self.assertEqual(execution.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertTrue(Device.objects.filter(name="server-a", site=self.site).exists())
        self.rack.refresh_from_db()
        self.assertEqual(self.rack.u_height, 41)

    def test_a_selected_unit_cannot_silently_expand_to_its_dependency(self):
        """Leaving a required Rack unit out makes the explicit Device selection invalid."""
        from dcim.models import Device, Rack

        self.rack.delete()
        accepted = self._plan()
        unit = accepted.unit("device:source:D-1")

        with self.assertRaises(SelectionError):
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                [unit.identity],
                "dependency-not-selected",
                self.actor,
            )

        self.assertFalse(Rack.objects.filter(name="rack-a", site=self.site).exists())
        self.assertFalse(Device.objects.filter(name="server-a", site=self.site).exists())
        execution = ImportExecution.objects.get(idempotency_key="dependency-not-selected")
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(execution.failure_detail["reason"], "selection")
        self.assertEqual(execution.failure_detail["not_attempted"], [unit.changes[0].identity])

    def test_an_unknown_selected_identity_is_a_selection_failure(self):
        """The current plan must carry every identity the operator submitted."""
        from dcim.models import Device

        accepted = self._plan()

        with self.assertRaises(SelectionError):
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                ["device:source:UNKNOWN"],
                "unknown-selection",
                self.actor,
            )

        self.assertFalse(Device.objects.filter(name="server-a", site=self.site).exists())
        execution = ImportExecution.objects.get(idempotency_key="unknown-selection")
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(execution.failure_detail["reason"], "selection")

    def test_an_incompatible_plan_schema_fails_before_reservation(self):
        """An old executable plan writes neither a target object nor an audit row."""
        from dcim.models import Device

        from netbox_data_import.plan import SCHEMA_VERSION, PlanSchemaMismatch

        accepted = self._plan()
        serialized = accepted.to_dict()
        # Derive the incompatible version, so bumping the schema cannot make this test vacuous.
        serialized["schema_version"] = SCHEMA_VERSION + 1

        with self.assertRaises(PlanSchemaMismatch):
            ImportEngine.execute(
                self.profile,
                self.document,
                serialized,
                ["device:source:D-1"],
                "wrong-schema",
                self.actor,
            )

        self.assertEqual(ImportExecution.objects.count(), 0)
        self.assertFalse(Device.objects.filter(name="server-a", site=self.site).exists())

    def test_non_actionable_units_cannot_be_selected(self):
        """No-op, blocked, and excluded units never enter an execution transaction."""
        from dcim.models import Device

        no_op = self._plan()
        self._assert_selection_refused(no_op, self.document, "rack:source:R-1", "select-no-op")

        blocked_document = SourceDocument.store(
            profile=self.profile,
            content=_workbook(
                (
                    "D-BLOCKED",
                    "Server",
                    "blocked-server",
                    "missing-rack",
                    self.manufacturer.name,
                    self.device_type.model,
                    1,
                )
            ),
            filename="blocked.xlsx",
        )
        blocked = self._plan(blocked_document)
        self._assert_selection_refused(
            blocked,
            blocked_document,
            "device:source:D-BLOCKED",
            "select-blocked",
        )

        IgnoredDevice.objects.create(
            profile=self.profile,
            source_id="D-EXCLUDED",
            device_name="excluded-server",
        )
        excluded_document = SourceDocument.store(
            profile=self.profile,
            content=_workbook(
                (
                    "D-EXCLUDED",
                    "Server",
                    "excluded-server",
                    self.rack.name,
                    self.manufacturer.name,
                    self.device_type.model,
                    1,
                )
            ),
            filename="excluded.xlsx",
        )
        excluded = self._plan(excluded_document)
        self._assert_selection_refused(
            excluded,
            excluded_document,
            "device:source:D-EXCLUDED",
            "select-excluded",
        )

        self.assertEqual(ImportExecution.objects.filter(outcome=ExecutionOutcome.FAILED).count(), 3)
        self.assertFalse(Device.objects.filter(name__in=["blocked-server", "excluded-server"]).exists())

    def _assert_selection_refused(self, accepted, document, identity, key):
        """Assert that one non-actionable selected unit fails with no target write."""
        with self.assertRaises(SelectionError):
            ImportEngine.execute(
                self.profile,
                document,
                accepted.to_dict(),
                [identity],
                key,
                self.actor,
            )
        execution = ImportExecution.objects.get(idempotency_key=key)
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(execution.failure_detail["reason"], "selection")

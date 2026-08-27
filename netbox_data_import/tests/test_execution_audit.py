# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The stored source document and the Import Execution audit record."""

import uuid
from datetime import timedelta

from core.choices import JobStatusChoices
from core.models import Job
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from netbox_data_import.models import (
    ExecutionOutcome,
    FailureReason,
    ImportExecution,
    ImportProfile,
    SourceDocument,
)

WORKBOOK = b"PK\x03\x04 fake workbook bytes"


def _operator(username="audit-operator"):
    """Return the saved user of this name, so one test can store several documents."""
    user, _ = get_user_model().objects.get_or_create(username=username)
    return user


def _document(profile, *, content=WORKBOOK, filename="cans.xlsx", uploaded_by=None):
    """Store one uploaded workbook for *profile*."""
    return SourceDocument.store(
        profile=profile, content=content, filename=filename, uploaded_by=uploaded_by or _operator()
    )


def _reservation(profile, document, actor, key="key-1"):
    """Return the keyword arguments one execution reservation needs."""
    return {
        "profile": profile,
        "source_document": document,
        "actor": actor,
        "idempotency_key": key,
        "plan_schema_version": 1,
        "accepted_plan_fingerprint": "f" * 64,
        "selected_units": ["unit:1", "unit:2"],
    }


class SourceDocumentTest(TestCase):
    """Section 9.1: the stored upload every planning and execution read shares."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="Audit Profile", adapter_config={})

    def test_it_stores_the_bytes_and_fingerprints_the_content(self):
        """Planning, replanning, a background execution, and an audit read see the same input."""
        document = _document(self.profile)
        self.assertEqual(bytes(document.content), WORKBOOK)
        self.assertRegex(document.content_fingerprint, r"^[0-9a-f]{64}$")

    def test_identical_content_fingerprints_identically(self):
        """The fingerprint is content-addressed, so a re-upload is recognizable."""
        self.assertEqual(
            _document(self.profile).content_fingerprint,
            _document(self.profile, filename="other-name.xlsx").content_fingerprint,
        )

    def test_different_content_fingerprints_differently(self):
        """A changed source invalidates every selection made against the old one."""
        self.assertNotEqual(
            _document(self.profile).content_fingerprint,
            _document(self.profile, content=b"different bytes").content_fingerprint,
        )

    def test_a_newer_upload_never_removes_an_older_one(self):
        """Two operators previewing one profile cannot delete each other's input."""
        first = _document(self.profile)
        second = _document(self.profile, content=b"second upload")
        self.assertEqual(SourceDocument.objects.filter(pk__in=[first.pk, second.pk]).count(), 2)


class SourceDocumentRetentionTest(TestCase):
    """Section 9.1: two retention rules, one permanent and one timed."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="Retention Profile", adapter_config={})

    def _age(self, document, days):
        """Backdate the creation time, which auto_now_add sets on insert."""
        SourceDocument.objects.filter(pk=document.pk).update(created=timezone.now() - timedelta(days=days))

    def test_an_unreferenced_document_older_than_the_window_is_deleted(self):
        """Housekeeping reclaims uploads no audit record needs."""
        stale = _document(self.profile)
        self._age(stale, 31)
        self.assertEqual(SourceDocument.purge_unreferenced(), 1)
        self.assertFalse(SourceDocument.objects.filter(pk=stale.pk).exists())

    def test_an_unreferenced_document_inside_the_window_survives(self):
        """The window is 30 days and housekeeping never runs sooner."""
        recent = _document(self.profile)
        self._age(recent, 29)
        self.assertEqual(SourceDocument.purge_unreferenced(), 0)
        self.assertTrue(SourceDocument.objects.filter(pk=recent.pk).exists())

    def test_a_referenced_document_is_permanent_audit_input(self):
        """An execution's input is never reclaimed, however old it is."""
        referenced = _document(self.profile)
        self._age(referenced, 400)
        ImportExecution.objects.create(
            profile=self.profile,
            source_document=referenced,
            actor=_operator("retention-actor"),
            idempotency_key="retention-key",
            plan_schema_version=1,
            accepted_plan_fingerprint="a" * 64,
            selected_units=["unit:1"],
            outcome=ExecutionOutcome.SUCCEEDED,
        )
        self.assertEqual(SourceDocument.purge_unreferenced(), 0)
        self.assertTrue(SourceDocument.objects.filter(pk=referenced.pk).exists())

    def test_deleting_a_profile_keeps_the_documents_its_executions_reference(self):
        """An execution's input is permanent audit input, so it outlives its Import Profile."""
        profile = ImportProfile.objects.create(name="Deleted Profile", adapter_config={})
        referenced = _document(profile, uploaded_by=_operator("delete-actor"))
        ImportExecution.objects.create(
            profile=profile,
            source_document=referenced,
            actor=_operator("delete-actor"),
            idempotency_key="delete-key",
            plan_schema_version=1,
            accepted_plan_fingerprint="a" * 64,
            selected_units=["unit:1"],
            outcome=ExecutionOutcome.SUCCEEDED,
        )
        profile.delete()
        referenced.refresh_from_db()
        self.assertIsNone(referenced.profile_id)
        self.assertTrue(SourceDocument.objects.filter(pk=referenced.pk).exists())

    def test_an_orphaned_unreferenced_document_is_still_reclaimed(self):
        """Deleting a profile leaves its unreferenced uploads to the retention window."""
        profile = ImportProfile.objects.create(name="Orphan Profile", adapter_config={})
        orphan = _document(profile, uploaded_by=_operator("orphan-actor"))
        profile.delete()
        self._age(orphan, 31)
        self.assertEqual(SourceDocument.purge_unreferenced(), 1)
        self.assertFalse(SourceDocument.objects.filter(pk=orphan.pk).exists())

    def test_the_retention_query_does_not_load_the_stored_bytes(self):
        """A protecting reverse relation blocks fast deletion, so the collector would load content."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._age(_document(self.profile, content=b"a" * 4096), 31)
        with CaptureQueriesContext(connection) as captured:
            SourceDocument.purge_unreferenced()
        selects = [q["sql"] for q in captured.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertTrue(selects, "the collector must read the rows it deletes")
        self.assertFalse([sql for sql in selects if '."content"' in sql], selects)

    def test_a_referenced_document_cannot_be_deleted_directly(self):
        """The database enforces the audit rule, not only the housekeeping query."""
        referenced = _document(self.profile)
        ImportExecution.objects.create(
            profile=self.profile,
            source_document=referenced,
            actor=_operator("protect-actor"),
            idempotency_key="protect-key",
            plan_schema_version=1,
            accepted_plan_fingerprint="a" * 64,
            selected_units=["unit:1"],
            outcome=ExecutionOutcome.SUCCEEDED,
        )
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            referenced.delete()


class ImportExecutionReservationTest(TransactionTestCase):
    """Section 4.7: the pending insert commits first and reserves the idempotency key."""

    def setUp(self):
        """Create the profile, upload, and actor each reservation needs."""
        self.profile = ImportProfile.objects.create(name="Reservation Profile", adapter_config={})
        self.actor = _operator("reservation-actor")
        self.document = _document(self.profile, uploaded_by=self.actor)

    def test_a_reservation_creates_a_pending_row(self):
        """The row exists before the target transaction opens."""
        execution, created = ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        self.assertTrue(created)
        self.assertEqual(execution.outcome, ExecutionOutcome.PENDING)
        self.assertEqual(execution.selected_units, ["unit:1", "unit:2"])

    def test_a_reservation_without_a_profile_is_refused(self):
        """The partial unique index cannot hold: PostgreSQL treats two NULL profiles as distinct."""
        fields = _reservation(self.profile, self.document, self.actor)
        fields["profile"] = None
        with self.assertRaises(ValueError):
            ImportExecution.reserve(**fields)
        self.assertEqual(ImportExecution.objects.count(), 0)

    def test_a_reservation_missing_the_profile_entirely_is_refused(self):
        """Omitting the profile must raise the same reservation error, not a KeyError."""
        fields = _reservation(self.profile, self.document, self.actor)
        fields.pop("profile")
        with self.assertRaises(ValueError):
            ImportExecution.reserve(**fields)
        self.assertEqual(ImportExecution.objects.count(), 0)

    def test_a_duplicate_submission_returns_the_existing_pending_row(self):
        """A duplicate HTTP submission or job delivery never starts a second write."""
        first, _ = ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        second, created = ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(ImportExecution.objects.count(), 1)

    def test_a_duplicate_submission_returns_a_finished_row_unchanged(self):
        """A redelivered job returns the outcome instead of re-running the writes."""
        first, _ = ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        first.mark_succeeded(applied_changes={"changes": ["device:1"], "deleted": []})
        second, created = ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.outcome, ExecutionOutcome.SUCCEEDED)

    def test_a_new_key_reserves_a_new_row(self):
        """An abandoned attempt consumes no key permanently."""
        ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor, key="key-1"))
        _, created = ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor, key="key-2"))
        self.assertTrue(created)
        self.assertEqual(ImportExecution.objects.count(), 2)

    def test_the_same_key_on_another_profile_is_a_separate_reservation(self):
        """The unique constraint is per Import Profile."""
        other = ImportProfile.objects.create(name="Other Reservation Profile", adapter_config={})
        ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        _, created = ImportExecution.reserve(**_reservation(other, _document(other), self.actor))
        self.assertTrue(created)

    def test_reserving_inside_an_open_transaction_is_refused(self):
        """The reservation must commit before the target transaction opens."""
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))

    def test_a_reservation_requires_an_idempotency_key(self):
        """Section 9.2 makes the key required on a new row; without it nothing is reserved."""
        for missing in ("", None):
            with self.subTest(key=missing):
                with self.assertRaises(ValueError):
                    ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor, key=missing))

    def test_a_reservation_requires_every_new_execution_audit_field(self):
        """Nullable legacy columns must not permit an incomplete new audit record."""
        required = (
            "source_document",
            "actor",
            "plan_schema_version",
            "accepted_plan_fingerprint",
            "selected_units",
        )
        for field_name in required:
            fields = _reservation(
                self.profile,
                self.document,
                self.actor,
                key=f"missing-{field_name}",
            )
            fields.pop(field_name)
            with self.subTest(field=field_name), self.assertRaises(ValueError):
                ImportExecution.reserve(**fields)

    def test_an_unrelated_integrity_error_is_not_reported_as_a_duplicate(self):
        """Only a lost race for this key returns an existing row; anything else must surface."""
        doomed = _document(self.profile, content=b"doomed bytes", uploaded_by=self.actor)
        SourceDocument.objects.filter(pk=doomed.pk).delete()
        with self.assertRaises(IntegrityError):
            ImportExecution.reserve(**_reservation(self.profile, doomed, self.actor, key="doomed-key"))

    def test_a_legacy_row_never_satisfies_an_idempotency_lookup(self):
        """A retained ImportJob row keeps its historical fields and null new fields."""
        legacy = ImportExecution.objects.create(
            profile=self.profile, input_filename="legacy.xlsx", site_name="Legacy Site"
        )
        self.assertIsNone(legacy.idempotency_key)
        self.assertIsNone(legacy.outcome)
        execution, created = ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        self.assertTrue(created)
        self.assertNotEqual(execution.pk, legacy.pk)

    def test_several_legacy_rows_coexist_under_the_partial_constraint(self):
        """The unique constraint ignores rows with no idempotency key."""
        ImportExecution.objects.create(profile=self.profile, input_filename="one.xlsx")
        ImportExecution.objects.create(profile=self.profile, input_filename="two.xlsx")
        self.assertEqual(ImportExecution.objects.filter(idempotency_key__isnull=True).count(), 2)

    def test_the_database_rejects_a_duplicate_key_directly(self):
        """The constraint is the reservation guarantee, not the helper method."""
        ImportExecution.reserve(**_reservation(self.profile, self.document, self.actor))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ImportExecution.objects.create(
                    outcome=ExecutionOutcome.PENDING, **_reservation(self.profile, self.document, self.actor)
                )


class ImportExecutionOutcomeTest(TestCase):
    """Section 9.2: the outcome fields the transaction writes."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="Outcome Profile", adapter_config={})
        cls.actor = _operator("outcome-actor")
        cls.document = _document(cls.profile, uploaded_by=cls.actor)

    def _pending(self, key="outcome-key", **overrides):
        """Insert one pending row directly, bypassing the commit-order guard."""
        fields = {**_reservation(self.profile, self.document, self.actor, key=key)}
        fields.update(overrides)
        return ImportExecution.objects.create(outcome=ExecutionOutcome.PENDING, **fields)

    def test_success_records_the_applied_changes(self):
        """The applied-changes field records every applied identity and deleted object."""
        execution = self._pending()
        applied = {
            "changes": ["device:1", "rack:1"],
            "deleted": [{"object_type": "dcim.cable", "id": 7, "display": "#7", "terminations": []}],
        }
        execution.mark_succeeded(applied_changes=applied)
        execution.refresh_from_db()
        self.assertEqual(execution.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertEqual(execution.applied_changes, applied)
        self.assertIsNone(execution.failure_detail)

    def test_failure_records_what_failed_and_what_was_not_attempted(self):
        """Section 4.7 fixes the three parts of a failure detail."""
        execution = self._pending()
        execution.mark_failed(
            failed_change="device:2", rolled_back=["device:1"], not_attempted=["device:3"], reason="precondition"
        )
        execution.refresh_from_db()
        self.assertEqual(execution.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(
            execution.failure_detail,
            {
                "failed_change": "device:2",
                "rolled_back": ["device:1"],
                "not_attempted": ["device:3"],
                "reason": "precondition",
            },
        )
        self.assertIsNone(execution.applied_changes)


class PendingRecoveryTest(TestCase):
    """Section 9.2: a crashed attempt cannot strand a permanently pending row."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="Recovery Profile", adapter_config={})
        cls.actor = _operator("recovery-actor")
        cls.document = _document(cls.profile, uploaded_by=cls.actor)

    def _job(self, status):
        """Return a saved native NetBox Job in *status*."""
        return Job.objects.create(
            object_type=ContentType.objects.get_for_model(ImportProfile),
            object_id=self.profile.pk,
            name="Data Import",
            status=status,
            user=self.actor,
            job_id=uuid.uuid4(),
        )

    def _pending(self, key, *, job=None, age=timedelta(0)):
        """Insert one pending row and backdate its creation time."""
        execution = ImportExecution.objects.create(
            outcome=ExecutionOutcome.PENDING,
            job=job,
            job_backed=job is not None,
            **_reservation(self.profile, self.document, self.actor, key=key),
        )
        if age:
            ImportExecution.objects.filter(pk=execution.pk).update(created=timezone.now() - age)
            execution.refresh_from_db()
        return execution

    def test_a_job_backed_row_whose_job_is_terminal_reads_as_abandoned(self):
        """The linked Job finished without completing the row, so the attempt is gone."""
        execution = self._pending("terminal-key", job=self._job(JobStatusChoices.STATUS_ERRORED))
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.FAILED)
        execution.refresh_from_db()
        self.assertEqual(execution.failure_detail["reason"], FailureReason.ABANDONED)

    def test_a_job_backed_row_whose_job_still_runs_stays_pending(self):
        """A live execution is never marked failed by a concurrent read."""
        for status in JobStatusChoices.ENQUEUED_STATE_CHOICES:
            with self.subTest(status=status):
                execution = self._pending(f"live-{status}", job=self._job(status))
                self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.PENDING)

    def test_a_job_backed_row_whose_job_is_gone_reads_as_abandoned(self):
        """A deleted Job nulls the reference, so the row must record job-backing separately."""
        job = self._job(JobStatusChoices.STATUS_RUNNING)
        execution = self._pending("missing-job-key", job=job)
        job.delete()
        execution.refresh_from_db()
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.FAILED)

    def test_a_synchronous_row_stays_pending_inside_the_request_bound(self):
        """A read during a live synchronous execution never marks it failed."""
        execution = self._pending("sync-live-key", age=timedelta(minutes=9))
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.PENDING)

    def test_a_synchronous_row_older_than_the_request_bound_reads_as_abandoned(self):
        """Ten minutes is the web request bound, so an older row cannot still be running."""
        execution = self._pending("sync-stale-key", age=timedelta(minutes=11))
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.FAILED)
        execution.refresh_from_db()
        self.assertEqual(execution.failure_detail["reason"], FailureReason.ABANDONED)

    def test_a_row_reserved_before_its_job_is_linked_stays_pending(self):
        """Section 4.7 commits the reservation before the Job exists, so that window is live."""
        execution = self._pending("window-key")
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.PENDING)

    def test_a_row_never_linked_to_a_job_is_abandoned_past_the_bound(self):
        """A background attempt that never reached the queue cannot stay pending forever."""
        execution = self._pending("never-enqueued-key", age=timedelta(minutes=11))
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.FAILED)

    def test_linking_a_job_switches_the_row_onto_the_job_status(self):
        """Once linked, the Job decides whether the attempt is still live."""
        execution = self._pending("linked-key")
        execution.link_job(self._job(JobStatusChoices.STATUS_RUNNING))
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.PENDING)
        Job.objects.filter(pk=execution.job_id).update(status=JobStatusChoices.STATUS_ERRORED)
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.FAILED)

    def test_a_terminal_row_refuses_a_second_outcome(self):
        """A redelivered attempt must never overwrite committed audit evidence."""
        succeeded = self._pending("terminal-success-key")
        succeeded.mark_succeeded(applied_changes={"changes": ["device:1"], "deleted": []})
        with self.assertRaises(ValueError):
            succeeded.mark_failed(reason="precondition")
        succeeded.refresh_from_db()
        self.assertEqual(succeeded.applied_changes["changes"], ["device:1"])

        failed = self._pending("terminal-failure-key")
        failed.mark_failed(reason=FailureReason.ABANDONED)
        with self.assertRaises(ValueError):
            failed.mark_succeeded(applied_changes={"changes": [], "deleted": []})
        failed.refresh_from_db()
        self.assertEqual(failed.failure_detail["reason"], FailureReason.ABANDONED)

    def test_a_concurrent_transition_cannot_overwrite_a_committed_outcome(self):
        """Two instances both read pending, so the database must arbitrate the single transition."""
        first = self._pending("concurrent-key")
        second = ImportExecution.objects.get(pk=first.pk)
        first.mark_succeeded(applied_changes={"changes": ["device:1"], "deleted": []})
        with self.assertRaises(ValueError):
            second.mark_failed(reason="precondition")
        stored = ImportExecution.objects.get(pk=first.pk)
        self.assertEqual(stored.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertEqual(stored.applied_changes["changes"], ["device:1"])

    def test_reconciliation_yields_to_a_worker_that_finished_the_row(self):
        """A worker may finish the row between the reconciling read and its transition."""
        stale = self._pending("reconcile-race-key", age=timedelta(minutes=11))
        ImportExecution.objects.get(pk=stale.pk).mark_succeeded(applied_changes={"changes": [], "deleted": []})
        self.assertEqual(stale.reconcile_pending().outcome, ExecutionOutcome.SUCCEEDED)
        self.assertEqual(ImportExecution.objects.get(pk=stale.pk).outcome, ExecutionOutcome.SUCCEEDED)

    def test_a_rolled_back_reconciliation_self_heals_on_the_next_read(self):
        """The transition is recomputed on every read, so losing it to a rollback costs nothing."""
        execution = self._pending("rollback-key", age=timedelta(minutes=11))
        with transaction.atomic():
            execution.reconcile_pending()
            transaction.set_rollback(True)
        stored = ImportExecution.objects.get(pk=execution.pk)
        self.assertEqual(stored.outcome, ExecutionOutcome.PENDING, "the write rolled back with the caller")
        self.assertEqual(stored.reconcile_pending().outcome, ExecutionOutcome.FAILED)

    def test_a_finished_row_is_never_reconciled(self):
        """Reconciliation only ever transitions a pending row."""
        execution = self._pending("finished-key")
        execution.mark_succeeded(applied_changes={"changes": [], "deleted": []})
        self.assertEqual(execution.reconcile_pending().outcome, ExecutionOutcome.SUCCEEDED)

    def test_an_idempotency_lookup_reconciles_before_returning(self):
        """Section 9.2 transitions an abandoned row at the next read, with no sweeper."""
        self._pending("lookup-key", age=timedelta(minutes=11))
        found = ImportExecution.for_idempotency(self.profile, "lookup-key")
        self.assertEqual(found.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(found.failure_detail["reason"], FailureReason.ABANDONED)

    def test_a_lookup_ignores_a_legacy_row(self):
        """A legacy row never satisfies an idempotency lookup."""
        ImportExecution.objects.create(profile=self.profile, input_filename="legacy.xlsx")
        self.assertIsNone(ImportExecution.for_idempotency(self.profile, None))


class SourceDocumentRetentionJobTest(TestCase):
    """The retention rule runs on NetBox's housekeeping schedule, not only on demand."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="Retention Job Profile", adapter_config={})

    def test_a_real_django_startup_registers_the_daily_system_job(self):
        """NetBox does not import a plugin's jobs module, so the plugin config must.

        This boots Django in a subprocess: importing the jobs module here would run the decorator
        and register the job, which is exactly what the test has to prove happens without it.
        """
        import os
        import subprocess
        import sys
        import textwrap

        probe = textwrap.dedent(
            """
            import django
            django.setup()
            from netbox.registry import registry

            found = {
                f"{cls.__module__}.{cls.__name__}": meta["interval"]
                for cls, meta in registry["system_jobs"].items()
            }
            print(found.get("netbox_data_import.jobs.SourceDocumentRetentionJob"))
            """
        )
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            "DJANGO_SETTINGS_MODULE": os.environ.get("DJANGO_SETTINGS_MODULE", "netbox.settings"),
        }
        completed = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=300, check=False, env=environment
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertEqual(completed.stdout.strip().splitlines()[-1], str(60 * 24), completed.stderr[-2000:])

    def test_the_job_run_method_applies_the_retention_rules(self):
        """The scheduled entry point is run(), so it must reach the retention rules."""
        import uuid

        from netbox_data_import.jobs import SourceDocumentRetentionJob

        stale = _document(self.profile, filename="run-stale.xlsx", content=b"run stale bytes")
        SourceDocument.objects.filter(pk=stale.pk).update(created=timezone.now() - timedelta(days=31))
        job = Job.objects.create(
            object_type=ContentType.objects.get_for_model(ImportProfile),
            object_id=self.profile.pk,
            name="Data Import source document retention",
            status=JobStatusChoices.STATUS_RUNNING,
            job_id=uuid.uuid4(),
        )
        self.assertEqual(SourceDocumentRetentionJob(job).run(), 1)
        self.assertFalse(SourceDocument.objects.filter(pk=stale.pk).exists())

    def test_running_it_reclaims_only_the_unreferenced_stale_documents(self):
        """One run applies both retention rules against the real database."""
        from netbox_data_import.jobs import SourceDocumentRetentionJob

        stale = _document(self.profile, filename="stale.xlsx")
        referenced = _document(self.profile, filename="referenced.xlsx", content=b"referenced bytes")
        recent = _document(self.profile, filename="recent.xlsx", content=b"recent bytes")
        ImportExecution.objects.create(
            profile=self.profile,
            source_document=referenced,
            actor=_operator("retention-job-actor"),
            idempotency_key="retention-job-key",
            plan_schema_version=1,
            accepted_plan_fingerprint="a" * 64,
            selected_units=["unit:1"],
            outcome=ExecutionOutcome.SUCCEEDED,
        )
        old = timezone.now() - timedelta(days=31)
        SourceDocument.objects.filter(pk__in=[stale.pk, referenced.pk]).update(created=old)

        self.assertEqual(SourceDocumentRetentionJob.purge(), 1)
        self.assertFalse(SourceDocument.objects.filter(pk=stale.pk).exists())
        self.assertTrue(SourceDocument.objects.filter(pk=referenced.pk).exists())
        self.assertTrue(SourceDocument.objects.filter(pk=recent.pk).exists())

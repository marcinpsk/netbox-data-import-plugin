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

    def test_the_job_is_registered_as_a_daily_system_job(self):
        """Retention needs no operator action, so NetBox schedules it."""
        from netbox.registry import registry

        from netbox_data_import.jobs import SourceDocumentRetentionJob

        self.assertEqual(registry["system_jobs"][SourceDocumentRetentionJob]["interval"], 60 * 24)

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

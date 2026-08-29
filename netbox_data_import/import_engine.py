# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The target-neutral Import Engine coordinator.

Section 2.3 gives the coordinator the merged dependency graph, transaction scope, idempotency and
audit. It reaches Target Modules and Source Adapters through their registries alone, so it names no
workbook, no column and no NetBox object type.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import DatabaseError

from . import adapter_config, adapters, catalog, target_modules
from .models import FailureReason, ImportExecution, SourceDocument, locked_profile_policy
from .netbox_reader import NetBoxReader, PlanningTargetUnavailable
from .object_permissions import ObjectPermissionDenied
from .plan import Diagnostic, Disposition, ImportPlan, PlanInvalid, Severity, executable_units, merge_changes
from .source_resolution import derive_effective_rows
from .target_modules import ExecutionContext, PreconditionFailed


_RESOLUTION_SECTION = "source_resolutions"


def _resolution_section():
    """Return the policy section that declares where a saved Source Resolution applies."""
    section = catalog.policy_section(_RESOLUTION_SECTION)
    if section is None:
        raise LookupError(f"The catalog declares no '{_RESOLUTION_SECTION}' policy section.")
    return section


class StaleSourceDocument(Exception):
    """The referenced stored source no longer exists, so the operator has to upload it again."""


class StalePlan(Exception):
    """A selected unit no longer has the decision inputs the operator accepted."""


class SelectionError(Exception):
    """The requested Synchronization Unit selection is not executable as stated."""


class _ExecutionFailed(Exception):
    """Carry one apply failure out of the transaction, with what rolled back behind it."""

    def __init__(self, *, cause, reason, failed_change, rolled_back, not_attempted):
        super().__init__(str(cause))
        self.cause = cause
        self.reason = reason
        self.failed_change = failed_change
        self.rolled_back = list(rolled_back)
        self.not_attempted = list(not_attempted)


class ImportEngine:
    """Coordinate source interpretation and Target Module planning."""

    @classmethod
    def plan(cls, profile, source_document, actor, planning_context) -> ImportPlan:
        """Return the deterministic Import Plan for one stored source."""
        document = cls._stored_source(profile, source_document)
        adapter = adapters.get_adapter(profile.source_adapter)
        if adapter is None:
            raise adapters.UnknownSourceAdapter(
                f"This release does not register the source adapter '{profile.source_adapter}'."
            )
        if not catalog.has_implemented_module(adapter.output_kinds):
            raise adapters.UnknownSourceAdapter(
                f"This release has no Target Module for source adapter '{profile.source_adapter}'."
            )
        source_batch = adapter.interpret(
            bytes(document.content),
            adapter_config.interpreter_config_for(profile),
            collect_unused=True,
        )
        # The catalog already declares which output kinds the resolution policy applies to.
        if _resolution_section().applies_to(source_batch.output_kinds):
            source_batch = adapters.SourceBatch(
                output_kinds=source_batch.output_kinds,
                rows=tuple(derive_effective_rows(list(source_batch.rows), profile)),
                diagnostics=source_batch.diagnostics,
                unused_columns=source_batch.unused_columns,
            )
        reader = NetBoxReader.for_actor(actor).for_planning_context(planning_context)

        units = []
        for declaration in catalog.TARGET_MODULES:
            if not declaration.consumes & source_batch.output_kinds:
                continue
            runtime = target_modules.runtime_for(declaration.key)
            if runtime is not None:
                units.extend(runtime.plan(source_batch, profile, catalog.CATALOG, reader))

        # Section 4.4 makes the merged graph the coordinator's, so a bad one fails here, not at a write.
        merge_changes(executable_units(units))
        return ImportPlan(
            units=tuple(units),
            diagnostics=(
                *(cls._source_diagnostic(item) for item in source_batch.diagnostics),
                *(cls._unused_column_diagnostic(name, stats) for name, stats in source_batch.unused_columns.items()),
            ),
            source_fingerprint=document.content_fingerprint,
            profile_fingerprint=profile.planning_fingerprint,
            actor=str(actor.pk),
            planning_context=planning_context,
        )

    @classmethod
    def execute(
        cls,
        profile,
        source_document,
        accepted_plan,
        selection,
        idempotency_key,
        actor,
        *,
        job=None,
        progress_callback=None,
    ) -> ImportExecution:
        """Apply selected units from one accepted serialized plan and return their audit row."""
        accepted = ImportPlan.from_dict(accepted_plan)
        selected_identities = tuple(selection)
        if not selected_identities:
            raise SelectionError("An execution needs at least one Synchronization Unit.")
        execution, created = ImportExecution.reserve(
            profile=profile,
            source_document=source_document,
            actor=actor,
            idempotency_key=idempotency_key,
            plan_schema_version=accepted.schema_version,
            accepted_plan_fingerprint=accepted.fingerprint,
            selected_units=list(selected_identities),
        )
        if not created:
            return execution

        try:
            if job is not None:
                execution.link_job(job)
            # One lock over the replan, the comparison and the writes: policy cannot move between them.
            with locked_profile_policy(profile.pk):
                profile.refresh_from_db()
                cls._write_selection(
                    execution,
                    profile,
                    source_document,
                    accepted,
                    selected_identities,
                    actor,
                    progress_callback,
                )
        except _ExecutionFailed as failure:
            cls._mark_failed(
                execution,
                reason=failure.reason,
                failed_change=failure.failed_change,
                rolled_back=failure.rolled_back,
                not_attempted=failure.not_attempted,
            )
            raise failure.cause
        except Exception as exc:
            # Every other failure after the reservation, so the row can never stay pending.
            cls._mark_failed(
                execution,
                reason=cls._failure_reason(exc),
                not_attempted=cls._selected_change_identities(accepted, selected_identities),
            )
            raise
        return execution

    @classmethod
    def _write_selection(
        cls,
        execution,
        profile,
        source_document,
        accepted,
        selected_identities,
        actor,
        progress_callback,
    ) -> None:
        """Compare the selection against a fresh plan and apply it, inside the caller's transaction."""
        current = cls.plan(profile, source_document, actor, accepted.planning_context)
        units = cls._selected_units(accepted, current, selected_identities)
        try:
            changes = merge_changes(units)
        except PlanInvalid as exc:
            raise SelectionError(str(exc)) from exc
        total = len(units) + len(changes)
        if progress_callback is not None:
            progress_callback(0, total)
            progress_callback(len(units), total)
        context = ExecutionContext(
            actor=actor,
            reader=NetBoxReader.for_actor(actor).for_planning_context(accepted.planning_context),
            profile=profile,
        )
        completed: list[str] = []
        for index, change in enumerate(changes):
            runtime = target_modules.runtime_for(change.target_module)
            if runtime is None:
                raise LookupError(f"No Target Module runtime is registered for '{change.target_module}'.")
            try:
                runtime.apply(change, context)
            except (PreconditionFailed, ObjectPermissionDenied, ValidationError, DatabaseError) as exc:
                raise _ExecutionFailed(
                    cause=exc,
                    reason=cls._failure_reason(exc),
                    failed_change=change.identity,
                    rolled_back=completed,
                    not_attempted=[later.identity for later in changes[index + 1 :]],
                ) from exc
            completed.append(change.identity)
            if progress_callback is not None:
                progress_callback(len(units) + index + 1, total)
        execution.mark_succeeded(applied_changes={"changes": completed, "deleted": []})

    @staticmethod
    def _failure_reason(exc) -> str:
        """Return the typed audit reason for one failure a Target Module can raise."""
        if isinstance(exc, StalePlan):
            return FailureReason.STALE_PLAN
        if isinstance(exc, SelectionError):
            return FailureReason.SELECTION
        if isinstance(exc, (StaleSourceDocument, PlanningTargetUnavailable)):
            return FailureReason.PLANNING
        if isinstance(exc, PreconditionFailed):
            return FailureReason.PRECONDITION
        if isinstance(exc, ObjectPermissionDenied):
            return FailureReason.PERMISSION
        if isinstance(exc, ValidationError):
            return FailureReason.VALIDATION
        if isinstance(exc, DatabaseError):
            return FailureReason.DATABASE
        return FailureReason.PLANNING

    @staticmethod
    def _selected_units(accepted, current, selected_identities):
        """Return the current actionable units whose accepted fingerprints still match."""
        if len(set(selected_identities)) != len(selected_identities):
            raise SelectionError("A Synchronization Unit can be selected only once.")
        selected = []
        for identity in selected_identities:
            accepted_unit = accepted.unit(identity)
            current_unit = current.unit(identity)
            if accepted_unit is None or current_unit is None:
                raise SelectionError(f"The current Import Plan does not carry selected unit '{identity}'.")
            if accepted_unit.disposition != Disposition.ACTIONABLE:
                raise SelectionError(f"Synchronization Unit '{identity}' was not actionable when selected.")
            if current_unit.disposition != Disposition.ACTIONABLE:
                raise StalePlan(f"Synchronization Unit '{identity}' is no longer actionable.")
            if accepted.unit_fingerprint(identity) != current.unit_fingerprint(identity):
                raise StalePlan(f"Synchronization Unit '{identity}' changed after the plan was accepted.")
            selected.append(current_unit)
        return tuple(selected)

    @staticmethod
    def _selected_change_identities(plan, selected_identities) -> list[str]:
        """Return unique Planned Change identities carried by the selected units."""
        identities = []
        for unit_identity in selected_identities:
            unit = plan.unit(unit_identity)
            if unit is None:
                continue
            for change in unit.changes:
                if change.identity not in identities:
                    identities.append(change.identity)
        return identities

    @staticmethod
    def _mark_failed(execution, **detail) -> None:
        """Finish a failed row unless a concurrent finisher already chose its outcome."""
        try:
            execution.mark_failed(**detail)
        except ValueError:
            pass

    @staticmethod
    def _stored_source(profile, source_document) -> SourceDocument:
        """Return the stored bytes this profile may plan, refusing a reference it does not own."""
        document = SourceDocument.objects.filter(pk=source_document.pk).first()
        if document is None:
            raise StaleSourceDocument("The stored source no longer exists. Upload it again.")
        if document.profile_id != profile.pk:
            raise StaleSourceDocument(
                f"Source document {document.pk} belongs to another Import Profile, so this one cannot plan it."
            )
        return document

    @staticmethod
    def _source_diagnostic(diagnostic) -> Diagnostic:
        """Return one adapter diagnostic in the plan vocabulary, under the adapter's own namespace.

        The code passes through: a Source Adapter names its own domain, and prefixing a second one
        would push a namespaced code past what a plan diagnostic code accepts.
        """
        display = {"message": diagnostic.message}
        if diagnostic.row_number is not None:
            display["row_number"] = diagnostic.row_number
        return Diagnostic(code=diagnostic.code, severity=Severity.WARNING, display=display)

    @staticmethod
    def _unused_column_diagnostic(name, stats) -> Diagnostic:
        """Carry one unmapped source column as display-only review information."""
        return Diagnostic(
            code="flat_workbook.unused_column",
            severity=Severity.INFO,
            display={
                "name": str(name),
                "count": int((stats or {}).get("count", 0)),
                "samples": [str(value) for value in (stats or {}).get("samples", ())],
            },
        )


__all__ = ("ImportEngine", "PreconditionFailed", "SelectionError", "StalePlan", "StaleSourceDocument")

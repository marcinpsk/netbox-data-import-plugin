# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The target-neutral Import Engine coordinator.

Section 2.3 gives the coordinator the merged dependency graph, transaction scope, idempotency and
audit. It reaches Target Modules and Source Adapters through their registries alone, so it names no
workbook, no column and no NetBox object type.
"""

from __future__ import annotations

from . import adapters, catalog, target_modules
from .models import SourceDocument
from .netbox_reader import NetBoxReader
from .plan import Diagnostic, ImportPlan, Severity, executable_units, merge_changes
from .source_resolution import derive_effective_rows


_RESOLUTION_SECTION = "source_resolutions"


def _resolution_section():
    """Return the policy section that declares where a saved Source Resolution applies."""
    section = catalog.policy_section(_RESOLUTION_SECTION)
    if section is None:
        raise LookupError(f"The catalog declares no '{_RESOLUTION_SECTION}' policy section.")
    return section


class StaleSourceDocument(Exception):
    """The referenced stored source no longer exists, so the operator has to upload it again."""


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
        source_batch = adapter.interpret(bytes(document.content), adapter.config_for(profile))
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
            diagnostics=tuple(cls._source_diagnostic(item) for item in source_batch.diagnostics),
            source_fingerprint=document.content_fingerprint,
            profile_fingerprint=profile.planning_fingerprint,
            actor=actor.username,
            planning_context=planning_context,
        )

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


__all__ = ("ImportEngine", "StaleSourceDocument")

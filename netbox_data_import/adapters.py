# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Source Adapter registry.

The registry is a static in-plugin mapping from a stable adapter key to the adapter class. Forms,
REST, GraphQL, and YAML derive their choices from it. There is no third-party extension point. An
adapter is offered as a choice only once the catalog declares a Target Module that consumes it.

An adapter declares source interpretation only, so this module imports no NetBox model and no Target
Module implementation. The configuration form is imported lazily because it validates references at
the NetBox boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import OutputKind, has_implemented_module


class UnknownSourceAdapter(Exception):
    """A profile names a Source Adapter key this release does not register."""


class SourceUnreadable(Exception):
    """The source document, or the configuration naming its shape, cannot produce rows."""


@dataclass(frozen=True)
class SourceDiagnostic:
    """One thing the adapter could not interpret, reported without failing the batch."""

    code: str
    message: str
    row_number: int | None = None


@dataclass(frozen=True)
class SourceBatch:
    """The typed source items and source diagnostics from one file (section 1)."""

    output_kinds: frozenset[str]
    rows: tuple[dict, ...] = ()
    diagnostics: tuple[SourceDiagnostic, ...] = ()
    unused_columns: dict[str, dict] = field(default_factory=dict)


class SourceAdapter:
    """Base class for a Source Adapter declaration."""

    key: str = ""
    label: str = ""
    output_kinds: frozenset[str] = frozenset()

    @classmethod
    def config_form_class(cls):
        """Return the Django form that validates this adapter's ``adapter_config``."""
        raise NotImplementedError

    @classmethod
    def interpret(cls, source_document, adapter_config, *, collect_unused: bool = False) -> SourceBatch:
        """Return the Source Batch one source document carries under *adapter_config*."""
        raise NotImplementedError


class FlatWorkbookAdapter(SourceAdapter):
    """One flat worksheet whose rows describe devices and racks."""

    key = "flat_workbook"
    label = "Flat workbook"
    output_kinds = frozenset({OutputKind.DEVICE_SOURCE_ROW, OutputKind.RACK_SOURCE_ROW})

    @classmethod
    def config_form_class(cls):
        """Return the flat-workbook configuration form."""
        from .adapter_forms import FlatWorkbookConfigForm

        return FlatWorkbookConfigForm

    @classmethod
    def interpret(cls, source_document, adapter_config, *, collect_unused: bool = False) -> SourceBatch:
        """Interpret workbook bytes under a `FlatWorkbookConfig`."""
        from . import flat_workbook

        rows, unused = flat_workbook.interpret(source_document, adapter_config, collect_unused=collect_unused)
        return SourceBatch(output_kinds=cls.output_kinds, rows=tuple(rows), unused_columns=unused)


class TraceWorkbookAdapter(SourceAdapter):
    """A cable-trace workbook whose sheet names are fixed by the Source Trace model."""

    key = "trace_workbook"
    label = "Trace workbook"
    output_kinds = frozenset({OutputKind.SOURCE_TRACE})

    @classmethod
    def config_form_class(cls):
        """Return the trace-workbook configuration form, which declares no settings."""
        from .adapter_forms import TraceWorkbookConfigForm

        return TraceWorkbookConfigForm


ADAPTERS: tuple[type[SourceAdapter], ...] = (FlatWorkbookAdapter, TraceWorkbookAdapter)

_ADAPTERS_BY_KEY = {adapter.key: adapter for adapter in ADAPTERS}

DEFAULT_ADAPTER_KEY = FlatWorkbookAdapter.key


def get_adapter(key: str) -> type[SourceAdapter] | None:
    """Return the adapter class registered under *key*, or None."""
    return _ADAPTERS_BY_KEY.get(key)


def adapter_choices():
    """Return Django choice pairs for every registered adapter."""
    return [(adapter.key, adapter.label) for adapter in ADAPTERS]


def selectable_adapter_choices():
    """Return choice pairs for the adapters a Target Module in this release can consume."""
    return [(adapter.key, adapter.label) for adapter in ADAPTERS if has_implemented_module(adapter.output_kinds)]


def output_kinds_for(key: str) -> frozenset[str]:
    """Return the output kinds the adapter registered under *key* emits."""
    adapter = get_adapter(key)
    return adapter.output_kinds if adapter is not None else frozenset()


__all__ = (
    "ADAPTERS",
    "DEFAULT_ADAPTER_KEY",
    "FlatWorkbookAdapter",
    "SourceAdapter",
    "SourceBatch",
    "SourceDiagnostic",
    "SourceUnreadable",
    "TraceWorkbookAdapter",
    "UnknownSourceAdapter",
    "adapter_choices",
    "get_adapter",
    "output_kinds_for",
    "selectable_adapter_choices",
)

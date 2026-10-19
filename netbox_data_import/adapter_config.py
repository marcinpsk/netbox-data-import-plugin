# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Project stored Import Profile policy and preview decisions into plain Source Adapter configuration."""

from collections.abc import Mapping

from .adapters import FlatWorkbookAdapter, TraceWorkbookAdapter, UnknownSourceAdapter
from .field_keys import port_class_mapping_errors
from .flat_workbook import FlatWorkbookConfig, TransformRule
from .plan import PlanError
from .trace_workbook import TraceWorkbookConfig

#: The planning-context key of the PortClass map one preview decides for itself.
PREVIEW_PORT_CLASS_MAP = "port_class_map"


def preview_port_class_map(planning_context: Mapping) -> dict[str, str]:
    """Return the PortClass map a planning context carries, refusing one that maps a label it may not."""
    stated = planning_context.get(PREVIEW_PORT_CLASS_MAP, {})
    if not isinstance(stated, Mapping) or any(
        not isinstance(label, str) or not isinstance(port_class, str) or port_class_mapping_errors(label, port_class)
        for label, port_class in stated.items()
    ):
        raise PlanError("The preview PortClass map is invalid.")
    return dict(stated)


def _flat_workbook_config(profile, planning_context) -> FlatWorkbookConfig:
    """Return the detached configuration for one flat-workbook profile."""
    del planning_context
    return FlatWorkbookConfig(
        sheet_name=profile.adapter_settings.sheet_name,
        column_map={field: tuple(columns) for field, columns in profile.grouped_column_map().items()},
        transform_rules=tuple(
            TransformRule(
                source_column=rule.source_column,
                pattern=rule.pattern,
                group_1_target=rule.group_1_target or "",
                group_2_target=rule.group_2_target or "",
            )
            for rule in profile.column_transform_rules.all()
        ),
        capture_extra_data=profile.adapter_settings.capture_extra_data,
    )


def _trace_workbook_config(profile, planning_context) -> TraceWorkbookConfig:
    """Return the trace-workbook configuration: the profile PortClass map, then the preview's own over it."""
    port_class_map = {row.source_label: row.port_class for row in profile.port_class_mappings.all()}
    port_class_map.update(preview_port_class_map(planning_context))
    return TraceWorkbookConfig(port_class_map=port_class_map)


_CONFIG_PROJECTORS = {
    FlatWorkbookAdapter.key: _flat_workbook_config,
    TraceWorkbookAdapter.key: _trace_workbook_config,
}


def interpreter_config_for(profile, planning_context: Mapping):
    """Return the detached interpreter configuration for an Import Profile and one planning context."""
    projector = _CONFIG_PROJECTORS.get(profile.source_adapter)
    if projector is None:
        raise UnknownSourceAdapter(
            f"No interpreter configuration projector is registered for '{profile.source_adapter}'."
        )
    return projector(profile, planning_context)


__all__ = ("PREVIEW_PORT_CLASS_MAP", "interpreter_config_for", "preview_port_class_map")

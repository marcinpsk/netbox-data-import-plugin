# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Project stored Import Profile policy into plain Source Adapter configuration."""

from .adapters import FlatWorkbookAdapter
from .flat_workbook import FlatWorkbookConfig, TransformRule


def _flat_workbook_config(profile) -> FlatWorkbookConfig:
    """Return the detached configuration for one flat-workbook profile."""
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


_CONFIG_PROJECTORS = {FlatWorkbookAdapter.key: _flat_workbook_config}


def interpreter_config_for(profile):
    """Return the detached interpreter configuration for an Import Profile."""
    projector = _CONFIG_PROJECTORS.get(profile.source_adapter)
    if projector is None:
        raise LookupError(f"No interpreter configuration projector is registered for '{profile.source_adapter}'.")
    return projector(profile)


__all__ = ("interpreter_config_for",)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Value comparison and translation shared by source interpretation and target planning.

Two source columns agreeing, and a source value matching what NetBox already holds, are the same
question asked twice. Keeping one implementation is what stops the two answers drifting. The same
holds for what a source word means: `Back` and `Rear` are one NetBox face, wherever they are read.
"""

from __future__ import annotations

NUMERIC_TARGET_FIELDS: frozenset[str] = frozenset({"u_position", "u_height"})

# A spreadsheet exports an empty cell as one of these words, and no caller wants the literal text.
_NONE_LIKE = frozenset({"none", "nan", "null", "n/a", "#n/a"})


def source_text(value) -> str:
    """Return a source value as stripped text, with a null-like word read as empty."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NONE_LIKE else text


def normalize_for_compare(value) -> str:
    """Normalize a value for field-diff comparison.

    Whole-number floats (e.g. 35.0, "35.0") are normalized to their integer string form ("35") to
    avoid false diffs caused by type differences between the source file and what NetBox returns.
    """
    if value is None:
        return ""
    try:
        number = float(value)
        if number == int(number):
            return str(int(number))
        return str(number)
    except (TypeError, ValueError, OverflowError):
        return str(value).strip()


def comparison_key(target_field: str, value) -> str:
    """Return the comparison key for *value* appropriate to the field's kind."""
    if target_field in NUMERIC_TARGET_FIELDS:
        return normalize_for_compare(value)
    return "" if value is None else str(value).strip()


def status_map() -> dict[str, str]:
    """Return the source words that name a NetBox device status, taken from NetBox's own choices."""
    from dcim.choices import DeviceStatusChoices

    return {
        "live": DeviceStatusChoices.STATUS_ACTIVE,
        "production": DeviceStatusChoices.STATUS_ACTIVE,
        "planned": DeviceStatusChoices.STATUS_PLANNED,
        "staged": DeviceStatusChoices.STATUS_STAGED,
        "failed": DeviceStatusChoices.STATUS_FAILED,
        "offline": DeviceStatusChoices.STATUS_OFFLINE,
        "decommissioning": DeviceStatusChoices.STATUS_DECOMMISSIONING,
    }


def translation_maps():
    """Return (side, airflow, status) source-word tables, with the choice values imported lazily."""
    from dcim.choices import DeviceAirflowChoices, DeviceFaceChoices

    side = {
        "front": DeviceFaceChoices.FACE_FRONT,
        "back": DeviceFaceChoices.FACE_REAR,
        "rear": DeviceFaceChoices.FACE_REAR,
    }
    airflow = {
        "front to back": DeviceAirflowChoices.AIRFLOW_FRONT_TO_REAR,
        "back to front": DeviceAirflowChoices.AIRFLOW_REAR_TO_FRONT,
        "passive": DeviceAirflowChoices.AIRFLOW_PASSIVE,
    }
    return side, airflow, status_map()


__all__ = (
    "NUMERIC_TARGET_FIELDS",
    "comparison_key",
    "source_text",
    "status_map",
    "normalize_for_compare",
    "translation_maps",
)

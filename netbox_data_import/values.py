# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Value comparison shared by source interpretation and target planning.

Two source columns agreeing, and a source value matching what NetBox already holds, are the same
question asked twice. Keeping one implementation is what stops the two answers drifting.
"""

from __future__ import annotations

NUMERIC_TARGET_FIELDS: frozenset[str] = frozenset({"u_position", "u_height"})


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


__all__ = ("NUMERIC_TARGET_FIELDS", "comparison_key", "normalize_for_compare")

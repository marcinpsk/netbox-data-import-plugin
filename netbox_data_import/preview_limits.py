# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shared startup and runtime validation of the preview storage limit."""

from typing import Any

MAX_PLAN_BYTES = 64 * 1024 * 1024


class InvalidPreviewConfiguration(ValueError):
    """The deployment's preview storage limit cannot be used."""


def preview_plan_byte_limit(value: Any = MAX_PLAN_BYTES) -> int:
    """Return a positive byte limit within the coordinator's storage ceiling."""
    if type(value) is not int or not 1 <= value <= MAX_PLAN_BYTES:
        raise InvalidPreviewConfiguration(f"preview_max_plan_bytes must be an integer from 1 to {MAX_PLAN_BYTES}.")
    return value

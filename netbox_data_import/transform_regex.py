# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Compile transform patterns with the safe regular-expression engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

import re2  # type: ignore[import-untyped]


class TransformPatternError(ValueError):
    """A transform pattern uses invalid or unsupported syntax."""


@dataclass(frozen=True)
class TransformPattern:
    """A compiled transform pattern with engine details kept private."""

    _compiled: Any

    @classmethod
    def compile(cls, pattern: str) -> Self:
        """Compile one pattern without writing parser errors to the server log."""
        options = re2.Options()
        options.log_errors = False
        try:
            return cls(re2.compile(pattern, options=options))
        except re2.error as exc:
            detail = exc.args[0]
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            raise TransformPatternError(str(detail)) from exc

    @property
    def group_count(self) -> int:
        """Return the number of capturing groups in the pattern."""
        return int(self._compiled.groups)

    def capture_groups(self, text: str) -> tuple[str | None, ...] | None:
        """Return captured values for a full match, or None when the text does not match."""
        match = self._compiled.fullmatch(text)
        if match is None:
            return None
        return tuple(match.groups())

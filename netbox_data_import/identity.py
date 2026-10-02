# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The one name identity: Python builds keys with it, and PostgreSQL compares stored names with it.

Both sides map one explicit whitespace set to a space, collapse each run of spaces to one, trim the
ends, and then apply the full Unicode uppercase mapping. PostgreSQL uppercases under NetBox's
`natural_sort` ICU collation, whose locale is the root locale, so no language tailors the mapping.
No side applies NFC or NFKC. `test_identity` runs every storable code point through both sides.
A Source Adapter imports this module, so Django loads only inside the database helpers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# U+0009-000D, U+001C-0020, U+0085, U+00A0, U+1680, U+2000-200A, U+2028, U+2029, U+202F, U+205F, U+3000.
WHITESPACE = "".join(
    map(
        chr,
        (
            *range(0x09, 0x0E),
            *range(0x1C, 0x21),
            0x85,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        ),
    )
)
# NetBox creates this collation with the ICU root locale, `und-u-kn-true`, for its own name columns.
IDENTITY_COLLATION = "natural_sort"
CANONICAL_NAME = "_ndi_canonical_name"
# A bracket expression of literal characters, which Python and PostgreSQL read the same way under any collation.
WHITESPACE_RUN = f"[{WHITESPACE}]+"

_WHITESPACE_RUN = re.compile(WHITESPACE_RUN)


def identity_text(value: str) -> str:
    """Return the comparison key of one name."""
    return _WHITESPACE_RUN.sub(" ", value).strip(" ").upper()


def identity_expression(expression):
    """Return the ORM expression that computes `identity_text` of one text value inside the database."""
    from django.db.models import Func, TextField, Value
    from django.db.models.functions import Cast, Collate, Upper

    # The bytewise collation keeps the input column's own collation out of every step before UPPER.
    text = Collate(Cast(expression, TextField()), "C")
    spaced = Func(
        text, Value(WHITESPACE_RUN), Value(" "), Value("g"), function="REGEXP_REPLACE", output_field=TextField()
    )
    trimmed = Func(spaced, Value(" "), function="BTRIM", output_field=TextField())
    return Upper(Collate(trimmed, IDENTITY_COLLATION))


def identity_in(field: str, keys: Iterable[str]):
    """Return a filter that keeps the rows whose *field* has one of these identity keys."""
    from django.db.models import F
    from django.db.models.lookups import In

    return In(identity_expression(F(field)), sorted(set(keys)))


def with_name_identity(queryset):
    """Annotate each row with the identity of its name, under `CANONICAL_NAME`."""
    from django.db.models import F

    return queryset.annotate(**{CANONICAL_NAME: identity_expression(F("name"))})


def matching_search(queryset, search: str):
    """Keep the named rows whose identity contains the identity of *search*; a blank search keeps every row."""
    from django.db.models import F
    from django.db.models.lookups import Contains

    wanted = identity_text(search)
    if not wanted:
        return queryset
    return queryset.filter(Contains(identity_expression(F("name")), wanted))


__all__ = (
    "CANONICAL_NAME",
    "IDENTITY_COLLATION",
    "WHITESPACE",
    "WHITESPACE_RUN",
    "identity_expression",
    "identity_in",
    "identity_text",
    "matching_search",
    "with_name_identity",
)

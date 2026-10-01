# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The one PostgreSQL identity expression for target names, and the search rule built on it.

Python `casefold` and SQL `UPPER` disagree on some characters, so both sides of a comparison are
computed in the database. `UPPER` follows the collation of its input, and a NetBox name column and a
query parameter have different collations, so the expression names one collation for both sides.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import connection
from django.db.models import CharField, F, Func
from django.db.models.lookups import Contains

CANONICAL_NAME = "_ndi_canonical_name"
# NetBox creates this ICU collation for its name columns, so most stored identities keep their value.
IDENTITY_COLLATION = "natural_sort"


def _database_identity_sql(expression: str) -> tuple[str, tuple[str, str, str]]:
    """Return the one SQL expression used for every target identity comparison."""
    return (
        f'UPPER(TRIM(REGEXP_REPLACE({expression}, %s, %s, %s)) COLLATE "{IDENTITY_COLLATION}")',
        (r"\s+", " ", "g"),
    )


class DatabaseIdentity(Func):
    """Apply the shared target identity expression to one ORM value."""

    arity = 1

    def __init__(self, expression):
        super().__init__(expression, output_field=CharField())

    def as_sql(self, compiler, connection, **extra_context):
        """Wrap the compiled value in the shared identity expression."""
        expression_sql, expression_params = compiler.compile(self.source_expressions[0])
        sql, identity_params = _database_identity_sql(expression_sql)
        return sql, (*expression_params, *identity_params)


def with_database_identity(queryset):
    """Annotate rows that have a name field with their shared target identity."""
    return queryset.annotate(**{CANONICAL_NAME: DatabaseIdentity(F("name"))})


def database_identities(values: Iterable[str]) -> dict[str, str]:
    """Return PostgreSQL's whitespace-insensitive case key for each text value."""
    unique_values = sorted(set(values))
    if not unique_values:
        return {}
    identity_sql, identity_params = _database_identity_sql("source_value")
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT source_value, {identity_sql} FROM unnest(%s::text[]) AS source_value",  # noqa: S608 - The SQL fragment is fixed; values use query parameters.
            [*identity_params, unique_values],
        )
        return dict(cursor.fetchall())


def search_identity(search: str) -> str:
    """Return the identity of picker search text, or an empty string when the text is blank."""
    text = search.strip()
    return database_identities((text,))[text] if text else ""


def matching_search(queryset, wanted: str):
    """Keep the named rows whose identity contains *wanted*, so a normalized exact match is never lost."""
    if not wanted:
        return queryset
    return queryset.filter(Contains(DatabaseIdentity(F("name")), wanted))


__all__ = (
    "CANONICAL_NAME",
    "DatabaseIdentity",
    "database_identities",
    "matching_search",
    "search_identity",
    "with_database_identity",
)

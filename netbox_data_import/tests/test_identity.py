# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The name identity gives one answer in Python and in PostgreSQL, for every value PostgreSQL can store.

A Python or ICU upgrade that changes one uppercase mapping on one side only must fail this module.
"""

import itertools
import json
import sys
import unicodedata
from pathlib import Path

from django.db import connection
from django.db.models import F, TextField
from django.db.models.expressions import RawSQL
from django.db.models.sql import Query
from django.test import SimpleTestCase, TestCase

from netbox_data_import.identity import (
    IDENTITY_COLLATION,
    WHITESPACE,
    identity_expression,
    identity_in,
    identity_text,
)
from netbox_data_import.tests.helpers import make_dcim_objects

# PostgreSQL text holds every Unicode scalar value except NUL.
STORABLE = tuple(chr(code) for code in range(1, 0x110000) if not 0xD800 <= code <= 0xDFFF)
BATCH = 65536
# A NetBox name column, a bytewise column and a column with the database default collation.
PROBE_COLUMNS = {"name_value": f'COLLATE "{IDENTITY_COLLATION}"', "bytewise_value": 'COLLATE "C"', "default_value": ""}
EXPANSIONS = tuple(character for character in STORABLE if len(character.upper()) > 1)
NAMED_CASES = (
    "Straße",
    "STRAẞE",
    "straẞe",
    "STRASSE",
    "Kelvin \u212a",
    "\u212bngstr\u00f6m \u00c5",
    "Ohm \u2126 \u03a9",
    "ϴ θ Θ",
    "\u0130stanbul",
    "istanbul",
    "\u0131i",
    "i\u0307",
    "ΣΑΣ σας ς",
    "ΐ ᾳ ᾼ ἀι",
    "ﬃ ﬀ ﬁ ﬅ",
    "ŉ ǰ ǅ ǈ",
    "long \u017f",
    "µ μ",
    "\uff21\uff22\uff23 \uff41\uff42\uff43",
    "𐐨𐐐",
    "e\u0301 é",
    "ƛ ɤ",
    "none",
    "N/A",
    "#N/A",
    "nan",
    "null",
    "100%",
    "a_b",
    "a\\b",
    "%_\\",
    "eth0",
    "Ethernet 1/1",
    "",
)


def _whitespace_cases() -> tuple[str, ...]:
    """Return each whitespace character at the start, between two words, in a run, at the end, and alone."""
    cases = []
    for space in WHITESPACE:
        cases += [f"{space}x", f"x{space}y", f"x{space}{space}y", f"x {space} y", f"x{space}", space, space * 3]
    return (*cases, "".join(WHITESPACE), f"x{WHITESPACE}y", "a\t \n\u3000b\u2028\u2029c")


COMPOSITES = (
    *NAMED_CASES,
    *_whitespace_cases(),
    *(f"x{character}y" for character in EXPANSIONS),
    *(f"{character}{character}" for character in EXPANSIONS),
)


def _described(text: str) -> str:
    """Name each code point of *text*, so a failure says which character drifted."""
    return " ".join(f"U+{ord(character):04X} {unicodedata.name(character, '?')}" for character in text)


def identity_sql(column: str) -> tuple[str, tuple]:
    """Compile the ORM identity expression around one raw SQL column reference."""
    query = Query(None)
    expression = identity_expression(RawSQL(column, (), output_field=TextField()))  # noqa: S611 - fixed column names
    expression = expression.resolve_expression(query)
    return query.get_compiler(connection=connection).compile(expression)


def _unicode_versions() -> str:
    """Name the Unicode data of both sides, so a drift failure says which side moved."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_collation_actual_version(oid) FROM pg_collation WHERE collname = %s", [IDENTITY_COLLATION]
        )
        (collator,) = cursor.fetchone()
        # PostgreSQL 17 added icu_unicode_version(); an older server cannot state it.
        cursor.execute("SELECT to_regproc('pg_catalog.icu_unicode_version') IS NOT NULL")
        (stated,) = cursor.fetchone()
        icu_unicode = "unknown on this PostgreSQL version"
        if stated:
            cursor.execute("SELECT icu_unicode_version()")
            (icu_unicode,) = cursor.fetchone()
    return (
        f"Python {sys.version.split()[0]} states Unicode {unicodedata.unidata_version}; the database ICU states "
        f"Unicode {icu_unicode} (collator version {collator}). Two different versions mean that one side has newer case data."
    )


def _batches(values):
    """Yield *values* in fixed-size chunks."""
    iterator = iter(values)
    while chunk := tuple(itertools.islice(iterator, BATCH)):
        yield chunk


class IdentityTextTest(SimpleTestCase):
    """The Python side on its own."""

    def test_keys_are_idempotent_for_every_storable_code_point_and_composite(self):
        drifted = [
            value for value in (*STORABLE, *COMPOSITES) if identity_text(identity_text(value)) != identity_text(value)
        ]

        self.assertEqual([_described(value) for value in drifted], [])

    def test_known_keys(self):
        """Fixed answers that do not come from the implementation under test."""
        cases = {
            "  Ethernet\u00a01/1\t": "ETHERNET 1/1",
            "Straße": "STRASSE",
            "STRAẞE": "STRAẞE",
            "\u0131": "I",
            "\u0130": "\u0130",
            "\u212a": "\u212a",
            "ﬃ": "FFI",
            "ς": "Σ",
            "none": "NONE",
            "N/A": "N/A",
            "a\x1c\x1db": "A B",
            "e\u0301": "E\u0301",
            "": "",
        }

        self.assertEqual({value: identity_text(value) for value in cases}, cases)


JS_CORPUS = Path(__file__).resolve().parent / "js" / "identity_corpus.json"


class JavaScriptIdentityCorpusTest(SimpleTestCase):
    """The split modal compares names in the browser, and vitest checks its identity against this corpus."""

    def test_the_corpus_states_the_python_key_of_every_composite(self):
        expected = [[value, identity_text(value)] for value in COMPOSITES]

        self.assertEqual(
            json.loads(JS_CORPUS.read_text(encoding="utf-8")),
            expected,
            "Write [[value, identity_text(value)] for value in COMPOSITES] to tests/js/identity_corpus.json.",
        )


class IdentityAgreementTest(TestCase):
    """Python keys and the PostgreSQL expression agree on stored columns and on parameters."""

    def disagreements(self, values) -> list[str]:
        """Return each value whose Python key differs from the database identity of a column or a parameter."""
        columns = ", ".join(f"{name} text {collation}" for name, collation in PROBE_COLUMNS.items())
        found = []
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TEMPORARY TABLE ndi_identity_probe ({columns}, expected text)")
            for chunk in _batches(values):
                cursor.execute(
                    "INSERT INTO ndi_identity_probe SELECT value, value, value, expected "
                    "FROM unnest(%s::text[], %s::text[]) AS source(value, expected)",
                    [list(chunk), [identity_text(value) for value in chunk]],
                )
                parameter_sql, parameter_params = identity_sql("source.value")
                cursor.execute(
                    f"SELECT source.value FROM unnest(%s::text[], %s::text[]) AS source(value, expected) "  # noqa: S608 - fixed SQL
                    f'WHERE ({parameter_sql}) COLLATE "C" IS DISTINCT FROM source.expected COLLATE "C"',
                    [list(chunk), [identity_text(value) for value in chunk], *parameter_params],
                )
                found += [f"parameter: {_described(row[0])}" for row in cursor.fetchall()]
            for column in (*PROBE_COLUMNS, "expected"):
                column_sql, column_params = identity_sql(f"probe.{column}")
                cursor.execute(
                    f"SELECT probe.name_value FROM ndi_identity_probe AS probe "  # noqa: S608 - fixed SQL
                    f'WHERE ({column_sql}) COLLATE "C" IS DISTINCT FROM probe.expected COLLATE "C"',
                    column_params,
                )
                found += [f"{column} column: {_described(row[0])}" for row in cursor.fetchall()]
            cursor.execute("DROP TABLE ndi_identity_probe")
        return found

    def test_every_storable_code_point_agrees(self):
        found = self.disagreements(STORABLE)

        self.assertEqual(found[:40], [], f"{len(found)} disagreements. {_unicode_versions()}")

    def test_composites_and_whitespace_placements_agree(self):
        self.assertEqual(self.disagreements(COMPOSITES), [], _unicode_versions())

    def test_the_identity_collation_is_icu_with_the_root_locale(self):
        """A language tailoring, such as Turkish dotted I, would change the uppercase mapping."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_jsonb(c) FROM pg_collation AS c WHERE collname = %s", [IDENTITY_COLLATION])
            (collation,) = cursor.fetchone()
        if isinstance(collation, str):
            collation = json.loads(collation)
        # PostgreSQL 17 names the column colllocale, 15 and 16 colliculocale, and 14 keeps it in collcollate.
        locale = collation.get("colllocale") or collation.get("colliculocale") or collation["collcollate"]

        self.assertEqual((collation["collprovider"], locale.split("-")[0]), ("i", "und"))


class IdentityOrmTest(TestCase):
    """The ORM expression on a real NetBox name column finds each name by its Python key."""

    def test_each_composite_name_is_found_by_its_python_key(self):
        from dcim.models import Device, Interface

        site, _manufacturer, device_type, role = make_dcim_objects("Identity")
        device = Device.objects.create(name="identity-probe", site=site, device_type=device_type, role=role)
        names = [name for name in dict.fromkeys(value[:64] for value in COMPOSITES) if name]
        Interface.objects.bulk_create(Interface(device=device, name=name, type="virtual") for name in names)
        interfaces = Interface.objects.filter(device=device)

        annotated = dict(interfaces.annotate(key=identity_expression(F("name"))).values_list("name", "key"))
        missed = [
            name
            for name in names
            if not interfaces.filter(identity_in("name", [identity_text(name)])).filter(name=name).exists()
        ]

        self.assertEqual({name: key for name, key in annotated.items() if key != identity_text(name)}, {})
        self.assertEqual(missed, [])

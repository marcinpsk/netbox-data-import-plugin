# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Rekey every stored name identity from the casefold key to the uppercase key.

A stored key holds casefolded text whose whitespace is already one ASCII space per run, so the new
key of that text is its uppercase. That is exact for every name without the capital sharp s
(U+1E9E), the Kelvin, Angstrom or Ohm sign, the theta symbol (U+03F4) or the dotted capital I
(U+0130), which casefold to other letters, and the dotless i (U+0131), which now shares the key of i.

Each table is read in pages of BATCH_SIZE rows and staged in a temporary table, which groups the
collisions in the database, so the migration never holds a whole table in memory.
"""

import json
import logging

from django.db import migrations

# A netbox-branching branch migrate fakes this migration: see D7 in docs/design/netbox-branching.md.
fake_on_branch = True

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
# The pg_temp schema keeps every create, drop and write away from a permanent table of the same name.
STAGE = "pg_temp.netbox_data_import_0045_stage"
GROUPS = "pg_temp.netbox_data_import_0045_groups"
# A trace identity endpoint ends with a claimed kind, which is a protocol value and keeps its case.
CLAIMED_KINDS = frozenset({"interface", "front_port", "rear_port"})
NAME_PARTS = ("cards", "device", "port")
# PostgreSQL computes the digest from the staged key, as `index_digest` does in Python.
DIGEST_SQL = "encode(sha256(convert_to(page.new_key, 'UTF8')), 'hex')"


def _field_key(old):
    """Return the uppercase form of one termination field key, or None when it is not canonical JSON."""
    try:
        data = json.loads(old)
    except ValueError:
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(part), str) for part in NAME_PARTS):
        return None
    return json.dumps(
        {**data, **{part: data[part].upper() for part in NAME_PARTS}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _endpoint(old):
    device, cards, port, kind = old
    return [device.upper(), cards.upper(), port.upper(), kind if kind in CLAIMED_KINDS else kind.upper()]


def _trace_identity(old):
    """Return the new trace identity and whether its endpoint order reversed, or None when it is not canonical."""
    try:
        endpoints = json.loads(old)
    except ValueError:
        return None
    if (
        not isinstance(endpoints, list)
        or len(endpoints) != 2
        or not all(
            isinstance(item, list) and len(item) == 4 and all(isinstance(part, str) for part in item)
            for item in endpoints
        )
    ):
        return None
    rekeyed = [_endpoint(item) for item in endpoints]
    ordered = sorted(rekeyed)
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":")), ordered != rekeyed


def _pages(model, alias, fields):
    """Yield every row of *model* as (pk, *fields) tuples, BATCH_SIZE rows per read, in pk order."""
    rows = model.objects.using(alias).order_by("pk")
    last = None
    while True:
        page = list((rows if last is None else rows.filter(pk__gt=last)).values_list("pk", *fields)[:BATCH_SIZE])
        if page:
            yield page
        if len(page) < BATCH_SIZE:
            return
        last = page[-1][0]


def _staged_pages(cursor):
    """Yield the staged row ids, BATCH_SIZE per page, in id order."""
    last = 0
    while True:
        cursor.execute(f"SELECT id FROM {STAGE} WHERE id > %s ORDER BY id LIMIT %s", [last, BATCH_SIZE])  # noqa: S608 - fixed table name
        ids = [row[0] for row in cursor.fetchall()]
        if ids:
            yield ids
        if len(ids) < BATCH_SIZE:
            return
        last = ids[-1]


def _stage(cursor, rows):
    """Write one page of (id, scope, new key, target, reversed) rows, with the digest of each key, to the stage."""
    cursor.execute(
        f"INSERT INTO {STAGE} (id, scope, new_key, digest, target, reversed) "  # noqa: S608 - fixed table name
        f"SELECT page.id, page.scope, page.new_key, {DIGEST_SQL}, page.target, page.reversed "
        "FROM unnest(%s::bigint[], %s::text[], %s::text[], %s::text[], %s::boolean[]) "
        "AS page(id, scope, new_key, target, reversed)",
        [list(column) for column in zip(*rows, strict=True)],
    )


def _new_stage(cursor):
    cursor.execute(f"DROP TABLE IF EXISTS {STAGE}")
    cursor.execute(
        f"CREATE TEMPORARY TABLE {STAGE} "
        '(id bigint PRIMARY KEY, scope text COLLATE "C" NOT NULL, new_key text COLLATE "C" NOT NULL, '
        'digest text COLLATE "C" NOT NULL, target text COLLATE "C" NOT NULL, reversed boolean NOT NULL)'
    )


def _resolve_collisions(cursor, model, *, quote, merge_all=False, written=None):
    """Merge each group of staged rows that now share a key and a target; drop a group with several targets.

    A merge keeps the lowest id, and a proposal that wrote a removed row points to it. A drop deletes every
    row of the group and clears the link of each proposal that wrote one. PostgreSQL keeps each group's
    members, and the migration reads them one page at a time.
    """
    # A btree entry cannot exceed about 2704 bytes, so the index holds the fixed-width digest of the key.
    cursor.execute(f"CREATE INDEX ON {STAGE} (scope, digest, id)")
    cursor.execute(f"DROP TABLE IF EXISTS {GROUPS}")
    cursor.execute(
        f"CREATE TEMPORARY TABLE {GROUPS} AS "  # noqa: S608 - fixed table names
        "SELECT row_number() OVER (ORDER BY min(id)) AS n, scope, new_key, digest, min(id) AS kept, "
        f"count(DISTINCT target) AS targets FROM {STAGE} GROUP BY scope, new_key, digest HAVING count(*) > 1"
    )
    last = 0
    while True:
        cursor.execute(
            f"SELECT n, scope, new_key, digest, kept, targets FROM {GROUPS} WHERE n > %s ORDER BY n LIMIT %s",  # noqa: S608
            [last, BATCH_SIZE],
        )
        groups = cursor.fetchall()
        for _n, scope, new_key, digest, kept, targets in groups:
            merge = merge_all or targets == 1
            for ids in _members(cursor, scope, new_key, digest, after=kept if merge else 0):
                _remove(cursor, model, ids, kept if merge else None, quote=quote, written=written)
                if merge:
                    logger.warning(
                        "Merged %s rows %s into row %s: one key %r in scope %s now names them, and they chose one "
                        "target.",
                        model.__name__,
                        ids,
                        kept,
                        new_key,
                        scope,
                    )
                else:
                    logger.warning(
                        "Dropped %s rows %s: one key %r in scope %s now names them, and they chose different "
                        "targets. Make the decision again.",
                        model.__name__,
                        ids,
                        new_key,
                        scope,
                    )
        if len(groups) < BATCH_SIZE:
            return
        last = groups[-1][0]


def _members(cursor, scope, new_key, digest, *, after):
    """Yield the staged ids of one collision group above *after*, BATCH_SIZE per page."""
    while True:
        cursor.execute(
            f"SELECT id FROM {STAGE} WHERE scope = %s AND digest = %s AND new_key = %s AND id > %s "  # noqa: S608
            "ORDER BY id LIMIT %s",
            [scope, digest, new_key, after, BATCH_SIZE],
        )
        ids = [row[0] for row in cursor.fetchall()]
        if ids:
            yield ids
        if len(ids) < BATCH_SIZE:
            return
        after = ids[-1]


def _remove(cursor, model, ids, kept, *, quote, written):
    """Delete one page of collision members, after pointing their proposals at *kept* or at no row."""
    if written is not None:
        _relink(cursor, written, ids, kept, quote=quote)
    cursor.execute(f"DELETE FROM {quote(model._meta.db_table)} WHERE id = ANY(%s)", [ids])  # noqa: S608
    cursor.execute(f"DELETE FROM {STAGE} WHERE id = ANY(%s)", [ids])  # noqa: S608 - fixed name


def _relink(cursor, ResolutionProposal, ids, kept, *, quote):
    """Point each proposal that wrote one of *ids* at *kept*, or at no row when *kept* is None."""
    cursor.execute(
        f"UPDATE {quote(ResolutionProposal._meta.db_table)} SET written_resolution_id = %s "  # noqa: S608
        "WHERE written_resolution_id = ANY(%s)",
        [kept, ids],
    )


def _store(cursor, model, *, key_field, digest_field, quote, extra_sql=""):
    """Write the staged keys in two bounded passes, so no row meets a unique digest another row still holds."""
    table = quote(model._meta.db_table)
    key = quote(model._meta.get_field(key_field).column)
    digest = quote(model._meta.get_field(digest_field).column)
    for ids in _staged_pages(cursor):
        cursor.execute(f"UPDATE {table} SET {digest} = 'rekey-' || id WHERE id = ANY(%s)", [ids])  # noqa: S608
    for ids in _staged_pages(cursor):
        cursor.execute(
            f"UPDATE {table} AS stored SET {key} = stage.new_key, {digest} = stage.digest{extra_sql} "  # noqa: S608
            f"FROM {STAGE} AS stage WHERE stored.id = stage.id AND stage.id = ANY(%s)",
            [ids],
        )


def _skip(model, pk, field):
    logger.warning("Kept %s %s unchanged: its %s is not a canonical identity.", model, pk, field)


def _rekey_policy(cursor, model, *, rekey, key_field, digest_field, scope, target, alias, quote, written=None):
    """Rekey one decision model, page by page, and settle the keys that now collide."""
    _new_stage(cursor)
    for page in _pages(model, alias, (key_field, *scope, *target)):
        rows = []
        for pk, old, *values in page:
            new = rekey(old)
            if new is None:
                _skip(model.__name__, pk, key_field)
                new = old
            scope_values, target_values = values[: len(scope)], values[len(scope) :]
            rows.append((pk, json.dumps(scope_values), new, json.dumps(target_values), False))
        _stage(cursor, rows)
    _resolve_collisions(cursor, model, quote=quote, written=written)
    _store(cursor, model, key_field=key_field, digest_field=digest_field, quote=quote)


def _rekey_cable_sources(cursor, CableImportSource, alias, quote):
    """Rekey provenance, and state an unknown segment position when the canonical endpoint order reversed."""
    _new_stage(cursor)
    for page in _pages(CableImportSource, alias, ("trace_identity", "cable_id", "profile_id")):
        rows = []
        for pk, old, cable_id, profile_id in page:
            found = _trace_identity(old)
            if found is None:
                _skip("CableImportSource", pk, "trace_identity")
                found = (old, False)
            rows.append((pk, json.dumps([cable_id, profile_id]), found[0], "", found[1]))
        reversed_ids = [row[0] for row in rows if row[4]]
        if reversed_ids:
            logger.warning(
                "CableImportSource rows %s: the canonical endpoint order of their trace reversed, so their segment "
                "position is now unknown.",
                reversed_ids,
            )
        _stage(cursor, rows)
    _resolve_collisions(cursor, CableImportSource, quote=quote, merge_all=True)
    # A reversed order counts the segments from the other end, and the trace length is not stored.
    _store(
        cursor,
        CableImportSource,
        key_field="trace_identity",
        digest_field="trace_key",
        quote=quote,
        extra_sql=(
            ", segment_index = CASE WHEN stage.reversed THEN NULL ELSE stored.segment_index END"
            ", direction = CASE WHEN NOT stage.reversed THEN stored.direction WHEN stored.direction = 'canonical' "
            "THEN 'reversed' WHEN stored.direction = 'reversed' THEN 'canonical' ELSE stored.direction END"
        ),
    )


def _rekey_segment_overrides(cursor, CableSegmentOverride, alias, quote):
    """Rekey the trace an override was decided from. Its pair key names NetBox objects and keeps its value."""
    table = quote(CableSegmentOverride._meta.db_table)
    for page in _pages(CableSegmentOverride, alias, ("source_trace_identity",)):
        rows = []
        for pk, old in page:
            found = _trace_identity(old)
            if found is None:
                _skip("CableSegmentOverride", pk, "source_trace_identity")
                continue
            rows.append((pk, found[0]))
        if rows:
            cursor.execute(
                f"UPDATE {table} AS stored SET source_trace_identity = new.identity "  # noqa: S608 - quoted name
                "FROM unnest(%s::bigint[], %s::text[]) AS new(id, identity) WHERE stored.id = new.id",
                [list(column) for column in zip(*rows, strict=True)],
            )


def rekey_name_identities(apps, schema_editor):
    """Retire the requests in flight, then rekey every decision and provenance row."""
    alias = schema_editor.connection.alias
    quote = schema_editor.connection.ops.quote_name

    def model(name):
        return apps.get_model("netbox_data_import", name)

    ResolutionProposal = model("ResolutionProposal")
    # An answer would bind to a key and evidence built under the casefold identity.
    ResolutionProposal.objects.using(alias).filter(status__in=("queued", "running")).update(
        status="failed", failure_reason="superseded_request"
    )
    # Every proposal is now terminal and keeps its casefold key, so it answers no current question.
    with schema_editor.connection.cursor() as cursor:
        _rekey_policy(
            cursor,
            model("TerminationResolution"),
            rekey=_field_key,
            key_field="field_key",
            digest_field="field_key_digest",
            scope=("profile_id", "task_type"),
            target=("selected_object_type_id", "selected_object_id"),
            alias=alias,
            quote=quote,
            written=ResolutionProposal,
        )
        _rekey_policy(
            cursor,
            model("TraceDeviceResolution"),
            rekey=str.upper,
            key_field="source_device_key",
            digest_field="source_device_key_digest",
            scope=("profile_id",),
            target=("selected_device_id",),
            alias=alias,
            quote=quote,
        )
        _rekey_policy(
            cursor,
            model("TraceLocationResolution"),
            rekey=str.upper,
            key_field="source_location_key",
            digest_field="source_location_key_digest",
            scope=("profile_id",),
            target=("selected_location_id",),
            alias=alias,
            quote=quote,
        )
        _rekey_cable_sources(cursor, model("CableImportSource"), alias, quote)
        _rekey_segment_overrides(cursor, model("CableSegmentOverride"), alias, quote)
        cursor.execute(f"DROP TABLE IF EXISTS {STAGE}, {GROUPS}")


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0044_cableimportsource_segment_index_unknown"),
    ]

    operations = [
        # No reverse callable: the casefold key of a merged or dropped decision cannot be restored.
        migrations.RunPython(rekey_name_identities),
    ]

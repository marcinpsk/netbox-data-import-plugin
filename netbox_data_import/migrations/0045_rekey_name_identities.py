# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Rekey every stored name identity from the casefold key to the uppercase key.

A stored key holds casefolded text whose whitespace is already one ASCII space per run, so the new
key of that text is its uppercase. That is exact for every name without the capital sharp s
(U+1E9E), the Kelvin, Angstrom or Ohm sign, the theta symbol (U+03F4) or the dotted capital I
(U+0130), which casefold to other letters, and the dotless i (U+0131), which now shares the key of i.
"""

import hashlib
import json
import logging
from collections import defaultdict

from django.db import migrations

# A netbox-branching branch migrate fakes this migration: see D7 in docs/design/netbox-branching.md.
fake_on_branch = True

logger = logging.getLogger(__name__)

# A trace identity endpoint ends with a claimed kind, which is a protocol value and keeps its case.
CLAIMED_KINDS = frozenset({"interface", "front_port", "rear_port"})
NAME_PARTS = ("cards", "device", "port")


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _skip(model, row, field):
    logger.warning("Kept %s %s unchanged: its %s is not a canonical identity.", model, row.pk, field)


def _rekey_policy(model, rows, *, rekey, key_field, digest_field, scope, target, alias, written=None):
    """Rekey one policy model: merge colliding rows with one target, drop colliding rows with several."""
    keyed = {}
    for row in rows:
        new = rekey(getattr(row, key_field))
        if new is None:
            _skip(model.__name__, row, key_field)
            continue
        keyed[row.pk] = (row, new)
    groups = defaultdict(list)
    for row, new in keyed.values():
        groups[(*(getattr(row, field) for field in scope), new)].append(row)
    removed = set()
    for (*scope_values, new), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda row: row.pk)
        others = [row.pk for row in members[1:]]
        if len({tuple(getattr(row, field) for field in target) for row in members}) == 1:
            kept = members[0].pk
            if written is not None:
                written.objects.using(alias).filter(written_resolution_id__in=others).update(written_resolution_id=kept)
            logger.warning(
                "Merged %s rows %s into row %s: one key %r in scope %r now names them, and they chose one target.",
                model.__name__,
                others,
                kept,
                new,
                scope_values,
            )
            removed.update(others)
        else:
            dropped = [row.pk for row in members]
            if written is not None:
                written.objects.using(alias).filter(written_resolution_id__in=dropped).update(
                    written_resolution_id=None
                )
            logger.warning(
                "Dropped %s rows %s: one key %r in scope %r now names them, and they chose different targets. "
                "Make the decision again.",
                model.__name__,
                dropped,
                new,
                scope_values,
            )
            removed.update(dropped)
    model.objects.using(alias).filter(pk__in=removed).delete()
    survivors = [(row, new) for pk, (row, new) in keyed.items() if pk not in removed]
    _store_keys(model, survivors, key_field=key_field, digest_field=digest_field, alias=alias)


def _store_keys(model, rows, *, key_field, digest_field, alias, extra=()):
    """Write each new key and digest in two passes, so no row meets a unique digest another row still holds."""
    for row, _new in rows:
        setattr(row, digest_field, f"rekey-{row.pk}")
    model.objects.using(alias).bulk_update([row for row, _new in rows], [digest_field], batch_size=500)
    for row, new in rows:
        setattr(row, key_field, new)
        setattr(row, digest_field, _digest(new))
    model.objects.using(alias).bulk_update(
        [row for row, _new in rows], [key_field, digest_field, *extra], batch_size=500
    )


def _rekey_cable_sources(CableImportSource, alias):
    """Rekey provenance, and state an unknown segment position when the canonical endpoint order reversed."""
    keyed = []
    for row in CableImportSource.objects.using(alias).order_by("pk"):
        found = _trace_identity(row.trace_identity)
        if found is None:
            _skip("CableImportSource", row, "trace_identity")
            continue
        new, reversed_order = found
        if reversed_order:
            row.segment_index = None
            row.direction = {"canonical": "reversed", "reversed": "canonical"}.get(row.direction, row.direction)
            logger.warning(
                "CableImportSource %s: the canonical endpoint order of its trace reversed, so its segment "
                "position is now unknown.",
                row.pk,
            )
        keyed.append((row, new))
    groups = defaultdict(list)
    for row, new in keyed:
        groups[(row.cable_id, row.profile_id, new)].append(row)
    removed = set()
    for (cable_id, profile_id, new), members in groups.items():
        others = sorted(row.pk for row in members)[1:]
        if others:
            logger.warning(
                "Merged CableImportSource rows %s of Cable %s and profile %s: one trace identity %r now names them.",
                others,
                cable_id,
                profile_id,
                new,
            )
            removed.update(others)
    CableImportSource.objects.using(alias).filter(pk__in=removed).delete()
    _store_keys(
        CableImportSource,
        [(row, new) for row, new in keyed if row.pk not in removed],
        key_field="trace_identity",
        digest_field="trace_key",
        alias=alias,
        extra=("segment_index", "direction"),
    )


def _rekey_segment_overrides(CableSegmentOverride, alias):
    """Rekey the trace an override was decided from. Its pair key names NetBox objects and keeps its value."""
    changed = []
    for row in CableSegmentOverride.objects.using(alias).order_by("pk"):
        found = _trace_identity(row.source_trace_identity)
        if found is None:
            _skip("CableSegmentOverride", row, "source_trace_identity")
            continue
        row.source_trace_identity = found[0]
        changed.append(row)
    CableSegmentOverride.objects.using(alias).bulk_update(changed, ["source_trace_identity"], batch_size=500)


def rekey_name_identities(apps, schema_editor):
    """Retire the requests in flight, then rekey every decision and provenance row."""
    alias = schema_editor.connection.alias

    def model(name):
        return apps.get_model("netbox_data_import", name)

    ResolutionProposal = model("ResolutionProposal")

    # An answer would bind to a key and evidence built under the casefold identity.
    ResolutionProposal.objects.using(alias).filter(status__in=("queued", "running")).update(
        status="failed", failure_reason="superseded_request"
    )
    # Every proposal is now terminal and keeps its casefold key, so it answers no current question.

    TerminationResolution = model("TerminationResolution")
    _rekey_policy(
        TerminationResolution,
        TerminationResolution.objects.using(alias).order_by("pk"),
        rekey=_field_key,
        key_field="field_key",
        digest_field="field_key_digest",
        scope=("profile_id", "task_type"),
        target=("selected_object_type_id", "selected_object_id"),
        alias=alias,
        written=ResolutionProposal,
    )
    TraceDeviceResolution = model("TraceDeviceResolution")
    _rekey_policy(
        TraceDeviceResolution,
        TraceDeviceResolution.objects.using(alias).order_by("pk"),
        rekey=str.upper,
        key_field="source_device_key",
        digest_field="source_device_key_digest",
        scope=("profile_id",),
        target=("selected_device_id",),
        alias=alias,
    )
    TraceLocationResolution = model("TraceLocationResolution")
    _rekey_policy(
        TraceLocationResolution,
        TraceLocationResolution.objects.using(alias).order_by("pk"),
        rekey=str.upper,
        key_field="source_location_key",
        digest_field="source_location_key_digest",
        scope=("profile_id",),
        target=("selected_location_id",),
        alias=alias,
    )
    _rekey_cable_sources(model("CableImportSource"), alias)
    _rekey_segment_overrides(model("CableSegmentOverride"), alias)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0044_cableimportsource_segment_index_unknown"),
    ]

    operations = [
        # No reverse callable: the casefold key of a merged or dropped decision cannot be restored.
        migrations.RunPython(rekey_name_identities),
    ]

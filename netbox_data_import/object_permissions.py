# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shared object-permission checks for import writes.

NetBox grants ``user.has_perm("app.add_thing")`` when the user may act on *any* object of that
type. An ObjectPermission's constraints are only evaluated against a saved instance, so a write
is inside the caller's scope only once the saved object passes ``has_perm(permission, instance)``.

Every scoped write goes through this module. Denial raises rather than returning a flag: a caller
can ignore a False and return from an enclosing ``atomic()`` block, which commits the very write
the denial was meant to prevent.
"""

import logging
from copy import copy
from dataclasses import dataclass
from typing import Any, Literal

from django.core.exceptions import EmptyResultSet, FieldError, ValidationError
from django.db import DatabaseError, IntegrityError, connection, models, transaction
from django.db.models.expressions import Col
from django.db.models.lookups import IsNull
from users.constants import CONSTRAINT_TOKEN_USER
from utilities.permissions import get_permission_for_model, qs_filter_from_constraints


logger = logging.getLogger(__name__)


class ObjectPermissionDenied(Exception):
    """Reject a write outside the caller's NetBox object scope."""


@dataclass(frozen=True)
class PermissionScopedSaveResult:
    """One scoped write: the saved object and whether this call created it."""

    instance: Any
    created: bool


@dataclass(frozen=True)
class PermissionScopedSaveAssessment:
    """Whether one prospective create or update stays inside the actor's object scope."""

    allowed: bool
    permission: str


def _synthetic_primary_key(instance):
    """Return a collision-free primary key without consuming the model's sequence."""
    if instance.pk is not None:
        return instance.pk, False
    field = instance._meta.pk
    if not isinstance(field, (models.AutoField, models.BigAutoField, models.SmallAutoField)):
        raise TypeError(f"Cannot assess an unsaved {instance._meta.label} without a primary key.")
    value = -1
    while instance._meta.model._base_manager.filter(pk=value).exists():
        value -= 1
    return value, True


def _depends_on_generated_root_primary_key(constraint, instance) -> bool:
    """Return whether the root predicate needs an automatic primary key not yet allocated."""
    field = instance._meta.pk
    names = {"pk", field.name, field.attname}
    for key, value in constraint.items():
        parts = key.split("__")
        if parts[0] not in names:
            continue
        suffix = parts[1:]
        if suffix == ["isnull"] or ((not suffix or suffix == ["exact"]) and value is None):
            continue
        return True
    return False


def _related_generated_primary_key_aliases(query, model) -> set[str]:
    """Return the exact related aliases whose unknown primary key must not see the probe."""
    aliases = set()
    pending = [query.where]
    while pending:
        node = pending.pop()
        pending.extend(getattr(node, "children", ()))
        if isinstance(node, IsNull):
            continue
        expression = getattr(node, "lhs", None)
        while expression is not None and not isinstance(expression, Col):
            expression = getattr(expression, "lhs", None)
        if (
            isinstance(expression, Col)
            and expression.alias != query.base_table
            and expression.target.primary_key
            and expression.target.model._meta.concrete_model is model._meta.concrete_model
        ):
            aliases.add(expression.alias)
    return aliases


def _prospective_row_matches(user, model, instance, constraint) -> bool:
    """Evaluate one NetBox constraint arm against a read-only prospective database world."""
    probe = copy(instance)
    probe.pk, generated_primary_key = _synthetic_primary_key(probe)
    if generated_primary_key and _depends_on_generated_root_primary_key(constraint, probe):
        return False
    permission_filter = qs_filter_from_constraints([constraint], {CONSTRAINT_TOKEN_USER: user})
    queryset = model.objects.filter(permission_filter, pk=probe.pk).values_list("pk", flat=True).order_by()
    query = queryset.query.clone()
    physical_aliases = _related_generated_primary_key_aliases(query, model) if generated_primary_key else set()
    candidate_cte = "ndi_permission_candidate"
    world_cte = "ndi_permission_world"
    if model._meta.db_table in (candidate_cte, world_cte):
        raise ValueError("The model table conflicts with an internal permission query name.")
    for alias, join in tuple(query.alias_map.items()):
        if join.table_name != model._meta.db_table:
            continue
        replacement = copy(join)
        if alias == query.base_table:
            replacement.table_name = candidate_cte
        elif alias not in physical_aliases:
            replacement.table_name = world_cte
        query.alias_map[alias] = replacement
    sql, params = query.sql_with_params()
    fields = model._meta.concrete_fields
    field_types = [field.db_type(connection) for field in fields]
    if any(field_type is None for field_type in field_types):
        raise ValueError(f"Cannot assess every concrete field of {model._meta.label}.")
    quote = connection.ops.quote_name
    columns = ", ".join(quote(field.column) for field in fields)
    row = ", ".join(f"CAST(%s AS {field_type})" for field_type in field_types)
    quoted_candidate = quote(candidate_cte)
    quoted_world = quote(world_cte)
    quoted_table = quote(model._meta.db_table)
    quoted_pk = quote(model._meta.pk.column)
    # Model metadata supplies every identifier and type. Values remain query parameters.
    statement = (
        f"WITH {quoted_candidate} ({columns}) AS (VALUES ({row})), "  # noqa: S608
        f"{quoted_world} ({columns}) AS ("
        f"SELECT {columns} FROM {quoted_table} WHERE {quoted_pk} <> %s "
        f"UNION ALL SELECT {columns} FROM {quoted_candidate}) {sql}"
    )
    values = [field.value_from_object(probe) for field in fields]
    with connection.cursor() as cursor:
        cursor.execute(statement, [*values, probe.pk, *params])
        return cursor.fetchone() is not None


def _assess_permission_scoped_save(
    user,
    model,
    lookup: dict,
    values: dict,
    *,
    on_existing: Literal["update", "keep", "reject"],
    current,
) -> PermissionScopedSaveAssessment:
    """Assess one known current row without locking or writing it."""
    if current is not None and on_existing == "reject":
        return PermissionScopedSaveAssessment(False, get_permission_for_model(model, "add"))
    action = "add" if current is None else "view" if on_existing == "keep" else "change"
    permission = get_permission_for_model(model, action)
    if user is None or user.is_superuser:
        return PermissionScopedSaveAssessment(True, permission)
    try:
        if not user.has_perm(permission):
            return PermissionScopedSaveAssessment(False, permission)
        if current is not None and not user.has_perm(permission, current):
            return PermissionScopedSaveAssessment(False, permission)
        if on_existing == "keep":
            return PermissionScopedSaveAssessment(True, permission)
        prospective = model(**lookup, **values) if current is None else copy(current)
        if current is not None:
            for field_name, value in values.items():
                setattr(prospective, field_name, value)
        constraints = getattr(user, "_object_perm_cache", {}).get(permission, ())
        allowed = any(
            not constraint or _prospective_row_matches(user, model, prospective, constraint)
            for constraint in constraints
        )
    except (DatabaseError, EmptyResultSet, FieldError, TypeError, ValueError) as exc:
        logger.warning("Prospective %s permission assessment failed closed: %s", model._meta.label, exc)
        allowed = False
    return PermissionScopedSaveAssessment(allowed, permission)


def assess_permission_scoped_save(
    user,
    model,
    lookup: dict,
    values: dict,
    *,
    on_existing: Literal["update", "keep", "reject"] = "update",
) -> PermissionScopedSaveAssessment:
    """Assess the save against current object constraints without writing or locking."""
    current = model.objects.filter(**lookup).first()
    return _assess_permission_scoped_save(
        user,
        model,
        lookup,
        values,
        on_existing=on_existing,
        current=current,
    )


def enforce_saved_object_permission(obj, user, action):
    """Reject a saved object whose final state is outside the user's scope.

    ``has_perm`` rather than ``restrict()``: this decides one already-saved instance, and it holds
    for any model rather than only those whose manager is a ``RestrictedQuerySet``.
    """
    if user is None:
        return
    permission = get_permission_for_model(type(obj), action)
    if not user.has_perm(permission, obj):
        raise ObjectPermissionDenied(permission)


def reject_overlong_fields(instance, model):
    """Reject a string the column cannot hold, before the database raises DataError.

    Only length is checked. `full_clean` would also refuse a field that is legitimately blank.
    """
    for field in model._meta.concrete_fields:
        max_length = getattr(field, "max_length", None)
        value = getattr(instance, field.attname, None)
        if max_length and isinstance(value, str) and len(value) > max_length:
            raise ValidationError(f"{model._meta.verbose_name} {field.name} cannot exceed {max_length} characters.")


def save_or_refetch(instance, model, lookup):
    """Save *instance*, or refetch a row that won the same concurrent insert."""
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError:
        existing = model.objects.filter(**lookup).first()
        if existing is None:
            raise
        return existing, False
    return instance, True


def _policy_profile_id(model, lookup: dict) -> int | None:
    """Return the profile a policy write belongs to, or None when the model carries no policy."""
    from .models import PolicySectionModel

    if not (isinstance(model, type) and issubclass(model, PolicySectionModel)):
        return None
    profile = lookup.get("profile")
    profile_id = lookup.get("profile_id") if profile is None else profile.pk
    if profile_id is None:
        # A caller that names no profile cannot be serialized against an import, so it is a bug.
        raise ValueError(f"A {model._meta.verbose_name} write must name the profile it belongs to.")
    return profile_id


def save_permission_scoped_object(
    user,
    model,
    lookup: dict,
    values: dict,
    *,
    on_existing: Literal["update", "keep", "reject"] = "update",
) -> PermissionScopedSaveResult:
    """Create or update one object within the user's NetBox permission scope.

    ``on_existing`` decides what an already-present row means: ``update`` writes *values* under the
    change permission, ``keep`` returns it untouched under the view permission, and ``reject``
    refuses it. Raises ``ObjectPermissionDenied`` when any of those checks fails.

    A policy write also holds its import profile, so it cannot commit inside an execution that has
    already replanned against the old policy.
    """
    from .models import locked_profile_policy

    profile_id = _policy_profile_id(model, lookup)
    if profile_id is None:
        return _scoped_write(user, model, lookup, values, on_existing=on_existing)
    with locked_profile_policy(profile_id):
        # atomic-exit-safe: policy-write-committed
        return _scoped_write(user, model, lookup, values, on_existing=on_existing)


def _scoped_write(
    user,
    model,
    lookup: dict,
    values: dict,
    *,
    on_existing: Literal["update", "keep", "reject"],
) -> PermissionScopedSaveResult:
    """Write one object and prove its saved state stays inside the user's object scope."""
    with transaction.atomic():
        instance = model.objects.select_for_update().filter(**lookup).first()
        if instance is None:
            permission = get_permission_for_model(model, "add")
            if user is not None and not user.has_perm(permission):
                raise ObjectPermissionDenied(permission)
            instance = model(**lookup, **values)
            reject_overlong_fields(instance, model)
            instance, created = save_or_refetch(instance, model, lookup)
            if not created:
                instance = model.objects.select_for_update().get(pk=instance.pk)
        else:
            created = False

        if not created:
            if on_existing == "keep":
                # Reusing someone else's row still exposes it, so it needs the view permission.
                enforce_saved_object_permission(instance, user, "view")
                # atomic-exit-safe: existing-row-kept-unwritten
                return PermissionScopedSaveResult(instance=instance, created=False)
            if on_existing == "reject":
                raise ObjectPermissionDenied(get_permission_for_model(model, "add"))
            # Before, so a row outside the user's scope cannot be taken over.
            enforce_saved_object_permission(instance, user, "change")
            for field_name, value in values.items():
                setattr(instance, field_name, value)
            reject_overlong_fields(instance, model)
            instance.save(update_fields=list(values))
        # After, so the new state cannot be moved outside the user's scope.
        enforce_saved_object_permission(instance, user, "add" if created else "change")
        # atomic-exit-safe: scoped-write-committed
        return PermissionScopedSaveResult(instance=instance, created=created)


def delete_permission_scoped_objects(user, queryset) -> int:
    """Delete every row of *queryset* the user may delete, or none of them.

    The rows are locked and checked one by one first, so a refusal leaves the whole set intact.
    """
    with transaction.atomic():
        rows = list(queryset.select_for_update())
        for row in rows:
            enforce_saved_object_permission(row, user, "delete")
        for row in rows:
            row.delete()
        # atomic-exit-safe: scoped-delete-committed
        return len(rows)

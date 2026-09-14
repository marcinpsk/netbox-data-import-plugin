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
from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass
from typing import Any, Literal

from django.core.exceptions import EmptyResultSet, FieldDoesNotExist, FieldError, ValidationError
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


@dataclass(frozen=True)
class ProspectiveRelation:
    """One final related row, including concrete values that use generated keys."""

    instance: models.Model
    generated_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _PreparedCandidate:
    """One related candidate row shared by forward relations."""

    instance: models.Model
    generated_primary_key: bool
    generated_fields: frozenset[str]


@dataclass(frozen=True)
class _ProspectiveRelationState:
    """One connected forward relation in the read-only database world."""

    field: models.ForeignKey
    instance: models.Model
    generated_primary_key: bool
    generated_target_value: bool
    generated_fields: frozenset[str]


@dataclass(frozen=True)
class _ProspectiveWorld:
    """Copied root and relation rows that one read-only assessment can query."""

    root: Any
    candidates: dict
    relations: dict
    generated_values: dict[type[models.Model], dict[Any, set[str]]]


def _synthetic_primary_key(instance, occupied=None):
    """Return a collision-free primary key without consuming the model's sequence."""
    if instance.pk is not None:
        return instance.pk, False
    field = instance._meta.pk
    if not isinstance(field, (models.AutoField, models.BigAutoField, models.SmallAutoField)):
        raise TypeError(f"Cannot assess an unsaved {instance._meta.label} without a primary key.")
    concrete_model = instance._meta.concrete_model
    used = set() if occupied is None else occupied.setdefault(concrete_model, set())
    value = -1
    while value in used or concrete_model._base_manager.filter(pk=value).exists():
        value -= 1
    used.add(value)
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


def _generated_value_aliases(query, generated_values) -> set[str]:
    """Return aliases whose predicates or outward joins must not see generated values."""
    fields_by_model = {
        candidate_model: set().union(*rows.values()) for candidate_model, rows in generated_values.items()
    }
    candidate_tables = {candidate_model._meta.db_table for candidate_model in generated_values}

    def field_is_generated(field) -> bool:
        """Return whether any candidate carries an unknown value for this concrete field."""
        model = getattr(field, "model", None)
        return model is not None and getattr(field, "attname", None) in fields_by_model.get(
            model._meta.concrete_model, set()
        )

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
            and field_is_generated(expression.target)
        ):
            aliases.add(expression.alias)
    for alias, join in query.alias_map.items():
        parent_alias = getattr(join, "parent_alias", None)
        if parent_alias is None:
            continue
        parent = query.alias_map[parent_alias]
        for parent_field, related_field in getattr(join, "join_fields", ()):
            if field_is_generated(parent_field) and join.table_name not in candidate_tables:
                aliases.add(parent_alias)
            if field_is_generated(related_field) and parent.table_name not in candidate_tables:
                aliases.add(alias)
    return aliases


def _is_nullness_lookup(parts, value) -> bool:
    """Return whether a lookup asks only whether a key or relation exists."""
    return parts == ["isnull"] or ((not parts or parts == ["exact"]) and value is None)


def _depends_on_generated_relation_value(constraint, relations) -> bool:
    """Return whether a predicate treats a generated related value as authoritative."""
    for key, value in constraint.items():
        parts = key.split("__")
        for relation_name, relation in relations.items():
            field = relation.field
            if parts[0] not in {relation_name, field.attname}:
                continue
            suffix = parts[1:]
            if relation.generated_target_value:
                if _is_nullness_lookup(suffix, value):
                    continue
                if parts[0] == field.attname or not suffix or suffix[0] in field.get_lookups():
                    return True
            if relation.generated_primary_key and suffix:
                related_pk = relation.instance._meta.pk
                if suffix[0] in {"pk", related_pk.name, related_pk.attname} and not _is_nullness_lookup(
                    suffix[1:], value
                ):
                    return True
            if suffix and suffix[0] in relation.generated_fields and not _is_nullness_lookup(suffix[1:], value):
                return True
    return False


def _uses_root_reverse_relation(constraint, model) -> bool:
    """Return whether a predicate needs root-owned rows this assessment does not simulate."""
    for key in constraint:
        current_model = model
        for part in key.split("__"):
            try:
                field = current_model._meta.get_field(part)
            except FieldDoesNotExist:
                break
            reverse = field.is_relation and (field.one_to_many or (field.auto_created and not field.concrete))
            if current_model._meta.concrete_model is model._meta.concrete_model and reverse:
                return True
            if not field.is_relation or field.related_model is None:
                break
            current_model = field.related_model
    return False


def _normalized_generated_fields(relation, instance) -> frozenset[str]:
    """Return generated concrete field attribute names, or reject an unknown field."""
    concrete_fields = {
        name: field.attname for field in instance._meta.concrete_fields for name in (field.name, field.attname)
    }
    unknown = relation.generated_fields - concrete_fields.keys()
    if unknown:
        names = ", ".join(sorted(unknown))
        raise TypeError(f"The prospective {instance._meta.label} has unknown generated fields: {names}.")
    return frozenset(concrete_fields[name] for name in relation.generated_fields)


def _concrete_row_values(instance):
    """Return the complete concrete state used to detect conflicting candidate rows."""
    return tuple(field.value_from_object(instance) for field in instance._meta.concrete_fields)


def _prospective_relation_is_unsaved(instance, expected_model) -> bool:
    """Validate the model save state and return whether it needs a synthetic key."""
    unsaved = instance.pk is None and instance._state.adding
    saved = instance.pk is not None and not instance._state.adding
    if not unsaved and not saved:
        raise ValueError(f"The prospective {instance._meta.label} has an invalid saved state.")
    if saved and not expected_model._base_manager.filter(pk=instance.pk).exists():
        raise ValueError(f"The prospective {instance._meta.label} row does not exist.")
    return unsaved


def _prepare_prospective_world(instance, prospective_relations):
    """Copy and connect the root row and each final forward relation."""
    root = copy(instance)
    occupied = {}
    root.pk, root_generated_key = _synthetic_primary_key(root, occupied)
    candidates = {root._meta.concrete_model: [root]}
    generated_values = {root._meta.concrete_model: {root.pk: {root._meta.pk.attname}}} if root_generated_key else {}
    relations = {}
    prepared_by_key = {}
    prepared_unsaved = {}
    for relation_name, relation_value in (prospective_relations or {}).items():
        try:
            field = root._meta.get_field(relation_name)
        except FieldDoesNotExist as exc:
            raise TypeError(f"{root._meta.label}.{relation_name} is not a model field.") from exc
        if not isinstance(field, models.ForeignKey) or field.auto_created or not field.concrete:
            raise TypeError(f"{root._meta.label}.{relation_name} is not a concrete forward relation.")
        relation = (
            relation_value if isinstance(relation_value, ProspectiveRelation) else ProspectiveRelation(relation_value)
        )
        related_instance = relation.instance
        if not isinstance(related_instance, models.Model):
            raise TypeError(f"The prospective {root._meta.label}.{relation_name} value is not a model row.")
        expected_model = field.remote_field.model._meta.concrete_model
        if related_instance._meta.concrete_model is not expected_model:
            raise TypeError(f"{root._meta.label}.{relation_name} does not reference {related_instance._meta.label}.")
        generated_fields = _normalized_generated_fields(relation, related_instance)
        unsaved = _prospective_relation_is_unsaved(related_instance, expected_model)

        source_key = (expected_model, id(related_instance))
        prepared = prepared_unsaved.get(source_key) if unsaved else None
        if prepared is None:
            related = copy(related_instance)
            related.pk, generated_key = _synthetic_primary_key(related, occupied)
            candidate_key = (expected_model, related.pk)
            row_values = _concrete_row_values(related)
            existing = prepared_by_key.get(candidate_key)
            if existing is not None:
                prepared, existing_values = existing
                if existing_values != row_values or prepared.generated_fields != generated_fields:
                    raise ValueError(f"Prospective {related._meta.label} rows with key {related.pk} conflict.")
            else:
                prepared = _PreparedCandidate(related, generated_key, generated_fields)
                prepared_by_key[candidate_key] = (prepared, row_values)
                candidates.setdefault(expected_model, []).append(related)
            if unsaved:
                prepared_unsaved[source_key] = prepared
        elif prepared.generated_fields != generated_fields:
            raise ValueError(f"The prospective {related_instance._meta.label} generated fields conflict.")

        related = prepared.instance
        related_generated_fields = set(prepared.generated_fields)
        if prepared.generated_primary_key:
            related_generated_fields.add(related._meta.pk.attname)
        if related_generated_fields:
            generated_values.setdefault(expected_model, {}).setdefault(related.pk, set()).update(
                related_generated_fields
            )
        relation_key = field.target_field.value_from_object(related)
        generated_target_value = field.target_field.attname in related_generated_fields
        current_key = getattr(root, field.attname)
        if current_key is not None and current_key != relation_key:
            raise ValueError(f"The prospective {root._meta.label} already names a different {relation_name}.")
        setattr(root, field.attname, relation_key)
        if generated_target_value:
            generated_values.setdefault(root._meta.concrete_model, {}).setdefault(root.pk, set()).add(field.attname)
        relations[relation_name] = _ProspectiveRelationState(
            field,
            related,
            prepared.generated_primary_key,
            generated_target_value,
            prepared.generated_fields,
        )
    return _ProspectiveWorld(root, candidates, relations, generated_values)


def _prospective_ctes(candidates, generated_values):
    """Return typed candidate and database-world CTEs for each participating model."""
    quote = connection.ops.quote_name
    clauses = []
    params = []
    aliases = {}
    for index, (candidate_model, rows) in enumerate(candidates.items()):
        fields = candidate_model._meta.concrete_fields
        field_types = [field.db_type(connection) for field in fields]
        if any(field_type is None for field_type in field_types):
            raise ValueError(f"Cannot assess every concrete field of {candidate_model._meta.label}.")
        candidate_cte = f"ndi_permission_candidates_{index}"
        world_cte = f"ndi_permission_world_{index}"
        known_value_world_cte = f"ndi_permission_known_value_world_{index}"
        if candidate_model._meta.db_table in (candidate_cte, world_cte, known_value_world_cte):
            raise ValueError("A model table conflicts with an internal permission query name.")
        columns = ", ".join(quote(field.column) for field in fields)
        typed_row = "(" + ", ".join(f"CAST(%s AS {field_type})" for field_type in field_types) + ")"
        row_values = ", ".join(typed_row for _row in rows)
        quoted_candidate = quote(candidate_cte)
        quoted_world = quote(world_cte)
        quoted_known_value_world = quote(known_value_world_cte)
        quoted_table = quote(candidate_model._meta.db_table)
        quoted_pk = quote(candidate_model._meta.pk.column)
        key_placeholders = ", ".join("%s" for _row in rows)
        candidate_clause = f"{quoted_candidate} ({columns}) AS (VALUES {row_values})"
        world_clause = (
            f"{quoted_world} ({columns}) AS ("  # noqa: S608 - Every identifier is quoted model metadata; values are parameters.
            f"SELECT {columns} FROM {quoted_table} WHERE {quoted_pk} NOT IN ({key_placeholders}) "
            f"UNION ALL SELECT {columns} FROM {quoted_candidate})"
        )
        clauses.extend(
            (
                candidate_clause,
                world_clause,
            )
        )
        params.extend(
            field.get_db_prep_save(field.value_from_object(row), connection) for row in rows for field in fields
        )
        params.extend(row.pk for row in rows)
        model_generated_keys = set(generated_values.get(candidate_model, {}))
        if model_generated_keys:
            generated_placeholders = ", ".join("%s" for _key in model_generated_keys)
            known_value_world_clause = (
                f"{quoted_known_value_world} ({columns}) AS ("  # noqa: S608 - Identifiers are quoted metadata.
                f"SELECT {columns} FROM {quoted_table} WHERE {quoted_pk} NOT IN ({key_placeholders}) "
                f"UNION ALL SELECT {columns} FROM {quoted_candidate} "
                f"WHERE {quoted_pk} NOT IN ({generated_placeholders}))"
            )
            clauses.append(known_value_world_clause)
            params.extend(row.pk for row in rows)
            params.extend(model_generated_keys)
            known_value_world = known_value_world_cte
        else:
            known_value_world = world_cte
        aliases[candidate_model._meta.db_table] = (candidate_cte, world_cte, known_value_world)
    return clauses, params, aliases


def _prospective_row_matches(user, model, constraint, world) -> bool:
    """Evaluate one NetBox constraint arm against a read-only prospective database world."""
    if (
        _uses_root_reverse_relation(constraint, model)
        or (
            world.root._meta.concrete_model in world.generated_values
            and _depends_on_generated_root_primary_key(constraint, world.root)
        )
        or _depends_on_generated_relation_value(constraint, world.relations)
    ):
        return False
    permission_filter = qs_filter_from_constraints([constraint], {CONSTRAINT_TOKEN_USER: user})
    queryset = model.objects.filter(permission_filter, pk=world.root.pk).values_list("pk", flat=True).order_by()
    query = queryset.query.clone()
    known_value_aliases = _generated_value_aliases(query, world.generated_values)
    if query.base_table in known_value_aliases:
        return False
    clauses, cte_params, aliases = _prospective_ctes(world.candidates, world.generated_values)
    root_candidate_cte = aliases[model._meta.db_table][0]
    for alias, join in tuple(query.alias_map.items()):
        cte_names = aliases.get(join.table_name)
        if cte_names is None:
            continue
        replacement = copy(join)
        if alias == query.base_table:
            replacement.table_name = root_candidate_cte
        else:
            replacement.table_name = cte_names[2] if alias in known_value_aliases else cte_names[1]
        query.alias_map[alias] = replacement
    sql, params = query.sql_with_params()
    # Model metadata supplies every identifier and type. Values remain query parameters.
    statement = f"WITH {', '.join(clauses)} {sql}"
    with connection.cursor() as cursor:
        cursor.execute(statement, [*cte_params, *params])
        return cursor.fetchone() is not None


def _assess_permission_scoped_save(
    user,
    model,
    lookup: dict,
    values: dict,
    *,
    on_existing: Literal["update", "keep", "reject"],
    current,
    prospective_relations: Mapping[str, models.Model | ProspectiveRelation] | None = None,
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
        if on_existing == "keep" and current is not None:
            return PermissionScopedSaveAssessment(True, permission)
        prospective = model(**lookup, **values) if current is None else copy(current)
        if current is not None:
            for field_name, value in values.items():
                setattr(prospective, field_name, value)
        world = _prepare_prospective_world(prospective, prospective_relations)
        constraints = getattr(user, "_object_perm_cache", {}).get(permission, ())
        allowed = any(
            not constraint or _prospective_row_matches(user, model, constraint, world) for constraint in constraints
        )
    except (DatabaseError, EmptyResultSet, FieldError, TypeError, ValidationError, ValueError) as exc:
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
    prospective_relations: Mapping[str, models.Model | ProspectiveRelation] | None = None,
) -> PermissionScopedSaveAssessment:
    """Assess a save and its final forward relations without writing or locking."""
    current = model.objects.filter(**lookup).first()
    return _assess_permission_scoped_save(
        user,
        model,
        lookup,
        values,
        on_existing=on_existing,
        current=current,
        prospective_relations=prospective_relations,
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

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

from dataclasses import dataclass
from typing import Any, Literal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from utilities.permissions import get_permission_for_model


class ObjectPermissionDenied(Exception):
    """Reject a write outside the caller's NetBox object scope."""


@dataclass(frozen=True)
class PermissionScopedSaveResult:
    """One scoped write: the saved object and whether this call created it."""

    instance: Any
    created: bool


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

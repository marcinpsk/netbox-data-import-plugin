# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shared object-permission checks for import writes."""


class ObjectPermissionDenied(Exception):
    """Reject a write outside the caller's NetBox object scope."""


def enforce_saved_object_permission(obj, user, action):
    """Reject a saved object whose final state is outside the user's scope."""
    if user is not None and not obj.__class__.objects.restrict(user, action).filter(pk=obj.pk).exists():
        raise ObjectPermissionDenied(f"{obj._meta.app_label}.{action}_{obj._meta.model_name}")

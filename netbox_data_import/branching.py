# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The one owner of every netbox-branching fact the plugin relies on.

netbox-branching is optional. Without it, every function here is a no-op.
"""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models

APP_LABEL = "netbox_data_import"
BRANCHING_APP_LABEL = "netbox_branching"


def installed() -> bool:
    """Return whether netbox-branching is an installed app."""
    return apps.is_installed(BRANCHING_APP_LABEL)


def active_branch():
    """Return the active Branch, or None when no branch is active or netbox-branching is absent."""
    if not installed():
        return None
    from netbox_branching.contextvars import active_branch as branch_context

    return branch_context.get()


def is_branchable(model: type[models.Model]) -> bool | None:
    """Resolve branching support for a plugin model, and defer (None) for every other model.

    A plugin model is branchable when one of its concrete foreign keys reaches a branchable model
    outside the plugin: a core delete in a branch then cascades into that table, which must exist
    in the branch schema.
    """
    if model._meta.app_label != APP_LABEL:
        return None
    from netbox_branching.utilities import supports_branching

    return any(
        supports_branching(field.related_model)
        for field in model._meta.concrete_fields
        if isinstance(field, models.ForeignKey) and field.related_model._meta.app_label != APP_LABEL
    )


def register() -> None:
    """Register the resolver with netbox-branching, and refuse a configuration that overrides it."""
    if not installed():
        return
    try:
        from netbox_branching.utilities import register_branching_resolver, supports_branching
    except ImportError as exc:
        raise ImproperlyConfigured(
            f"{APP_LABEL}: this netbox-branching release has no register_branching_resolver: {exc}"
        ) from exc
    register_branching_resolver(is_branchable)
    overridden = sorted(
        model._meta.label
        for model in apps.get_app_config(APP_LABEL).get_models()
        if supports_branching(model) != is_branchable(model)
    )
    if overridden:
        raise ImproperlyConfigured(
            f"{APP_LABEL}: netbox-branching disagrees with the plugin on branching for {', '.join(overridden)}. "
            f"Remove {APP_LABEL} models from the netbox_branching exempt_models setting."
        )

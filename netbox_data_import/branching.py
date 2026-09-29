# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The one owner of every netbox-branching fact the plugin relies on.

netbox-branching is optional. Without it, every function here is a no-op.
"""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed
from django.db import models
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse

APP_LABEL = "netbox_data_import"
BRANCHING_APP_LABEL = "netbox_branching"
REFUSAL_CODE = "branch_not_supported"


class BranchActive(Exception):
    """A plugin entry point was reached while a netbox-branching branch is active."""


def installed() -> bool:
    """Return whether netbox-branching is an installed app."""
    return apps.is_installed(BRANCHING_APP_LABEL)


def active_branch():
    """Return the active Branch, or None when no branch is active or netbox-branching is absent."""
    if not installed():
        return None
    from netbox_branching.contextvars import active_branch as branch_context
    from netbox_branching.models import Branch

    branch = branch_context.get()
    # A header naming a branch that is not ready puts netbox-branching's 400 response in the context.
    return branch if isinstance(branch, Branch) else None


def refusal_message(branch) -> str:
    """Return the one message every refused entry point shows."""
    if branch is None:
        return "NetBox Data Import runs on main only, and this request selects a branch that is not active."
    return f"NetBox Data Import runs on main only, and the active branch is {branch.name}."


def refuse_branch() -> None:
    """Raise BranchActive when a branch is active."""
    branch = active_branch()
    if branch is not None:
        raise BranchActive(refusal_message(branch))


def fail_job_in_branch(runner) -> None:
    """Fail a background job with the refusal in its log when its worker has a branch active."""
    from core.exceptions import JobFailed

    branch = active_branch()
    if branch is None:
        return
    message = refusal_message(branch)
    runner.logger.error(message)
    raise JobFailed(message)


def _selects_a_branch(request) -> bool:
    """Return whether a request names a branch; `?_branch=` without the header is the switch to main."""
    from netbox_branching.constants import BRANCH_HEADER, COOKIE_NAME, QUERY_PARAM

    if BRANCH_HEADER in request.headers:
        return True
    if QUERY_PARAM in request.GET:
        return bool(request.GET[QUERY_PARAM])
    return COOKIE_NAME in request.COOKIES


def _is_plugin_view(view_func) -> bool:
    owner = getattr(view_func, "view_class", None) or getattr(view_func, "cls", None) or view_func
    return owner.__module__ == APP_LABEL or owner.__module__.startswith(f"{APP_LABEL}.")


class BranchRefusalMiddleware:
    """Refuse a request to a plugin-owned view while a branch is active or selected."""

    def __init__(self, get_response):
        if not installed():
            raise MiddlewareNotUsed
        self.get_response = get_response

    def __call__(self, request):
        """Pass the request on; the refusal needs the resolved view, so it happens in process_view."""
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Answer 409 in place of the plugin view: JSON for the REST API, a page for the UI."""
        if not _is_plugin_view(view_func):
            return None
        branch = active_branch()
        if branch is None and not _selects_a_branch(request):
            return None
        message = refusal_message(branch)
        if request.path_info.startswith(reverse("api-root")):
            return JsonResponse({"detail": message, "code": REFUSAL_CODE}, status=409)
        return render(request, "netbox_data_import/branch_refused.html", {"message": message}, status=409)


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

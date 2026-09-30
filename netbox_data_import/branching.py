# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The one owner of every netbox-branching fact the plugin relies on.

netbox-branching is optional. Without it, every function here is a no-op.
"""

from core.choices import ObjectChangeActionChoices
from django.apps import apps
from django.contrib.messages import get_messages
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed
from django.db import models
from django.http import JsonResponse
from django.shortcuts import render
from packaging.version import Version
from utilities.api import is_api_request

APP_LABEL = "netbox_data_import"
BRANCHING_APP_LABEL = "netbox_branching"
REFUSAL_CODE = "branch_not_supported"
# The design covers netbox-branching 1.2.x; 1.1.x also has register_branching_resolver.
MINIMUM_BRANCHING_RELEASE = (1, 2)


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


# An anonymous caller is refused without the name or the state of any branch.
ANONYMOUS_REFUSAL = "NetBox Data Import runs on main only, and this request selects a branch."


def refusal_message(branch) -> str:
    """Return the one message every refused entry point shows."""
    if branch is None:
        return "NetBox Data Import runs on main only, and this request selects a branch that is not active."
    # Typographic quotes need no HTML escaping, so the page, the REST detail and GraphQL show one text.
    return f"NetBox Data Import runs on main only, and the active branch is \u201c{branch.name}\u201d."


def refuse_branch() -> None:
    """Raise BranchActive when a branch is active."""
    branch = active_branch()
    if branch is not None:
        raise BranchActive(refusal_message(branch))


def fail_job_in_branch(runner) -> None:
    """Fail a background job with the refusal in its log when its worker has a branch active."""
    from core.exceptions import JobFailed

    try:
        refuse_branch()
    except BranchActive as exc:
        refusal = exc
    else:
        return
    runner.logger.error(str(refusal))
    raise JobFailed(str(refusal)) from refusal


def _selects_a_branch(request) -> bool:
    """Return whether a request names a branch, in netbox-branching's own order of precedence."""
    from netbox_branching.constants import BRANCH_HEADER, COOKIE_NAME, QUERY_PARAM
    from netbox_branching.utilities import is_api_request

    # netbox-branching reads the header on REST and GraphQL requests only.
    if is_api_request(request) and BRANCH_HEADER in request.headers:
        return True
    if QUERY_PARAM in request.GET:
        return bool(request.GET[QUERY_PARAM])
    return COOKIE_NAME in request.COOKIES


def request_refusal(request) -> str | None:
    """Return the refusal message for a request that has a branch active or selected, else None."""
    if not installed():
        return None
    branch = active_branch()
    if branch is None and not _selects_a_branch(request):
        return None
    if not request.user.is_authenticated:
        return ANONYMOUS_REFUSAL
    return refusal_message(branch)


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
        message = request_refusal(request)
        if message is None:
            return None
        if is_api_request(request):
            return JsonResponse({"detail": message, "code": REFUSAL_CODE}, status=409)
        # The plugin's scripts ask for JSON and show the `error` of an `ok: false` envelope.
        if request.get_preferred_type(["text/html", "application/json"]) == "application/json":
            return JsonResponse({"ok": False, "error": message, "code": REFUSAL_CODE}, status=409)
        if request.user.is_authenticated:
            return render(request, "netbox_data_import/branch_refused.html", {"message": message}, status=409)
        # netbox-branching queues messages that name the branch; reading them here deletes them unseen.
        list(get_messages(request))
        # The full layout shows the branch selector, so an anonymous caller gets the bare page.
        return render(
            request,
            "netbox_data_import/branch_refused_anonymous.html",
            {"message": message, "messages": ()},
            status=409,
        )


def _branchable_references(model: type[models.Model]) -> set[type[models.Model]]:
    """Return the branchable models outside the plugin that a concrete foreign key of the model references."""
    from netbox_branching.utilities import supports_branching

    return {
        field.related_model
        for field in model._meta.concrete_fields
        if isinstance(field, models.ForeignKey)
        and field.related_model._meta.app_label != APP_LABEL
        and supports_branching(field.related_model)
    }


def is_branchable(model: type[models.Model]) -> bool | None:
    """Resolve branching support for a plugin model, and defer (None) for every other model.

    A plugin model is branchable when one of its concrete foreign keys reaches a branchable model
    outside the plugin: a core delete in a branch then cascades into that table, which must exist
    in the branch schema.
    """
    if model._meta.app_label != APP_LABEL:
        return None
    return bool(_branchable_references(model))


def _unrevertable_models() -> set[type[models.Model]]:
    """Return the models whose delete a revert cannot undo without losing plugin data."""
    from extras.models import Tag, TaggedItem

    # A Tag delete removes Import Profile assignments in main, and a sync records a TaggedItem delete.
    unrevertable = {Tag, TaggedItem}
    for model in apps.get_app_config(APP_LABEL).get_models():
        if references := _branchable_references(model):
            unrevertable |= {model, *references}
    return unrevertable


def validate_revert(branch):
    """Refuse a revert of a branch that deleted an object the plugin's data depends on.

    Revert replays the branch's ObjectChange rows, and plugin data has none, except the synthetic
    deletes that a sync records. So a revert cannot restore the plugin data such a delete removed.
    """
    from django.contrib.contenttypes.models import ContentType
    from netbox_branching.utilities import BranchActionIndicator

    content_types = ContentType.objects.get_for_models(*_unrevertable_models())
    models_by_type = {content_type.pk: model for model, content_type in content_types.items()}
    deleted = (
        branch.get_changes()
        .filter(action=ObjectChangeActionChoices.ACTION_DELETE, changed_object_type__in=models_by_type)
        .order_by()
        .values_list("changed_object_type", flat=True)
        .distinct()
    )
    names = sorted(str(models_by_type[type_id]._meta.verbose_name) for type_id in deleted)
    if not names:
        return BranchActionIndicator(True)
    return BranchActionIndicator(
        False,
        "NetBox Data Import data cannot be restored by a revert, and this branch deleted objects of these "
        f"types: {', '.join(names)}.",
    )


def register() -> None:
    """Register the resolver and the revert validator, and refuse a configuration that overrides the resolver."""
    if not installed():
        return
    release = apps.get_app_config(BRANCHING_APP_LABEL).version
    # Compare the release segment only, so a 1.2 pre-release is not refused.
    if Version(release).release < MINIMUM_BRANCHING_RELEASE:
        raise ImproperlyConfigured(
            f"{APP_LABEL}: netbox-branching {release} is installed; this plugin needs 1.2 or later."
        )
    from netbox_branching.models import Branch
    from netbox_branching.utilities import register_branching_resolver, supports_branching

    register_branching_resolver(is_branchable)
    Branch.register_preaction_check(validate_revert, "revert")
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

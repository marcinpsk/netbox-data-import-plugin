# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Manage the materialized preview used by asynchronous row actions."""

import secrets


PREVIEW_DIRTY_SESSION_KEY = "import_preview_dirty"
PREVIEW_REVISION_SESSION_KEY = "import_preview_revision"
PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY = "import_preview_use_materialized_once"


def current_preview_revision(session) -> str:
    """Return the active preview revision, creating it when needed."""
    revision = session.get(PREVIEW_REVISION_SESSION_KEY)
    if not revision:
        revision = secrets.token_urlsafe(18)
        session[PREVIEW_REVISION_SESSION_KEY] = revision
    return revision


def record_recalculated_preview(session, plan) -> str:
    """Store one authoritative preview and return its new revision."""
    revision = secrets.token_urlsafe(18)
    session["import_plan"] = plan.to_dict()
    session[PREVIEW_DIRTY_SESSION_KEY] = False
    session[PREVIEW_REVISION_SESSION_KEY] = revision
    return revision


def retire_preview_revision(session) -> str:
    """Invalidate the token any open preview is holding, without storing a new result."""
    revision = secrets.token_urlsafe(18)
    session[PREVIEW_REVISION_SESSION_KEY] = revision
    return revision


def load_cached_preview(request):
    """Return the active Import Profile and materialized Review Workspace."""
    from .models import ImportProfile
    from .review_workspace import ReviewWorkspace

    context = request.session.get("import_context")
    plan_data = request.session.get("import_plan")
    if (
        request.session.get("import_preview_pending") is not True
        or not isinstance(context, dict)
        or not isinstance(plan_data, dict)
    ):
        return None
    revision = current_preview_revision(request.session)
    if "application/json" in request.headers.get("Accept", ""):
        if request.POST.get("preview_revision") != revision:
            return None
    profile = ImportProfile.objects.restrict(request.user, "change").filter(pk=context.get("profile_id")).first()
    if profile is None:
        return None
    return profile, ReviewWorkspace.from_dict(plan_data)


def mark_preview_dirty(session) -> None:
    """Record that saved changes require one authoritative recalculation."""
    session[PREVIEW_DIRTY_SESSION_KEY] = True


def pending_preview_payload(row_number: int, message: str) -> dict:
    """Return the small response shared by deferred preview-row actions."""
    return {
        "ok": True,
        "row_number": row_number,
        "preview_state": "recalculation_required",
        "message": message,
    }

# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Manage the materialized preview used by asynchronous row actions."""

import secrets


class PreviewActionInvalid(ValueError):
    """A row action refused for a reason this plugin wrote, so the response may state it.

    Any other exception carries internal detail, so it reaches the operator as a generic message.
    """


PREVIEW_DIRTY_SESSION_KEY = "import_preview_dirty"
PREVIEW_PLAN_SESSION_KEY = "import_plan"
PREVIEW_REVISION_SESSION_KEY = "import_preview_revision"
PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY = "import_preview_use_materialized_once"
RETAINED_SYNC_JOB_SESSION_KEY = "import_retained_sync_job_id"


def current_preview_revision(session) -> str:
    """Return the active preview revision, creating it when needed."""
    revision = session.get(PREVIEW_REVISION_SESSION_KEY)
    if not revision:
        revision = secrets.token_urlsafe(18)
        session[PREVIEW_REVISION_SESSION_KEY] = revision
    return revision


RETAINED_SYNC_BLOCK_REASON = (
    "A trace synchronization is still running. Wait for it to finish before changing this workspace."
)


class PreviewLocked(RuntimeError):
    """The preview may not move while the trace sync it queued is still running.

    A per-trace sync keeps the preview open, so the operator stays on a page whose plan the queued
    Job is about to invalidate. Recalculating adopts NetBox state that predates the Job's writes and
    clears the guard that stops a second queue, so both are refused until the Job is terminal.
    """


def retained_sync_block_reason(session, user) -> str:
    """Return why the retained trace sync holds this preview, or ``""``.

    Scope: this reads one session and is consulted before the enqueue, so it orders one operator's
    commands. It does not serialize two concurrent requests.
    """
    from core.choices import JobStatusChoices

    from .jobs import ImportJobRunner

    job_pk = session.get(RETAINED_SYNC_JOB_SESSION_KEY)
    if not job_pk:
        return ""
    retained = ImportJobRunner.get_jobs().filter(
        pk=job_pk,
        user=user,
        data__job_type=ImportJobRunner.job_type,
        status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
    )
    return RETAINED_SYNC_BLOCK_REASON if retained.exists() else ""


def assert_preview_may_move(session, user) -> None:
    """Raise `PreviewLocked` when the retained trace sync still holds this preview."""
    if reason := retained_sync_block_reason(session, user):
        raise PreviewLocked(reason)


def _store_preview(session, plan) -> str:
    """Write one authoritative preview and return its new revision."""
    revision = secrets.token_urlsafe(18)
    session[PREVIEW_PLAN_SESSION_KEY] = plan.to_dict()
    session[PREVIEW_DIRTY_SESSION_KEY] = False
    session[PREVIEW_REVISION_SESSION_KEY] = revision
    return revision


def record_recalculated_preview(session, plan, *, user) -> str:
    """Replace the current preview with a freshly read one, refusing while a sync holds it."""
    assert_preview_may_move(session, user)
    return _store_preview(session, plan)


def start_new_preview(session, plan) -> str:
    """Store the first preview of a newly uploaded source, replacing whatever came before.

    Unguarded on purpose: this is a different import, so it inherits no earlier sync. Releasing the
    retained key is what stops the previous preview's Job from refusing commands on this one.
    """
    session.pop(RETAINED_SYNC_JOB_SESSION_KEY, None)
    return _store_preview(session, plan)


def restore_preview_plan(session, plan_data) -> None:
    """Adopt the accepted plan a failed Job stored, so its preview can be reviewed again."""
    session[PREVIEW_PLAN_SESSION_KEY] = plan_data


def retain_sync_job(session, job_pk) -> None:
    """Record the per-trace sync whose writes this preview is now waiting on."""
    session[RETAINED_SYNC_JOB_SESSION_KEY] = job_pk


def release_retained_sync(session) -> None:
    """Forget the retained sync, because this preview no longer waits on one."""
    session.pop(RETAINED_SYNC_JOB_SESSION_KEY, None)


def clear_preview_state(session) -> None:
    """Drop the stored plan and any retained sync, for a preview that is being discarded."""
    session.pop(PREVIEW_PLAN_SESSION_KEY, None)
    session.pop(RETAINED_SYNC_JOB_SESSION_KEY, None)


def retire_preview_revision(session) -> str:
    """Invalidate the token any open preview is holding, without storing a new result."""
    revision = secrets.token_urlsafe(18)
    session[PREVIEW_REVISION_SESSION_KEY] = revision
    return revision


def load_cached_preview(request):
    """Return the active Import Profile and materialized Review Workspace."""
    from .models import ImportProfile
    from .plan import PlanError
    from .review_workspace import ReviewWorkspace

    context = request.session.get("import_context")
    plan_data = request.session.get(PREVIEW_PLAN_SESSION_KEY)
    if (
        request.session.get("import_preview_pending") is not True
        or not isinstance(context, dict)
        or not isinstance(plan_data, dict)
    ):
        return None
    revision = current_preview_revision(request.session)
    if "application/json" in request.headers.get("Accept", ""):
        # A read carries its revision in the query, because a GET has no posted body to hold it.
        posted = request.POST.get("preview_revision", request.GET.get("preview_revision"))
        if posted != revision:
            return None
    profile = ImportProfile.objects.restrict(request.user, "change").filter(pk=context.get("profile_id")).first()
    if profile is None:
        return None
    try:
        workspace = ReviewWorkspace.from_dict(plan_data)
    except PlanError:
        return None
    return profile, workspace


def mark_preview_dirty(session) -> None:
    """Record that saved changes require one authoritative recalculation."""
    session[PREVIEW_DIRTY_SESSION_KEY] = True


def pending_preview_payload(row_number: int, message: str, detail: str = "", resolution: dict | None = None) -> dict:
    """Return the small response shared by deferred preview-row actions.

    `detail` names a write this action already made in NetBox, which the page reports rather than
    leaving the operator to discover it. A save that only records a decision carries none.

    `resolution` is the decision as it was stored, which the page keeps in place of the one it
    posted. Only an action that saves a resolution carries it.
    """
    payload = {
        "ok": True,
        "row_number": row_number,
        "preview_state": "recalculation_required",
        "message": message,
        "detail": detail,
    }
    if resolution is not None:
        payload["resolution"] = resolution
    return payload

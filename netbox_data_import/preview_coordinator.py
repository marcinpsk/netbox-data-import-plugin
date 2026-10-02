# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Preview Coordinator: the one owner of each browser session's active preview (ADR 0004).

`setup_claim` gives the setup page the claim of the session's row, creating the empty row on first
use. `read_preview` returns an immutable snapshot for a page or a read. `apply_preview_command` runs
one command under the coordinator lock, then the profile lock, then whatever the command locks, and
it is the only code that stores a plan or moves the revision. No other module touches preview state,
and this one reads nothing from the session beyond its key.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Any, ClassVar

from core.signals import clear_events
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import branching
from .import_engine import ImportEngine, StaleSourceDocument
from .models import (
    ImportProfile,
    PreviewCoordinator,
    PreviewState,
    SourceDocument,
    locked_profile_policy,
    validate_adapter_target_module,
    validate_registered_adapter,
)
from .plan import ImportPlan, PlanError, canonical_json
from .object_permissions import clear_user_permission_caches
from .review_workspace import ReviewWorkspace, refuse_moved_policy

logger = logging.getLogger(__name__)


def _refresh_actor(user) -> None:
    """Read the actor's current status and grants after any wait for the profile lock."""
    try:
        user.refresh_from_db(fields=["is_active", "is_superuser"])
    except ObjectDoesNotExist as exc:
        raise PermissionDenied from exc
    if not user.is_active:
        raise PermissionDenied
    clear_user_permission_caches(user)


CLAIM_FIELDS = ("preview_token", "preview_revision", "preview_document", "preview_profile")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
PLANNING_KEYS = ("site_id", "location_id", "tenant_id")
# A plan larger than this is refused before it commits, with every write the command made.
MAX_PLAN_BYTES = 64 * 1024 * 1024

NO_PREVIEW = "No import preview is in progress. Start a new import."
CLAIM_INVALID = "This request does not name a preview. Reload the page and try again."
STALE_PREVIEW = "This preview changed in another tab or request. Reload it and try again."
NEWER_PREVIEW = "A newer import was started in another tab. Reload the page to continue there."
EXPIRED_PREVIEW = "This preview expired. Start a new import."
SUBMITTED_PREVIEW = "The import already started, so this preview can no longer take a decision."
RETAINED_SYNC_BLOCK_REASON = (
    "A trace synchronization is still running. Wait for it to finish before changing this workspace."
)
SYNC_FINISHED = "The trace synchronization finished. Re-read the preview before the next decision."
UNREADABLE_PREVIEW = "This preview cannot be read. Re-read the preview."
PREVIEW_TOO_LARGE = "This preview is too large to store. Split the source file and import each part."
SOURCE_GONE = "The stored source is no longer available. Upload it again."


class StalePreview(Exception):
    """The command named a preview that is not the active one, or one that cannot take it now."""


class PreviewCommandRefused(Exception):
    """A command refused for a reason this plugin wrote, so the response may state it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PreviewClaim:
    """The preview a page displays: its generation token, revision, Source Document and profile."""

    token: str
    revision: int
    document_id: int | None
    profile_id: int | None

    @classmethod
    def posted(cls, data) -> PreviewClaim:
        """Return the claim a request carries, refusing a missing or malformed one."""
        token = data.get("preview_token", "")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise StalePreview(CLAIM_INVALID)
        revision = _claim_number(data.get("preview_revision"))
        if revision is None:
            raise StalePreview(CLAIM_INVALID)
        return cls(
            token=token,
            revision=revision,
            document_id=_optional_claim_number(data.get("preview_document")),
            profile_id=_optional_claim_number(data.get("preview_profile")),
        )

    def fields(self) -> dict[str, str]:
        """Return the claim as the form fields a page renders and posts back."""
        return {
            "preview_token": self.token,
            "preview_revision": str(self.revision),
            "preview_document": "" if self.document_id is None else str(self.document_id),
            "preview_profile": "" if self.profile_id is None else str(self.profile_id),
        }


def _claim_number(value) -> int | None:
    """Return one posted positive integer, or None for anything else."""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 19:
        return None
    number = int(value)
    return number if number >= 1 else None


def _optional_claim_number(value) -> int | None:
    """Return one posted id, None for an empty value, and refuse a malformed one."""
    if value in (None, ""):
        return None
    number = _claim_number(value)
    if number is None:
        raise StalePreview(CLAIM_INVALID)
    return number


def _claim_of(row: PreviewCoordinator) -> PreviewClaim:
    return PreviewClaim(
        token=row.preview_token,
        revision=row.revision,
        document_id=row.source_document_id,
        profile_id=row.profile_id,
    )


def _same_generation(row: PreviewCoordinator, expected: PreviewClaim) -> bool:
    """Return whether a claim names this row's generation and identity, at any revision up to now."""
    return (
        secrets.compare_digest(row.preview_token, expected.token)
        and expected.revision <= row.revision
        and expected.document_id == row.source_document_id
        and expected.profile_id == row.profile_id
    )


def _exact(row: PreviewCoordinator, expected: PreviewClaim) -> bool:
    return _same_generation(row, expected) and expected.revision == row.revision


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _session_binding(request, *, create: bool = False) -> str | None:
    """Return the digest of the server-side session key, which selects the session's row."""
    if create and not request.session.session_key:
        request.session.save()
    key = request.session.session_key
    if not key:
        return None
    return hashlib.sha256(key.encode()).hexdigest()


def _session_expiry(request):
    return request.session.get_expiry_date()


def _payload_expiry(request):
    """Return when a new preview expires: with its session, and no later than its Source Document."""
    return min(_session_expiry(request), timezone.now() + PreviewCoordinator.PAYLOAD_LIFETIME)


def _owned(row: PreviewCoordinator | None, user) -> PreviewCoordinator | None:
    if row is not None:
        _check_owner(row, user)
    return row


def _check_owner(row: PreviewCoordinator, user) -> PreviewCoordinator:
    if row.owner_id != user.pk:
        # Django rotates the key at login, so a binding owned by another user is never this session's.
        raise PermissionDenied("This preview belongs to another user.")
    return row


def _payload_expired(row: PreviewCoordinator) -> bool:
    return row.state in PreviewState.ACTIVE and row.expires_at <= timezone.now()


def _clear_payload(row: PreviewCoordinator, state: str) -> None:
    row.state = state
    row.profile_id = None
    row.source_document_id = None
    row.context = {}
    row.plan = None
    row.job_id = None


def _expire(row: PreviewCoordinator) -> None:
    """Clear an expired payload and start a new empty generation, so its claims stay refused."""
    _clear_payload(row, PreviewState.EXPIRED)
    row.preview_token = _new_token()
    row.revision += 1
    row.save()


def _planning_context(context: Mapping) -> dict:
    return {key: context.get(key) for key in PLANNING_KEYS}


def _job_is_active(job_id) -> bool:
    from core.choices import JobStatusChoices
    from core.models import Job

    return Job.objects.filter(pk=job_id, status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).exists()


def setup_claim(request) -> PreviewClaim:
    """Return the claim the setup page posts, creating the session's empty row on its first visit."""
    branching.refuse_branch()
    binding = _session_binding(request, create=True)
    row = _owned(PreviewCoordinator.objects.filter(session_binding=binding).defer("plan").first(), request.user)
    if row is None:
        try:
            with transaction.atomic():
                row = PreviewCoordinator.objects.create(
                    session_binding=binding,
                    owner=request.user,
                    preview_token=_new_token(),
                    expires_at=_session_expiry(request),
                )
        except IntegrityError:
            # A concurrent first visit created it; the unique binding leaves exactly one row.
            row = _check_owner(PreviewCoordinator.objects.defer("plan").get(session_binding=binding), request.user)
    return _claim_of(row)


@dataclass(frozen=True)
class PreviewSnapshot:
    """One immutable read of the session's preview, with its profile and document checked live."""

    claim: PreviewClaim
    state: str
    expired: bool
    context: Mapping[str, Any]
    job_id: int | None
    profile: ImportProfile | None
    document: SourceDocument | None
    _plan_data: dict | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        """Return whether a live, unexpired preview with its profile and document stands behind this read."""
        return (
            self.state in PreviewState.ACTIVE
            and not self.expired
            and self.profile is not None
            and self.document is not None
        )

    @property
    def planning_context(self) -> dict:
        """Return the site, location and tenant the preview plans against."""
        return _planning_context(self.context)

    @cached_property
    def plan(self) -> ImportPlan:
        """Return the stored plan, which is immutable, raising PlanError when this release cannot read it."""
        if self._plan_data is None:
            raise PlanError("The preview stores no Import Plan.")
        return ImportPlan.from_dict(self._plan_data)

    def workspace(self, viewer) -> ReviewWorkspace:
        """Return the stored plan as one viewer's Review Workspace."""
        return ReviewWorkspace(self.plan, viewer)


def _frozen(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    return value


def read_preview(
    request, *, expected: PreviewClaim | None = None, profile_action: str = "change", include_plan: bool = True
) -> PreviewSnapshot:
    """Return the session's preview as it stands now; with `expected`, refuse any other one."""
    branching.refuse_branch()
    binding = _session_binding(request)
    row = None
    if binding is not None:
        rows = PreviewCoordinator.objects.filter(session_binding=binding)
        row = _owned((rows if include_plan else rows.defer("plan")).first(), request.user)
    if row is None:
        if expected is not None:
            raise StalePreview(NO_PREVIEW)
        return PreviewSnapshot(
            claim=PreviewClaim("", 1, None, None),
            state=PreviewState.EMPTY,
            expired=False,
            context=MappingProxyType({}),
            job_id=None,
            profile=None,
            document=None,
        )
    if expected is not None:
        if not _exact(row, expected):
            raise StalePreview(STALE_PREVIEW)
        if _payload_expired(row):
            raise StalePreview(EXPIRED_PREVIEW)
    profile = document = None
    if row.state in PreviewState.ACTIVE:
        profile = ImportProfile.objects.restrict(request.user, profile_action).filter(pk=row.profile_id).first()
        if profile is not None:
            document = (
                SourceDocument.objects.filter(pk=row.source_document_id, profile=profile).defer("content").first()
            )
    return PreviewSnapshot(
        claim=_claim_of(row),
        state=row.state,
        expired=_payload_expired(row),
        context=_frozen(row.context or {}),
        job_id=row.job_id,
        profile=profile,
        document=document,
        _plan_data=row.plan if include_plan else None,
    )


class LockedPreview:
    """The preview one command runs against, read under the coordinator and the profile locks."""

    def __init__(self, *, actor, profile, document, context, plan, job_id):
        self.actor = actor
        self.profile = profile
        self.document = document
        self.context = MappingProxyType(dict(context))
        self.plan = plan
        self.job_id = job_id

    @property
    def planning_context(self) -> dict:
        """Return the site, location and tenant the preview plans against."""
        return _planning_context(self.context)

    @property
    def reviewed_fingerprint(self) -> str:
        """Return the profile fingerprint the stored plan was made under."""
        return self.plan.profile_fingerprint

    @cached_property
    def workspace(self) -> ReviewWorkspace:
        """Return the stored plan as the acting operator's Review Workspace."""
        return ReviewWorkspace(self.plan, self.actor)


@dataclass(frozen=True)
class CommandOutcome:
    """What one command did. A command never stores a plan: the coordinator stores `plan` for it."""

    message: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    # A replacement the command already produced, inside its own savepoint.
    plan: ImportPlan | None = None
    # Commit what the command wrote durably, keep the preview unchanged, then raise this.
    refusal: Exception | None = None
    # The state and Job a queueing command leaves behind, and its after-commit compensation.
    state: str | None = None
    job_id: int | None = None
    compensate: Callable[[], None] | None = None


@dataclass(frozen=True)
class PreviewResult:
    """A committed command: the claim the page now holds, the outcome, and the preview's profile."""

    claim: PreviewClaim
    outcome: CommandOutcome
    profile_id: int | None
    state: str


class PreviewCommand:
    """One coordinated change of the active preview (ADR 0004).

    A subclass implements `apply`, which runs under the coordinator and profile locks against the
    stored plan. The coordinator then replans when `replans` is set, and advances the revision when
    `advances` is set. `apply` raises to refuse; everything it wrote rolls back.
    """

    allowed_states: ClassVar[frozenset[str]] = frozenset({PreviewState.READY})
    profile_action: ClassVar[str] = "change"
    reviews_policy: ClassVar[bool] = True
    replans: ClassVar[bool] = True
    advances: ClassVar[bool] = True
    reads_plan: ClassVar[bool] = True
    # Only setup replaces an expired preview; every other command finds it gone.
    replaces_expired: ClassVar[bool] = False

    def accepts(self, row: PreviewCoordinator, expected: PreviewClaim) -> bool:
        """Return whether the posted claim names the row exactly."""
        return _exact(row, expected)

    def apply(self, preview: LockedPreview) -> CommandOutcome:
        """Make this command's own writes against the locked preview."""
        raise NotImplementedError

    def run(self, request, row: PreviewCoordinator) -> CommandOutcome:
        """Run the command against the locked row; the generic path every writer takes."""
        _refuse_state(row, self.allowed_states)
        with locked_profile_policy(row.profile_id):
            _refresh_actor(request.user)
            profile = (
                ImportProfile.objects.restrict(request.user, self.profile_action).filter(pk=row.profile_id).first()
            )
            if profile is None:
                raise ImportProfile.DoesNotExist("The import profile is no longer available.")
            _refuse_unplannable(profile)
            document = SourceDocument.objects.filter(pk=row.source_document_id, profile=profile).first()
            if document is None:
                raise StaleSourceDocument(SOURCE_GONE)
            plan = None
            if self.reads_plan:
                plan = _stored_plan(row, request.user, document)
                if self.reviews_policy:
                    refuse_moved_policy(profile, plan.profile_fingerprint)
            preview = LockedPreview(
                actor=request.user,
                profile=profile,
                document=document,
                context=row.context or {},
                plan=plan,
                job_id=row.job_id,
            )
            outcome = self.apply(preview)
            if outcome.refusal is not None:
                # atomic-exit-safe: durable-refusal-committed
                return outcome
            replacement = outcome.plan
            if replacement is None and self.replans:
                replacement = ImportEngine.plan(profile, document, request.user, preview.planning_context)
            if replacement is not None:
                _store_plan(row, replacement)
                row.state = PreviewState.READY
                row.job_id = None
            if outcome.state is not None:
                row.state = outcome.state
                row.job_id = outcome.job_id
            if self.advances:
                row.revision += 1
            row.save()
            # atomic-exit-safe: command-published
            return outcome


def _refuse_unplannable(profile) -> None:
    """Refuse a profile whose adapter this release cannot plan, because no command can replan it."""
    try:
        validate_registered_adapter(profile)
        validate_adapter_target_module(profile.source_adapter)
    except ValidationError as exc:
        raise PreviewCommandRefused("; ".join(exc.messages), 409) from exc


def _refuse_state(row: PreviewCoordinator, allowed) -> None:
    """Refuse a command the row's state does not take, naming why."""
    if row.state in allowed:
        return
    if row.state == PreviewState.SYNC_PENDING:
        raise StalePreview(RETAINED_SYNC_BLOCK_REASON if _job_is_active(row.job_id) else SYNC_FINISHED)
    if row.state == PreviewState.SUBMITTED:
        raise StalePreview(SUBMITTED_PREVIEW)
    if row.state == PreviewState.EXPIRED:
        raise StalePreview(EXPIRED_PREVIEW)
    raise StalePreview(NO_PREVIEW)


def _stored_plan(row: PreviewCoordinator, actor, document) -> ImportPlan:
    """Return the row's plan, refusing one this release cannot read or one made for another preview."""
    try:
        plan = ImportPlan.from_dict(row.plan)
    except PlanError as exc:
        raise StalePreview(UNREADABLE_PREVIEW) from exc
    if (
        plan.actor != str(actor.pk)
        or plan.source_fingerprint != document.content_fingerprint
        or dict(plan.planning_context) != _planning_context(row.context or {})
    ):
        logger.warning("Preview coordinator %s holds a plan made for another preview.", row.pk)
        raise StalePreview(UNREADABLE_PREVIEW)
    return plan


def validate_preview_plan(plan: ImportPlan) -> dict:
    """Return a storable preview, or refuse it before the command's writes commit."""
    data = plan.to_dict()
    if len(canonical_json(data).encode()) > MAX_PLAN_BYTES:
        raise PreviewCommandRefused(PREVIEW_TOO_LARGE, status=413)
    return data


def _store_plan(row: PreviewCoordinator, plan: ImportPlan) -> None:
    row.plan = validate_preview_plan(plan)


def apply_preview_command(request, expected: PreviewClaim, command: PreviewCommand) -> PreviewResult:
    """Run one command under the coordinator lock and publish what it changed; refuse a stale claim."""
    branching.refuse_branch()
    binding = _session_binding(request)
    if binding is None:
        raise StalePreview(NO_PREVIEW)
    committed: list[bool] = []
    outcome: CommandOutcome | None = None
    refusal: Exception | None = None
    try:
        with transaction.atomic():
            # Registered first, so it runs before a queue push that may raise after the commit.
            transaction.on_commit(lambda: committed.append(True))
            row = _owned(
                PreviewCoordinator.objects.select_for_update().filter(session_binding=binding).first(),
                request.user,
            )
            row = _refuse_claim(row, expected, command)
            if _payload_expired(row) and not command.replaces_expired:
                _expire(row)
                refusal = StalePreview(EXPIRED_PREVIEW)
            else:
                outcome = command.run(request, row)
                refusal = outcome.refusal
            claim, profile_id, state = _claim_of(row), row.profile_id, row.state
    except BaseException:
        if not committed:
            # Everything the command wrote rolled back, so NetBox must not send the events it queued.
            clear_events.send(sender=PreviewCommand)
        elif outcome is not None and outcome.compensate is not None:
            outcome.compensate()
        raise
    if refusal is not None:
        raise refusal
    if outcome is None:
        raise RuntimeError("A preview command finished without an outcome.")
    return PreviewResult(claim=claim, outcome=outcome, profile_id=profile_id, state=state)


def _refuse_claim(
    row: PreviewCoordinator | None, expected: PreviewClaim, command: PreviewCommand
) -> PreviewCoordinator:
    """Refuse a command whose claim does not name this session's row as the command requires."""
    if row is None:
        raise StalePreview(NO_PREVIEW)
    if not command.accepts(row, expected):
        raise StalePreview(
            STALE_PREVIEW if secrets.compare_digest(row.preview_token, expected.token) else NEWER_PREVIEW
        )
    return row


class StartPreview(PreviewCommand):
    """Store a new upload, plan it, and make it the session's preview (the setup command).

    It may replace any revision of the generation the setup page showed, never a newer one.
    """

    allowed_states = frozenset({value for value, _label in PreviewState.CHOICES})
    replaces_expired = True

    def __init__(self, *, profile, content: bytes, filename: str, site, location=None, tenant=None):
        self.profile = profile
        self.content = content
        self.filename = filename
        self.context = {
            "site_id": site.pk,
            "location_id": location.pk if location else None,
            "tenant_id": tenant.pk if tenant else None,
            "filename": filename,
        }

    def accepts(self, row, expected):
        """Return whether the claim names the row's generation, at any revision it reached."""
        return _same_generation(row, expected)

    def run(self, request, row):
        """Store the upload, plan it, and start a new generation in the locked row."""
        with locked_profile_policy(self.profile.pk):
            _refresh_actor(request.user)
            profile = ImportProfile.objects.restrict(request.user, "change").filter(pk=self.profile.pk).first()
            if profile is None:
                raise ImportProfile.DoesNotExist("The import profile is no longer available.")
            document = SourceDocument.store(
                profile=profile, content=self.content, filename=self.filename, uploaded_by=request.user
            )
            plan = ImportEngine.plan(profile, document, request.user, _planning_context(self.context))
            # A new generation: every claim on the old one is refused from here on.
            row.preview_token = _new_token()
            row.revision += 1
            row.state = PreviewState.READY
            row.profile_id = profile.pk
            row.source_document_id = document.pk
            row.context = self.context
            row.job_id = None
            _store_plan(row, plan)
            row.expires_at = _payload_expiry(request)
            row.save()
            # atomic-exit-safe: new-generation-published
            return CommandOutcome(payload={"plan": plan})


class DiscardPreview(PreviewCommand):
    """End the preview; the next setup starts from an empty generation."""

    allowed_states = frozenset(PreviewState.ACTIVE)
    # An expired preview is still discarded, rather than refused for having expired.
    replaces_expired = True

    def run(self, request, row):
        """Clear the locked row and start an empty generation."""
        _refuse_state(row, self.allowed_states)
        _clear_payload(row, PreviewState.EMPTY)
        row.preview_token = _new_token()
        row.revision += 1
        row.expires_at = _session_expiry(request)
        row.save()
        return CommandOutcome(message="The import preview was discarded.")


class RereadPreview(PreviewCommand):
    """Replan the preview from its stored source against live NetBox.

    It is also the schema recovery, so it never reads the stored plan, and it ends a finished trace
    sync's hold. It refuses while that sync's Job still runs, because NetBox is mid-write then.
    """

    allowed_states = frozenset({PreviewState.READY, PreviewState.SYNC_PENDING, PreviewState.SUBMITTED})
    reviews_policy = False
    reads_plan = False

    def run(self, request, row):
        """Re-read a submitted preview only if its native Job has disappeared."""
        from core.models import Job

        if row.state == PreviewState.SUBMITTED and Job.objects.filter(pk=row.job_id).exists():
            raise StalePreview(SUBMITTED_PREVIEW)
        return super().run(request, row)

    def apply(self, preview):
        """Refuse while any sync of this source still writes; the coordinator replans."""
        from core.choices import JobStatusChoices
        from .jobs import ImportJobRunner, recover_abandoned_import_job, retained_sync_running

        jobs = ImportJobRunner.get_jobs().filter(
            user=preview.actor,
            data__keeps_preview=True,
            data__profile_id=preview.profile.pk,
            data__source_document_id=preview.document.pk,
            status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
        )
        for job in jobs:
            recover_abandoned_import_job(job)

        if retained_sync_running(preview.actor, preview.profile.pk, preview.document.pk):
            raise StalePreview(RETAINED_SYNC_BLOCK_REASON)
        return CommandOutcome(message="The preview was re-read from NetBox.")


class RestorePreview(PreviewCommand):
    """Return to the preview whose final import failed, replanned against live NetBox."""

    allowed_states = frozenset({PreviewState.SUBMITTED})
    reviews_policy = False
    reads_plan = False

    def __init__(self, job_id: int):
        self.job_id = job_id

    def apply(self, preview):
        """Refuse a Job this preview did not submit, or one that did not fail."""
        from core.choices import JobStatusChoices
        from core.models import Job
        from .jobs import recover_abandoned_import_job

        if preview.job_id != self.job_id:
            raise StalePreview("This preview does not belong to that failed import.")
        job = Job.objects.filter(pk=self.job_id).first()
        if job is not None:
            recover_abandoned_import_job(job)

        failed = Job.objects.filter(
            pk=self.job_id, status__in=(JobStatusChoices.STATUS_FAILED, JobStatusChoices.STATUS_ERRORED)
        )
        if preview.job_id != self.job_id or not failed.exists():
            raise StalePreview("This preview does not belong to that failed import.")
        return CommandOutcome(message="The preview of the failed import was re-read from NetBox.")


class QueueImport(PreviewCommand):
    """Queue a selection of the reviewed plan, recording the Job on the preview it came from.

    The final import submits the preview. A per-trace sync keeps it, pending until its Job ends.
    """

    replans = False
    keeps_preview: ClassVar[bool] = False

    def selection_for(self, preview: LockedPreview) -> list[str]:
        """Return the Synchronization Units to queue, or raise PreviewCommandRefused."""
        raise NotImplementedError

    def apply(self, preview):
        """Queue one Job for the selection and leave the preview submitted or pending on it."""
        from core.choices import JobNotificationChoices, JobStatusChoices
        from core.models import Job

        from .cable_disclosure import redact_deleted_cables
        from .jobs import ImportJobRunner, retained_sync_running

        selection = self.selection_for(preview)
        # The Job rows order this against a sync another request queued for the same source.
        if retained_sync_running(preview.actor, preview.profile.pk, preview.document.pk):
            raise StalePreview(RETAINED_SYNC_BLOCK_REASON)
        job = ImportJobRunner.enqueue(
            name=ImportJobRunner.name,
            user=preview.actor,
            notifications=JobNotificationChoices.NOTIFICATION_NEVER,
            job_timeout=3600,
            profile_id=preview.profile.pk,
            source_document_id=preview.document.pk,
            accepted_plan=redact_deleted_cables(preview.plan.to_dict()),
            selection=selection,
            idempotency_key=uuid.uuid4().hex,
        )
        job.data = {
            "job_type": ImportJobRunner.job_type,
            "phase": "queued",
            "processed": 0,
            "total": 0,
            "filename": preview.context.get("filename", ""),
            "profile_id": preview.profile.pk,
            "profile_name": preview.profile.name,
            "source_document_id": preview.document.pk,
            "context_data": dict(preview.context),
            # What makes this Job hold its preview, for the conservative check above.
            "keeps_preview": self.keeps_preview,
        }
        job.save(update_fields=["data"])

        def compensate():
            # The push runs after commit, so a Job no worker will run must not hold the preview.
            Job.objects.filter(pk=job.pk, status=JobStatusChoices.STATUS_PENDING).update(
                status=JobStatusChoices.STATUS_ERRORED
            )
            _release_job(job.pk)

        return CommandOutcome(
            state=PreviewState.SYNC_PENDING if self.keeps_preview else PreviewState.SUBMITTED,
            job_id=job.pk,
            compensate=compensate,
        )


def _release_job(job_id: int) -> None:
    """Return a preview to review when its queued Job never reached a worker, and only that one."""
    with transaction.atomic():
        row = PreviewCoordinator.objects.select_for_update().filter(job_id=job_id).first()
        if row is None or row.state not in PreviewState.HOLDS_JOB:
            # atomic-exit-safe: release-not-needed
            return
        row.state = PreviewState.READY
        row.job_id = None
        row.revision += 1
        row.save()


def expire_previews(*, now=None) -> tuple[int, int]:
    """Clear payloads past their expiry and delete rows long after it; return both counts.

    A deleted row cannot revive a claim: only the setup page creates a row, with a fresh token.
    """
    reference_time = now or timezone.now()
    if timezone.is_naive(reference_time):
        raise ValueError("now must be timezone-aware")
    expired = 0
    while True:
        with transaction.atomic():
            batch = list(
                PreviewCoordinator.objects.select_for_update(skip_locked=True)
                .filter(state__in=sorted(PreviewState.ACTIVE), expires_at__lte=reference_time)
                .order_by("pk")[:100]
            )
            for row in batch:
                _expire(row)
        expired += len(batch)
        if len(batch) < 100:
            break
    cutoff = reference_time - PreviewCoordinator.PAYLOAD_LIFETIME
    deleted, _ = PreviewCoordinator.objects.filter(
        state__in=(PreviewState.EMPTY, PreviewState.EXPIRED), expires_at__lte=cutoff
    ).delete()
    return expired, deleted


__all__ = (
    "CLAIM_FIELDS",
    "CommandOutcome",
    "DiscardPreview",
    "LockedPreview",
    "PreviewClaim",
    "PreviewCommand",
    "PreviewCommandRefused",
    "PreviewResult",
    "PreviewSnapshot",
    "QueueImport",
    "RereadPreview",
    "RestorePreview",
    "StalePreview",
    "StartPreview",
    "apply_preview_command",
    "expire_previews",
    "read_preview",
    "setup_claim",
)

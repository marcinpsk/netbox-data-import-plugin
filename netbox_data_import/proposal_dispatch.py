# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Start Resolution Proposal Jobs only while the active Inference Backend has a free request slot.

A requested proposal waits as a queued row without a Job. The dispatcher gives the oldest waiting rows
a Job while slots are free. A slot is in use while a proposal's Job can still run, whatever the
proposal's status: the worker of a cancelled row can still be inside its backend call.
"""

import logging
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone
from netbox.constants import RQ_QUEUE_LOW
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .inference_backend import NoActiveInferenceBackend, resolve_active_backend
from .inference_credentials import CredentialFailure
from .inference_trust import InvalidInferenceConfiguration
from .jobs import ResolutionProposalJob, _task_lost, import_queue_task
from .models import ProposalFailureReason, ProposalStatus, ResolutionProposal
from .proposal_jobs import CREDENTIAL_FAILURE_REASONS, proposal_job_timeout
from .resolution_proposals import fail_proposal, fail_queued_proposal, record_proposal_job

logger = logging.getLogger(__name__)

#: How long a running Job may stay past its own timeout before its slot counts as leaked.
STALE_SLOT_GRACE = timedelta(minutes=1)
SLOT_TASK_LOST = "The proposal task is no longer queued or running."
SLOT_TIMED_OUT = "The proposal Job ran past its timeout without ending."
_DISPATCH_LOCK = "netbox-data-import-proposal-dispatch"


def dispatch_proposals() -> list[int]:
    """Release leaked slots, then give the oldest waiting proposals a Job while slots are free.

    Call it outside a transaction. Return the ids of the dispatched proposals. A queue push that
    fails after the commit fails its attempt with queue_unavailable, and its error propagates.
    """
    committed: list[bool] = []
    dispatched: list[tuple[int, object]] = []
    try:
        with transaction.atomic():
            # Registered first, so it runs before a queue push that may raise after the commit.
            transaction.on_commit(lambda: committed.append(True))
            # Two dispatchers that both counted the same free slot would exceed the limit.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [_DISPATCH_LOCK])
            dispatched = _fill_free_slots()
    except BaseException:
        if committed:
            for proposal_id, job in dispatched:
                _fail_unpushed(proposal_id, job)
        raise
    return [proposal_id for proposal_id, _job in dispatched]


def _fill_free_slots() -> list[tuple[int, object]]:
    """Enqueue one Job for each waiting proposal that a free slot can take, oldest first."""
    waiting = ResolutionProposal.objects.filter(status=ProposalStatus.QUEUED, job__isnull=True).order_by(
        "created", "pk"
    )
    try:
        backend = resolve_active_backend()
    except (NoActiveInferenceBackend, InvalidInferenceConfiguration, CredentialFailure) as exc:
        _release_leaked_slots(None)
        # No Job could call a backend, so each waiting row fails with the reason its Job would record.
        reason = ProposalFailureReason.INVALID_CONFIGURATION
        if isinstance(exc, CredentialFailure):
            reason = CREDENTIAL_FAILURE_REASONS.get(exc.category, ProposalFailureReason.CREDENTIAL_UNAVAILABLE)
        for proposal_id in waiting.values_list("pk", flat=True):
            fail_queued_proposal(proposal_id, reason=reason)
        return []
    job_timeout = proposal_job_timeout(backend)
    free = max(backend.max_concurrent_requests - _release_leaked_slots(job_timeout), 0)
    dispatched = []
    for proposal in waiting.select_related("requested_by")[:free]:
        # The low queue lets a trace sync queued later start before minutes of inference.
        job = ResolutionProposalJob.enqueue(
            name=ResolutionProposalJob.Meta.name,
            user=proposal.requested_by,
            queue_name=RQ_QUEUE_LOW,
            job_timeout=job_timeout,
            proposal_id=proposal.pk,
        )
        record_proposal_job(proposal.pk, job)
        dispatched.append((proposal.pk, job))
    return dispatched


def _release_leaked_slots(job_timeout: int | None) -> int:
    """Release each slot whose Job can no longer run, and return how many slots stay in use."""
    from core.choices import JobStatusChoices
    from core.models import Job

    in_use = 0
    holders = ResolutionProposal.objects.filter(job__status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES)
    for proposal in holders.select_related("job"):
        job = proposal.job
        if _task_lost(job, import_queue_task(job)):
            reason, message = ProposalFailureReason.QUEUE_UNAVAILABLE, SLOT_TASK_LOST
        elif (
            job_timeout is not None
            and job.status == JobStatusChoices.STATUS_RUNNING
            and job.started is not None
            and job.started + timedelta(seconds=job_timeout) + STALE_SLOT_GRACE < timezone.now()
        ):
            reason, message = ProposalFailureReason.TIMEOUT, SLOT_TIMED_OUT
        else:
            in_use += 1
            continue
        logger.warning("Released the request slot of Resolution Proposal %s: %s", proposal.pk, message)
        Job.objects.filter(pk=job.pk, status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).update(
            status=JobStatusChoices.STATUS_ERRORED, completed=timezone.now(), error=message
        )
        # A terminal row keeps its status; only an attempt that is still active fails here.
        fail_proposal(proposal.pk, reason=reason)
    return in_use


def _fail_unpushed(proposal_id: int, job) -> None:
    """Fail an attempt whose task never reached the queue, so it neither waits nor holds a slot."""
    from core.choices import JobStatusChoices
    from core.models import Job

    try:
        pushed = import_queue_task(job) is not None
    except (RedisConnectionError, RedisTimeoutError):
        # A queue that cannot answer cannot show the task, so the attempt fails.
        pushed = False
    # A failed push stops only the later pushes, so a task that reached the queue keeps its attempt.
    if pushed:
        return
    # A worker that took the attempt owns it, so only an attempt still queued fails here.
    if fail_queued_proposal(proposal_id, reason=ProposalFailureReason.QUEUE_UNAVAILABLE):
        Job.objects.filter(pk=job.pk, status=JobStatusChoices.STATUS_PENDING).update(
            status=JobStatusChoices.STATUS_ERRORED
        )


__all__ = ("STALE_SLOT_GRACE", "dispatch_proposals")

---
status: accepted
date: 2026-10-01
supersedes: the storage clause of 0001-target-neutral-import-plans.md
---

# Store the active preview in one database Preview Coordinator per browser session

ADR 0001 let an active preview keep its Import Plan in the session, and it added no durable
review-session model until a design proved that resumable plans need one. The trace Device resolution
design ([trace-device-resolution.md](../design/trace-device-resolution.md)) then required one
database-backed compare-and-set record per browser session, for concurrent correctness and not for
resumability. That record was never built. The preview lived in `request.session`, so two tabs in one
session, or a late session save by an older response, could overwrite a newer preview for every
workspace writer. A revision kept in the session cannot stop this, because the session itself is the
value that is overwritten.

## Decision

One Preview Coordinator row per authenticated browser session is the authoritative record of the
active preview. It holds the preview generation (a random token), a monotonic revision, an explicit
state, the Import Profile and Source Document identities, the planning context, the materialized
Import Plan, and the association with a queued import Job. The session holds no preview state. The
row is selected by a digest of the server-side session key and is checked against its owner on every
access, so a rotated or ended session cannot reach an old preview.

Every preview command posts a Preview Claim: the token, the revision, the Source Document, and the
profile. A read that answers a question the page displays carries the same claim. The coordinator
module is the only owner of the transaction. It locks the coordinator row, then the profile policy
row, then any proposal or target row, and never the reverse. Under those locks it validates the
claim, reloads the profile and the document, compares the reviewed profile fingerprint, runs the
command, replans, stores the new plan, and advances the revision. Only the coordinator publishes a
plan.

The claim rules are:

- An ordinary command needs exact equality of all four claim values. A second request with the
  same claim waits for the first and receives HTTP 409, with no write.
- A new setup may replace any revision of the same preview generation, but never a newer
  generation. Two setups started from one generation therefore have one winner. A decision that
  locks before a setup can commit; the setup then replaces its preview.
- A missing coordinator row is never created by a command. Only the setup page creates the empty
  row, so a stale claim cannot bootstrap a replacement.

Plain page loads are read-only. Re-reading a preview from NetBox, recovering a plan whose schema
changed, discarding a preview, and returning to a preview after a failed import are coordinated POST
commands. Proposal acceptance writes the resolution and replans in one coordinated transaction, so
the next decision needs no re-read. A synchronous single-row sync uses the Import Engine operation
that executes a selection and replans inside one savepoint: the audit reservation stays outside the
savepoint, a failed execution or replan rolls back the NetBox writes and the replan together, the
failed audit row is kept, and the preview revision does not advance.

Workers never read or write the coordinator. Queueing an import records the Job on the coordinator
and advances the revision under the locks; the queue push happens after commit. A push that fails
after commit is compensated: the Job is marked errored, and the coordinator is reset only while it
still holds that generation and that Job. A per-trace sync keeps the preview in a pending state that
refuses decisions, re-reads and further syncs until its Job is terminal. A terminal Job does not
make the old plan executable: the operator re-reads first. A new setup detaches the old preview from
its Job without cancelling it.

A preview payload expires no later than the session or 30 days after the setup that created it,
which matches the Source Document retention. Housekeeping clears an expired payload under the
coordinator lock and keeps the empty, expired generation, so a stale claim still receives 409. The
coordinator references the profile and the document by scalar id and validates both live, so a
deleted profile or a purged document never cascades away the compare-and-set record. The model has
no foreign key to a branchable model and stays on main only.

A stale command receives a real HTTP 409 in every response format: a JSON envelope, a page for a
form post, and a 409 with a redirect header for an HTMX request.

Amended 2026-10-08: a command that changes no plan does not advance the revision. Requesting,
cancelling and rejecting a Resolution Proposal, one at a time or for every open termination, write
only proposal rows, and the plan and every claim on the page stay valid. So the operator can ask about
several terminations from one page load, and each card updates in place. The proposal lifecycle, not
the revision, orders two such commands: the partial unique index and the conditional updates refuse a
second active proposal or a second decision. A command that writes policy or replans, which includes
proposal acceptance, still advances the revision.

## Considered options

- A lock around the session helpers: rejected. Response middleware saves the session after the lock
  is released, so an older response can still restore an older preview.
- A claim scoped to one Source Document: rejected in the design's second ratification round. It did
  not order an old decision against a new upload in the same session.
- A database row that holds only a pointer, with the plan still in the session: rejected in the
  third ratification round, for the same late-save reason.
- Deferred recalculation, where a write marks the preview stale and a later page load replans:
  rejected. A stale preview cannot carry an exact claim, and a page load that replans is a write that
  no claim guards.

## Consequences

Every flat-preview row action now replans in its own transaction, and the page reloads after each
successful command that advances the revision. A trace workspace command swaps the page content with
htmx, which renders every claim on the page again. The setup, progress and results pages no longer change the preview when they
load. The results page takes the Import Execution in its URL. The `import_*` session keys are gone,
and a test keeps any new code from reading or writing preview state through the session.

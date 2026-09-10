<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> -->

# T8 Resolution Proposal: design record

Revision r6 (RATIFIED at round 5, after adversarial rounds 1-5). The adapter diagnostic mechanism was split to #94 and is now an implemented prerequisite, not a deferral. Scope: issue #95, the 13 acceptance criteria of ticket T8. Design only; no T8
production code exists yet.

## Problem, as a class

A suggestion must never silently become an authority. The failure class covers: a proposal accepted
after the world moved, a decision recorded twice, a late worker response overwriting a terminal row,
an LLM-invented candidate id treated as a selection, and a credential reachable through
operator-supplied input.

## How this record was produced

Two designs from one factual brief, neither designer seeing the other's until both were complete.
The blind design ran on `gpt-6-astra` at high reasoning in a fresh `codex exec` context; the brief
carried the problem, the hard constraints, the merged seams and the known spec conflict, and no
proposed solution. Convergence below is not treated as verification.

## Divergence table

| # | Decision | Primary (Claude) | Blind (astra) | Disposition | Evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | Backend selection | payload = proposal id; worker calls `resolve_active_backend()` | same | **Converged.** Requires a spec amendment to 7.5 and 10.6 | `resolve_backend_by_id` docstring: "an editable key must not let a scoped operator resolve the deployment's own credential reference" |
| 2 | Candidate-set completeness | not addressed | refuse unless `0 < total <= 20` and `total == len(candidates)` | **Revised in r2.** Completeness kept, the ceiling of 20 rejected | see blocker 1 below |
| 3 | Raw-response retention vs no-secret rule | not addressed | credential-echo exception: fail, redact, record that redaction occurred | **Take blind.** Criterion 7 and constraint 12 cannot both mean unconditional byte-for-byte retention | ticket criteria vs spec 8.6 |
| 4 | Lost worker / orphaned active row | not addressed | dispatch + execution deadlines, run token, recovery job | **Take blind.** Primary's partial unique index makes a stuck row block that key forever | self-inflicted by disposition 6 |
| 5 | Model family | unstated | plain audit model, excluded from policy export | **Take blind.** `TerminationResolution` is a `PolicySectionModel`; making the proposal one too would put it in profile YAML export | `test_inference_secret_containment` asserts no `credential_reference` in the YAML export |
| 6 | One-active-per-key | partial unique index `WHERE status IN ('queued','running')` | same | **Converged** | mirrors the `InferenceBackend` one-enabled-row index |
| 7 | Snapshot equality | exclude `display_name` | include `display_name`; a rename is stale | **Take blind.** The model chose on labels, so a changed label changes the evidence | spec 7.4 "the eligible candidate set changed" |
| 8 | Cancel vs late response | conditional `UPDATE ... WHERE status='running'`; rowcount 0 = discard | lock + reread + run token | **Take primary as the mechanism, blind's run token as an addition.** A conditional update needs no lock and cannot be lost to a read-then-write | |
| 9 | Temporal invariants | conditional UPDATE + `CheckConstraint` | `django-pgtrigger` OLD/NEW triggers | **CONTESTED → round 1** | a new runtime dependency needing DDL rights on every deployment |
| 10 | Freshness protection | row locks on frozen candidates + resolved Device, recompute under `locked_profile_policy` | SHARE relation locks on `dcim` tables with NOWAIT | **CONTESTED → round 1.** Primary's position: the repo already answers this | `locked_profile_policy` docstring: locking child rows "would leave an insert free to land in that window, because a row that does not exist yet cannot be locked" — the profile row is that lock, and `save_termination_resolution_and_replan` already takes it |
| 11 | Response schema validation | hand-written validator | add `jsonschema` dependency | **CONTESTED → round 1.** Lean primary: the rules are a dozen exact checks | |
| 12 | Generic lifecycle vs concrete FK | provider writes the resolution | adapter returns an opaque `DecisionReceipt`; coordinator stores `written_resolution_id` | **Take blind.** Cleaner: the coordinator never interprets a termination | |
| 13 | Task seam | registry of task-type providers | closed registry with `prepare`/`current`/`write_resolution` | **Converged** | |

## Merged design (r1)

**Modules.** `resolution_proposals.py` (lifecycle: request, read, cancel, decide),
`proposal_tasks.py` (closed task-type registry — the seam that keeps termination specifics out),
`termination_proposal.py` (the only module knowing termination kinds and the `TerminationResolution`
FK), `proposal_response.py` (pure strict validator), `proposal_jobs.py` + a thin `ResolutionProposalJob`.

**Invariant ownership.** Database: the partial unique index (one active per key), check constraints
for decision-group shape, `no_match` carries no selection, terminal rows carry their content.
Service: every ordering-dependent rule as a conditional `UPDATE` whose rowcount is the refusal —
status edges, one-shot decision, and the cancel-versus-late-response race. Model: digest derivation
and canonical-key validation only.

**Acceptance transaction.** `locked_profile_policy(profile_id)` → `select_for_update` the proposal →
recheck completed/candidate/undecided → recompute candidates and resolved Device → compare → upsert
`TerminationResolution` → conditional-write the decision group. Lock order is always profile, then
proposal, then resolution.

**Backend selection.** The payload carries the proposal id alone. The worker calls
`resolve_active_backend()` after claiming, and records its source. No operator-editable selector
reaches credential resolution.

## Section 0: closed and refuted claims

Round 1 (`gpt-6-astra`, high, fresh read-only context) returned **BLOCKED r1** with three contested
items closed for the primary design.

| Claim | State | Evidence | Reopening condition |
| --- | --- | --- | --- |
| Temporal invariants need `django-pgtrigger` OLD/NEW triggers | **CLOSED for primary** | Reviewer found no current or proposed proposal writer outside the service functions, and no race the conditional UPDATE leaves open | An actual writer bypasses the service predicates |
| Freshness needs `SHARE` relation locks on `dcim` tables | **CLOSED for primary** | Reviewer could not construct an interleaving meeting the overturn conditions; relation-wide SHARE is not justified for a revalidation requirement | Acceptance must guarantee inventory equality *through commit* rather than revalidate inside the transaction |
| Response validation needs a `jsonschema` dependency | **CLOSED for primary** | Fixed contract, a dozen exact checks | The response contract becomes externally supplied or substantially more complex |
| Narrowing candidates by port text removes the ceiling problem | **REFUTED** (round 2) | Normal resolution already tries the normalized exact port name; zero or several matches is exactly what leaves the field unresolved. For source `Gi1/0/1` against inventory `GigabitEthernet1/0/1`, name filtering removes the candidate the proposal exists to consider. The `mapped_peer` narrowing at `cable_target.py:371` is justified by real `PortMapping` relationships, which have no equivalent for an unresolved termination name | An authoritative naming or mapping rule proves excluded ports ineligible |

One correction the reviewer made to my own reasoning, recorded because it matters: `locked_profile_policy`
orders **cooperating profile-policy writers**. It does not protect against arbitrary NetBox inventory
inserts, and I overstated it when I cited it. The disposition still stands, on the weaker and correct
ground that revalidation is not serialization.

A second correction: duplicate JSON member names must be rejected **during decoding** via
`object_pairs_hook`. Python's decoder keeps the last duplicate, so any check after `json.loads` cannot
see that a duplicate existed.

## Blockers found in round 1, and their r2 revisions

**Blocker 1 — the ceiling of 20 would make the feature unusable.** Verified in source: for the
`termination` role, `eligible_terminations` returns every visible termination of the claimed kind on
the resolved Device; the port-name narrowing at `cable_target.py:371` applies only to the
`mapped_peer` role. A 48-port switch therefore yields `total=48`, and an `r1` request would refuse
every interface question on ordinary equipment. The 20 is the **picker's page size**, not an
eligibility bound.

*r3 (CLOSED in round 2):* keep complete-set comparison, and give the proposal its own retrieval
bound, independent of `ELIGIBLE_TERMINATION_LIMIT`. Require `0 < total <= bound` and
`len(candidates) == total`; refuse with `too_many_candidates` above it and `no_candidates` at zero.
A truncated result must never establish freshness.

**Operator decision, taken 2026-09-10: the bound is a plugin setting, `inference_proposal_candidate_limit`,
defaulting to 64,** so a deployment can support denser equipment or hold prompt cost down without a
code change.

Section 8.2.1 defines no shape for a new setting, so r4 states the predicate explicitly. It is
validated at startup even when inference is otherwise unconfigured:

- accept a positive integer; reject `bool`, `float`, `str` and an explicit `None`
- apply the default of 64 only when the key is **omitted**
- reject a value beyond the retrieval implementation's supported numeric range

`True` is the trap worth naming: it is numerically 1 and would silently admit a one-candidate set.
The repository already excludes booleans this way in its integer timeout validation
(`inference_settings.py:205`), and this setting follows it. Zero and negatives admit no set at all;
`10**100` must never reach a database slice, because `eligible_terminations` materializes the slice
before computing `total`.

**Blocker 2 — T7's adapter cannot supply the raw response T8 must retain.** Neither design saw this.
Verified in source: `InferenceCompletion` carries `content_text`, `is_refusal`, `finish_reason` and
the backend ids only, so a structured refusal's text is discarded; and
`inference_adapter.py:220` raises `MalformedEnvelope(...)` with a message string for a non-`stop`
finish reason, before any content is read, so the body is lost. Criterion 7 ("sets the row to failed
with its typed reason **and the raw response text**") is not implementable through the current seam.

*r3:* the adapter's **diagnostic** interface carries the complete HTTP body, decoded to text and
**captured before any validation**, alongside its existing typed classifications. Transport ownership
is unchanged and no `requests.Response`, session, headers, prepared request or credential-bearing
object crosses the seam: strings and existing scalars suffice.

Round 2 found r2's version still short. `_read` raises `InvalidBackendConfiguration` (redirect),
`AuthenticationFailure` (401/403), `RateLimited` (429) and `TransportFailure` (>=400) **before**
`response.json()` is ever called, verified at `inference_adapter.py:190-205`, so a 401 carrying
diagnostic text loses it. Spec 7.3 requires retaining failure response text when one was received.

*r3 therefore extends diagnostic carriage to* **every typed adapter failure for which a body was
received**, distinguishing "no body received" from "an empty body", and preserving every existing
classification and retry behaviour.

Sanitization is correspondingly wider: the worker redacts response-derived **metadata** (the adapter
copies backend ids and model straight from the envelope) and **exception text** (a non-`stop`
finish reason is interpolated unchecked into the `MalformedEnvelope` message) as well as the retained
body. Redacting only the body would leave the other two representations exposed.

**Operator decision, taken 2026-09-10: this lands by reopening #94 (T7), not inside T8.** Criterion 7
of T8 was unbuildable against what T7 shipped, so it is a T7 defect. #94 is reopened and carries the
full scope, including the two transport paths round 3 proved by execution: an interrupted body
(`ChunkedEncodingError`, `response=None`, bytes unrecoverable after `post()` returns) and a malformed
redirect target, which raises an **untyped** `ValueError` inside `requests` before `_read()` and so is
not covered by "every typed adapter failure" at all.

**T8 asserts the revised interface rather than tolerating either shape.** Shared contract tests, run
through the real adapter and through failure persistence, require: one diagnostic representation
across successes and every typed failure; absent, empty and partial receipt distinguishable; existing
categories and `RateLimited.retry_after` intact; refusal text and pre-validation bodies available; and
only declared values crossing the seam, with **no `getattr(..., None)`** fallback to the old shape.
Field-existence tests alone would not stop drift.

### Correction in r5: "preserve every existing classification" was wrong

r3 and r4 required the #94 work to preserve every existing classification. Round 4 showed that
requirement contradicts spec 13.3, and it is my error: the blind design raised this as divergence 6,
I dispositioned it "take blind", and then failed to carry it into the merged requirements.

Spec 13.3 makes HTTP **400, 404 and 405 non-transient** (fail immediately) and **500, 502, 503 and
504 transient** (bounded retry). `inference_adapter.py:200` collapses all seven into `TransportFailure`
with category `transport_failure` and no status or retryability field. Verified by executing the real
`_read()` against real `requests.Response` objects: every one of those statuses produced the same
category.

So T8 cannot derive its retry policy from the category. Retrying `transport_failure` gives a
misconfigured 404 three attempts; not retrying it breaks the required retries for a temporary 5xx.

**r5 requirement:** the adapter interface must distinguish non-transient HTTP request and
configuration errors from transient failures, by corrected typed classifications or a declared
machine-readable discriminator. The worker never parses exception prose or HTTP objects to guess.
Contract tests assert, through real T8 persistence, **one outbound attempt for 400/404/405** and
**three after repeated transient failures**. The implementation belongs to #94 with the rest of the
adapter work.

## Status

**RATIFIED.** See "Round 5 and revision r6" below for the verdict and its scope.

## Split (round 3)

Blockers stopped clustering in the T8 core after round 1 and now sit entirely in the **adapter
diagnostic mechanism**. That mechanism is deferred to #94 by the operator's decision above.

The retained core is: the model and its database guarantees, the lifecycle and task seam, backend
selection, candidate policy and freshness, the acceptance transaction and lock order, the strict
validator, and worker ownership, cancellation and recovery. All were closed across rounds 1-3.

**The core does not stand fully alone, and this record says so rather than claiming otherwise.**
T8 criterion 7 requires the raw response text, which only the #94 interface can supply. T8 is already
declared blocked by T7 in the specification, so the dependency is stated, not introduced here. The
core design is ratifiable; the *delivery* of T8 waits on #94.

## Open work

Three spec amendments are required and are the operator's call, not this design's:

- Section 7.5's editable-key lookup with same-name file fallback, and the job-payload summary in
  section 10.6. Both diverge from the ratified backend selection. The operator decided on
  2026-09-10 that they land inside T8's first pull request, with the code that diverges from them.
- ~~Section 13.3 does not name HTTP 408 Request Timeout.~~ **Decided 2026-09-10:** 408 stays
  non-transient. RFC 9110 permits a retry there but does not require one. Rather than special-case
  one status, 13.3 now states the rule the adapter implements: the four statuses it names are the
  transient ones, and every other status at or above 400 fails closed with its diagnostic.

## Round 5 and revision r6

Round 5 reviewed r5 against the implemented prerequisite and returned **BLOCKED**. It closed the
design half of the retry requirement and blocked on two defects in the adapter itself.

**Half B belongs to T8's first increment, not to this ratification.** The r5 requirement asked for
contract tests "through real T8 persistence". Round 5 accepted that requiring them before any T8 code
exists confuses design acceptance with delivery acceptance. They stay mandatory as acceptance
conditions on T8's first increment: real adapter, real worker, real proposal row, proving one outbound
attempt for 400/404/405 and three after repeated transient failures, with the terminal failure and its
diagnostic stored.

**Two defects in the prerequisite, both verified here before being accepted, both fixed in `ce49271`.**

| # | Defect | Verification | Resolution |
| --- | --- | --- | --- |
| 5.1 | Every error status outside 400/404/405 was retryable | Reproduced 406, 409, 413, 415, 422 and 501 as `TransportFailure(retryable=True)` against a real local HTTP server | The default is inverted. Specification 13.3 enumerates the transient statuses and nothing else, so `TRANSIENT_STATUSES` decides and every other status at or above 400 is non-transient |
| 5.2 | A decoder failure escaped `complete()` untyped | `response.json()` on `[`x10000 `0` `]`x10000, real `requests.Response`: **3.12.14 and 3.13.5 raise `RecursionError`, 3.14.4 parses it** | Caught with `ValueError`. Two tests, because the suite's own interpreter cannot reproduce the raise |

5.2 carries a trap worth stating plainly: `requires-python` is `>=3.12.0`, so the defect is real on
supported deployments, but the NetBox image the suite runs in ships Python 3.14.4, where CPython's
decoder no longer recurses. An end-to-end test of that input alone would be an assertion incapable of
failing. The guard that can fail on any interpreter drives the real `_read` with a `requests.Response`
subclass whose `json()` raises, and removing `RecursionError` from the catch fails it.

The unused-constant finding closed as a side effect of 5.1: `NON_TRANSIENT_STATUSES` is deleted, and
specification 13.3's transient list now has one declaration that both `_read` and the test read.

**New open question for the specification.** 13.3 does not name HTTP 408 Request Timeout. Under the
inverted default it is now non-transient. That is the operator's call, not this design's.

## Verdict

`RATIFY — revision r5 core, prerequisite at ce49271`, returned after findings 5.1, 5.2 and the
unused-constant finding closed against that commit and the independence objection was re-answered
with the record in scope.

The reviewer's reasoning on independence is recorded because it decides how this protocol treats a
prerequisite: the rule that a retained core must stand "without the deferred mechanism" permits
dependencies on established implementations, or an ordinary dependency on NetBox or the database
would defeat every ratification. The adapter is still a correctness dependency of T8. Its required
function is no longer deferred, because it is built, reviewed and lands first. The r5 text calling
that mechanism "deferred", at lines 194-197, describes the state before ce49271 and is superseded.

Ratification is not a claim that T8 passes its acceptance tests. It must still prove failure
persistence through this interface.

## First implementable increment

The `ResolutionProposal` model, a hand-written migration, and the transition service.

Observable acceptance conditions:

- The five statuses and the edge table, each edge enforced by a conditional `UPDATE` whose rowcount
  is the refusal.
- The partial unique index refuses a second active row for one key.
- Check constraints refuse a half-attributed decision, a `no_match` carrying a selection, and a
  terminal row with no content.
- `field_key_digest` is populated on write.
- The migration depends on `("extras", "0001_initial")`, so the oldest job in the test matrix does
  not error at setup.

Carried into that increment, per the operator on 2026-09-10: the specification amendments to 7.5 and
10.6, so the sections and the code that diverges from them change together.

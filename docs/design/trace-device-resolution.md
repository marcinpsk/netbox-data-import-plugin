<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
-->

# Trace device resolution

## 0. Review record

Round 4: RATIFY. The ratifier accepted the merged design after revisions for policy-row
authorization, placement-evidence permissions, adapter-neutral Source Trace types, exact routing,
and an authoritative database-backed preview coordinator.

## Problem brief

The trace import currently opens the generic preview after setup. It must open the Trace Review
Workspace directly when the selected Source Adapter emits only Source Traces. A future adapter that
mixes output kinds stays on the generic workspace until a combined review surface exists.

The Cable Target Module resolves a source device label only by one exact Device-name match at the
selected import target. When that lookup finds zero or several Devices, the trace is blocked before
the termination picker can offer a port. An operator needs to select the intended NetBox Device once.
The Import Profile must reuse that decision when a later workbook uses the same source device label.

Rack, U position, and Location are corroborating source evidence. They can help rank and explain
Device candidates. They must not identify the source device or silently select a different Device.

The current `trace_workbook` Source Adapter recognizes one fixed workbook shape. Its output types
also live in that parser module, which makes another trace adapter depend on the wrong module.
Supporting changed sheet names, columns, or layouts is follow-up scope. The device-resolution
interface must remain usable by another Source Adapter that emits the same `SourceTrace` output kind.

## Observable acceptance conditions

1. A successful import setup for a profile whose output kinds are exactly `{SOURCE_TRACE}` redirects
   directly to the Trace Review Workspace. A mixed-output profile keeps the generic preview.
2. A source device label with no unique exact Device-name match offers an operator-visible Device
   picker inside that workspace.
3. Candidate reads remain inside the actor's object permissions and the selected import target.
4. Rack, U position, and Location can rank or explain candidates. They never make the final selection.
5. Saving a Device selection replans the current preview and stores one profile-owned decision.
6. A later workbook under the same profile reuses the decision for the same normalized source device
   label, including when it names a different port.
7. A deleted, hidden, moved outside the selected Site, or otherwise ineligible selected Device is
   not used silently. A move within the Site does not break an explicit mapping. Rack and U position
   are evidence, not identity.
8. A trace profile does not require a second helper Import Profile.
9. Flat-workbook Device matching keeps its current behavior.
10. The direct-route and persistent-resolution behavior have end-to-end tests through real views,
    ORM rows, planning, and permission-scoped candidates.

## Evidence

- `ImportSetupView.post` stores the plan and always redirects to `import_preview`.
- `CablePlanner._load_devices` and `_resolve_one` use exact Device-name lookup. No Device candidate
  query follows `trace.device_unresolved`.
- `TerminationResolution` stores a selected termination under a field key that contains device,
  cards, port, kind, and role. It cannot answer a later question about another port on the same source
  device.
- `DeviceExistingMatch` stores a flat source row ID to Device choice and is declared applicable only
  to Device source rows.
- A `SourceTrace` already carries Rack, U position, and Location as corroborating evidence on its
  termination references.

## Candidate shapes

### A. Helper profile

A trace profile refers to a flat-workbook profile and borrows its Device matches.

### B. Trace-owned Device resolution

A new profile-owned decision maps a normalized source device label to one NetBox Device.

### C. Adapter-neutral Device binding

The existing persistent Device-match concept becomes an output-kind-neutral binding used by both
flat Device rows and Source Traces through one shared interface.

## Independent design A

### Ownership and persistent identity

Add a trace-only `TraceDeviceResolution` policy row. It belongs to one Import Profile and maps one
canonical source Device label to one NetBox Device ID. Keep the source label used for display and a
fixed-width digest of its canonical form. Uniqueness applies to the profile and canonical source
label only.

Do not make the target Device unique within a profile. Two source labels can be aliases for the same
Device after a source rename. The explicit mapping makes that aliasing intentional. Existing Cable
and termination conflict checks still reject an impossible topology.

Keep this row separate from `DeviceExistingMatch`. A flat source row has a stable row ID and supplies
Device fields for a create-or-update decision. A trace Device label identifies the owner of a port
reference. These are different identity contracts even though both can point at a Device.

Keep this row separate from `TerminationResolution`. One Device decision must apply to all ports and
all termination kinds named under that source Device label.

### Planning interface

Put trace Device binding behind one Cable Target Module function. Given the profile, every source
Device label in a `SourceTrace` batch, and a permission-scoped `NetBoxReader`, it returns one of:

- a saved manual Device that is still visible inside the selected Site;
- one automatic exact-name match when no saved decision exists;
- an open Device decision with a reason.

A saved decision takes precedence over name matching. If its Device is unavailable, planning keeps
the decision open. It does not silently fall back to a different exact-name match.

The plan carries one Device-resolution presentation record per canonical source label. The Trace
Review Workspace aggregates repeated references, so one decision answers every port on that source
Device. A request can act only on a Device key carried by the active reviewed plan.

### Candidate query and ranking

The candidate endpoint reads only Devices the actor can view in the selected Site. Text search
filters the same set. Each candidate includes a short explanation for matching source evidence.

Order candidates by these hints, then by Device name and primary key:

1. exact source Device name;
2. matching Location;
3. matching Rack;
4. matching U position.

The picker always needs an operator click. Hint scores never select a Device. Conflicting evidence
from repeated references is shown as multiple source values. It is not collapsed to a guessed fact.

### Save and replan

The save endpoint repeats the permission-scoped candidate query that produced the offer. It saves
the policy row and replans while holding the profile policy lock. The current preview records the
new plan only after the transaction succeeds.

If a saved Device is deleted, hidden from the actor, or outside the selected Site, the next plan
shows a generic unavailable-selection reason and asks for a new choice. It does not disclose a
hidden Device. A Rack, Location, or U-position change inside the Site changes candidate evidence but
does not invalidate an explicit mapping.

### Workspace routing

Derive the review destination from the selected profile's registered output kinds. A profile that
emits `SourceTrace` opens the Trace Review Workspace even when the parsed workbook produces zero
valid traces. This lets trace-specific parse and planning feedback stay in its own workspace. All
other profiles keep the generic preview route.

Keep the route choice in one helper used after setup. Do not infer it from `workspace.has_traces`,
because an empty or invalid trace workbook still has trace semantics.

### Future workbook shapes

Keep workbook layout inside Source Adapters. A later change can make sheet and column mappings part
of validated `adapter_config`, or register another Source Adapter. Both approaches must emit the same
`SourceTrace` and `TerminationReference` types. Device resolution and the Cable Target Module must
not know workbook column names.

## Independent design B

The blind design also selected a trace-only `TraceDeviceResolution` row and rejected a helper
profile, `DeviceExistingMatch`, and `TerminationResolution` as owners. It placed normalization,
bulk resolution, stale-selection checks, candidate ranking, and evidence explanations in one deep
Trace Device Resolution Module used by planning and workspace commands.

It added these constraints:

- Store a plain Device ID so deleting a Device preserves a stale decision that the workspace can
  explain and replace.
- Do not show the saved display snapshot after the actor loses access to the Device.
- Let several source aliases select one Device. Only the source key is unique within a profile.
- Bulk-load decisions and Devices so query count does not grow with Termination References.
- Rank exact name first, then more matching placement dimensions, then fewer conflicting dimensions.
- Reproduce and lock the permission-scoped candidate offer during the save command.
- Route only a trace-only output set directly. Keep a future mixed-output adapter on the generic
  workspace until a combined review surface exists.

## Divergence and merge

| Question | Design A | Design B | Merged decision |
| --- | --- | --- | --- |
| Module boundary | Cable Target Module function | Sibling Trace Device Resolution Module | Use one sibling deep module owned by the Cable Target Module. |
| Stale Device deletion | Plain ID implied by existing patterns | Plain ID required to preserve stale state | Store a plain ID and a display snapshot. Never show the snapshot when the Device is not visible. |
| Candidate ordering | Exact name, Location, Rack, U | Exact name, more matches, fewer conflicts | Use deterministic match and conflict counts. Show the matched and conflicting evidence. |
| Route predicate | Profile emits `SourceTrace` | Output kinds equal trace-only | Route trace-only profiles directly. Keep mixed-output profiles on the generic workspace. |
| Write concurrency | Lock profile and replan | Also reproduce and lock the candidate offer | Reproduce the offer under the profile lock, lock candidate rows, save, then replan. |
| Portable YAML | Not specified | Exclude installation-local IDs | Do not add a portable YAML schema. The catalog section still participates in the planning fingerprint. |

## Merged design

Implement the Trace Device Resolution Module described by the two designs, with these public
responsibilities:

1. Canonicalize one source Device label.
2. Aggregate placement evidence for that label from all active `TerminationReference` values.
3. Bulk-resolve saved choices and exact Device-name matches through one permission-scoped reader.
4. Return automatic, manual, unresolved, or stale presentation state.
5. Return a bounded and deterministically ranked candidate page with evidence explanations.
6. Save a reproduced candidate choice and replan under the profile policy lock.

Before adding a second Source Adapter, move `SourceTrace`, `TerminationReference`, and their related
output value types from `trace_workbook.py` to an adapter-neutral `source_trace.py`. The current
parser and Cable Target Module both import that contract. A test-only trace adapter must be able to
emit the contract without importing `trace_workbook`.

The Cable Target Module consumes only resolved outcomes. The Trace Review Workspace consumes only
server-authored Device questions and candidate pages. Neither caller queries Device identity on its
own.

The active plan carries each Device question. A Device candidate or save request is valid only when
its canonical source key appears in that reviewed plan. Termination questions for an unresolved or
stale Device remain visible but disabled with the instruction to resolve the Device first.

Candidate placement evidence follows separate object-permission scopes. The module reads visible
Rack and Location IDs through `NetBoxReader.racks()` and `NetBoxReader.locations()`. A Device's Rack
or Location affects ranking and explanation only when that related object is also visible to the
actor. Hidden placement values produce no score, conflict, text, or count.

The policy upsert uses `save_permission_scoped_object(actor, TraceDeviceResolution, ...)`. Add and
change permission constraints apply to the decision row itself. A denial rolls back both the
decision and its replan, even when the Device candidate is visible.

One database-backed preview coordinator owns the active preview identity and revision for each
browser session. Its row is the authoritative compare-and-set record. Starting or replacing a
preview, discarding it, re-reading it, or running any command that writes policy or replaces the
materialized plan must lock the same session-owned row.

A mutation verifies the posted active-preview token, revision, Source Document, and profile before
it writes. The policy write, candidate recheck, and replan occur in that transaction. The winner
advances the revision. A second request carrying the old identity or revision receives 409 and
cannot commit a policy row or replace the newer preview.

The database coordinator stores the authoritative active-preview identity, revision, context, and
materialized plan. Session data is only a pointer or cache. Every preview read validates it against
the coordinator row and refreshes or refuses a stale cache. No response middleware session save can
restore an older preview after the coordinator has advanced.

Device and termination decisions, proposal decisions, the re-read command, new import setup, and
preview discard use this same coordinator rather than implementing separate checks. A concurrent
new setup for document B therefore has one linear order with an older Device decision for document
A. If A locks first, A can commit and B then replaces it. If B locks first, A receives 409. In both
orders B is the final active preview, and a stale session save cannot restore A.

Direct routing uses the profile's declared output kinds after the preview state is stored. A
trace-only profile opens the Trace Review Workspace, including when it produces no valid trace unit.

The first implementation keeps the `trace_workbook` parser fixed. Its validated adapter settings
can later declare sheet roles, header aliases, and column mappings. That change must still emit the
current `SourceTrace` types and must not alter this Device-resolution interface.

## Open decisions

No product-shape decisions remain. Ratification can still reject an invariant or require a narrower
first delivery.

## Ratification round 1

Result: REJECT.

Required revisions were:

1. State object-permission enforcement for the policy-row write.
2. Prevent Rack and Location hints from crossing their own object-permission scopes.
3. Move Source Trace output types out of the fixed workbook parser.
4. Make the trace-only route predicate explicit for mixed-output adapters.
5. Serialize concurrent preview mutations with a database-backed revision claim.

The merged design above includes all five revisions.

## Ratification round 2

Result: REJECT.

The document-scoped preview claim did not serialize an old Device decision against a concurrent new
upload in the same browser session. The coordinator now owns the session's active preview identity,
and every preview lifecycle operation uses the same compare-and-set record.

## Ratification round 3

Result: REJECT.

An implementation that saved only the request session under the coordinator lock could still be
overwritten later by unrelated response middleware. The coordinator now stores the authoritative
preview context and plan. The session cannot restore state. The concurrent ordering statement now
allows an old decision to commit before a later setup replaces it.

## Ratification round 4

Result: RATIFY.

The accepted design closes every observable acceptance condition and the permission, stale-state,
adapter-boundary, and concurrent-preview failure cases found in the earlier rounds.

## Follow-up scope

Make trace workbook structure configurable or add more Source Adapters that emit `SourceTrace`.

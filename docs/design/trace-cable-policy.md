# Trace Cable policy correction and media consistency

Status: RATIFIED at r2 (design-blind, 2 rounds). Increment 1 built.

## 1. Brief

### Decision

Where the Cable policy for one planned segment is decided, who owns that decision, and what the
plugin checks about media consistency along one Source Trace.

Today the only answer is `CableClassMapping`, one row per `(Import Profile, CableClass value)`, held
on the Import Profile edit page. Three operator needs do not fit it.

1. The policy can be set only on the profile page, away from the trace being reviewed.
2. One CableClass value carries different media on different traces. A reported workbook gives one
   segment a generic fiber CableClass, and that run is multimode OM4. The same value names a
   single-mode run on another trace. A `(profile, cable_class)` row cannot state both.
3. Nothing checks that one path stays in one media family. The source is not always correct, and a
   wrong CableClass silently writes a Cable of the wrong type.

The framing from the operator: "input data from trace is sometimes not 100% correct". The workspace
has to let the operator correct the source without editing the workbook.

### Constraints

House rules that bind this design:

- No backwards-compatibility layer. An old path is deleted in the change that replaces it.
- One source of truth. Everything else derives from it.
- Validate at boundaries and fail fast. No silent fallback, no default that hides an error.
- The Import Plan is the authority on state. The presentation layer may refine what the plan says
  and may never contradict it. A defect of exactly that kind was fixed in
  `proposal_presentation.card` on 2026-09-21.
- Cable Type and Cable Profile values come from the running NetBox instance, never a hardcoded list
  (spec section 3.6).
- Tests exercise real wiring first: HTTP request, view, real DB, real plan.

### Observable acceptance conditions

1. From the trace workspace, with no visit to the Import Profile page, an operator can give every
   segment of a 9-segment trace a Cable Type and Cable Profile, and the trace reaches
   disposition `actionable`.
2. Two segments of one trace that share a CableClass value can carry different Cable Types.
3. A trace whose segments resolve to more than one media family is reported, and the report names
   the segments and the families. The worked example, once all nine segments carry one
   fiber family, reports nothing.
4. Two Source Traces that state one shared segment still cannot give it two policies:
   `cable.resolved_segment_conflict` still fires.
5. No media family list appears in plugin source. The families come from
   `dcim.choices.CableTypeChoices.CHOICES` group labels.
6. A stored policy value the running instance no longer offers still blocks with
   `cable.cableclass_stale_mapping`.

### Evidence

| Fact | Location |
| --- | --- |
| `CableClassMapping`, `(profile, cable_class)` unique, two tri-state dimensions | `netbox_data_import/models.py:619` |
| Policy read, and the block on an unresolved dimension | `netbox_data_import/cable_target.py:1025`, `:1045` |
| Policy stored per segment index, then consumed by the create change | `cable_target.py:1034`, `:1274` |
| Shared-segment policy conflict, keyed on `_DesiredSegment.key` | `cable_target.py:1101` |
| `_DesiredSegment.key` is the direction-independent pair of resolved termination identities | `cable_target.py:148` |
| `_Termination` carries no NetBox port type | `cable_target.py:112` |
| Choice groups flattened, group label discarded | `models.py:555`, `:565` |
| Runtime choice and cardinality validation | `models.py:599` |
| Decision persistence shape to mirror | `models.py` `TerminationResolution`, `TraceDeviceResolution`; `review_workspace.py:31` |
| Spec: CableClass mappings, creation policy, diagnostics | `docs/spec/target-neutral-import-architecture.md` 3.6, 6.4, 6.7 |
| Trace identity | `docs/adr/0002-source-trace-identity.md` |

NetBox `CableTypeChoices.CHOICES` group labels, read from the running 4.7.0 instance:
`Copper - Twisted Pair (UTP/STP)`, `Copper - Twinax (DAC)`, `Copper - Coaxial`,
`Fiber - Multimode` (`mmf`, `mmf-om1`..`mmf-om5`), `Fiber - Single-mode` (`smf`, `smf-os1`,
`smf-os2`), `Fiber - Other` (`aoc`), `Power`, `USB`.

`CableProfileChoices` groups: `Single`, `Trunk`, `Breakout`. Spec 3.6 already restricts a created
segment to a profile with one termination per side, so `Trunk` and `Breakout` are out of scope for
creation.

Worked example, from an operator report. One 9-segment trace between two switch interfaces, across
8 fiber cassettes. Four CableClass values: one on segments 0, 2, 4, 6, 8; one on 1; one on 3; one on
5 and 7. Segments 1, 3, 5, 7 are rear-to-rear backbone runs. Segments 0, 2, 4, 6, 8 are
front-to-front patch leads. Two of the four values are ambiguous: the same label names multimode on
this path and single-mode elsewhere.

## 2. Open decisions between competing shapes

- **A. Override key.** Resolved termination pair (`_DesiredSegment.key`), or a source-side key built
  from the two source Termination References, or `(trace identity, segment index)`.
- **B. Media consistency check.** Block, warn, or info. Whole-trace agreement, or the narrower
  invariant across one pass-through. Whether a separate port-media against cable-media check is part
  of this design.
- **C. Trace-view edit of the profile-level mapping.** The same write as the profile form, or a
  separate narrow endpoint. Interaction with `locked_profile_policy` and the preview revision.
- **D. Presentation.** Precedence between a per-segment override and the profile row, how the card
  states which one is in force, and how an override is cleared.

## 3. Designs

Two designs were made blind from one factual brief. Neither designer saw the other.

| Design | Model | Effort | Isolation |
| --- | --- | --- | --- |
| 1 | Opus 5 (this session) | n/a | Drafted while design 2 ran |
| 2 | `gpt-6-astra` via `codex exec -s read-only` | high | Fresh context, brief only, no access to design 1 |

The brief stated the problem, the constraints, the evidence pointers and the four open decisions.
It named no preferred shape.

## 4. Divergence table

Both designs agreed on: the resolved termination pair as the override key; a complete override
rather than a per-dimension overlay; a new `cable_policy.py` owning the effective-policy question;
reuse of `CableClassMappingForm` behind a dedicated workspace endpoint that locks, replans and
rotates the preview revision; reused segments contributing their existing Cable type to the media
check; and families derived from the running instance's grouped choices. Agreement narrows where to
look. It closes nothing.

| # | Decision | Design 1 | Design 2 | Evidence | Disposition |
| --- | --- | --- | --- | --- | --- |
| 1 | Media mismatch severity | `blocked` | warning, disposition unchanged | `trace.pass_through_at_interface` (`trace_workbook.py:501`) rejects a Pass-Through Claim at an interface, and a span only spans verified PortMapping continuity, so the source contract cannot state a legitimate mixed passive span today | **Settled twice. See 5.1 and section 8.** |
| 2 | Span definition | "across one pass-through" | consecutive segments joined by verified FrontPort/RearPort mappings, including verified same-port substitution; a device name alone proves nothing | Design 2 is strictly more precise and excludes the converter case by construction | Design 2 |
| 3 | Breakout carve-out | none needed, cardinality already excludes breakout from creation | a Breakout-classified profile on a reused Cable excludes that segment and splits the span | Design 1 missed reused Cables, which can already carry a Breakout profile | Design 2 |
| 4 | Unknown media | one `info` code | explicit incomplete coverage; `Fiber - Other` indeterminate; unknown never reads as agreement; an unclassified group is explicit, not assumed | Design 2 is sharper and refuses a false "consistent" claim | Design 2 |
| 5 | Diagnostic fingerprint | not addressed | add `Diagnostic.evidence`, fingerprint it, bump the plan schema | Verified: `Diagnostic.fingerprint_data` (`plan.py:196`) is code, severity, identities only. Media facts placed in `display` would not change the fingerprint, so a live Cable could change under an accepted plan | Design 2 |
| 6 | `cable.resolved_segment_conflict` | leave as `invalid` | becomes `blocked`, and compares only effective Type and Profile, dropping `cable_class` from the comparison and from the shared payload | Once an override exists, a decision inside the plugin resolves the conflict, which is what `blocked` means (`cable_target.py:260`) | Design 2 |
| 7 | Source-level CableClass disagreement | not addressed | remove the label condition from `_cross_trace_conflicts` | Verified: `trace_workbook.py:786` flags two traces that "disagree about a CableClass" on one source segment pair. With overrides, two labels can resolve to one policy, so the label check would invalidate a correct pair | Design 2 |
| 8 | Rename `cable.cableclass_stale_mapping` to `cable.policy_stale` | not proposed | proposed, no alias | The code is no longer about a CableClass row alone once an override can go stale. House rule forbids an alias | Design 2 |
| 9 | Choice helpers | leave in `models.py` | move into `cable_policy.py`, delete the originals | One definition for forms, models, planning and classification | Design 2 |
| 10 | Editing before resolution | nothing useful before both ends resolve | the CableClass default stays editable; only the segment override is disabled, with a reason | Design 2 gives the operator work to do earlier | Design 2 |
| 11 | Concurrency | preview revision plus profile lock | also compare the reviewed profile fingerprint under the lock | A session revision cannot see another operator's profile edit | Design 2 |

Design 2 wins every dispositioned divergence. That is a finding about design 1, not a formality:
design 1 stopped at the planning seam and under-read the reused-Cable, fingerprint and cross-trace
consequences.

## 5. Merged design (r1)

Adopt design 2 entire, with divergence 1 left open for the operator.

The one substantive argument design 1 contributes is on divergence 1. Design 2 chose warning on the
ground that a media result is "not sufficient evidence to prohibit a write". But its own span
definition already removes the case that argument protects: a span is only the run of segments
joined by *verified* PortMapping continuity, a converter is not that, and
`trace.pass_through_at_interface` refuses to let the workbook state one anyway. Inside a verified
passive span, two known different families is a physical impossibility, and the segment override is
a decision inside the plugin that resolves it, which is the definition of `blocked` in this
codebase. Unknown and unclassified stay non-blocking under either choice.

Reopening condition for divergence 1: converter support, which would split a span at the converter
and remove the blocking case.

### 5.1 Divergence 1, settled

The operator settled it on 2026-09-21: "trace xls can be wrong, so I need an option to just force
given cable to a type if it differs on a path."

That is a request for the escape, not for a softer check. A mismatch inside a verified passive span
**blocks**, and the segment override is what clears it. The operator forces the one segment to the
correct Type, the families agree, and the trace proceeds. Design 2's "no ignore-media checkbox"
rule stands: the escape is to state the correct media, not to accept a contradiction.

This keeps design 2's own reasoning intact everywhere it applies. Unknown, unclassified and
`Fiber - Other` observations stay non-blocking, because they are incomplete coverage and not a
contradiction. Only two known and different families inside one verified passive span block.

Reopening condition: converter support. A converter would split the span and remove the blocking
case.

Note on wording: the operator said "force ... to a type", one dimension. The override still stores
both dimensions, because both designs agreed a partial overlay gives four precedence states per
segment for no stated requirement. The editor prepopulates both from the effective policy, so
forcing the Type is one field change in practice.

## 6. Ratify round 1

Reviewer `gpt-6-astra`, effort high, read-only, scope r1 whole merged design.
**Verdict: NOT RATIFIED.** Two blockers, both verified independently here by reading the code.

### Blocker 1 (CONFIRMED). The physical-impossibility premise is false.

A passive balun is a counterexample: one device with a BNC FrontPort, an 8P8C RearPort and a
one-position PortMapping. Coax on the front, twisted pair on the rear, both correct.

Verified here:

- `Position Front` and `Punch-Down` are valid non-interface port classes at a join
  (`field_keys.py:12`).
- `_pass_through_error` rejects a join only when a port class is an interface class
  (`trace_workbook.py:488`). A balun join uses neither.
- NetBox `PortMapping.clean()` checks same device and rear position. It never compares media
  (`dcim/models/base.py`, `dcim/models/device_components.py:1495`).

So a verified passive span can legitimately hold two media families, and r1 would block correct
data with no escape: an override could clear it only by recording a cable type that is wrong.

The disposition on divergence 1 rested on this premise. The premise is refuted, so the disposition
reopens. The operator's authority to choose blocking is untouched; the evidence offered for it was
wrong.

### Blocker 2 (CONFIRMED). The override cannot fix a disagreement between two retained Cables.

Section 5.1 promises the operator forces the offending segment and the trace proceeds. That holds
only for a segment the import creates. A reused segment keeps its existing Cable unchanged, and the
media check reads that Cable's actual type.

Verified here: `pending` excludes `proven` (`cable_target.py:232`), and creation runs over `pending`
only (`cable_target.py:1274`).

Case: an existing first patch Cable recorded OM4, an existing backbone recorded SMF, both joins
verified. No override changes either. The remedy is a NetBox correction.

r2 must state that contract and the workspace must say it, rather than offering an action that
cannot work.

### Non-blocking findings accepted for r2

3. The plan schema bump leaves the trace workspace unable to recover its own cached plan: loading
   and Re-read both redirect to setup, because Re-read needs a successful deserialization first
   (`views.py:3895`, `preview_row_actions.py:142`). Queued Jobs fail safely. Specify the recovery
   route without a compatibility reader.
4. Override loss is not limited to an operator re-pick. A PortMapping edit that changes the unique
   mapped peer also changes the resolved pair (`cable_target.py:771`, `:802`). The notification and
   the tests must cover every pair change, not only a re-pick.

### Checked and clean (reviewer, spot-checked here)

Shared-conflict and media cannot deadlock; removing `cable_class` from the shared payload is sound
and `test_a_shared_termination_invalidates_every_involved_trace` guards the regression; the
fingerprint gap is real; the helper move needs no import cycle; the permission helpers fit, with
the outer lock supplying what the deletion helper does not take.

## 8. Revision r2

### 8.1 Divergence 1, settled again: warn

The operator chose warn on 2026-09-21, after blocker 1 refuted the premise behind the earlier
block decision. Design 2's original position stands, now on evidence rather than assumption.

- A mismatch of two known families inside one verified passive span records
  `cable.media_family_mismatch`, severity WARNING, disposition unchanged.
- The warning names the differing segments, their effective Cable Types and their families.
- Unknown, unclassified and `Fiber - Other` remain incomplete coverage, never agreement, never a
  claim of consistency.
- The segment override stays. It is the operator's "force this cable to a type" action, and it is
  how a mismatch caused by a wrong workbook gets corrected.
- No ignore-media control, and no per-pass-through converter decision in this delivery. A legitimate
  converter, for example a passive balun, produces a warning that is simply true: the path does
  change media there. Nothing is blocked, so nothing needs an exception.

This removes the balun trap without a second model. It is also why the converter feature stays out
of scope: with a warning, a converter needs no representation to be importable.

### 8.2 Blocker 2, recovery contract

The override changes what the import writes. It cannot change a Cable the import retains.

- A mismatch between two retained Cables, or between a retained Cable and a created segment, is
  reported the same way, but the workspace must state which observations come from a retained
  Cable.
- Where every mismatched observation comes from a retained Cable, the workspace says the override
  cannot change it and names the remedy: correct the Cable in NetBox, then re-read.
- The override action is not offered for a retained segment. It is shown disabled with that reason,
  following the existing action-with-a-reason pattern.

### 8.3 Findings 3 and 4

3. Plan schema bump. The trace workspace must be able to recover its own stale cached plan. Re-read
   currently needs a successful deserialization first (`views.py:3895`), so a bumped schema sends
   the operator to setup. r2 routes a schema rejection to a rebuild from the stored Source Document,
   the route the preview already uses (`views.py:1212`). No compatibility reader.
4. Override loss covers every resolved-pair change, not only an operator re-pick: a PortMapping edit
   that changes the unique mapped peer does it too (`cable_target.py:771`, `:802`). The workspace
   notice and the regression tests cover all of them.

### 8.4 Unchanged from design 2

Everything else is adopted as written: the resolved-pair override key, the complete override, the
`cable_policy.py` seam and the helper move, fingerprinted `Diagnostic.evidence` with the schema
bump, `cable.resolved_segment_conflict` becoming `blocked` and comparing effective policy only,
`cable_class` leaving the shared payload and `_cross_trace_conflicts`, the rename to
`cable.policy_stale`, the workspace endpoint with the profile lock, the fingerprint comparison and
the revision rotation, and the presentation rules.

## 9. Ratify round 2

Reviewer `gpt-6-astra`, effort high, read-only. **Verdict: RATIFY revision r2**, scope: workspace
Cable policy correction, resolved-pair overrides, warning-based media assessment, and the recovery
contracts in 8.2 and 8.3. No blockers. Design approval, not implementation verification.

Two corrections supplied in the same round, both verified here and folded into r2.

**8.2 is a property of the reviewed plan, not of the source segment.** A retained pair can become
pending: re-picking endpoints to free ports replans, and pair equality decides reuse again
(`cable_target.py:937`). Deleting the Cable in NetBox and re-reading does the same for the same
pair. So the override is disabled while **the reviewed plan retains that pair**, and offered once a
replan gives a resolved pending pair.

Do **not** gate the override on segment status `create`. Verified: `_segment_status` returns `""`
for a pending segment when the unit writes nothing (`cable_target.py:1249`), which is exactly the
unmapped-policy case. Gating on `create` would make an unmapped pending segment unable to receive
the override that would unblock it. That is a recovery deadlock.

**8.3 must not discard the preview first.** Verified: `_discard_import_preview` pops
`import_context` (`views.py:1404`), which carries `source_document_id`, so calling it before the
rebuild destroys the reference the rebuild needs. Recovery must distinguish a schema rejection from
any other loader failure, preserve `import_context`, bypass materialized-preview mode while
respecting an active retained sync (`views.py:1205`), reuse the rebuild branch (`views.py:1212`),
and return to the selected trace without replaying the original mutation.

### Accepted limitations, recorded not fixed

1. **A uniformly wrong family is not diagnosed.** Two `1000base-t` interfaces with a resolved policy
   of `mmf-om4` produce one observed family, so no mismatch. `_Termination` carries no port type
   (`cable_target.py:112`), policy validation checks offered values and cardinality only
   (`models.py:596`), and NetBox `Cable.clean()` does not compare Cable media to termination media.
   This is the port-compatibility check that both designs put out of scope. The block choice would
   have missed it too, so it is not a consequence of warn. Split candidate, see section 10.
2. **The execution record does not preserve the warning.** A trace carrying a media warning can be
   synchronized, and the Import Execution stores change identities and deletion snapshots, not the
   selected units' warnings (`import_engine.py:272`). The accepted plan survives in the Job data
   (`views.py:1549`), but the results page says nothing. Follow-up, see section 10.

## 10. First increment, and follow-ups

**First increment: edit an existing CableClass default from the trace workspace.** Existing form and
model, a scoped POST, the profile lock, the freshness comparison, a whole-document replan, revision
rotation, and a refreshed policy display on the segment. It needs neither the override model nor
any media diagnostic.

Acceptance: upload a real workbook with an unmapped CableClass through the HTTP import flow, save
both dimensions from the workspace, assert the trace reaches `actionable`, then execute and assert
the persisted Cable Type and Profile. Red before, green after.

The split is valid: the later slices depend on the policy semantics that already exist, not on this
editor. The editor stands alone.

Then, in order:

1. The override slice: `CableSegmentOverride`, precedence, pair identity, shared-conflict
   reconciliation, the payload change, and override-loss handling. These belong together.
2. The media slice: assessment, the warning, fingerprinted `Diagnostic.evidence`, the plan schema
   bump and its recovery route.
3. Follow-up: persist selected-unit warnings in the execution audit (limitation 2).
4. Split candidate: a conservative port-compatibility diagnostic for known contradictions, without
   inferring converters and without claiming multimode or single-mode from an LC connector
   (limitation 1).

## 11. Increment 1, as built

Route `trace-workspace/cable-policy/`, view `TraceCablePolicyView`, workspace command
`save_cable_class_mapping_and_replan`.

- The view validates the CableClass against `_workspace_cable_classes(workspace)`, so a command
  answers a question the preview asked.
- The workspace command reads the row under `locked_profile_policy`, binds `CableClassMappingForm`
  with the CableClass fixed server-side, writes through `save_permission_scoped_object`, replans the
  whole document, and rotates the preview revision through `record_recalculated_preview`.
- The page lists each CableClass the selected trace states, with the policy in force and a Save
  form built from `CableClassMappingForm`, so the runtime choices and the tri-state control values
  have one definition.
- `cable.cableclass_unmapped` no longer says "Map it on the import profile", because it is no
  longer the only place.

**One design change made during the build.** The first attempt added a `cable_policies` key to the
plan's trace display. A cached plan predates that key, so the live page said "This trace states no
CableClass" for a trace that states four. The CableClass values now come from the plan's own
`segments`, which every cached plan already carries, and the policy row is read live beside them.
No plan-shape change, no cached-plan skew.

Verified on a live instance: all four CableClass values of the worked example render, each
unresolved, each with its own Save form.

Tests: three end-to-end tests in `TraceWorkspaceCablePolicyTest`. The first uploads a workbook with
an unmapped CableClass through the real HTTP flow, saves the policy from the workspace, asserts the
trace becomes `actionable`, then runs the real execution and asserts the persisted Cable `type` and
`profile`. The other two cover an invented CableClass and a stale preview revision, and both assert
nothing was written.

Full suite 2700 passed. `ruff format --check`, `ruff check` and `mypy` clean.

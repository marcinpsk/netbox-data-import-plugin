<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
-->

# Trace claimed kind that disagrees with the NetBox model

## 0. Review record

Round 1 (gpt-6-astra, high effort, read-only): **RATIFY r1**, scope "explicit cross-kind selection
in source-valid traces, with acceptance condition 6 restated per resolved Device". No blockers.
Four non-blocking findings, all accepted and verified, applied as r2 (editorial, no change to the
ratified rules). No claim was refuted.

## Problem brief

### Decision

Decide how a Source Trace termination resolves when its source PortClass claims one termination
kind, and NetBox models the real port with a model that kind does not admit.

### Observed case

A trace workbook states one end as `<device> > Port 18 (NIC)`. The PortClass `NIC` claims the
`interface` kind (spec section 6.1). In NetBox, the resolved Device is a passive optical device
type: 48 LC FrontPorts named `14` to `61`, one RearPort, and 11 management Interfaces. FrontPort
`18` is the port the cable lands on. A second end in the same workbook, `<device> > Port 60 (NIC)`,
has the same shape on a Device of the same type.

The `interface` claim admits Interface, ConsolePort, ConsoleServerPort, PowerPort, and PowerOutlet
(`field_keys.py`, `ADMITTED_TERMINATION_MODELS`). It never admits FrontPort. So:

- the exact-name rule cannot match (the name differs, and the model is not admitted);
- the manual picker never offers FrontPort `18`, because it reads the same eligible set
  (`cable_target.py`, `eligible_terminations`);
- an Ask AI proposal gets the same set, and it correctly answers "no match";
- a PortClass mapping cannot help, because fixed PortClasses refuse a mapping
  (`field_keys.py`, `port_class_mapping_errors`), and a mapping would apply to the whole preview or
  profile, while the same source class `NIC` is correct for router ports in the same workbook.

The operator therefore has no path in the plugin to cable this end.

### Scale on the live preview

Of 32 unresolved `interface`-claimed ends in one production preview, 2 sit on a Device whose port
with the same number is a FrontPort. The other 30 are ordinary naming differences on Devices with
no FrontPorts, which the picker and Ask AI already handle. The class is rare per workbook but
blocks the end completely when it happens.

### Constraints (from the spec and existing decisions)

1. Spec section 6.1: the claimed kind fixes an ordered list of admitted models. Deterministic
   matching stops at the first admitted model with any exact-name match. Precedence does not
   depend on the actor's view scope.
2. The two power claims admit only their own model, so an Interface with a power port's name never
   answers a power PortClass. That rule exists for automatic matching.
3. A candidate's identity is its concrete model and its id. The picker, the proposal snapshot, and
   every selection write carry that pair and never derive the model from the claimed kind.
4. A saved selection whose model the claimed kind does not admit reports
   `cable.termination_kind_mismatch` (`cable_target.py`, `_stored_termination`).
5. The field key holds `device`, `cards`, `port`, `kind`, and `role`. `TerminationResolution`,
   `ResolutionProposal`, and the mapped-peer role all key on it.
6. A resolved FrontPort or RearPort loads its PortMappings, and a pass-through port can raise a
   mapped-peer question (`_mapped_peer_sources`, `_load_mappings`).
7. Two resolved ends of one segment must be a pair NetBox can cable. NetBox's own compatibility
   table decides this. Interface to FrontPort is compatible.
8. Fixed PortClasses take no mapping; only labels outside the vocabulary can be mapped
   (PR #232 decision).
9. Global engineering rules: simplest shape that fully meets current requirements, no compatibility
   layers, one source of truth, fail fast, no silent fallbacks.

### Observable acceptance conditions

1. In the observed case, an operator can select FrontPort `18` for the end in the Trace Review
   Workspace, and the trace becomes actionable.
2. That choice persists for the profile and resolves the same end in a later workbook.
3. The manual picker and an Ask AI proposal still read one shared candidate set.
4. Automatic exact-name resolution of every existing workbook stays the same, unless the design
   names the change and why it is safe.
5. Any resolution whose model the source claim does not admit is visible to the operator in the
   workspace before a sync.
6. Two actors with different view scopes still resolve one source end to the same object or leave
   it open.
7. The per-segment NetBox cable compatibility check still runs on the selected pair.
8. End-to-end tests through the real views, ORM rows, planning, and permission-scoped candidates
   cover conditions 1, 2, 3, and 5.

### Evidence pointers

- `docs/spec/target-neutral-import-architecture.md`, section 6.1 "Port resolution".
- `netbox_data_import/field_keys.py`: `PORT_CLASS_CLAIMED_KINDS`, `ADMITTED_TERMINATION_MODELS`,
  `port_class_mapping_errors`.
- `netbox_data_import/cable_target.py`: `eligible_terminations`, `_exact_name_match`,
  `_stored_termination`, `_mapped_peer_sources`, `_load_mappings`.
- `netbox_data_import/termination_proposal.py`, `netbox_data_import/proposal_presentation.py`.
- `docs/design/trace-cable-policy.md`, `docs/design/trace-device-resolution.md`.

## Candidate shapes

- **A. Wider manual set.** The claimed kind still drives automatic matching. The picker and the
  proposal set admit every Cable End Kind on the resolved Device, ranked with the claimed models
  first. A saved cross-kind choice is valid and shows a "differs from the source claim" mark.
- **B. Device-type kind policy.** A profile row says that a claim of kind X on a given NetBox
  DeviceType resolves against model Y. Automatic matching and the picker both follow it.
- **C. Per-end kind override.** The operator overrides the claimed kind of one source end. The
  override becomes part of the end's identity, so its field key, saved choice, and proposal follow.
- **D. Model-led matching.** The claim only ranks. Deterministic matching runs over every Cable End
  Kind, claimed models first.

## Independent design A (Claude Opus 5.5, main session)

Written from the brief and the code, before design B was read.

### Shape

Split the one fact "admitted models" into two facts with two jobs:

- **Matched models** (`MATCHED_TERMINATION_MODELS`, today's `ADMITTED_TERMINATION_MODELS`): the
  ordered list per claimed kind that the exact-name rule walks. Unchanged.
- **Selectable models**: every Cable End Kind, for every claimed kind. A manual choice and an
  accepted proposal can select any of them. `CABLE_END_KINDS` already holds this set, so no new
  constant is needed.

The claim stays a fact about the source end. It decides automatic matching and candidate rank. It
no longer limits what an operator may select.

### Ownership and seam

`cable_target.py` owns both rules, behind the existing functions: `eligible_terminations` (the
shared candidate set for the picker and proposals), `_exact_name_match` (automatic), and
`_stored_termination` (saved choices). No caller changes its interface. `field_keys.py` keeps the
two facts.

### Persisted identity

The field key keeps the claimed kind. `TerminationResolution` already stores the concrete model and
id, so a saved FrontPort choice under an `interface` key needs no schema change and no schema migration.
A later workbook that states the same end with a PortClass of the same claimed kind builds the
same key and reuses the choice. A later workbook whose PortClass claims another kind builds a new
key and asks again.

### Planning behaviour

1. Automatic matching: unchanged, walks the matched models of the claim.
2. Candidate set (`eligible_terminations`, termination role): every Cable End Kind model on the
   resolved Device, as one set with one count and one bound. Order: exact search identity first,
   then **claimed before unclaimed**, then name, then model order, then id. The claimed-first key
   keeps today's first pages for every Device that has only claimed models, and keeps a dense
   panel's FrontPorts behind its Interfaces for an `interface` claim.
3. Saved choice: `_stored_termination` accepts any Cable End Kind on the resolved Device. A model
   outside the claim's matched models adds a `warning` diagnostic,
   `cable.termination_kind_differs`, with the claimed kind and the selected model. The disposition
   does not change. The termination card shows a "NetBox model differs from the source claim" mark.
   This replaces the blocking `cable.termination_kind_mismatch`, which no input can reach any more.
4. Mapped-peer role: `_mapped_peer_sources` today takes the base model from
   `ADMITTED_TERMINATION_MODELS[kind][0]`. It must take it from the saved base selection's model
   when one exists, and from the exact-name match otherwise. Without this a FrontPort chosen under
   an `interface` claim, in a path that passes through that port, offers no mapped peer.
5. Ask AI: the snapshot is the shared candidate set, so FrontPort `18` is offered. The prompt states
   the claimed kind as a hint and says NetBox can model the port with another kind. Bump
   `PROMPT_VERSION`. Existing proposals read their candidate set as changed, which the staleness
   check already reports.
6. Cable compatibility: unchanged. It reads the resolved models, so Interface to FrontPort passes
   and PowerPort to Interface still blocks.

### Failure behaviour

Automatic precedence is unchanged, so view-scope independence holds as today. A saved cross-kind
choice that is deleted, hidden, or leaves the Device reports `cable.termination_unresolved` without
its display name, as every saved choice does. An unsupported model still refuses with
`cable.unsupported_termination_kind`.

### Spec changes

Section 6.1: rename the table column to "Matched NetBox terminations, in match order", and state
that the candidate set for every claim is every Cable End Kind, claimed models ranked first. Replace
the sentence that makes a saved selection outside the claim stale. Section 9 table: replace the
`cable.termination_kind_mismatch` row with `cable.termination_kind_differs`, `warning`, disposition
unchanged. Section 7: the prompt treats the claim as a hint.

### Validation

End-to-end through the trace workspace views, with a real Device that has FrontPorts and no
matching Interface: the picker offers the FrontPort, the save makes the trace actionable, the card
and the diagnostic show the difference, a re-upload of the same workbook reuses the choice, and the
proposal request snapshot contains the FrontPort. Planning tests: automatic matching of an
`interface` claim never selects a same-named FrontPort; a power claim never auto-selects an
Interface; candidate order puts claimed models first; the mapped-peer question offers the RearPort
of a FrontPort chosen under an `interface` claim.

### Rejected

- **B, device-type policy**: a new policy model, CRUD, REST, and YAML surface for two ends per
  workbook. It also needs a name rule (`Port 18` against `18`), so the operator still picks.
- **C, per-end kind override**: changes the field key, so saved choices and proposals rekey, and
  the operator still picks the port after the override. Two steps where one is enough.
- **D, model-led matching**: changes automatic resolution of existing workbooks and reopens the
  power-claim rule. Fails acceptance condition 4.

## Independent design B (gpt-6.1-sol, high effort, read-only)

Run with `codex exec -s read-only`. Its input was the problem brief above (sections "Decision"
through "Evidence pointers") plus a section list, and nothing else: no candidate shapes and no
design A. The full text is kept with the review packets. Summary:

- Automatic matching keeps the claim's admitted models. Explicit selection (picker and accepted
  proposal) may choose every Cable End Kind on the resolved Device.
- No schema change and no key change. The field key's `kind` stays the source claim.
- `cable.termination_kind_mismatch` becomes a `warning` with disposition unchanged, emitted after
  the selected object is read. Its display needs the disclosure mechanism, because it names the
  selected model.
- Candidate order: unchanged ordering keys, with model ties broken by the deduplicated union of the
  admission table.
- The picker marks candidates outside the claim. A fresh proposal shows the mark before Accept.
- `_mapped_peer_sources` and planning share one internal base-resolution rule: saved selection
  first, else the unchanged exact-name rule.
- Bump the plan schema version, because a diagnostic's meaning and disclosure change.
- Remove the Source Adapter rule `trace.pass_through_at_interface`, so a NIC-labelled intermediate
  continuation reaches target resolution.
- Open: acceptance condition 6 holds per port once both actors resolve the same Device; Device
  resolution itself is scoped per actor today.

## Merge

| Decision | A | B | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Core rule | Claim matches, every Cable End Kind selectable | Same | Both read `_exact_name_match`, `eligible_terminations`, `_stored_termination` | Agreed. Convergence does not verify it; round 1 attacks it | |
| Persisted identity | Field key unchanged, no migration | Same | `TerminationResolution` stores model and id (`models.py`) | Agreed | |
| Diagnostic | New `cable.termination_kind_differs` | Reuse `cable.termination_kind_mismatch` as `warning` | The condition is the same fact; only severity changes | **B**. One code, no rename churn in the disclosure schema and the workspace wording | Spec section 9 row changes severity only |
| Candidate order | Claimed models rank before others, then name | Name first, model tie by union order | Differs only on a Device holding both claimed and unclaimed models: on the observed Device, A offers the 11 Interfaces first, B offers FrontPorts `14`..`61` first | **A**, open to round 1. The claim is source evidence, and Device candidates already rank by source evidence | One new ordering key in `eligible_terminations` |
| Outside-claim mark in picker and on a fresh proposal | Card mark after save only | Picker, proposal, and card | Acceptance condition 5 asks for visibility before a sync; the operator decides at the picker | **B** | Picker JSON gains one flag per candidate |
| Disclosure of the warning | Not addressed | Gate through `cable_disclosure.py` | `cable_disclosure.py:123` registers the schema with `selected_object_type` | **B**, verify in round 1 | |
| Plan schema bump | Not addressed | Bump | Spec section 4.8 | **B**, verify in round 1 | Old previews rebuild |
| Mapped-peer base | Saved model, else exact match | Same, as one shared internal rule | `cable_target.py:506` derives the base from the claim | **B** shape | |
| `trace.pass_through_at_interface` | Out of scope | Remove | `docs/design/trace-cable-policy.md` row 1 settled media severity twice on the premise that this rule exists | **Excluded from this design.** No acceptance condition needs it; the observed ends are trace endpoints. Removing it reopens a ratified decision | A follow-up only when a workbook shows a NIC-labelled pass-through |
| Proposal eligible-set ceiling | Not addressed | Keep the refusal | `proposal_tasks.py:189`, ceiling `PROPOSAL_CANDIDATE_LIMIT_MAX = 1024` | **B**. A wider set can push a dense Device over the ceiling; the picker still pages | Note in spec section 7 |
| Acceptance condition 6 | Implicit | Holds per port after one Device resolution | `trace_device_resolution.py:212` scopes Devices per actor | **B**. Condition 6 is restated per port | |

## Merged design r1

1. `field_keys.py` keeps `ADMITTED_TERMINATION_MODELS` as the automatic-match table. The selectable
   set for every claim is `CABLE_END_KINDS`, in the deduplicated union order of that table.
2. `eligible_terminations` (termination role) returns every Cable End Kind on the resolved Device:
   one set, one count, one bound. Order: exact search identity, claimed before unclaimed, name,
   union model order, id. Each candidate carries `outside_claim`.
3. `_exact_name_match` is unchanged.
4. `_stored_termination` accepts any Cable End Kind on the resolved Device. A model outside the
   claim emits `cable.termination_kind_mismatch` as a `warning` after the object is read, with its
   display gated by `cable_disclosure.py`. `cable.unsupported_termination_kind` is unchanged.
5. One internal base-resolution rule (saved selection, else exact name) serves planning and
   `_mapped_peer_sources`. The mapped-peer base model is the concrete resolved model.
6. Ask AI reads the same set. The prompt states the claim as a hint that NetBox can model
   differently. `PROMPT_VERSION` and the plan schema version are bumped.
7. Picker, fresh proposal, and termination card show the outside-claim mark.
8. Cable compatibility, view-scope precedence, stale handling, and the proposal ceiling are
   unchanged.
9. Out of scope: `trace.pass_through_at_interface`, Device resolution precedence, automatic
   cross-kind matching.

Acceptance condition 6 now reads: once two actors resolve the same Device, they resolve one source
end to the same object or leave it open.

## Round 1

Verdict: **RATIFY r1** (gpt-6-astra, high, read-only). The reviewer read every use of
`ADMITTED_TERMINATION_MODELS`, `claimed_termination_kind`, `PORT_CLASS_CLAIMED_KINDS`, and the
field-key `kind`, and found no other path that refuses a cross-kind selection: model `clean()`,
forms, REST, GraphQL, the picker save, proposal acceptance, templates, and JS carry the concrete
model and id. It agreed with every merge disposition, including claimed-first order and the
exclusion of `trace.pass_through_at_interface`.

| # | Finding | Check | State |
| --- | --- | --- | --- |
| 1 | The reused code's wording still says "Choose the termination again" | `review_workspace.py:614` holds that text | CLOSED in r2 |
| 2 | Not every existing proposal goes stale: a Device with only claimed models keeps one ordered set, so its proposal stays fresh; a queued proposal on the old prompt fails the version check | `proposal_tasks.py` compares ordered entries; `proposal_jobs.py` checks `PROMPT_VERSION` | CLOSED in r2 |
| 3 | Section 4.8 asks to reject incompatible executable plans; the reason for the bump is the new disclosure contract, not the severity | Spec section 4.8 | CLOSED in r2 |
| 4 | The diagnostics table is section 6.7, not 9; sections 6.1, 7.1, 10.2 also change | Spec line 1174 sits under "6.7 Planning diagnostics" | CLOSED in r2 |

## Merged design r2

r1 with these changes:

- The `cable.termination_kind_mismatch` wording becomes a statement, not an instruction: the
  selected NetBox port is a `<model>`, and the source PortClass claims `<kind>`.
- Existing proposals: a Device whose ordered candidate set does not change keeps its proposal
  fresh. A Device that gains unclaimed models marks its proposal stale. A queued proposal on the old
  prompt version fails the worker's version check.
- The plan schema bump exists because the warning's disclosure contract changes. Old plans then
  fail section 4.8 validation instead of rendering an ungated model name.
- Spec amendments: section 6.1 (candidate set, order, saved selection), 6.2 (mapped-peer base),
  6.7 (severity and wording), 7.1 (`kind` is the source claim), 7, 7.3, 7.4 (shared set, ceiling),
  10.2 (picker mark). The T5 and T6 criteria follow the new candidate set.

## Decision

Ratified at r1; r2 applies the round-1 findings only. Scope: explicit cross-kind selection for
trace ends in source-valid traces.

## First increment and its acceptance

One change delivers it, test first, end to end through the trace workspace views:

1. A workbook end `Port 18 (NIC)` on a Device with FrontPorts and no matching Interface: the picker
   offers FrontPort `18` with the outside-claim mark; saving it makes the trace actionable; the card
   shows the warning.
2. A second upload under the same profile reuses the choice with no new selection.
3. A proposal request snapshot holds the same ordered set as the unfiltered picker.
4. Automatic matching stays: an exact-named FrontPort never answers an `interface` claim, and a
   power claim never auto-selects an Interface.
5. A FrontPort chosen under an `interface` claim in a path that passes through it offers its
   mapped RearPort.
6. The warning is suppressed when the actor cannot view the selected port or its Device.
7. Interface to FrontPort syncs; an incompatible pair still blocks with
   `cable.incompatible_terminations`.

## Open work

- `trace.pass_through_at_interface` stays. Reopen it only when a workbook shows a NIC-labelled
  pass-through. That would also reopen `docs/design/trace-cable-policy.md` row 1.
- The name gap (`Port 18` against `18`) stays manual or Ask AI. No automatic number-only matching.

## Implementation review

Round 1 (gpt-6-astra, high, read-only) on the implementation: no runtime blocker. One should-fix:
the spec and this record said any PortClass change asks again, but the key holds the claimed kind,
so `NIC` to `Switch Port` reuses the choice. Fixed in both texts. The reviewer also noted that the
prompt version 3 retirement migration (`0049`, required by the existing contract-retirement test)
had no behavioural test; one is added.

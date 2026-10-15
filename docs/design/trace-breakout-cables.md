# Trace import of breakout Cables on channelized interfaces

Status: RATIFIED r7 (split core) on operator override after BLOCKED r5 and r6: 6 review rounds, deadlock freedom deferred, R6-1 an accepted residual with detection. First increment built.

## 1. Brief

### Decision

How the trace import plans and writes one NetBox breakout Cable for one physical breakout harness,
when the source states the harness only as separate leg traces.

### Source evidence

The source export has no breakout object. One breakout transceiver is a card (cards label such as
`Slot 1 >> 1/3 (10)`) that holds ports `P01`..`P04`. Each leg is its own trace block, from `P0k` to a
different peer, with an ordinary one-to-one CableClass (`MM Fiber LC-LC`, one `MM Fiber LC-SC`). Some
legs continue through patch panels after the first hop. The shared device and cards label is the only
thing that ties the legs together. Lane `P0k` is NetBox channel `k`: the existing one-to-one Cables in
the target instance confirm it for three lanes.

### NetBox 4.7 model (read from the 4.7.2 source)

- A parent Interface with `channels=N` takes the Cable. Children carry `parent` and `channel_id`
  (1-based). A child mirrors the parent's Cable at position `channel_id`
  (`Interface.propagate_channel_cables`) and traces on its own.
- A child cannot hold a CableTermination: "Cables cannot be terminated directly to a channel
  subinterface; cable the parent interface instead." (`CableTermination.clean`). A child with an
  existing Cable cannot get a `channel_id`.
- `breakout-1c4p-4c1p`: A side one connector with positions 1..4, B side connectors 1..4 with one
  position each. A position `k` maps to B connector `k`.
- A termination's connector is its index in that end's termination list
  (`Cable.update_terminations`). A B side with legs 1, 3, 4 puts leg 3 on connector 2. A gap cannot
  be written.
- `Cable.clean`: "Cannot connect different termination types to same end of cable." Every B leg must
  be the same model.
- `BaseCableProfile.clean` caps each side at its connector count. Fewer is allowed.
- Interface channels arrive in 4.7 (`dcim` migration 0249). NetBox 4.6.10 has Cable profiles but no
  `channels` or `channel_id`. The plugin supports 4.6.9 and later.

### Current plugin behaviour

- One Source Trace is one Synchronization Unit (spec 4.1). The unit identity is the unordered
  endpoint pair (ADR 0002).
- A created Cable has one termination per side (`cable_target.py`, `a_terminations=[ends[0]]`).
  CableClass mappings offer only profiles with one connector per side (spec 3.6,
  `compatible_cable_profile_choices`). Spec section 15 lists breakout creation as out of scope.
- An existing Cable with several terminations on a side that touches a desired port blocks with
  `cable.multi_termination_conflict`. It is never reused or deleted.
- Identical Planned Changes with one identity are shared across units and execute once. One
  identity with different payloads makes the plan invalid (spec 4.4). Two traces that state one
  segment share `cable:create:<pair>`.
- Port auto-match is the unique exact port name on the resolved Device. The cards label never takes
  part (spec 5.1, 6.1).
- Nothing recognises a channel child. A trace that resolves `P0k` to a child plans an actionable
  one-to-one Cable, which NetBox then refuses at execution, so the whole selected transaction rolls
  back.

### Constraints

- The importer never creates or edits ports, so it never sets `channels` or `channel_id`
  (spec 15). The operator migrates NetBox data.
- The operator decided on one profiled Cable per harness, not one Cable per leg.
- House rules: no compatibility layer, one source of truth, fail fast, the plan is the authority,
  tests through real wiring.

### Finding before design: mixed leg kinds

In the reported workbook, two of the three breakout cards have legs whose first hop is a different
model: one leg reaches a patch panel FrontPort, the others reach Interfaces. NetBox 4.7 refuses that
B side, and a channel child cannot be cabled on its own. Once that transceiver is channelized, those
harnesses have no valid NetBox representation. Only the card whose four legs all reach Interfaces
can become one breakout Cable.

Operator answer (2026-10-15): channelize every breakout transceiver, and block a harness whose legs
reach different models. The physical build is an MPO harness whose LC legs land on patch-panel
FrontPorts, then patch cords to the end devices. That build is valid in NetBox: A side the
channelized parent, B side four FrontPorts. The reported export states the panel hop for only one
leg of one card, so most legs read as direct to an Interface. The importer plans what the export
states.

### Observable acceptance conditions

1. Leg traces from `P01`..`P0n` on one channelized parent, every first hop the same model, plan one
   Cable change. Execution writes exactly one Cable: profile `breakout-1c<n>p-<n>c1p`, A side the
   parent, B side the far end of leg `k` on connector `k`. NetBox's CablePath for each channel child
   then reaches its leg's far end, or continues through the panel.
2. A replan after that execution shows every leg trace as no-op.
3. Leg first hops of different models: each leg trace is blocked with one specific diagnostic code,
   and nothing is written.
4. A leg whose near end resolves to a channel child never plans a one-to-one Cable on the child.
   The current execution-time NetBox refusal cannot occur.
5. On NetBox 4.6 every existing fixture plans byte-identical units, and planning reads no channel
   attribute that 4.6 lacks.
6. A harness that cannot be written with each leg on its own channel (a missing middle leg, more
   legs than connectors, a leg count with no breakout profile) is never written with a shifted
   channel. Its behaviour is stated and deterministic.
7. An existing breakout Cable that already states the desired harness is reused. Any other existing
   Cable on the parent or on a leg end follows a stated rule.
8. The unit fingerprints change when any harness fact changes: leg set, connector order, profile.

### Candidate shapes

- **A. Shared harness change.** Each leg trace stays its own unit. The breakout Cable is one Planned
  Change, identical in every leg unit, with the shared-segment precedent (`_shared_deletes`,
  `_shared_sources`).
- **B. Harness unit.** The legs of one harness group into one Synchronization Unit, changing the
  "one Source Trace, one unit" rule of spec 4.1.
- **C. Per-leg extension.** Each leg unit adds its own B termination to one Cable, creating it on the
  first leg. This means editing an existing Cable, which the plugin never does today, and the
  connector-by-index rule rewires connectors when a lower leg arrives later.

## 2. Designs

| Design | Model | Effort | Isolation |
| --- | --- | --- | --- |
| 1 | Opus 5.5 (this session) | n/a | Drafted while design 2 ran, before reading it |
| 2 | `gpt-6-astra` via `codex exec -s read-only` | high | Fresh context, brief sections 1 (decision through acceptance) only, no candidate shapes, no design 1 |

### 2.1 Design 1 (shape A, shared harness change)

**Grouping from resolved NetBox state.** A leg is a trace segment one of whose ends resolves to an
Interface with a `channel_id`. A channel child can only be a trace endpoint, because a trace passes a
Device only through a FrontPort/RearPort pass-through. The harness key is the child's parent
Interface. The cards label corroborates in review and decides nothing (spec 5.1 stays true). Leg `k`
is the child with `channel_id = k`.

**Module.** New `cable_harness.py`, the only reader of `channels`, `channel_id` and the breakout
profile classes. Interface: one function that takes the batch's resolved leg segments (child, far
end, trace identity, effective Cable Type, first-hop health) and returns, per parent, either a
desired harness (profile, A `[parent]`, B far ends ordered by channel) or one block reason that
applies to every leg of that parent. A second function compares a desired harness with one existing
Cable's CableTermination rows, connector included. On NetBox 4.6 the module sees no channel fields
(`Interface._meta` lookup, once) and returns no harness, so planning is unchanged.

**Plan shape.** Each leg trace stays its own unit (spec 4.1 and ADR 0002 unchanged). The harness is
one Planned Change, `cable:create-harness:<parent identity>`, identical in every leg unit, the way
two traces already share `cable:create:<pair>`. Its payload is the profile, the A termination and the
ordered B terminations. A change in the leg set, the order or the profile changes the payload, so
every leg unit's fingerprint moves (condition 8). Preconditions: the parent's id, `channels` and no
CableTermination; each child's `(parent, channel_id)`; each far end empty.

**Profile.** Found from the running instance's profile classes: the one with `a_connectors == {1: N}`
and `b_connectors == {1..N: 1}` and no second A connector, where `N` is the parent's `channels`. No
match blocks with `cable.harness_profile_unavailable`. A leg's CableClass mapping and segment
override decide Cable Type only, and every leg's effective type must agree, else
`cable.harness_policy_conflict`. The mapping's profile dimension does not apply to a leg.

**Coupled health.** The payload must be complete and identical in every unit, so one unhealthy first
hop blocks every leg of that parent: unresolved or invalid leg, far end occupied, incompatible kind
(`cable.harness_incomplete`, naming the leg). A block further down one leg's trace does not touch the
harness.

**Failure cases (all `blocked`).**

| Case | Code |
| --- | --- |
| Far ends of more than one model | `cable.harness_mixed_terminations` |
| Channels stated with a gap (1, 3) | `cable.harness_channel_gap` |
| Two legs on one channel | `cable.harness_channel_conflict` |
| Both ends of one segment are channel children | `cable.harness_unsupported` |
| Any existing Cable on the parent that is not exactly the desired harness | `cable.harness_occupied` |

Trailing unused channels (legs 1 and 2 of 4) are allowed: the B side is shorter, which
`BaseCableProfile.clean` permits and which shifts nothing.

**Existing Cables.** Occupancy already comes from CableTermination rows, so the cable NetBox mirrors
onto a child never reads as a child's own Cable. An existing Cable on the parent equal to the
desired harness proves every leg's first segment: each leg reads `cable.segment_reused` and the
units can be no-op. The plugin never deletes or edits a harness Cable in this design. A harness that
grows later (a second document states leg 3) stays `cable.harness_occupied` until the operator
edits it in NetBox. Cable extension is a split candidate. Direct per-lane Cables cannot survive the
migration on a child, so the plan never sees them on a channelized transceiver.

**Selective sync.** Selecting one leg executes the shared harness change, which writes the B ends of
every leg. That matches how a shared segment already executes from either unit. The other legs'
later segments stay in their own units.

**Spec changes.** 3.6: the cardinality rule exempts a harness leg and says where its profile comes
from. 6.3: harness satisfaction. 6.4: a harness creation writes connector order. 6.5: the new
preconditions. 6.7: the new codes. 15: remove breakout, keep trunk and shuffle.

**Guard.** An AST test that only `cable_harness.py` names `channel_id`, `channels` or a profile
class, the same way the preview guard test pins one owner.

**Validation.** End to end on the 4.7 stack: a workbook with four leg traces, a channelized parent,
FrontPort far ends, execute, assert the Cable's terminations by connector and each child's
CablePath, replan, assert no-op. One test per failure row, asserting the code and no write. The full
suite on the 4.6 stack proves condition 5.

**First increment.** Condition 4 alone: a channel child as a one-to-one termination blocks with
`cable.harness_*` rather than reaching NetBox's refusal. Then the create, reuse and failure rows.
Lane auto-match (`P0k` to channel `k`) is out of scope: spec 5.1 keeps the cards label out of
matching, and the operator picks each leg once.

### 2.2 Design 2 (shape B, harness unit)

Verbatim output of the blind run, kept for the merge record.

> **Proposal: represent a harness and its complete leg traces as one Synchronization Unit.** The Cable Target Module derives one ordered breakout Cable from resolved channel relationships. Ordinary traces retain their current behavior.
>
> This is an independent design proposal, not a ratified design. I read the repository contracts and implementation. No files changed, and no tests or containers ran. NetBox behavior below relies on the source excerpts supplied in the brief.
>
> 1. **Ownership, interface, and seam**
>
> The existing Cable Target Module owns harness detection, grouping, comparison, planning, and execution. `trace_workbook.py` continues to produce source evidence without querying NetBox or inventing a breakout source object.
>
> The public **interface** remains `CableModule.plan(...)` and `CableModule.apply(...)`, behind the existing `TargetModuleRuntime` **seam**. The Import Engine remains target-neutral.
>
> Inside `cable_target.py`, introduce a private harness analysis module after termination resolution and pass-through substitution, before ordinary segment classification:
>
> ```text
> analyze_harnesses(resolved_batch, topology_snapshot, policy)
>     -> harness analyses, consumed segment identities, diagnostics
> ```
>
> Its interface requires the complete batch, including unresolved or invalid sibling evidence. It returns immutable planning facts, never writes, and never silently drops a failed leg.
>
> The resulting **depth** hides grouping, channel ordering, occupancy, policy consensus, and complete-group refusal behind one interface. Its **leverage** serves preview, execution replanning, and integration tests. **Locality** keeps channel knowledge out of adapters, views, and `plan.py`.
>
> The **deletion test** is concrete: removing this module would redistribute harness ordering and safety into per-trace planning, selection, and execution. It earns its place. No new Target Module registry entry or generic topology framework is needed.
>
> 2. **Grouping and plan representation**
>
> Use both source evidence and resolved NetBox state, with different roles:
>
> - The source cohort key is `(name_identity(device), name_identity(cards))`. It identifies sibling evidence, including a leg whose target resolution failed.
> - The physical harness key is `("dcim.interface", parent.pk)`, scoped by the plan’s existing planning context.
> - A resolved Interface with `parent` and `channel_id` establishes harness membership. Source labels alone never authorize a breakout Cable.
> - Every leg in one source cohort must resolve consistently to the same parent. Different source card cohorts resolving to one parent are blocked as ambiguous, rather than silently combined.
> - Cards remain excluded from automatic port matching. Their new role is grouping and consistency checking after matching.
> - A cohort containing a channel child and unresolved or unchannelized siblings blocks as a whole. Unchannelized siblings must not fall through to ordinary Cable creation.
>
> Orient each trace locally from its channel child outward. “First hop” means the adjacent physical segment in that orientation, after existing pass-through resolution. It does not mean the trace’s final endpoint.
>
> A valid harness becomes:
>
> ```text
> Synchronization Unit: cable:harness:dcim.interface:<parent-id>
>   Member Source Traces: stable ADR 0002 identities
>   Planned Change: cable:create:harness:dcim.interface:<parent-id>
>   Other Planned Changes: required downstream physical segments
> ```
>
> The harness change identity excludes leg membership and profile: those are changing facts about the same synchronization purpose. Its payload contains the parent, ordered lane records, profile, Cable Type, and provenance from every contributing trace.
>
> Downstream changes keep their existing unordered-pair identities. Identical changes shared with other units still execute once under section 4.4. Conflicting payloads remain invalid.
>
> The harness unit contains the complete paths of its member traces. Any blocked or invalid member prevents all its writes. Unsupported traces that encounter two channelized parents are blocked explicitly; this design does not invent a breakout-to-breakout representation.
>
> Dependencies retain actual write requirements: creations depend on permitted deletions that free their terminations. No artificial chain is needed between independent Cable creations; the enclosing transaction makes the harness and downstream changes atomic.
>
> **Preconditions** include:
>
> - Parent identity, Device, and `channels`.
> - Child identities, parent relationships, and channel numbers.
> - Ordered lane-to-far-end identities and concrete model labels.
> - Runtime profile topology.
> - Existing Cable identities, ends, connector numbers, profile, and relevant attributes.
> - Occupancy or recorded absence on parent, children, and far ends.
> - Required PortMapping rows, policies, and permissions.
>
> Execution extends the existing locked replan to these references, in the repository’s consistent lock order, and rechecks before writing.
>
> Every harness unit carries a structured `cable.harness_state` informational diagnostic whose **evidence** records these semantic facts, even when the unit has no changes. This uses the existing fingerprint contract in `plan.py`; display data is insufficient. Membership, connector order, and profile therefore change the unit fingerprint on both actionable and no-op plans.
>
> 3. **Channel ordering and profile choice**
>
> Resolved `channel_id` is the authoritative channel number. For this source format, parse the stated `P0k` lane and require it to equal that number. A saved resolution to the wrong child must produce a diagnostic, not silently change lane meaning.
>
> Let `N = parent.channels`. Require:
>
> - Exactly one distinct leg for every channel `1..N`.
> - Exactly one distinct far termination per leg.
> - No channel child as a directly cabled far termination.
> - One concrete NetBox model across the B side.
> - Compatibility between the parent and that model under NetBox’s runtime rules.
>
> Duplicate source occurrences already collapsed by the adapter do not count as extra legs. Equivalent claims of the same child-to-far-end segment share one lane and retain their provenance; conflicting claims block.
>
> Select the runtime choice `breakout-1c<N>p-<N>c1p`. Verify its actual topology: one A connector with positions `1..N`, and N single-position B connectors with position `k` routed to connector `k`. Do not trust the choice string alone.
>
> Write:
>
> ```text
> a_terminations = [parent]
> b_terminations = [far_end(channel=1), ..., far_end(channel=N)]
> ```
>
> Never sort B ends by name or object ID. Never compact a sparse channel list.
>
> This follows the supplied behavior of `Cable.update_terminations` and the breakout profile.
>
> 4. **Diagnostics and deterministic refusal**
>
> All conditions below prevent harness writes. Diagnostics identify all affected member traces, subject to existing disclosure rules.
>
> | Condition | Code | Disposition |
> |---|---|---|
> | First-hop far ends have different concrete models | `cable.harness_mixed_termination_models` | `blocked` |
> | Missing middle **or trailing** channel, including an incomplete export | `cable.harness_incomplete` | `blocked` |
> | Distinct leg count exceeds N | `cable.harness_capacity_exceeded` | `blocked` |
> | Required runtime breakout profile does not exist | `cable.harness_profile_unavailable` | `blocked` |
> | Runtime profile exists but lacks the required routing or capacity | `cable.harness_profile_incompatible` | `blocked` |
> | Two different legs claim one channel or reuse one far termination | `cable.harness_leg_conflict` | `blocked` |
> | Source lane number differs from resolved `channel_id` | `cable.harness_channel_mismatch` | `blocked` |
> | Source lane label cannot establish `k` | `cable.harness_lane_unresolved` | `blocked` |
> | Source cohort and resolved parent grouping disagree | `cable.harness_group_conflict` | `blocked` |
> | Sibling resolution is incomplete, or migration left a sibling unchannelized | `cable.harness_member_unresolved` | `blocked` |
> | Child channel is outside the parent’s range, or target channel state is internally inconsistent | `cable.harness_channel_state_invalid` | `invalid` |
> | A leg requires directly cabling another channel child, or a trace spans two harness parents | `cable.harness_topology_unsupported` | `blocked` |
> | Member trace has no physical first-hop evidence | `trace.endpoint_evidence_only` | `blocked` |
> | A downstream member cannot be planned | Existing specific diagnostic | Propagate `blocked` or `invalid` to the harness |
>
> For the mixed-model case, every leg displays `cable.harness_mixed_termination_models`. Suppress consequential occupancy and policy messages until this structural refusal is resolved. Do not infer omitted patch-panel hops.
>
> Requiring all `1..N` channels is deliberately stricter than NetBox’s allowance for fewer connectors. It gives incomplete exports one predictable outcome, including a missing last leg.
>
> 5. **Existing Cables, migration leftovers, and replacement**
>
> Classify complete harness matches before applying the current multi-termination conflict rule.
>
> | Existing state | Rule |
> |---|---|
> | Cable implements the complete desired parent-to-lane mapping | Reuse it; no Cable write |
> | Same far-end set but different connector order | Block with `cable.harness_existing_conflict` |
> | Parent occupied by a partial, direct, or different breakout Cable | Block with `cable.harness_existing_conflict` |
> | Desired far end occupied by another Cable | Block with existing `cable.termination_occupied` or `cable.multi_termination_conflict` |
> | Child mirrors the matching parent Cable and its correct position | Expected derived state; not independent occupancy |
> | Child has a direct CableTermination | `cable.harness_child_termination_invalid`, `invalid` |
> | Child’s cached Cable/position contradicts the parent’s derived state | `cable.harness_channel_state_invalid`, `invalid` |
> | Existing downstream Cable exactly matches its ordinary desired segment | Reuse under current rules |
>
> Reuse requires profile routing to prove the same channel-to-far-end mapping. A side-reversed representation qualifies only if its runtime profile proves that equivalence. Matching unordered termination sets alone is insufficient.
>
> Topology-bearing profile differences are conflicts. Type, label, and other non-topology attribute drift remains informational and is not updated, following current reuse policy.
>
> **The importer never automatically replaces a conflicting harness Cable.** It also never treats a Cable touching a harness parent, channel child, or first-hop far end as a deletable per-leg Logical Cable. The operator corrects these occupancies and replans.
>
> Ordinary, unrelated Patched Path Replacement retains its current deletion rule. A Cable proven to satisfy any desired segment remains protected across the whole batch.
>
> For the operator’s migration:
>
> - If old per-lane Cables are deleted completely, the plan sees free parent/far ends and plans creation.
> - If only their child terminations are removed, any surviving far-end termination remains an occupancy conflict.
> - If Cables are moved elsewhere but retain a desired far end, they still conflict.
> - If moved entirely away from desired terminations, they are irrelevant and untouched.
> - If a child retains its old Cable and therefore cannot receive `channel_id`, the mixed migrated/unmigrated cohort blocks.
> - If migration produces the exact desired breakout Cable, the plan reuses it.
>
> A wholly unchannelized cohort remains ordinary input. The importer cannot infer an unrecorded intention to migrate it.
>
> 6. **Selective synchronization**
>
> Selecting a leg opens or highlights its harness unit. The executable action must state **“Sync harness: N traces”** and display every member and planned change.
>
> There is no executable per-leg unit for a recognized harness. A request carrying an old trace-unit identity is refused and requires replan. UI, REST, and queued jobs submit the accepted harness-unit identity explicitly.
>
> This changes the smallest selectable unit visibly. It does not silently expand a trace selection. Downstream changes of all member traces belong to that unit. Existing cross-unit dependencies still require explicit selection under section 4.5.
>
> Each leg retains its Source Trace identity for resolutions, provenance, and review. Its displayed disposition derives from the harness unit.
>
> 7. **NetBox 4.6 behavior**
>
> Detect channel support once from Interface model metadata. Only when both channel fields exist may planning read channel attributes or request channel-specific related data.
>
> On 4.6, bypass harness analysis entirely. Preserve existing unit identities, ordering, diagnostics, display structures, payloads, and fingerprints. Existing fixture units must serialize byte-identically with fixed inputs.
>
> Do not fabricate channels from names or profiles on 4.6. That would create a second representation of the same feature.
>
> An executable-plan schema bump can invalidate old previews at release time without changing serialized ordinary units. The byte-equivalence assertion concerns units; the versioned plan envelope changes deliberately.
>
> 8. **CableClass and Cable Type policy**
>
> Topology determines the harness profile. Per-leg CableClass profile choices do not determine it.
>
> For a new harness:
>
> - Resolve each leg’s Cable Type using its existing segment override, otherwise its CableClass mapping.
> - Require every effective type to be resolved, currently offered, and exactly equal.
> - Explicitly none is a resolved value. None versus a concrete type is disagreement.
> - Different CableClass labels are acceptable when their effective Cable Types agree.
> - Disagreement blocks with `cable.harness_type_conflict`.
> - Missing and stale type choices retain `cable.cableclass_unmapped` and `cable.policy_stale`.
>
> For consumed first-hop segments, only the type dimension applies. Their stored profile dimension is visibly marked “derived from channel topology”; it may remain unresolved without blocking the harness. Ordinary downstream segments retain the existing two-dimension policy.
>
> This requires an explicit amendment to override applicability, not a hidden exception inside `decided_policy()`. No new harness policy table is needed.
>
> Exact existing harness reuse does not require creation policy to be complete. Available policy differences can produce informational drift, as ordinary reused segments do today.
>
> 9. **Specification changes and deletions**
>
> Amend:
>
> - **3.6:** distinguish ordinary segment policy from harness type consensus and derived profile.
> - **4.1:** add the atomic multi-trace harness unit.
> - **4.2–4.4:** specify group diagnostics, identities, ordered facts, and shared downstream changes.
> - **4.5–4.7:** define explicit harness selection, transactional scope, and multi-trace provenance.
> - **4.8:** invalidate executable previews whose representation predates this change.
> - **5.1–5.2:** preserve Source Trace identity while separating it from harness unit identity.
> - **6.1–6.7:** add channel detection, grouping, ordered comparison, occupancy, preconditions, and diagnostics.
> - **10.2, 10.3, 10.6–10.7:** grouped review, selection, jobs, progress, and audit.
> - **12:** add acceptance tests and the 4.6 regression gate.
> - **15:** permit this bounded breakout creation; retain exclusions for trunk/shuffle creation and port mutation.
> - **CONTEXT.md and ADRs 0001/0002:** record the unit exception without redefining Source Trace identity.
>
> Delete the consumed child-to-far-end ordinary creation path and its independent policy/occupancy classification. Replace the universal “multi-termination always conflicts” assertion with exact harness matching followed by conflict detection.
>
> Delete tests that assert obsolete universal rules after their ordinary-segment and harness cases are covered. Keep the ordinary one-connector profile chooser for ordinary segments.
>
> 10. **Validation and mechanical prevention**
>
> Use real workbook input, real NetBox objects, `ImportEngine.plan`, accepted serialized plans, real execution, and a replan. Use isolated PostgreSQL/Redis with the configured worker pool on both supported versions.
>
> | Acceptance | Required end-to-end proof |
> |---|---|
> | **1** | Shuffled and reversed four-leg traces create exactly one correctly profiled Cable; assert connector numbers, no child CableTerminations, and each child’s actual CablePath. Repeat with four FrontPorts and real mapped panel continuations. |
> | **2** | Replan after execution: every member displays no-op, no changes remain, Cable IDs/counts stay unchanged. |
> | **3** | Interface/FrontPort first-hop mixture: every leg has the specific mixed-model code; attempted execution writes nothing. |
> | **4** | Real child resolution never emits ordinary child creation. Cover manual resolution and mixed migrated/unmigrated siblings. Start with a red regression against current planning. |
> | **5** | Compare canonical serialized units for every existing 4.6 fixture against the pre-change baseline in the real 4.6 runtime. |
> | **6** | Remove middle and trailing legs; exceed N; use a channel count without a runtime profile; duplicate a channel; mismatch source lane and channel. Assert deterministic codes and zero writes. |
> | **7** | Exact reuse, connector permutation, parent conflicts, far-end conflicts, child mirrors, and each migration-leftover case above. Assert conflicting Cable identities survive unchanged. |
> | **8** | Change membership, channel assignment, connector order, or profile, including a no-op harness. Assert fingerprint change and refusal of the accepted stale selection. |
>
> Also test grouped selection through the real review command, provenance for every member, shared downstream changes executing once, permission loss, transaction rollback after a later downstream failure, and idempotent job delivery.
>
> Use separate real transactions for occupancy/channel changes racing execution. Verify both writer schedules, rather than relying on sequential stale-plan tests.
>
> **Mechanical guard:** use one validated Cable-intent constructor shared by planning and execution decoding. Ordinary intents reject channel-child terminations. Harness intents require complete ordered channels, homogeneous distinct B ends, and verified profile topology. Execution rebuilds the intent from locked live objects before calling NetBox.
>
> This makes silent connector compaction and direct child termination impossible through the supported writer. Test the constructor with selected bad cases and nearby valid cases; no broad AST analyzer is needed.
>
> 11. **Smallest complete first increment and uncertainties**
>
> The first increment is a vertical slice: a fully resolved four-leg harness on free terminations, explicit grouped selection, one real Cable write, correct child CablePaths, provenance, and an idempotent no-op replan. It includes exact reuse, all refusal gates, fingerprinting, and the 4.6 bypass.
>
> Use runtime profile topology from the outset. Then extend acceptance coverage through the existing downstream panel machinery; do not create a disposable four-lane writer or a second execution path.
>
> Three matters require verification against the actual 4.7.2 source and runtime during implementation:
>
> - The exact profile metadata and CableTermination fields needed to prove connector routing, including side-reversed reuse.
> - When NetBox finishes child propagation and CablePath generation within the write transaction.
> - Whether existing lock coverage also serializes concurrent native NetBox edits to child membership and termination occupancy. The supplied excerpts do not establish that.
>
> Those are verification gates. The design does not claim their behavior has been tested in this read-only pass.
## 3. Merge

Added evidence after both designs were complete: the operator supplied a second export, made by the
source tool's newer per-module trace. It holds exactly the four leg traces of the one breakout card,
row for row the same as those blocks in the full export: no transceiver object, no harness, and the
same direct first hops for three legs. A per-module export is therefore a complete statement of one
card's legs, not new structure.

| # | Decision | Design 1 | Design 2 | Evidence | Disposition |
| --- | --- | --- | --- | --- | --- |
| 1 | Unit shape | one unit per trace, harness as a shared change | one harness unit holding every member trace | D2 changes spec 4.1, ADR 0002, selection, REST, jobs and audit. D1 keeps them, but today a shared change executes from one owner and never pulls its co-owners (`review_workspace.sync_selection`), so selecting one leg would write B ends that leg's trace does not state, which spec 4.5 forbids ("never expands silently") | **Merged, contested.** Keep per-trace units. A harness change pulls every unit that carries it into the visible `Sync with dependencies` closure. A sibling that is not actionable then refuses the whole closure through the existing 4.5 rule. Round 1 attacks it; a second reversal makes it a split candidate |
| 2 | Grouping key | resolved parent only | source cohort (device, cards) plus resolved parent, both must agree | D1 hole: an unresolved sibling is invisible to D1, so legs 1 and 2 plan a two-leg harness and the later third leg is blocked forever (no Cable edit) | Design 2. The cards label groups and blocks, never matches (spec 5.1 holds) |
| 3 | Missing legs | trailing allowed, gap blocked | every channel `1..N` required | The target instance has unused lanes (channels with no child or no Cable), and one card's export states legs 1 and 2 only. D2 blocks that card forever. A gap cannot be written (connector by index) | Design 1, with a `cable.harness_partial` warning naming the missing channels and the per-module export as the way to confirm. Round 1 attacks it |
| 4 | Fingerprint of a reused harness | payload only | `cable.harness_state` diagnostic with evidence on every harness unit | D1 hole: a no-op unit has no change, so its payload fingerprints nothing | Design 2 |
| 5 | Lane label check | none | parse `P0k`, require `k == channel_id` | Parsing a source spelling in the Cable Target Module ties it to one export format; the operator's resolution is the decision (spec 6.1) | Design 1. Lane auto-match, where the parse would live, is a later increment |
| 6 | Profile choice | search profile classes by topology | choice string, then verify topology | One source of truth is the profile class | Design 1 |
| 7 | Guard | AST test on channel-field owners | one validated Cable-intent constructor used by planning and execution | D2's guard makes the failure class (child termination, compacted connectors) impossible at the writer; D1's only limits where fields are read | Design 2 |
| 8 | Child with its own CableTermination | impossible, not checked | `invalid` | NetBox enforces it only in `clean()`; fail fast on unexpected state | Design 2 |
| 9 | Mixed migrated and unmigrated siblings | not addressed | whole cohort blocks | Follows from row 2 | Design 2 |
| 10 | Two cohorts resolve to one parent | not addressed | blocked | Follows from row 2 | Design 2 |
| 11 | Side-reversed reuse | not addressed | allowed if profile proves it | Every breakout choice the instance offers has one A connector (`breakout-1c<N>p-<N>c1p`, `2c4p-8c1p-shuffle` excluded), so the parent can only be on A | Settled: reuse requires the parent on A |
| 12 | Coupled health scope | first-hop facts only | any member block, downstream included | Follows from row 1: the closure rule refuses a non-actionable sibling | Design 2 by consequence |

## 4. Merged candidate r1

1. **Owner.** A new `cable_harness.py` module, called by the Cable Target Module after termination
   resolution and pass-through substitution, before classification. It is the only reader of
   `channels`, `channel_id` and breakout profile classes. Interface: `analyze(batch facts) ->
   {parent: HarnessDecision}` plus the per-leg block reasons, and `matches(decision, cable rows)`.
   On NetBox 4.6 (no channel fields on `Interface._meta`) it returns nothing and planning is
   byte-identical.
2. **Grouping.** A leg is a segment one end of which resolves to an Interface with `channel_id`.
   Harness = resolved parent. Source cohort = `(name identity of device, name identity of cards)`
   of the leg's channel end. One cohort must resolve to one parent and one parent to one cohort. A
   cohort with an unresolved, invalid or unchannelized sibling blocks every member.
3. **Order and profile.** Leg `k` is the child with `channel_id = k`. B side = far ends ordered by
   channel. Profile = the running instance's profile class with one A connector of `N =
   parent.channels` positions and `N` one-position B connectors.
4. **Plan.** Units stay one per Source Trace. Every leg unit carries one identical Planned Change
   `cable:create:harness:<parent identity>`; payload: profile, Cable Type, A `[parent]`, ordered B,
   provenance of every leg. Every leg unit also carries `cable.harness_state` (`info`) whose
   evidence is the parent, channels, member legs, ordered far ends and profile, actionable or no-op.
   Preconditions: parent id, `channels`, no CableTermination on the parent; each child's
   `(parent, channel_id)` and no CableTermination; each far end empty.
5. **Selection.** `sync_selection` pulls every unit carrying a harness change into the closure.
   The existing 4.5 rule then refuses the closure when any member is not actionable.
6. **Writer.** One `CableIntent` constructor, used by planning and by execution after it relocks
   live objects. Ordinary intents refuse a channel child. Harness intents require ordered,
   distinct, single-model B ends with no channel gap, and a verified profile.
7. **Policy.** Each leg's override, else its CableClass mapping, decides Cable Type only; all legs
   must agree (explicitly none counts as a value). The profile dimension does not apply to a leg
   and may stay unresolved.
8. **Existing Cables.** An existing Cable with the parent on A, the derived profile and the same
   ordered B ends proves every leg (`cable.segment_reused`). Any other Cable on the parent, or a
   foreign Cable on a far end, blocks every member. The plugin never deletes or edits a Cable on a
   parent, a child or a first-hop far end. A child with its own CableTermination is `invalid`.
9. **Missing legs.** Trailing channels absent from the cohort are written as a shorter B side, with
   a `cable.harness_partial` warning naming them and the per-module export. A gap blocks.

| Condition | Code | Disposition |
| --- | --- | --- |
| Far ends of more than one model | `cable.harness_mixed_terminations` | `blocked` |
| Channel gap | `cable.harness_channel_gap` | `blocked` |
| Two legs on one channel, or one far end on two legs | `cable.harness_leg_conflict` | `blocked` |
| More legs than channels, or no profile for `N` | `cable.harness_profile_unavailable` | `blocked` |
| Cohort and parent disagree, or a sibling is unresolved, invalid or unchannelized | `cable.harness_member_unresolved` | `blocked` |
| Leg Cable Types disagree | `cable.harness_type_conflict` | `blocked` |
| Another Cable on the parent | `cable.harness_occupied` | `blocked` |
| A segment with a channel child at both ends, or a trace spanning two parents | `cable.harness_unsupported` | `blocked` |
| A child holds its own CableTermination, or `channel_id` exceeds `channels` | `cable.harness_channel_state_invalid` | `invalid` |
| Trailing channels absent | `cable.harness_partial` | `warning` |

10. **Spec.** 3.6, 4.5, 6.1 to 6.7 and 15 change; 4.1 and ADR 0002 do not.
11. **First increment.** One four-leg harness on free FrontPort far ends: create, child CablePaths,
    no-op replan, exact reuse, the guard, the 4.6 bypass, and every row of the table.

### 0. Refuted and settled claims

- Side-reversed reuse (D2): settled by the instance's choice list, see merge row 11. Reopens if
  NetBox adds a breakout profile with more than one A connector or an `Nc1p-1cNp` profile.

## 5. Round 1 (r1, `gpt-6-astra` high, read-only): NOT RATIFIED

| # | Finding | Severity | Verified | Status |
| --- | --- | --- | --- | --- |
| R1-1 | The harness closure lives only in `sync_selection`. `ImportEngine._selected_units` (`import_engine.py:420`) checks only the submitted identities and `merge_changes` checks only change dependencies, so the engine executes one leg's selection with every leg's B end | blocker | Read both functions: no co-owner check exists | Accepted, fixed in r2 item 5 |
| R1-2 | The cohort bijection blocks valid input: a blank or shared cards label puts two parents in one cohort, and source aliases put one parent in two cohorts. Spec 5.1 gives no uniqueness to `(device, cards)` | major | `trace_workbook.py:118` makes cards optional | Accepted, fixed in r2 item 2 |
| R1-3 | The parent is outside the execution lock set (`_lock_segment_terminations` locks segment ends only), so a concurrent `channels` change can slip under a reused harness. Locking an Interface does not lock the absence of a new CableTermination on it, and `CableTermination.save()` does not call `clean()` | major | Read `cable_target.py:1086`; NetBox `cables.py` 639-648, 766-777 | Accepted, fixed in r2 item 4, with the writer assumption stated |
| R1-4 | Reuse ignores positions. NetBox resolves a peer by connector and `positions__contains` (`cable_profiles.py` 89-95), and `CableTermination.clean()` does not check positions, so a corrupt existing harness would read as reused | major | Read both | Accepted, fixed in r2 item 8 |
| R1-5 | The partial-harness warning must say that a later leg needs a manual NetBox edit; a per-module export confirms present evidence only | minor | Follows from r1 item 8 | Accepted, r2 item 9 |

## 6. Merged candidate r2

Changes from r1, verbatim:

- **Item 2 (grouping).** The cohort is one-way negative evidence. A harness is the resolved parent.
  A harness blocks (`cable.harness_member_unresolved`) when any trace in a cohort that holds one of
  its legs has an unresolved or invalid termination at that cohort's Device. The bijection is gone:
  two parents may share a cohort, and two cohorts may resolve to one parent. A cohort member that
  resolves to an Interface without `channel_id` beside channel siblings reads
  `cable.harness_member_unchannelized` (`warning`): it can be a plain port that shares a blank cards
  label, so it never blocks.
- **Item 4 (locking).** The execution replan locks, in this order after the existing segment locks:
  parent Interfaces by pk, every child of those parents by pk, then the CableTermination rows on the
  parents, the children and the first-hop far ends. It rereads `channels`, `channel_id` and the
  termination snapshot under those locks before it derives and compares the harness, also when the
  harness is reused and no harness change runs. Writer assumption, stated in the spec: a
  CableTermination enters the database through `Cable.save()` after `Cable.clean()` (NetBox UI, REST,
  and `full_clean()` callers). A raw `CableTermination.save()` that skips `clean()` is outside the
  contract; a child termination found after the fact is `cable.harness_channel_state_invalid`.
- **Item 5 (selection).** A new target-neutral field `SynchronizationUnit.selected_with`: the unit
  identities that must be selected together with this one. It is in the unit fingerprint. Every leg
  unit of one harness names every other leg unit, whatever its disposition, so a blocked sibling
  still appears. One validator in `plan.py` decides a selection: every member of every selected
  unit's `selected_with`, in the accepted plan and in the current plan, must be selected and
  actionable. `ImportEngine._selected_units`, `review_workspace.sync_selection`, `cannot_sync` and
  `sync_all` all call it. Execution never expands a selection; it refuses one with
  `SelectionError` or `StalePlan`. Ordinary shared changes keep today's semantics.
- **Item 8 (reuse).** One termination snapshot per Cable: side, connector, positions and object
  identity for every CableTermination row. Reuse requires the parent alone on A at connector 1 with
  positions `1..N`, and B connector `k` holding exactly the leg `k` far end with positions `[1]`,
  for every stated leg, with no extra B row. Any other snapshot on the parent is
  `cable.harness_occupied`. The snapshot is also the stale-state comparison. `_cable_terminations`
  stays the deletion helper and is never the harness comparator.
- **Item 9 (missing legs).** The `cable.harness_partial` warning names the missing channels and
  states that a leg added later needs the Cable edited in NetBox, since the import never edits or
  replaces a harness Cable.
- **Item 5 addendum.** `selected_with` is set only while the harness change is pending. Once an
  existing Cable proves the harness (item 8), it is empty, so a sibling that is no-op never has to
  enter an execution for another leg's downstream work.

## 7. Round 2 (r2, `gpt-6-astra` high, read-only): NOT RATIFIED

R1-1, R1-2, R1-4 and R1-5 CLOSED. R1-3 NOT-CLOSED through R2-1 and R2-2.

| # | Finding | Severity | Verified | Status |
| --- | --- | --- | --- | --- |
| R2-1 | r2 locks parents after the segment locks, which already hold the children. NetBox saves a parent row, then `bulk_update`s its children (`signals.py` ~427, `propagate_channel_cables`), so the orders invert and PostgreSQL aborts one transaction with a deadlock instead of a stale refusal | major | Read both; the parent row is written before the children | Accepted, r3 item 4 |
| R2-2 | A third-party writer that ran `Cable.clean()` while a port was unchannelized can still insert a direct termination after the operator channelizes it, around the importer's harness write | major | The window is real in NetBox source | **REFUTED** as an importer defect, see section 0. r3 item 4 rewords the assumption |
| R2-3 | `_executable_units` (`review_workspace.py:1158`) picks a dependency owner without `selected_with`, so it can pick a harness leg whose companion is blocked and hold back a unit that another owner could serve | major | Read it: only change dependencies prune the set | Accepted, r3 item 5 |
| R2-4 | Acceptance 5: a serialized `selected_with: []` changes the bytes of every ordinary unit | minor | `SynchronizationUnit.to_dict` (`plan.py` ~364) lists its fields | Accepted, r3 item 5 |

## 8. Merged candidate r3

Changes from r2, verbatim:

- **Item 4 (locking), replaces the r2 lock order.** The execution replan reads the parent ids from
  the resolved children without a lock, then locks, in this order: every parent Interface by pk, then
  the existing segment locks (which take the children with the other ports), then Cables, then
  CableTermination rows, then PortMapping rows, keeping the existing Cable-before-CableTermination
  rule (`cable_target.py:1052`). It then rereads every parent's `channels`, every child's `parent`
  and `channel_id`, and the termination snapshot. A parent or child set that differs from the
  unlocked read refuses with `StalePlan`. The order matches NetBox's own parent-then-children write.
  Assumption, reworded: the import serializes its own writes against NetBox writes that take row
  locks. It does not serialize two NetBox writers against each other. A state they produce between
  themselves, such as a direct termination on a channel child, is detected at the next replan as
  `cable.harness_channel_state_invalid`.
- **Item 5 (selection), adds to r2.** `_executable_units` prunes to the fixed point of both
  relationships: a unit leaves the set when a change it depends on has no owner left in the set, or
  when any member of its `selected_with` is outside the set. `sync_selection` then picks owners only
  from that set, and the shared validator checks the result. `selected_with` is omitted from
  `SynchronizationUnit.to_dict()` and from the fingerprint when empty, so every unit without a
  pending harness serializes byte for byte as today.

### 0. Refuted and settled claims (continued)

- **R2-2.** Evidence: the bad state needs a third-party writer that validated before the operator
  channelized the port, and the operator's channelization. Those two NetBox writers produce it with
  the importer absent, so no importer lock removes it, and the importer writes only the parent's
  termination, which does not collide with a child row. The plan detects the state afterwards
  (r3 item 4). Reopens if a schedule shows the importer's own write producing the invalid state,
  or hiding it from the next replan.

## 9. Round 3 (r3, `gpt-6-astra` high, read-only)

Verdict A (core: grouping, plan shape, selection, writer, reuse, diagnostics): **RATIFIED r3** under
stable topology. Verdict B (concurrency): **NOT RATIFIED r3**. R1-3, R2-1, R2-3, R2-4 CLOSED. R2-2
WITHDRAWN: the section 0 refutation holds. The core depends on B for safe concurrent execution, so
A is not a standalone split core.

| # | Finding | Severity | Verified | Status |
| --- | --- | --- | --- | --- |
| R3-1 | A native Cable profile change runs `update_terminations(force=True)` (`cables.py` 388-389). `CableTermination.delete()` calls `clear_cable_termination()` (`cables.py` 779-785), which updates a channelized parent's children before the parent row is saved. That is children before parent, the reverse of a parent save, so the r3 parent-first order deadlocks against it | major | Read both paths | Accepted, r4 item 4 |

Three rounds produced three variants of one mechanism (R1-3, R2-1, R3-1): NetBox writes a parent and
its children in both orders, so no fixed acquisition order can be correct. r4 removes the
dependence on order instead of choosing another one.

## 10. Merged candidate r4

Changes from r3, verbatim:

- **Item 4 (locking), replaces the r3 lock order.** The harness lock set is every parent Interface
  and every child of those parents. The execution replan takes it first, in one
  `select_for_update(nowait=True)` query per model in pk order, before the existing segment locks.
  Lock contention (PostgreSQL `55P03`) refuses the execution with `StalePlan` and rolls back
  everything, so a concurrent native write in either order meets a refusal rather than a wait. Then
  the existing locks follow unchanged, and the plan rereads `channels`, `parent`, `channel_id` and
  the termination snapshot. A set that differs from the unlocked read refuses with `StalePlan`.
  The importer's own harness write takes no new parent or child row lock, because it already holds
  them; NetBox's propagation inside that write updates rows the transaction owns. The writer
  assumption of r3 is unchanged.

### Adjacent debt, reported for a follow-up

The existing segment locks wait on ordinary ports in object-type and pk order. A native write that
locks two of those ports in the reverse order can deadlock against them today, independently of
this design. Its outcome is a rolled-back execution classified as a database failure. That is a
separate ticket, not part of this design.

## 11. Round 4 (r4, `gpt-6-astra` high, read-only)

Verdict A: **RATIFIED r4** under stable topology (carried from r3). Verdict B: **NOT RATIFIED r4**,
R3-1 NOT-CLOSED. `55P03` to `StalePlan` judged sound if translated at the lock acquisition with a
contention-specific message. A NOWAIT error inside the write savepoint does not poison the outer
transaction once the savepoint unwinds.

| # | Finding | Severity | Verified | Status |
| --- | --- | --- | --- | --- |
| R4-1 | The later locks still wait. A native Cable status change or PortMapping save rebuilds child-origin paths, and `CablePath.delete()` / `retrace()` update the origin's `_path` row (`signals.py` 292-317, `cables.py` 906-915), so the native writer waits on the importer's child lock while the importer waits on that Cable or mapping | major | Read both | Accepted as a mechanism, see split |
| R4-2 | The importer's own `Cable.save()` runs `rebuild_paths()` over every path through a shared RearPort and updates each origin's `_path`, including children of a harness outside the batch, so it can wait on rows no source-derived lock set names | major | Read `signals.py` 283-290 and `cables.py` 906-915 | Accepted as a mechanism, see split |

**Mechanism analysis.** Four rounds produced four variants of one mechanism: freedom from deadlock
against NetBox's implicit writes. Those writes reach CablePath origins chosen by NetBox's path
rebuild, which no source-derived lock set predicts. Every Cable the import writes today already
triggers the same rebuild (`signals.py` 283-290), so ordinary execution has the same exposure
before this design. Integrity does not depend on it: the decision rows are locked and reread before
any write, and any abort, deadlock victim included, rolls back the whole selected transaction with
its audit row kept.

### Proposed split (operator decision)

- **Core, retained:** verdict A (r3) plus integrity-only concurrency. Lock parents, then children,
  then the existing locks, with normal waits. Reread and refuse a changed set with `StalePlan`.
  Translate a deadlock (`40P01`) or lock timeout raised during execution into a typed retryable
  refusal ("NetBox changed these objects during the sync; retry") instead of a database failure.
  The NOWAIT step of r4 is dropped: it removed one inversion and none of the others.
- **Deferred:** freedom from deadlock against NetBox's implicit path rebuilds. It covers ordinary
  Cables as much as harnesses, so it is one follow-up for the whole Cable Target Module.

Operator decision (2026-10-15): split. Deadlock freedom is deferred.

## 12. Split core r5

r5 = r3 (sections 4, 6, 8) with item 4 replaced as below. Changes from r4, verbatim:

- **Item 4 (locking, integrity only).** The execution replan reads the parent ids from the resolved
  children without a lock. It then locks, with normal waits: parent Interfaces by pk, then the
  existing segment locks (which take the children), then Cables, CableTerminations and PortMapping
  rows as today. Under those locks it rereads `channels`, `parent`, `channel_id` and the termination
  snapshot, and refuses with `StalePlan` when the parent or child set, or any harness fact, differs
  from the accepted plan. The r4 NOWAIT step is removed.
- **Item 4a (concurrency refusal).** A PostgreSQL deadlock (`40P01`) or lock timeout (`55P03`)
  raised inside the execution savepoint is translated where it is caught into a typed `StalePlan`
  whose message says NetBox changed these objects during the sync and the sync can be retried. The
  savepoint unwinds before any further SQL, the selected transaction rolls back whole, and the
  failed audit row is kept. The operator retries with a fresh execution key, as today.
- **Item 4b (writer assumption, unchanged from r3).** The import serializes its own decision facts,
  not two NetBox writers against each other; a state those produce is detected at the next replan.
- **Scope note.** Freedom from deadlock against NetBox's implicit CablePath rebuilds is out of this
  design. It applies equally to ordinary Cables and becomes one follow-up for the Cable Target
  Module. The core does not depend on it: integrity holds under every schedule because decision
  facts are locked and reread before any write, and every abort rolls back the whole selection.

Operator confirmation (2026-10-15): a deadlock that ends in a refusal and a retry is acceptable.

## 13. Round 5 (r5 split core, `gpt-6-astra` high, read-only): BLOCKED r5

Item 4a CLOSED as sound. No other core defect found.

| # | Finding | Severity | Verified | Status |
| --- | --- | --- | --- | --- |
| R5-1 | r5 locks only the children that are segment ends. With a partial harness an unexported child stays unlocked; `propagate_channel_cables` loads every child, then `bulk_update`s them (`device_components.py` 1442-1449), so a native move of that child to another parent between the load and the update leaves it mirroring the wrong Cable, with no deadlock and no refusal | major | Read; r2 item 4 locked "every child of those parents by pk", and r3 and r5 dropped that clause | Open: a regression in the r5 wording of a rule r2 already had |

Per section 5 of the process, a blocker in the split core's verdict round reports the design
blocked. The fix is to restore the r2 clause: after the parent locks, lock every Interface whose
`parent` is one of those parents, by pk, whether or not the export states it, and include that full
child set in the reread.

Operator decision (2026-10-15): override the block, apply the fix as r6, and run one more round
scoped to R5-1.

## 14. Split core r6

r6 = r5 with item 4 changed, verbatim:

- **Item 4 (locking, integrity only).** The execution replan reads the parent ids from the resolved
  children without a lock. It then locks, with normal waits: parent Interfaces by pk
  (`SELECT ... FOR UPDATE`), then **every Interface whose `parent` is one of those parents, by pk,
  whether or not the export states it**, then the existing segment locks, then Cables,
  CableTerminations and PortMapping rows as today. Under those locks it rereads `channels`, the full
  child set with each child's `channel_id`, and the termination snapshot, and refuses with
  `StalePlan` when any of them differs from the accepted plan. A native write that moves a child
  out of a locked parent needs that child's row lock. One that moves or adds a child into a locked
  parent writes a foreign key to the parent row, and PostgreSQL's `FOR KEY SHARE` check on that row
  waits for the importer's `FOR UPDATE`. Both therefore wait for the import to commit, then see the
  parent cabled, and NetBox's own signal recomputes the mirror.

## 15. Round 6 (r6, operator override, `gpt-6-astra` high, read-only): BLOCKED r6

R5-1 CLOSED for the move-out schedule.

| # | Finding | Severity | Verified | Status |
| --- | --- | --- | --- | --- |
| R6-1 | Django creates PostgreSQL foreign keys `DEFERRABLE INITIALLY DEFERRED`, so a native move or add of a child into a locked parent waits only at commit, after its synchronous `post_save` (`signals.py` 402-442) already ran with the parent still uncabled. The import's propagation cannot see the uncommitted child. Both commit: the child sits under the cabled parent with a null mirror, and nothing refuses | major | Confirmed in the 4.7.2 container: `postgresql/operations.py:151` returns `" DEFERRABLE INITIALLY DEFERRED"`. The r6 claim that the move waits before its signal was wrong | Open, see below |

**Analysis.** The same state arises with no import involved: one NetBox user cables the parent while
another moves a child into it. NetBox computes the mirror in `post_save` and serializes it against
nothing that the other writer takes before commit. The import cannot take a lock that the native
child save acquires before its signal, because that child is not known in advance and the parent's
foreign-key check is deferred. Prevention therefore needs NetBox cooperation. Detection does not: the
next replan reads the full child set and can compare each child's mirror (`cable`, `cable_end`,
`cable_connector`, `cable_positions == [channel_id]`) with its parent's Cable. Design 2 proposed
exactly that check (`cable.harness_channel_state_invalid` for a mirror that contradicts the parent),
and the merge did not carry it into r1.

Operator decision (2026-10-15): accept R6-1 as a residual, add the detection, and close the design on
this session's verification without a further round.

## 16. Final design r7

r7 = r6 plus one rule, verbatim:

- **Item 8a (mirror check).** For every parent the plan reads, each child's mirror must match
  NetBox's propagation rule (`propagate_channel_cables`, `clear_channel_cables`): with a profiled
  Cable on the parent, the child has the same `cable` and `cable_end`, the parent's
  `cable_connector`, and `cable_positions == [channel_id]`; otherwise all four are empty. Any
  difference makes every leg unit of that parent `invalid` with
  `cable.harness_channel_state_invalid`, naming the child and the repair (save the parent again in
  NetBox, or run `manage.py trace_paths`). A reused harness with a stale mirror is never no-op.

Verification behind the close, recorded as this session's own evidence rather than a reviewer
verdict: the rule restates the two NetBox functions read at `device_components.py` 1428-1464, and
the reread under the r6 locks sees every committed child, so the state R6-1 leaves behind is
detected by the first replan after both transactions commit.

**Accepted residuals (state them in the PR body).**

- R6-1: a child moved or added into a parent while the import cables it keeps an empty mirror until
  the next replan reports it. NetBox has the same race between two of its own users.
- Deadlock against NetBox's implicit writes ends in a retryable refusal (r5 item 4a). The operator
  accepted that outcome on 2026-10-15.

## 17. Scope, next action and open work

**Decision.** One breakout Cable per channelized parent, planned from the leg traces in the batch.
Units stay one per Source Trace. One shared Planned Change carries the harness, `selected_with` keeps
its legs together in every selection, `cable_harness.py` alone reads channel facts, one
`CableIntent` constructor guards every write, and reuse compares a full termination snapshot.

**First increment** (observable acceptance): on the 4.7 stack, a workbook with four leg traces on
one channelized parent with FrontPort far ends plans actionable leg units with one shared harness
change and a full `selected_with`. Selecting all four executes one Cable with the derived profile,
the parent alone on A and leg `k` on B connector `k`, and every child's CablePath reaches its far
end. A replan is no-op on every leg. Selecting one leg alone is refused by the engine and by the
workspace. Mixed far-end models, a channel gap, a stale child mirror, and an occupied parent each
block or invalidate every leg with their codes and write nothing. The 4.6 stack plans every
existing fixture byte for byte as before. The red test for acceptance 4 (a channel child resolved
as a one-to-one end today reaches NetBox's refusal at execution) comes first.

**Spec sections to change with the code:** 3.6, 4.5, 6.1 to 6.7, 13.3 (the concurrency refusal), 15.

**Follow-ups, not part of this design:**

- Deadlock freedom of the Cable Target Module's execution locks against NetBox's implicit CablePath
  writes, for ordinary Cables and harnesses alike (sections 10 and 11).
- Lane auto-match of a source lane to its channel child.
- Extending or replacing an existing harness Cable when a later export states more legs.
- The source export flattens most panel hops, so most real harnesses read as mixed and block until
  the source states every leg's first hop.

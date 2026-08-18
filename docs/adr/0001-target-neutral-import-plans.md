---
status: accepted
date: 2026-08-17
---

# Use target-neutral Import Plans

The import runtime currently treats Device and Rack rows as its universal model. Preview and execution share one `run_import()` function through a `dry_run` flag. Result rows also carry presentation data and execution-safety state. This structure cannot represent one Source Trace that plans several dependent Cable changes, and it makes views and jobs depend on private engine details.

Replace the internal runtime with one target-neutral Import Engine. Preserve the existing user workflow, outer engine seam, and NetBox Job adapter. Do not preserve the current Python signatures or Device-specific passes.

## Interfaces

The runtime has these conceptual interfaces:

```text
SourceAdapter.interpret(...) -> SourceBatch
ImportEngine.plan(...) -> ImportPlan
ImportEngine.execute(..., accepted_plan, selection) -> ImportExecution
```

A source adapter performs deterministic source interpretation only. It returns typed source items and source diagnostics. It does not query NetBox, enforce target permissions, match target objects, or plan writes.

The Import Engine coordinates planning and execution. Device, Rack, Cable, and future target modules plan against the complete relevant Source Batch. Each target module owns its target-specific matching, ORM queries, permissions, preconditions, locking, and writes. The target module applies one Planned Change at a time. It does not commit transactions, enqueue work, or call another target module.

The coordinator owns dependency ordering, semantic plan comparison, transaction scope, rollback, idempotency, audit completion, and progress. Views and jobs use only the Import Engine interface. They do not calculate safety intents or call target modules and private engine helpers.

A separate Review Workspace module consumes Import Plans. It validates explicit review commands, persists decisions through their owning domain models, and asks the Import Engine for a new plan. Review commands never edit an Import Plan.

## Import Plan model

An Import Plan is a serializable derived artifact. It contains no live ORM objects, lazy querysets, callables, HTML, template fragments, or untyped metadata bags.

An Import Plan contains Synchronization Units. A Synchronization Unit is the smallest independently reviewable and executable part of the plan. It contains zero or more typed Planned Changes that commit together. A flat Device or Rack source row usually produces one unit. One complete Source Trace produces one unit even when the source uses several workbook rows and the target requires several Cable changes.

Each unit has one disposition:

- `actionable`: it contains changes that can execute.
- `no-op`: current NetBox state already matches the desired state.
- `blocked`: it needs an operator decision or an unmet dependency.
- `invalid`: source or target state violates a planning invariant.
- `excluded`: synchronization policy intentionally omits it.

Diagnostics are separate structured values with `info`, `warning`, or `error` severity. Planned Changes record a stable identity, a target-specific serialized payload, explicit dependencies, and all target-state preconditions that can affect the planned result.

The plan revision, Synchronization Unit identities, and Planned Change identities have different roles. Unit and change identities remain stable across replanning while they represent the same synchronization purpose. They do not rely on list positions, display names, or workbook row numbers alone.

The plan and each unit have canonical fingerprints. Fingerprints include the schema version, stable identities, target payloads, dependencies, preconditions, dispositions, structured diagnostic codes, source fingerprint, Import Profile configuration, actor, and planning context. They exclude timestamps, URLs, translated text, and display-only wording.

Planning is side-effect free and deterministic. Equivalent source content, Import Profile, actor, planning context, and visible NetBox state produce the same canonical plan identities, ordering, and fingerprints.

## Dependencies and selection

The coordinator merges Planned Changes into one directed acyclic graph. Target modules declare dependencies by stable Planned Change identity. The coordinator rejects missing references and cycles. It executes independent changes in a deterministic order.

Identical changes with the same identity are shared and execute once. The same identity with different payloads or preconditions makes the plan invalid.

Target-owned supporting changes remain inside their Synchronization Unit. A dependency on another Synchronization Unit must already be reconciled or be included explicitly. Selective synchronization never expands silently. The review workspace can offer a visible `Sync with dependencies` selection for operator confirmation.

## Preview and execution safety

Preview presents an accepted Import Plan. Execution regenerates the current plan from the same source and configuration before it writes.

Selective execution compares the accepted unit and its explicit dependency closure with the equivalent current units. A change in an unrelated unit does not block a safe selection. A source, Import Profile, actor, or planning-context change invalidates every selection. The complete preview regenerates after each selective execution.

Final execution applies all currently actionable remaining units in one transaction. Each selective execution also uses one transaction. Blocked, invalid, excluded, and no-op units do not enter an execution transaction.

Inside the transaction, each target module resolves and locks its referenced NetBox objects and rechecks its opaque preconditions before writing. Any stale state, permission failure, validation error, or database error rolls back the complete selected transaction.

An accepted plan belongs to the operator who generated it. A background job executes as that operator and rechecks current permissions. Another operator must generate and accept a new plan.

Every execution request has an idempotency key. A duplicate HTTP submission or duplicate job delivery returns the existing Import Execution. Concurrent requests remain subject to target locks and plan preconditions.

Every selective and final execution creates an Import Execution audit record. A successful audit result commits atomically with its NetBox changes. On failure, NetBox changes roll back before the attempt is marked failed. Failure results identify the failed Planned Change, rolled-back changes, and dependent changes that were not attempted. The native NetBox Job references the Import Execution.

Progress counts selected Synchronization Units and Planned Changes. It does not count adapter-specific source rows or target-module ORM operations.

Serialized Import Plans carry an explicit schema version. An incompatible active preview or queued job fails before writes and requires replanning. Historical Import Executions remain audit records. The runtime does not migrate old executable plans or keep compatibility executors.

The Import Plan contract is storage-neutral. An active preview can store it in the session, and a background job can receive the accepted serialized plan. A durable review-session model is justified only if the review-workspace design proves that resumable plans require one.

## Examples

A Device source item can produce this Synchronization Unit:

```text
Synchronization Unit: one source Device
  Planned Change: ensure Manufacturer
  Planned Change: ensure Device Type
    depends on Manufacturer
  Planned Change: create or update Device
    depends on Device Type
Cross-unit dependency: Rack Synchronization Unit, when the source file must create it
```

A Source Trace can produce this Synchronization Unit:

```text
Synchronization Unit: one complete Source Trace
  Preconditions:
    endpoint identities
    current direct Cable
    termination occupancy
    pass-through mappings
  Planned Change: remove the LLDP-derived logical Cable
  Planned Changes: create or reuse the physical Cable segments
    ordered by their termination dependencies
  Result: the physical path terminates on the same two endpoint Devices
```

If the current direct Cable already represents the complete physical Source Trace, the Source Trace unit is a no-op. The Patched Path Replacement decision defines exact Cable matching and reuse rules separately.

## Rejected options

- Do not add another fixed Cable pass to the existing Rack and Device pass sequence. It would leave profiles, results, review, and safety tied to Device rows.
- Do not use a universal CRUD dictionary. Target-specific payloads and behavior belong to typed target modules.
- Do not keep `run_import(..., dry_run=...)` as the permanent planning and execution interface.
- Do not make presentation rows or session dictionaries the execution-safety contract.
- Do not split target planning and execution into separate public module hierarchies. One target module owns both sides so its target knowledge cannot drift.
- Do not retain a compatibility facade or a second runtime path after cutover.

## Consequences

The replacement moves existing Device and Rack behavior behind target modules, moves flat workbook parsing behind a source adapter, and updates preview, selective synchronization, final synchronization, background jobs, and review actions in one completed cutover. It removes fixed passes, `RowResult`, view-owned safety helpers, and calls to private engine behavior.

Tests use the new interfaces as their surface. End-to-end tests cover upload through real database outcomes. Import Engine and target-module integration tests use the real NetBox ORM. Source adapters use representative workbook fixtures. Tests for obsolete private helpers are deleted after their behavior is covered through the new interface.

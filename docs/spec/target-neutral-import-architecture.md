# Target-neutral import architecture

This specification defines the buildable architecture for a target-neutral import runtime. It
replaces the Device-centred internal runtime with one Import Engine, one Source Adapter contract,
and typed Target Modules. It proves the architecture with Cable and Source Trace support.

Every decision here is already recorded. The rationale lives in the referenced decision records and
closed issues. This document states the normative contract only.

Normative sources: ADR 0001 (target-neutral Import Plans), ADR 0002 (Source Trace identity),
ADR 0003 (adapter-owned Import Profile configuration), issues #81, #82, #83, #84, #85, #86, and the
research notes `netbox-cable-profile.md`, `openai-compatible-provider-contract.md`, and
`vault-provider-credentials.md`. The two file names that contain "provider" are historical citations
and keep their published names.

Where an input is silent on a detail an implementer must know, this document chooses the smallest
consistent option and marks it "(spec default)".

## 1. Domain model and terminology

`CONTEXT.md` is the single glossary. Use its terms with its exact capitalization in code,
identifiers, UI strings, diagnostics, and documentation. Do not introduce a synonym for a term that
already exists, and do not use any term listed under `_Avoid_`.

The runtime uses these terms:

| Term | Runtime role |
| --- | --- |
| Import Profile | Selects one Source Adapter and carries its configuration and target policy. |
| Source Adapter | Interprets one source file format into a Source Batch. |
| Source Batch | The typed source items and source diagnostics from one file. |
| Target Module | Owns one NetBox target's Target Fields, matching, planning, and writes. |
| Target Field | A semantic NetBox value the import can resolve or synchronize. |
| Import Plan | The reviewable derived artifact for one source file and the current NetBox state. |
| Synchronization Unit | The smallest independently reviewable and executable part of a plan. |
| Planned Change | One target-specific mutation inside a Synchronization Unit. |
| Import Execution | One audited attempt to apply selected units as one transaction. |
| Row Resolution | A saved operator decision that supplies Target Field values. |
| Resolution Proposal | An unaccepted suggestion of Target Field values. |
| Candidate Snapshot | The frozen eligible-candidate list sent in one proposal request. |
| Inference Backend | The configured component that answers one proposal request. |
| Source Trace | Source evidence for one end-to-end connectivity path. |
| Termination Reference | The source-side naming of one port inside a Source Trace. |
| Segment Evidence | One source-claimed physical cable between two Termination References. |
| Pass-Through Claim | The implied continuation through one device between two segments. |
| Endpoint Summary | The From and To statement of the two endpoint Termination References. |
| CableClass | The source label for one cable's kind. |
| Cable Type, Cable Profile | The fixed NetBox Cable choices a CableClass maps to. |
| Logical Cable | The direct Cable between the two endpoint terminations. |
| Patched Path Replacement | The reviewed change that replaces a Logical Cable with segments. |
| Direct Mapping, Candidate Mapping | Import Profile column rules. |
| Field Difference, Ignored Field Difference | Device review decisions. |

Two identifiers are distinct and must not be conflated. A Target Field key names a semantic value in
the target-field catalog. A field key names one bound instance of a Target Field for one source item,
and is what a Row Resolution and a Resolution Proposal bind to.

The component that answers a proposal request is always called an Inference Backend, never a bare
"Provider". NetBox core already defines `circuits.Provider` as the circuit carrier, so an unqualified
"Provider" collides with an existing NetBox concept. The glossary records Inference Backend with
_Avoid_: Provider. Its UI label is "AI backend". Its transport component is the backend adapter. Its
configuration, metadata, and failures are Inference Backend configuration, backend metadata, and
backend failures. The operator-facing action keeps the name "Ask AI".

## 2. Runtime module boundaries and dependency direction

### 2.1 Runtime interfaces

The runtime keeps the seam from ADR 0001 and fixes its call shapes.

`source_document` is a reference to a stored uploaded workbook: its id plus its content fingerprint.
The bytes live in the `SourceDocument` model (section 9.1), so preview, replanning, a background
execution, and an audit read all see the same input. Passing a reference instead of the bytes keeps
the plan serializable and keeps job payloads small.

`planning_context` carries the actor together with the other planning inputs that the plan fingerprint
covers. The explicit `actor` parameter is that same identity, passed separately so permission checks
and `netbox_reader` can bind to it.

`netbox_reader` is the read-only target-state accessor a Target Module uses while planning. It is
scoped to the actor's permissions: it returns only objects that actor may view, so planning cannot
leak target state the operator cannot see.

```text
SourceAdapter.interpret(source_document, adapter_config) -> SourceBatch
  errors: adapter configuration validation failure, unreadable source document

ImportEngine.plan(profile, source_document, actor, planning_context) -> ImportPlan
  errors: adapter configuration validation failure, unreadable source document,
          stale source document, permission failure

ImportEngine.execute(profile, source_document, accepted_plan, selection,
                     idempotency_key, actor) -> ImportExecution
  errors: plan schema version mismatch, stale plan, stale source document,
          selection validation failure, permission failure, precondition failure

TargetModule.plan(source_batch, profile, catalog, netbox_reader)
    -> list of Synchronization Units
  errors: policy validation failure, permission failure

TargetModule.apply(planned_change, execution_context) -> applied result
  errors: stale precondition, permission failure, target validation failure
```

Three failures are distinct. The coordinator raises a stale-source-document error when the referenced
`SourceDocument` no longer exists, and the operator must upload the file again. It raises a stale-plan
error before the transaction opens when the regenerated plan no longer matches the accepted selection.
A Target Module raises a precondition failure inside the transaction when a recheck fails.

### 2.2 Dependency direction

```text
views, jobs
    -> ImportEngine, ReviewWorkspace          (public seam)
ReviewWorkspace
    -> ImportEngine, domain models
ImportEngine
    -> SourceAdapter registry, TargetModule registry, target-field catalog, plan model
TargetModule
    -> NetBox ORM, target-field catalog, Import Profile policy models, plan model
SourceAdapter
    -> source parsing libraries, target-field catalog (keys only)
InferenceBackend adapter
    -> HTTP transport only
CredentialBackend
    -> Vault client only
```

No arrow points backwards. A Source Adapter never imports a Target Module, NetBox models, or the
Import Engine. A Target Module never imports a Source Adapter implementation; it consumes typed
source items by output kind. A backend adapter never imports NetBox models, Candidate Snapshot
models, Resolution Proposal models, or job models.

### 2.3 Responsibilities

**Source Adapter.** Deterministic source interpretation only. It returns typed source items and
source diagnostics. It never queries NetBox, enforces target permissions, matches target objects, or
plans writes.

**Target Module.** It owns target-specific matching, ORM queries, permission checks, preconditions,
locking, and writes. It plans against the complete relevant Source Batch. It applies one Planned
Change at a time. It never commits a transaction, enqueues work, or calls another Target Module.

**Import Engine coordinator.** It owns dependency ordering, the merged directed acyclic graph,
semantic plan comparison, transaction scope, rollback, idempotency, audit completion, and progress.

**Review Workspace.** It consumes Import Plans. It validates explicit review commands, persists
decisions through their owning domain models, and asks the Import Engine for a new plan. A review
command never edits an Import Plan.

### 2.4 What views and jobs may call

| Caller | Allowed | Forbidden |
| --- | --- | --- |
| Views | `ImportEngine.plan`, `ImportEngine.execute`, Review Workspace commands, domain model reads | Target Modules, Source Adapters, private engine helpers, safety-intent calculation |
| Jobs | `ImportEngine.execute`, the inference proposal service | Target Modules, plan mutation, safety recalculation |
| Templates | The serialized Import Plan and view-supplied presentation data | ORM traversal into planning state |

## 3. Source Adapter and Import Profile contracts

### 3.1 Registry and selection

The Source Adapter registry is a static in-plugin mapping from a stable adapter key to the adapter
class. Forms, REST, GraphQL, and YAML derive their choices from the registry. This delivery has no
third-party extension point.

`ImportProfile.source_adapter` is required and immutable after creation. A different source format
requires a new Import Profile. Every policy row's applicability is therefore stable for the profile's
lifetime.

Adapter keys in this delivery: `flat_workbook` and `trace_workbook` (spec default).

### 3.2 Adapter configuration

Scalar adapter settings live in one `adapter_config` JSON field. The selected adapter declares a
Django form that validates the field at the boundary. Unknown keys are rejected.

The `flat_workbook` adapter form carries the current device-format settings: sheet name, source-ID
column, custom-field name, update-existing, create-missing-device-types, capture-extra-data, primary
contact role, primary contact lookup field, and preview view mode.

The `trace_workbook` adapter declares an empty configuration. Its sheet names are fixed by the Source
Trace model (#84).

### 3.3 Natural-key references

An object reference inside `adapter_config` uses a natural key, never a database id. Today the only
reference is the primary contact role, referenced by its name (spec default). The adapter form
validates the reference at the boundary. Planning validates it again. A dangling reference blocks the
affected Synchronization Units with a diagnostic and does not fail the batch. This replaces
database-level PROTECT and keeps YAML profile export portable between NetBox instances.

### 3.4 Output kinds and target-field catalog

Each Source Adapter declares its output kinds. Each Target Module declares which output kinds it
consumes and which Target Fields it owns.

| Adapter | Output kinds | Consuming Target Modules |
| --- | --- | --- |
| `flat_workbook` | Device source row, Rack source row | Device, Rack |
| `trace_workbook` | Source Trace | Cable |

The target-field catalog is one static registry. Each entry declares a key, a label, a value kind,
the owning Target Module, and the adapter output kinds that can supply it.

A profile's valid Target Fields and policy sections are derived, never listed locally: adapter output
kinds, then consuming Target Modules, then their Target Fields and policy sections.

The catalog holds two entry shapes: static entries with a fixed key, and declared key families with a
prefix and a validator. A key family covers a Target Field whose exact key is data, not a fixed
choice. The Device Target Module declares the `extra_json` family: the key is `extra_json:` plus a
name, and its validator requires a non-empty name after the prefix. Every surface resolves a family
key through the catalog validator, so no surface reimplements the prefix rule.

These surfaces consume the catalog and keep no local field list: `ColumnMapping` and
`ColumnTransformRule` validation, all profile forms, REST serializers, GraphQL types, YAML import and
export, the Review Workspace, and execution. `ColumnMapping` and `ColumnTransformRule` validation
moves to the catalog and accepts exactly the same values it accepts today: the static keys plus a
non-empty `extra_json:` name, with `ColumnTransformRule` still excluding the candidate-target keys.
The current hand-rolled per-section YAML handling is replaced by catalog-derived serialization in the
cutover.

### 3.5 Policy applicability

Every policy section declares the adapter output and target it applies to. The profile UI shows only
applicable sections. Validation rejects an inapplicable row.

| Section | Classification | Adapter |
| --- | --- | --- |
| ColumnMapping, ColumnTransformRule, `adapter_config` source keys | Source-format fact | `flat_workbook` |
| ClassRoleMapping, DeviceTypeMapping, ManufacturerMapping, IgnoredDevice | Device and Rack target policy | `flat_workbook` |
| update-existing, create-missing-device-types, capture-extra-data, primary-contact settings, preview view mode | Device and Rack target policy stored in `adapter_config` | `flat_workbook` |
| CableClass mappings | Cable target policy | `trace_workbook` |
| SourceResolution, DeviceExistingMatch, IgnoredFieldDifference | Decision persistence scoped to the flat adapter output | `flat_workbook` |
| `TerminationResolution` | Decision persistence scoped to the trace adapter output | `trace_workbook` |

No model moves beyond the `adapter_config` change. Sections stay in their own tables.

### 3.6 CableClass mappings

One row exists per (Import Profile, CableClass value). It has two independent dimensions: Cable Type
and Cable Profile. Each dimension is tri-state:

- unresolved: not yet decided.
- an explicit runtime NetBox choice.
- explicitly none: a plain cable with no value for that dimension.

Values validate against the running NetBox instance's choices, never a hardcoded list. An unresolved
dimension blocks the unit. A stored value the running instance no longer offers also blocks, with a
distinct stale-mapping diagnostic, until the operator remaps it. The plugin never creates a Cable
Profile: Cable Profile is a fixed NetBox choice, not a creatable object.

**Cable Profile cardinality.** A Patched Path Replacement creates Cables with one termination per
side, so the Cable Profile dimension accepts only a Cable Profile whose connector topology is
compatible with one termination per side: the single and duplex family as reported by the running
NetBox instance. The mapping form offers only compatible choices. Trunk, breakout, and shuffle
profiles are out of scope for segment creation in this delivery (section 15).

Two block conditions are distinct and carry distinct diagnostic codes (section 6.7):

| Condition | Diagnostic code |
| --- | --- |
| The stored Cable Type or Cable Profile value is no longer offered by the running instance | `cable.cableclass_stale_mapping` |
| The stored Cable Profile is still offered but is incompatible with one termination per side | `cable.profile_incompatible` |

## 4. Target-neutral plan, safety, review, and execution contracts

This section restates ADR 0001 as the normative runtime contract.

### 4.1 Import Plan structure

An Import Plan is a serializable derived artifact. It contains no live ORM objects, lazy querysets,
callables, HTML, template fragments, or untyped metadata bags.

An Import Plan contains Synchronization Units. A Synchronization Unit is the smallest independently
reviewable and executable part of the plan. It contains zero or more typed Planned Changes that
commit together. A flat Device or Rack source row usually produces one unit. One complete Source
Trace produces one unit even when the source uses several workbook rows and the target requires
several Cable changes.

### 4.2 Dispositions and diagnostics

| Disposition | Meaning |
| --- | --- |
| `actionable` | The unit contains changes that can execute. |
| `no-op` | Current NetBox state already matches the desired state. |
| `blocked` | The unit needs an operator decision or an unmet dependency. |
| `invalid` | Source or target state violates a planning invariant. |
| `excluded` | Synchronization policy intentionally omits the unit. |

A unit has exactly one disposition. Diagnostics are separate structured values with `info`,
`warning`, or `error` severity. A diagnostic carries a stable structured code, the severity, the
affected identities, and display data. Diagnostic codes use a dotted lowercase namespace of the form
`<domain>.<condition>` (spec default).

`excluded` is reserved for operator-configured policy. An unsupported source construct is `invalid`,
never `excluded`.

### 4.3 Identities and fingerprints

Planned Changes record a stable identity, a target-specific serialized payload, explicit
dependencies, and all target-state preconditions that can affect the planned result.

Unit and change identities remain stable across replanning while they represent the same
synchronization purpose. They never rely on list positions, display names, or workbook row numbers
alone. The plan revision is a separate value from the identities.

The plan and each unit carry a canonical fingerprint. A fingerprint is the SHA-256 digest of a
canonical serialization (spec default for the algorithm; ADR 0002 already fixes SHA-256 for the
Source Trace content fingerprint, and one algorithm keeps the runtime consistent).

A fingerprint includes the schema version, the stable unit and change identities, the target payloads,
the dependencies, the preconditions, the dispositions, the structured diagnostic codes, the source
fingerprint, the Import Profile configuration, the actor, and the planning context. It excludes
timestamps, URLs, translated text, display-only wording, and the plan revision.

Planning is side-effect free and deterministic. Equivalent source content, Import Profile, actor,
planning context, and visible NetBox state produce the same canonical plan identities, ordering, and
fingerprints.

### 4.4 Dependency graph

The coordinator merges Planned Changes into one directed acyclic graph. Target Modules declare
dependencies by stable Planned Change identity. The coordinator rejects a missing reference and
rejects a cycle. It executes independent changes in a deterministic order.

Identical changes with the same identity are shared and execute once. The same identity with
different payloads or preconditions makes the plan invalid.

### 4.5 Selective synchronization

Target-owned supporting changes stay inside their Synchronization Unit. A dependency on another
Synchronization Unit must already be reconciled or be included explicitly. Selective synchronization
never expands silently. The Review Workspace offers a visible `Sync with dependencies` selection for
operator confirmation.

Preview presents an accepted Import Plan. Execution regenerates the current plan from the same source
and configuration before it writes. Selective execution compares the accepted unit and its explicit
dependency closure with the equivalent current units. A change in an unrelated unit does not block a
safe selection. A change of source, Import Profile, actor, or planning context invalidates every
selection. The complete preview regenerates after each selective execution.

### 4.6 Transactions

One outer database transaction covers one complete execution request. Final execution applies all
currently actionable remaining units in that one transaction. Each selective execution also uses one
transaction for its whole selection. No unit commits independently. Blocked, invalid, excluded, and
no-op units never enter an execution transaction.

Inside the transaction, each Target Module resolves and locks its referenced NetBox objects and
rechecks its opaque preconditions before writing. Any stale state, permission failure, validation
error, or database error rolls back the complete selected transaction.

### 4.7 Ownership, idempotency, and audit

An accepted plan belongs to the operator who generated it. A background job executes as that operator
and rechecks current permissions. Another operator must generate and accept a new plan.

Every execution request carries an idempotency key. The Review Workspace generates one key per submit
action (spec default: a UUID minted when the workspace renders the submit control). The request first
inserts an `ImportExecution` row with outcome `pending` and commits that insert, which reserves the
unique (Import Profile, idempotency key). A duplicate HTTP submission or duplicate job delivery
therefore returns the existing row in any outcome, including while the first attempt is still running.
The target transaction opens after the reservation commits. Concurrent requests remain subject to
target locks and plan preconditions.

A `pending` row stores its native NetBox Job reference, so a crashed attempt cannot strand it. At the
next read, a `pending` row whose linked Job is terminal or missing transitions to `failed` with typed
reason `abandoned`. Duplicate delivery of that idempotency key still returns the existing row, now
failed and abandoned. A new operator submission mints a new key, so no key is ever permanently
consumed and the operator is never locked out of retrying.

Every selective and final execution creates an `ImportExecution` audit record (section 9.2). The
successful audit result and the applied-changes field commit atomically with the NetBox changes. On
failure, NetBox changes roll back before the row is marked failed. A failure result identifies the
failed Planned Change, the rolled-back changes, and the dependent changes that were not attempted. The
native NetBox Job links one-to-one to its `ImportExecution` row.

Progress counts selected Synchronization Units and Planned Changes. It never counts adapter-specific
source rows or Target Module ORM operations.

### 4.8 Schema versioning and storage

A serialized Import Plan carries an explicit schema version. The first version is `1` (spec default).
An incompatible active preview or queued job fails before writes and requires replanning. Historical
Import Executions remain audit records. The runtime never migrates an old executable plan and never
keeps a compatibility executor.

The Import Plan contract is storage-neutral. This delivery stores the active preview plan in the
session and passes the accepted serialized plan to the background job (spec default, permitted by ADR
0001). No durable review-session model is added, because the review-workspace prototype (#83) did not
prove that resumable plans require one.

## 5. Canonical Source Trace and provenance model

### 5.1 Evidence structure

A Source Trace is input evidence for one end-to-end path. It is not a NetBox object, and it does not
correspond to one Cable. It carries:

```text
SourceTrace
  Endpoint Summary: From Termination Reference, To Termination Reference, original direction
  Ordered path evidence: [Segment Evidence(left, CableClass label, right), ...]
  Pass-Through Claims: [(device, cards, entry port, exit port), ...]
  Corroboration per termination: PortClass, UPos, Rack, Location
  Provenance: workbook fingerprint, sheet, block ordinal, row range,
              per-sheet export timestamp, original From/To text and direction
```

A Termination Reference is identified by its device, cards, and port labels. Normalization reuses the
plugin's existing source-identity convention: trim, collapse whitespace, casefold. Original spellings
stay in the evidence for display. The source Device column names the panel.

The cards label is part of Termination Reference identity, part of review display, and context for the
operator and the Inference Backend. Deterministic matching never uses it: port resolution uses only
the unique exact port-name match on the resolved Device (section 6.1). This supersedes the phrasing in
issue #84 that said the cards label narrows port matching, and follows the rule set in issue #86.

Corroboration values support validation and review display. They never identify anything.

### 5.2 Identity and content fingerprint

Per ADR 0002:

- **Trace identity**: the unordered pair of endpoint Termination References. Its canonical form is the
  JSON serialization of the two normalized termination triples, sorted lexicographically:
  `[["device","cards","port"],["device","cards","port"]]`. JSON is unambiguous by construction, so a
  label that contains a separator character cannot collide with another identity. This canonical JSON
  form is the stored key and the Synchronization Unit identity. It is stable across direction flips,
  re-exports, and patching changes.
- **Display form**: `device|cards|port <> device|cards|port`, derived from the canonical form for
  review and diagnostics. It is never a key, never stored as an identity, and never compared.

Storing the canonical JSON form instead of the readable string follows issue #84 as amended for the
spec (#78): the readable string keeps its role, but only as the display form.
- **Content fingerprint**: the SHA-256 digest of the canonical-orientation serialization of the
  Segment Evidence entries (identity triples, PortClass labels, CableClass label) and the Pass-Through
  Claims. Canonical orientation starts at the endpoint whose key sorts first. A reversed statement
  swaps sides and reverses the list before serializing. Corroboration values, timestamps, block
  positions, and Trace List data are excluded, so a device move or a re-export does not churn the
  fingerprint.

From and To direction is provenance and display data only. Reversing the path does not change
topology identity.

### 5.3 Sheet combination

- Blocks pair across sheets by exact From/To line text. A repeated block uses the ordinal as the
  tiebreak.
- `Trace From To` path rows are authoritative.
- A non-empty `Trace List` block corroborates. The ordered device-label sequence must match the visit
  sequence. Consecutive duplicate device rows are tolerated. The final device may be omitted when the
  trace ends at a rear port. Each listed port must match the entry or exit port of that visit. The
  `Cable` column is ignored: it is per-block, not per-segment. Any mismatch is a contradiction error
  on that trace.
- An empty `Trace List` block corroborates nothing.
- A `Trace List` block with data but no `Trace From To` partner becomes an Endpoint Summary fallback
  trace. Its visit rows are unordered corroboration, not Segment Evidence.

Sheet names are fixed in this delivery. Each sheet's export timestamp is provenance only and is never
cross-validated.

### 5.4 Duplicate collapse

Occurrences with identical evidence collapse silently into one Source Trace whose provenance lists
every occurrence. Comparison happens after canonical orientation, so a reversed re-statement also
matches. The same identity with differing evidence produces one invalid Source Trace with a
duplicate-conflict error naming the block locations.

### 5.5 Endpoint Summary fallback

A Source Trace with endpoint evidence but no path rows is valid evidence. Both endpoints resolve.
When a direct physical Cable already joins exactly these two terminations, the unit is a no-op.
Otherwise the unit is blocked with an endpoint-evidence-only diagnostic. It never plans changes.

### 5.6 Validation taxonomy

The owner column states which component detects the condition. A Source Adapter condition depends on
source structure only. A Cable Target Module condition depends on NetBox state (resolved Device,
existing Cables, PortMapping rows) and cannot be detected during source interpretation.

| Condition | Diagnostic code | Disposition | Owner |
| --- | --- | --- | --- |
| Workbook contains neither recognized sheet | `trace.no_recognized_sheet` | Batch-level error, no units | Source Adapter |
| Incomplete block (From without To, missing header row, unparseable Segment Evidence row) | `trace.incomplete_block` | `invalid` | Source Adapter |
| Non-linear or discontinuous path (broken continuity, duplicated or branching rows, first or last termination contradicting the From/To lines) | `trace.non_linear_path` | `invalid` | Source Adapter |
| `Trace List` contradicts the path rows | `trace.corroboration_mismatch` | `invalid` | Source Adapter |
| Same identity stated with differing evidence | `trace.duplicate_conflict` | `invalid` | Source Adapter |
| Pass-Through Claim whose entry or exit carries an interface-kind PortClass | `trace.pass_through_at_interface` | `invalid` | Source Adapter |
| Same termination claimed by different traces, or same segment pair with different CableClass | `trace.cross_trace_conflict` | `invalid` on every involved trace | Source Adapter |
| A PortClass value outside the adapter's fixed vocabulary | `trace.unknown_port_class` | `invalid` | Source Adapter |
| The resolved object is not an Interface, FrontPort, or RearPort | `cable.unsupported_termination_kind` | `invalid` | Cable Target Module |
| Ambiguous or missing Device, panels included | `trace.device_unresolved` | `blocked` | Cable Target Module |
| Endpoint evidence only and no matching direct Cable | `trace.endpoint_evidence_only` | `blocked` | Cable Target Module |

Diagnostic code strings are spec defaults; the conditions, dispositions, and owners are normative.

The Source Adapter emits its rows as source diagnostics on the Source Batch without querying NetBox.
The Cable Target Module emits its rows during planning.

An invalid trace does not stop the batch. The rest of the Source Batch continues.

Pass-Through Claim validation in the adapter checks continuity only: consecutive rows share device and
cards. The adapter records the claimed entry and exit pair verbatim. Same-port continuation is legal
evidence, including a trunk and a patch recorded on one rear port. Whether NetBox's front-to-rear
mapping can realize it is planning-time work (section 6).

An identical shared segment across traces (same termination pair, same CableClass) is allowed. It
dedupes at Planned Change identity per ADR 0001. The cross-trace occupancy check counts claims from
distinct traces only. A termination repeated inside one trace is either the legal same-port
continuation or part of an already-invalid structure.

### 5.7 Binding and provenance persistence

One complete Source Trace produces one Synchronization Unit. The unit identity is the trace identity.

After execution, provenance persists in one plugin table, in the same style as the existing per-Device
provenance table. Each row records the Import Profile, the Cable, the trace identity, the segment
index, the original From/To text and direction, the workbook provenance, and the source export
timestamp. There is no trace-level database record.

Cardinality is one row per (Cable, Import Profile, trace identity), enforced by a unique constraint on
that triple. The Cable reference is a plain foreign key, not a one-to-one relation. When two Source
Traces claim an identical shared segment, ADR 0001 shares one Planned Change and creates one Cable,
and that Cable carries one provenance row per contributing Source Trace. This follows issue #84 as
amended for the spec (#78): the per-created-Cable wording predates the shared-segment case that
issue #86 and ADR 0001 identity sharing introduce.

The removed Logical Cable is recorded by the `ImportExecution` deleted-object snapshot (section 9.2),
not by a provenance row.

### 5.8 Fixture corpus

The committed redacted structural fixtures are the trace adapter's test corpus. Redaction rewrote only
the workbook string table, so every structural artifact survives: block order, duplicated blocks,
blank-row layout, empty-string cells, and sheet dimensions.

| Fixture | Expected result |
| --- | --- |
| Copper trace workbook | 20 blocks per sheet collapse to 10 Source Traces, zero duplicate conflicts, all 10 valid with 3 Segment Evidence entries each, empty `Trace List` corroborates nothing |
| Fiber trace workbook | 20 blocks per sheet collapse to 10 Source Traces, zero duplicate conflicts, 8 valid with 4 to 9 segments, 4 ending at a rear port, every `Trace List` corroboration passing, 1 `trace.non_linear_path`, 1 `trace.pass_through_at_interface`, and one accepted legal same-rear-port continuation |
| Both | Zero shared terminations and zero CableClass conflicts between distinct traces |

## 6. Patched Path Replacement planning and transaction behavior

The Cable Target Module requires NetBox 4.6 or later, because it verifies pass-throughs against the
PortMapping model. The plugin's minimum NetBox version is 4.6.0 for this feature and for the plugin as
a whole.

### 6.1 Port resolution

Each Termination Reference resolves inside its resolved Device. Endpoint Device resolution uses the
existing explicit matching approach and runs before port resolution begins.

The adapter's fixed PortClass vocabulary claims the termination kind:

| PortClass | Claimed NetBox termination |
| --- | --- |
| NIC, Switch Port, Port | Interface |
| Position Front, Fiber Pair Front | FrontPort |
| Punch-Down, Fiber Pair Back | RearPort |

A PortClass value outside this vocabulary is a source-structure error the Source Adapter reports as
`trace.unknown_port_class`, so it never reaches planning. A resolved object whose real type is not an
Interface, FrontPort, or RearPort is a target-state error the Cable Target Module reports as
`cable.unsupported_termination_kind`.

Candidates are all terminations of the claimed kind on the resolved Device. Deterministic matching
requires a unique normalized exact port-name match. Anything else leaves the Target Field unresolved:
the unit is blocked, and manual searchable selection resolves it. The optional Resolution Proposal
flow (section 7) assists the same selection. The cards label is identity, display, and context for the
operator and the Inference Backend. Deterministic matching never uses it.

A termination resolved by the exact-name rule shows the `automatically resolved` badge state. A
termination resolved by an operator or by an accepted proposal shows `manually resolved` or
`accepted`.

### 6.2 PortMapping verification and same-port continuation

Pass-through verification uses the NetBox PortMapping model. A mapping row must link the resolved
FrontPort and the resolved RearPort. A contradicted claim makes the unit invalid with a diagnostic
naming both ports and the actual mappings. The fix is a NetBox panel-model correction or a
re-resolution, then a replan.

Same-port continuation substitutes the mapped peer port for the second cable end. With exactly one
distinct mapped peer, the planner substitutes automatically and records the substitution in the plan.
With several mapped peers, the unit is blocked and manual selection offers exactly the mapped peers.
The rule is symmetric for rear-to-front and front-to-rear, because both directions can fan out under
PortMapping.

A mapped-peer choice is a second field-key role, distinct from resolving the Termination Reference
itself. Its field key is the original termination field key plus the role marker `mapped_peer`. The
default role marker for termination resolution is `termination`. Both roles write a
`TerminationResolution` row that stores the selected object type, the selected object id, and the
display name at selection time. One termination can therefore carry one row for its own resolution and
one row for its mapped-peer choice, and the two never overwrite each other.

The planner asserts as an invariant that the resulting path's outer terminations belong to the two
resolved endpoint Devices.

### 6.3 Comparison and the no-op

Comparison is direction independent. A desired segment is an unordered termination pair. An existing
Cable satisfies it exactly when its two termination sets are precisely that pair, one termination per
side. A multi-termination Cable touching any desired port never matches; it is a conflict.

Attribute drift on a matched Cable (type, profile, status, label) surfaces as an `info` diagnostic. It
is not updated in this delivery.

**Segment precedence.** A direct Cable whose termination sets exactly match a desired segment is a
proven physical segment. It is never classified as the Logical Cable, and it is never deleted. The
planner classifies every existing Cable against the desired segment set before it looks for a Logical
Cable.

The Logical Cable is the current direct endpoint-to-endpoint Cable that matches no desired segment,
regardless of what created it. Its description and tags appear in review. There is no
provenance-marker gate.

The precedence rule decides the single-segment trace. When the trace claims exactly one segment
between the two endpoints, an existing direct Cable matches that desired segment, so it is a proven
physical segment and the unit is a no-op. Nothing is deleted and nothing is recreated.

The unit is a no-op exactly when every desired segment is satisfied by a proven existing Cable and no
Logical Cable remains to delete. Any partial state is actionable and reuses the proven segments. When
no direct Cable exists between the endpoints, the unit is still actionable as a creation-only
replacement, and the removal step is absent.

### 6.4 The replacement

One outer database transaction covers one complete execution request, selective or final (ADR 0001,
section 4.6). Units apply in dependency order inside that transaction. No unit commits independently,
and no unit opens a nested transaction that can commit on its own. Any failure in any selected unit
rolls back the complete selected transaction. This corrects the per-unit transaction phrasing used in
the text of issue #86: a Source Trace unit is the smallest reviewable and selectable part, not a
separate commit boundary.

Ordering inside the unit:

```text
Synchronization Unit: one complete Source Trace
  Planned Change 1: delete the Logical Cable            (present only when one exists)
  Planned Change 2..n: create or reuse each physical Cable segment
     each creation depends on change 1 when it exists
     creations follow canonical segment order
```

Deletion scope: the only Cable a Patched Path Replacement ever deletes is the one direct
endpoint-to-endpoint Logical Cable. Any other cable occupying a desired termination makes the unit
blocked. The operator clears it in NetBox and replans. The plugin never deletes or edits a third-party
cable.

Creation policy for a new segment:

| Attribute | Value |
| --- | --- |
| Status | `connected` |
| Type | From the Import Profile CableClass mapping |
| Profile | From the Import Profile CableClass mapping |
| Termination connector and position data | None written |
| Label, description, tenant | Empty |
| Color | NetBox default |

An unmapped CableClass blocks the unit. Provenance lives only in the plugin's per-Cable provenance
table, so nothing duplicates or drifts.

CablePath is never created or mutated. The plugin writes Cables and reads PortMapping rows. NetBox
derives the paths.

### 6.5 Preconditions and rollback

Preconditions recorded per unit:

- The Logical Cable's id and terminations, or its recorded absence.
- Each reused Cable's id and termination sets.
- Emptiness of every termination that receives a new cable end, evaluated net of the unit's own
  deletion.
- The PortMapping row ids the path relies on.
- The resolved termination ids.

Execution rechecks every precondition inside the transaction. Any mismatch rolls back the complete
selected transaction and requires a replan. This makes every topology change after preview a detected
stale state.

### 6.6 Permissions

Planning marks the unit blocked with a diagnostic when the operator lacks rights to delete the Logical
Cable, add cables, or view the involved devices and ports. Execution rechecks the same permissions
inside the transaction as the accepted plan's operator.

### 6.7 Planning diagnostics

| Condition | Diagnostic code | Disposition |
| --- | --- | --- |
| No unique exact name match for a Termination Reference | `cable.termination_unresolved` | `blocked` |
| The resolved object is not an Interface, FrontPort, or RearPort | `cable.unsupported_termination_kind` | `invalid` |
| PortMapping contradicts a Pass-Through Claim | `cable.pass_through_not_mapped` | `invalid` |
| Several mapped peers for a same-port continuation | `cable.ambiguous_mapped_peer` | `blocked` |
| A foreign Cable occupies a desired termination | `cable.termination_occupied` | `blocked` |
| A multi-termination Cable touches a desired port | `cable.multi_termination_conflict` | `blocked` |
| A CableClass dimension is unresolved | `cable.cableclass_unmapped` | `blocked` |
| A stored Cable Type or Cable Profile value is no longer offered by the running instance | `cable.cableclass_stale_mapping` | `blocked` |
| A stored Cable Profile is offered but is incompatible with one termination per side | `cable.profile_incompatible` | `blocked` |
| The operator lacks a required Cable or view permission | `cable.permission_denied` | `blocked` |
| A dangling natural-key reference in `adapter_config` | `profile.dangling_reference` | `blocked` |
| Attribute drift on a reused Cable | `cable.attribute_drift` | `info` diagnostic, disposition unchanged |

Diagnostic code strings are spec defaults; the conditions and dispositions are normative.

## 7. Resolution Proposal persistence, jobs, permissions, staleness, and acceptance

### 7.1 Binding

A Resolution Proposal binds to (Import Profile, task type, target-module-defined field key). Binding
is plan-independent, so a proposal survives preview close and replanning.

The first task type is `select_termination`. Its field key is the normalized termination identity
triple plus the claimed kind plus the role marker. Its canonical form is the JSON serialization of
those values, matching the trace identity rule in section 5.2:
`{"cards":<cards>,"device":<device>,"kind":<kind>,"port":<port>,"role":"termination"}`, with members
sorted by name (spec default for the member order). The stored key is this canonical JSON form. A
pipe-separated string is a display form only and is never a key.

| Role marker | Meaning | Written by |
| --- | --- | --- |
| `termination` | Resolve the Termination Reference to a NetBox termination | Manual selection or an accepted proposal |
| `mapped_peer` | Choose among the mapped peers for a same-port continuation (section 6.2) | Manual selection |

At most one active (non-terminal) proposal exists per bound key. A proposal can be requested only for
a currently-unresolved field. Proposals are requested for the `termination` role in this delivery.

### 7.2 States and the operator decision

The status enum has exactly five values, and it is immutable once terminal:

```text
queued -> running -> completed
queued -> cancelled
running -> cancelled
queued|running -> failed
terminal statuses: completed, failed, cancelled
```

All other transitions are forbidden. A retry is always a new proposal row.

Acceptance and rejection are not statuses. The operator decision is a separate one-shot set of fields
on the same row: `decision` (`accepted` or `rejected`), the deciding operator, the decision time, and
the link to the written `TerminationResolution`. The decision fields are null until an operator
decides, they can be set exactly once, and setting them never changes the status. A row whose status
is `completed` and whose `decision` is null is the only row an operator can accept or reject.

### 7.3 Immutable content

Frozen at request time: the source evidence for the bound field, the resolved Device object type and
id, the prompt version, the response schema version, the Candidate Snapshot, the requesting operator,
and the timestamps. A Candidate Snapshot entry carries an opaque candidate id, the NetBox object type
and id, and the display name at snapshot time.

The row is created when the request is made, so it is also the attempt record. It exists before any
Inference Backend call and survives every outcome.

Completion adds the outcome (`candidate` or `no_match`), the selected candidate reference, the
required explanation, backend metadata (model, request and response ids, finish reason, per-attempt
records, and which configuration source supplied the Inference Backend), and the raw response text for
diagnostics. Failure adds the typed failure reason and the raw response text when one was received.

Status `completed` requires a valid response object. Outcome `no_match` requires a valid JSON no-match
object with its own explanation, which the operator reads to understand why the evidence did not
distinguish the candidates. A refusal or an empty-content completion carries no such explanation, so it
is not `no_match`: it sets status `failed` with typed reason `backend_refusal` and retains the raw
response for diagnostics.

### 7.4 Staleness

Staleness is not a stored status. It is computed at read time by comparing the Candidate Snapshot and
the frozen resolved Device reference against current state. It is revalidated inside the acceptance
transaction. There are no change-signal listeners and no background sweeper.

| Trigger | Effect |
| --- | --- |
| The resolved Device differs from the frozen resolved Device reference | The proposal reads as stale |
| The eligible candidate set changed | The proposal reads as stale |
| An Import Profile change that affects resolution | Surfaces through the candidate comparison |
| The source value changed | A different field key, so the old proposal no longer applies |
| The plan changed | Irrelevant, because binding is plan-independent |

### 7.5 Jobs and failures

The job receives the Resolution Proposal id and the stable Inference Backend key, which is the
backend's unique name. It never receives a database id for the backend and never receives a secret.
The worker resolves the key itself: it reads the database row with that key first, and falls back to
the `inference_backend` plugin setting with the same `name` only when no database row exists. The
worker records which source it used in backend metadata, so an operator can tell a database-configured
run from a file-configured run.

The job issues one Inference Backend request at a time through the existing NetBox job system. Vault
credentials resolve in the worker.

Transient typed failures (rate limit, temporary backend failure, timeout, credential infrastructure
failure) retry automatically at most twice with backoff inside the same proposal, then fail.
Non-transient failures (backend authentication failure, invalid configuration, malformed response,
unknown candidate id, wrong finish reason, credential denial, invalid credential reference, invalid
secret material) fail immediately with the typed reason stored.

Operators can cancel a queued or running proposal. A late response to a cancelled or superseded
request is discarded and never overwrites the terminal row. Closing the preview changes nothing;
results appear in the next preview. After a failure, the operator re-requests manually.

### 7.6 Permissions

| Action | Requirement |
| --- | --- |
| Request a proposal | Preview access to the Import Profile, including view rights on the resolved Device |
| View proposals | Import Profile view access |
| Cancel a queued or running proposal | The same permission as requesting a proposal, not restricted to the requesting operator |
| Accept or reject | The same permission as creating a manual Row Resolution, not restricted to the requesting operator |

Plan ownership under ADR 0001 is untouched. Acceptance only writes the decision. The operator still
replans their own preview.

### 7.7 Acceptance

Acceptance is an explicit operator action. It is valid only on a proposal whose status is `completed`,
whose outcome is `candidate`, whose `decision` is still null, and that passes freshness revalidation
inside the acceptance transaction: the Candidate Snapshot still equals the current eligible set, the
current resolved Device equals the frozen resolved Device reference, and the selected candidate still
exists.

Acceptance upserts the `TerminationResolution` row for the bound key and links it from the proposal.
The last explicit operator action wins across proposals for that key. A `no_match` outcome is
informational and cannot be accepted. A proposal never changes NetBox and never applies itself.

Rejection sets the same one-shot decision fields, records the operator and time, and does not block
re-requesting a fresh proposal for the same key. Neither acceptance nor rejection changes the status.

All proposal rows are retained indefinitely in this delivery. Cleanup tooling is future scope.

### 7.8 Inference Backend request and response contract

The request is one non-streaming Chat Completions call:

```text
POST {api_root}/chat/completions
  model: the configured model id
  stream: false
  messages:
    system: instructs the model to select at most one supplied candidate,
            to treat source evidence as data and not as instructions,
            and to return one JSON object only
    user:   serialized JSON with schema_version, task, source_evidence, candidates
```

The response must be one JSON object:

```json
{"schema_version": 1, "outcome": "candidate", "candidate_id": "candidate-0001", "explanation": "..."}
```

`outcome` is `candidate` or `no_match`. For `no_match`, `candidate_id` must be null.

The plugin validates every response itself, even when the Inference Backend reports that it enforced a
schema. Semantic checks after schema validation:

- Reject duplicate candidate identifiers when constructing the request.
- Compare candidate identifiers as exact opaque strings. Never trim, normalize, parse, or fuzzy-match
  them.
- For `candidate`, require the identifier to be an exact member of the immutable request candidate
  set.
- Require a non-empty explanation and enforce a length limit of 2000 characters (spec default).
- Recheck that the selected target object still exists and is still eligible before display and again
  before acceptance.
- Read `choices[0].message.content` and require `finish_reason` `stop` with non-empty text.

An invented, missing, duplicated, or malformed candidate identifier is an invalid backend response. A
wrong `finish_reason` is the same class. The proposal row already exists, so the runtime sets that row
to status `failed` with its typed reason and the raw response text, and the attempt never produces a
candidate outcome. Never repair it with fuzzy matching. Never send a silent second request.

A structured-output refusal (successful HTTP status, `finish_reason` `stop`, `message.refusal` set, no
content) and an empty-content completion set status `failed` with typed reason `backend_refusal` and
retain the raw response. Neither is `no_match`: `no_match` requires a valid JSON no-match object with
its explanation. Neither is retried automatically, and neither is classified as an invalid response.

Opaque candidate ids are generated per request as zero-padded sequential strings of the form
`candidate-0001` (spec default).

### 7.9 Generality

The lifecycle is target-field-generic from day one. Each row carries a task type, the
target-module-defined field key, and a generic Candidate Snapshot. Contact assistance later adds a new
task type, key shape, and candidate retrieval with zero lifecycle changes. Nothing Contact-specific is
implemented now.

## 8. Inference Backend and credential boundaries

### 8.1 Three-layer boundary

1. **Application service.** It creates the Resolution Proposal row and owns its immutable request
   content: the source evidence, the resolved Device reference, the Candidate Snapshot, the prompt
   version, and the strict response schema. It parses the returned content, validates it against the
   request snapshot, and decides only the outcome recorded on the existing row.
2. **Backend adapter.** It converts one request into one non-streaming Chat Completions call. It owns
   authentication, HTTP transport, response-envelope parsing, timeout reporting, and backend error
   classification. It imports no NetBox, candidate, proposal, or job models.
3. **Asynchronous job service.** It owns state transitions, scheduling, retries, cancellation, and
   operator-visible progress.

The row-exists-first lifecycle decided in issue #82 supersedes the create-on-success phrasing of the
research note: the row is created at request time, so no layer ever decides whether a proposal can be
stored. The research note's "completed call without a proposal" maps to a failed Resolution Proposal
row with typed reason `backend_refusal`, not to an absent row.

```text
InferenceBackend.complete(InferenceRequest) -> InferenceCompletion
  raises: typed backend error (transport failure, timeout, authentication failure,
          rate limit, invalid configuration, malformed envelope)

InferenceRequest:  system_instruction, user_payload_json, requested_response_mode
InferenceCompletion: content_text (optional), is_refusal, finish_reason,
                     backend_request_id, backend_response_id, backend_model

  is_refusal is derived by the adapter: finish_reason `stop` with empty content,
  or with a refusal payload instead of content. Any other finish_reason, or a
  malformed envelope, raises a typed backend error rather than returning a completion.

CredentialBackend.resolve(reference) -> credential context
```

A returned `InferenceCompletion` therefore means the call reached the backend and the envelope parsed.
The application service then records `backend_refusal` when `is_refusal` is set, and otherwise
validates `content_text`.

### 8.2 Inference Backend configuration model

A plugin-level model holds named Inference Backend rows with at most one enabled row; the enabled row is the active backend. Its UI label is
"AI backend". Import Profiles do not select an Inference Backend in this delivery.

| Field | Requirement | Meaning |
| --- | --- | --- |
| Backend key | Required | The unique name of the Inference Backend, and the only identifier a job payload carries |
| Display name | Required | Operator-facing label |
| Adapter type | Required | `openai_compatible` in this delivery (spec default key) |
| `api_root` | Required | Exact API root without a trailing slash. The client appends `/chat/completions` or `/models`. It never adds `/v1`. |
| `model` | Required | Exact backend model id. The worker never chooses a model at run time. |
| `authentication` | Required | `bearer` in this delivery |
| `response_mode` | Required | `prompt_json` by default. `json_object` and `json_schema` are allowed only after an operator verifies the exact backend and model. |
| `credential_reference` | Required | A typed Vault KV v2 reference |
| `connect_timeout`, `read_timeout` | Required | Finite transport limits. Defaults: 5 seconds and 60 seconds (spec default). |
| `enabled` | Required | Whether Ask AI may use this row |

The active Inference Backend is the enabled database row. The `inference_backend` plugin setting is
the whole-backend fallback, and it acts as the active backend only when no enabled database row
exists. The two sources are never merged field by field. The worker resolves the active backend at run
time and records the source it used in backend metadata.

### 8.2.1 Plugin settings

All three settings live under the plugin's `PLUGINS_CONFIG` entry.

| Setting | Shape |
| --- | --- |
| `inference_backend_origin_allowlist` | A list of exact origin strings, each with scheme, host, and port, for example `https://backend.example.invalid:443`. No wildcards, no path component, no bare host. |
| `inference_backend` | The file-backed fallback: one mapping holding exactly the `InferenceBackend` row fields minus the backend key and `enabled`, namely `display_name`, `adapter_type`, `api_root`, `model`, `authentication`, `response_mode`, `credential_reference`, `connect_timeout`, and `read_timeout`. Its backend key is the fixed value `file-fallback`. |
| `vault` | A mapping with `address` (Vault Proxy or Vault server address), `auth_method`, `namespace` (optional, Vault Enterprise only), `ca_bundle` (optional path), `connect_timeout`, and `read_timeout`. |

`vault.auth_method` has exactly two permitted values:

| Value | Meaning |
| --- | --- |
| `proxy` | Default. The worker calls Vault through a Vault Proxy that holds an auto-auth token. The plugin stores and handles no Vault credential. |
| `token` | The worker reads a Vault token from a deployment environment variable. The token is never stored in the database, in a plugin setting value, or in any plugin-managed file. |

The `vault` mapping never contains a KV v2 mount. The mount belongs to the credential reference (section 8.5), whether that
reference lives on the Inference Backend row or in the file fallback, so one deployment can serve references on different mounts
without a settings change. It also never contains a token value, an AppRole RoleID, an AppRole
SecretID, or a TLS verification override.

**Validation split.** Configuration shape validates at application startup and fails fast: the setting
names, the field set of `inference_backend`, the `api_root` trust boundary (section 8.3), the
allowlist entry format, and the permitted `vault.auth_method` values. Credential resolution and
network liveness never run at startup; they fail at request time in the worker, with the typed reasons
in section 13.3. A malformed `inference_backend` mapping is an `invalid_configuration` failure, never
a silent fallback to a different backend.

### 8.3 `api_root` trust boundary

`api_root` names a destination the NetBox server itself calls. Validate it identically for a database
row and for an `inference_backend` setting value:

- Accept only a deployment-owned origin or an origin on a deployment allowlist. The allowlist is a
  plugin setting named `inference_backend_origin_allowlist` (spec default).
- Require `https` when `authentication` is `bearer`. Allow `http` only for an origin the allowlist
  marks as an approved local endpoint.
- Resolve the host and reject a private, link-local, loopback, or cloud metadata destination unless
  the allowlist approves that exact origin. Recheck after resolution.
- Disable redirects in the HTTP client, or revalidate every redirect target against the same rules
  before following it.

Model discovery is optional and is a configuration convenience only. The operator must always be able
to enter the exact model id. A failed model-list request does not prove that Chat Completions is
unavailable.

### 8.4 Rejected backend capabilities

The first adapter rejects: the Responses API, streaming, tools and tool calls, backend-side chat
persistence, files, retrieval, web search, memory, backend-specific functions, multiple choices,
multimodal input or output, automatic fallback between response modes, and automatic model selection.

### 8.5 Credential boundary

Inference Backend configuration stores only a credential reference. The reference has this shape:

```yaml
backend: vault_kv_v2
mount: <kv-mount-name>
path: <backend-secret-path>
field: <api-key-field>
```

The KV v2 mount lives here and only here. The reference never accepts a Vault server URL, namespace,
token, AppRole RoleID, AppRole SecretID, or TLS verification override. Connection and machine identity
data stay in the deployment-owned `vault` setting (section 8.2.1).

The credential backend seam has four responsibilities: validate a typed reference, resolve it through
the selected credential backend, return secret material for the lifetime of one outbound request, and
classify failures without including response bodies or credential values.

The Vault implementation supports KV v2 only, reads one named field from one configured path, rejects
a missing, empty, or non-string value, and omits the secret from all exception text. It sends no
Source Trace, device, contact, or operator data to Vault.

Vault Proxy auto-auth is the recommended deployment baseline. The worker calls the Vault API through
the Proxy without possessing the token. Direct Vault access is a valid deployment variant, but this
delivery does not implement direct AppRole login.

Resolution happens in the inference worker, once per outbound inference attempt. The plugin caches no
API-key value. Key rotation takes effect on the next resolution. An already running HTTP request
continues with the value it received. The system never silently replays a possibly accepted inference
request.

### 8.6 Secrets never persist

The resolved secret value never enters a model, form, serializer, profile YAML export, session, job
argument, Resolution Proposal, `ImportExecution` row, or log.

The typed credential reference is different from a secret value. It lives in exactly one authoritative
place: the enabled `InferenceBackend` row, or the `inference_backend` file fallback when no enabled
row exists. It is restricted configuration metadata, never copied elsewhere.

The inference job receives only the stable backend key and resolves the credential itself. The
request-handling process never resolves a secret and passes it to the worker.

Plugin audit and job state may record the backend key, the proposal job id, the credential backend
name, the resolution outcome category, and request start and completion times. They must not record
the secret value, the authorization header, a Vault token, a Vault response body, or inference request
headers. The Vault mount, path, and field are restricted configuration metadata.

Vault availability is never required at startup. The connection test runs as a worker Job, on the same
queue and the same secret boundary as a proposal job, so no web process ever resolves a credential. It
resolves the configured reference and returns a typed result: `ok`, `credential_unavailable`,
`credential_denied`, `invalid_credential_reference`, `invalid_secret_material`, or
`invalid_configuration`. It never returns a secret value or a Vault response body.

Running the connection test is authorized by the dedicated `InferenceBackend` object permission in
section 13.1. There is no separate administrator or superuser check.

## 9. Database changes and migration ownership

Migrations are generated artifacts. Change a model, then generate the migration with the project's
`makemigrations` helper. Never hand-edit a generated migration. The only exception is a data migration
whose operations cannot be generated, and that migration contains data operations only.

### 9.1 New models

| Model (spec default names) | Purpose | Key | Introduced by |
| --- | --- | --- | --- |
| `SourceDocument` | The stored uploaded workbook that `source_document` references | Content fingerprint indexed per Import Profile | T2 |
| `TerminationResolution` | The trace-side Row Resolution written by manual selection or proposal acceptance | (Import Profile, task type, field key) unique | T4 |
| `CableClassMapping` | Cable target policy for one CableClass value | (Import Profile, CableClass value) unique | T4 |
| `CableImportSource` | Provenance for one Cable and one contributing Source Trace | (Cable, Import Profile, trace identity) unique | T5 |
| `InferenceBackend` | One named Inference Backend definition, at most one row enabled | Backend key unique | T7 |
| `ResolutionProposal` | The Resolution Proposal request, attempt, and decision row | (Import Profile, task type, field key) with at most one active row | T8 |

`SourceDocument` stores the Import Profile, the uploaded workbook bytes, the content fingerprint, the
original file name, the uploading operator, and the creation time. Read access follows Import Profile
view permission, so a user who may not view the profile may not download its uploads.

Retention has two rules. A row referenced by an `ImportExecution` is permanent audit input and is
never deleted. An unreferenced row is deleted by housekeeping 30 days after its creation time (spec
default). A newer upload never deletes an older one, so two operators previewing the same profile at
the same time cannot delete each other's input.

An execution or a replan that references a deleted `SourceDocument` fails with the typed
stale-document error (section 2.1) and requires a fresh upload. That error is the complete recovery
contract; the runtime adds no other concurrent-preview machinery.

`TerminationResolution` is a Row Resolution in glossary terms. It stores the Import Profile, the task
type, the canonical JSON field key (which carries the role marker, section 7.1), the selected object
type, the selected object id, and the display name at selection time. The three value columns hold the
selection for both the `termination` and `mapped_peer` roles. `SourceResolution` keeps its flat
`(profile, source_id, source_column)` shape and stays flat-adapter-specific.

`CableImportSource` records the Import Profile, the Cable (a plain foreign key), the trace identity,
the segment index, the original From/To text and direction, the workbook provenance (fingerprint,
sheet, block ordinal, row range), and the source export timestamp. One Cable created for a shared
identical segment carries one row per contributing Source Trace (section 5.7).

`ResolutionProposal` stores the immutable request content (including the resolved Device object type
and id), the Candidate Snapshot, the five-value status, the completion content, the backend metadata
and per-attempt records, the raw response text, the typed failure reason, and the one-shot decision
fields (`decision`, deciding operator, decision time, and the `TerminationResolution` link). It stores
no secret value.

### 9.2 Changed models

**ImportProfile (ADR 0003 cutover).** The cutover is one ordered migration sequence in one release:

1. A generated schema migration that adds `source_adapter` and `adapter_config`.
2. A data migration that contains data operations only. It stamps the `flat_workbook` adapter key on
   every existing profile and copies the device-format columns into `adapter_config`. It rewrites
   `primary_contact_role` from a foreign key to a natural-key reference inside `adapter_config`.
3. A generated schema migration that drops the moved columns.

The moved columns are: `sheet_name`, `source_id_column`, `custom_field_name`, `update_existing`,
`create_missing_device_types`, `capture_extra_data`, `primary_contact_role`,
`primary_contact_lookup_field`, and `preview_view_mode`. Dropping `primary_contact_role` removes the
database-level PROTECT relationship. Forms, REST, and YAML switch to the new shape in the same
release. There is no compatibility path between the three steps and no dual read path after them.

**`ImportExecution` replaces `ImportJob`.** `ImportJob` carries `dry_run` and presentation result rows,
which ADR 0001 removes. `ImportExecution` is the audit record named by ADR 0001, and it carries:

| Field | Rule |
| --- | --- |
| Import Profile | Required on a new row |
| `SourceDocument` | The stored upload the execution planned from, required on a new row |
| Actor | The operator who owns the accepted plan, required on a new row |
| Idempotency key | Required on a new row, unique together with the Import Profile |
| Plan schema version | Required on a new row |
| Accepted plan fingerprint | Required on a new row |
| Selected Synchronization Unit identities | Required on a new row |
| Outcome | Required on a new row: `pending`, `succeeded`, or `failed` |
| Applied changes | Set when the transaction commits: the applied Planned Change identities, and for every deleted object an identity snapshot |
| Failure detail | Set only on a failed row: the failed Planned Change identity, the rolled-back change identities, and the dependent change identities that were not attempted |
| Native NetBox Job link | A one-to-one relation from the native Job to the `ImportExecution` row, set on the `pending` row for a background execution, null when an execution ran synchronously |

**Reservation order.** An execution request inserts the row with outcome `pending` and commits that
insert first. The insert reserves the unique (Import Profile, idempotency key), so a duplicate request
loses the race and returns the existing row whatever its outcome, including while it is still
`pending`. The target transaction opens only after the reservation commits. When it commits, the same
transaction sets the outcome to `succeeded` and writes the applied-changes field, so the audit result
still commits atomically with its NetBox changes. When it rolls back, the row becomes `failed` with
its failure detail.

**Pending-row recovery.** The `pending` row records whether the execution is job-backed and, when it
is, its native Job reference, so a crash cannot leave a permanently pending row. At the next read, a
job-backed `pending` row whose linked Job is terminal or missing transitions to `failed` with typed
reason `abandoned`. A synchronous `pending` row (no Job) transitions the same way when it is older
than the web request bound, 10 minutes (spec default), so a read during a live synchronous execution
never marks it failed. This needs no sweeper. Duplicate delivery of the same idempotency key returns the existing row in any outcome,
including a failed abandoned one, so a redelivered job never re-runs the writes. A new operator
submission mints a new idempotency key, so an abandoned attempt consumes no key permanently and the
operator can always resubmit.

**Deleted-object snapshot.** For each deleted object the applied-changes field records the object
type, the database id, the display value, and, for a Cable, its termination set at deletion time. This
is where a removed Logical Cable is recorded, which satisfies the audit requirement stated in sections
5.7 and 10.7. Provenance rows never record a deletion.

Every new field is nullable at the database level so retained `ImportJob` rows can keep their
historical columns. The cutover drops `dry_run` and the stored result rows, backfills nothing, and
marks every retained legacy row display-only: a legacy row has null new fields, never satisfies an
idempotency lookup, and is never used for plan comparison. The unique constraint on (Import Profile,
idempotency key) is partial, so it ignores rows with a null idempotency key (spec default for the
constraint form; ADR 0001 fixes the idempotency requirement).

### 9.3 Unchanged models

`ColumnMapping`, `ColumnTransformRule`, `ClassRoleMapping`, `DeviceTypeMapping`,
`ManufacturerMapping`, `IgnoredDevice`, `SourceResolution`, `DeviceExistingMatch`,
`IgnoredFieldDifference`, and the per-Device provenance model keep their shape. Each gains an
applicability tag derived from the catalog rather than a new column.

## 10. NetBox UI, REST, GraphQL, YAML, job, and audit impacts

### 10.1 Import Profile UI

The profile form asks for the Source Adapter first. The adapter selection is disabled after creation.
The form then renders the adapter-declared configuration form and only the policy sections the catalog
marks applicable. The CableClass mapping section appears only for a trace-adapter profile, and each
dimension offers the running instance's Cable Type and Cable Profile choices plus an explicit none
option.

### 10.2 Review Workspace

The workspace is one page per preview. Layout:

- **Summary strip**: unit dispositions, terminations resolved count, active proposals, saved
  decisions, and preview state.
- **Trace list** with a disposition badge per Source Trace.
- **Three panels** for the selected trace: source evidence (From and To plus the ordered Segment
  Evidence with implied Pass-Through Claims), current NetBox topology, and proposed physical topology
  with a per-segment status of create, reuse existing, delete Logical Cable, or conflict.

Termination field states render as badges: `unresolved`, `automatically resolved`, `manually
resolved`, `proposed`, `accepted`, `stale`, `failed`. The `automatically resolved` state is distinct
from `manually resolved`, so the operator can see which terminations the exact-name rule matched
without help.

The searchable picker is scoped to eligible candidates of the claimed kind on the resolved Device and
shows a visible "N of M eligible" count.

Proposal card contract:

| Card state | Contents |
| --- | --- |
| Completed with a candidate | A "Proposal - not applied" badge, the suggested candidate with its kind, the required explanation, backend metadata with attempt count, and explicit Accept and Reject buttons |
| Completed and stale | The same card with a "Proposal - stale, not applied" badge and a disabled Accept action showing its reason |
| Completed with no match | The backend's own explanation of why the evidence did not distinguish the candidates, and no accept action |
| Failed | The typed failure reason, including `backend_refusal` for a refusal or an empty-content completion, and an Ask AI again action that creates a new proposal |
| Queued or running | Live progress refreshed in place, with its own cancel action |

A pending card polls its own state every 3 seconds and stops on a terminal state (spec default). The
operator never leaves the field to learn what the Inference Backend is doing.

Every action is always visible. An illegal action renders disabled with its reason underneath.

A per-field proposal history list shows every attempt, its status, and its outcome.

A drift warning strip appears when live NetBox differs from the reviewed snapshot, with a re-read
action. The workspace compares the reviewed plan fingerprint with a freshly computed plan fingerprint
on each full workspace load and on the explicit re-read action. It does not poll for drift
(spec default).

Visual theming adopts NetBox theme variables at implementation time. The prototype palette from #83 is
not final.

### 10.3 REST

- `ImportProfile` gains `source_adapter` (read-only after creation) and `adapter_config`. The
  device-format fields are removed.
- Serializer choices, validation, and the writable field set for Target Fields derive from the
  target-field catalog. An endpoint that exposes no Target Field validates against its own form schema
  instead, which is how the Inference Backend endpoint works.
- New read-write endpoints: CableClass mapping and Inference Backend.
- New read-only endpoints: Resolution Proposal and per-Cable provenance.
- The Inference Backend serializer exposes the credential reference as write-only, and never exposes
  a secret value (spec default, consistent with the Vault research).
- Requesting, accepting, and rejecting a Resolution Proposal happen through Review Workspace
  endpoints, not through REST (spec default).

### 10.4 GraphQL

The `ImportProfile` type gains `source_adapter` and `adapter_config` and loses the device-format
fields. A CableClass mapping type is added. Import Plans, Import Executions, Resolution Proposals, and
Inference Backends are not exposed in GraphQL (spec default: they are operator workflow state, not
queryable configuration).

### 10.5 YAML profile export and import

Profile YAML serialization derives from the target-field catalog. The document carries the adapter
key, `adapter_config` with natural-key object references, and only the policy sections applicable to
that adapter, including CableClass mappings. The per-section hand-rolled handling is removed. An
Inference Backend is never part of profile YAML.

### 10.6 Jobs

Three job types run through the NetBox job system:

| Job | Input | Output |
| --- | --- | --- |
| Import execution | Import Profile id, `source_document`, accepted serialized plan, selection, idempotency key, actor | An `ImportExecution` row linked one-to-one from the native NetBox Job |
| Inference proposal | Resolution Proposal id and the stable Inference Backend key | A completed, failed, or cancelled Resolution Proposal |
| Inference Backend connection test | The stable Inference Backend key | A typed result category, never a secret value or a Vault response body |

No job receives a secret value or a database id for an Inference Backend. Import execution progress
counts Synchronization Units and Planned Changes.

### 10.7 Audit

| Record | Written by |
| --- | --- |
| `ImportExecution` | Every selective and final execution, committed atomically on success |
| Per-Device provenance row | The Device Target Module |
| Per-Cable provenance row | The Cable Target Module, one row per (Cable, Import Profile, trace identity) |
| NetBox changelog entries | NetBox, for every `NetBoxModel` write |
| Resolution Proposal row | Created by the request, completed or failed by the inference job, decided once by an operator |

The Logical Cable removal is recorded only in the `ImportExecution` deleted-object snapshot
(section 9.2), which stores the Cable's object type, id, display value, and termination set at
deletion time.

## 11. Cutover plan

The cutover is one completed change per ticket boundary. No compatibility facade and no second runtime
path survives it.

### 11.1 Removed

| Removed | Replacement |
| --- | --- |
| Fixed Device and Rack pass order | Target Modules resolved through the catalog and the dependency graph |
| `RowResult` and presentation result rows | Synchronization Units, Planned Changes, dispositions, and diagnostics |
| `ImportJob`, with its stored result rows and `dry_run` flag | The `ImportExecution` audit record; existing `ImportJob` rows are retained display-only |
| `run_import(..., dry_run=...)` | `ImportEngine.plan` and `ImportEngine.execute` |
| View-owned safety helpers and safety-intent calculation | Coordinator-owned preconditions, plan comparison, and transactions |
| Per-format Import Profile columns | `source_adapter` and `adapter_config` |
| Hand-rolled per-section YAML handling | Catalog-derived serialization |
| Session dictionaries as the execution-safety contract | The serialized Import Plan with its schema version and fingerprints |
| Calls from views and jobs into private engine behavior | The public Import Engine interface |

### 11.2 Preserved

- The outer import workflow: upload, preview, review, selective synchronization, final
  synchronization, results.
- The NetBox Job adapter and the background execution path.
- User-visible Device and Rack behavior, including preview view modes, review actions, and saved
  decisions.
- The existing explicit Device matching approach and per-Device provenance.
- Import Profile vocabulary that CONTEXT.md already defines.

### 11.3 Rules

- A cutover ticket deletes the old path in the same change. It leaves no dual code path, no
  commented-out code, and no "remove later" marker without a tracked follow-up.
- Tests for obsolete private helpers are deleted after the behavior is covered through the new
  interface.
- No migration keeps a dual read path. The profile move ships as one ordered migration sequence in one
  release (section 9.2), and no code reads the old columns after that release.

## 12. End-to-end and integration test strategy

### 12.1 Value order

1. **End-to-end first.** Drive the real HTTP request through the real view, the real database, and the
   real response. Cover upload, preview, review commands, selective synchronization, final
   synchronization, and the results page. Assert real database outcomes, not intermediate structures.
2. **Integration second.** Exercise the Import Engine and Target Modules against the real NetBox ORM,
   real forms, and real serializers. Use real Cable, Interface, FrontPort, RearPort, and PortMapping
   objects.
3. **Narrow unit last.** Reserve unit tests for pure functions such as normalization, canonical
   orientation, and fingerprinting.

### 12.2 Mocks

Mocks are allowed at exactly two external boundaries: the Inference Backend HTTP call and Vault.
Everything else uses real objects. Prefer a local fake or a recorded fixture over a bare mock even at
those two boundaries. Never mock the ORM, a serializer, a form, a Target Module, the Import Engine, or
the Source Adapter.

### 12.3 Source Adapter corpus

The committed redacted trace fixtures are the trace adapter's test corpus. The expected results in
section 5.8 are assertions, not documentation. The flat adapter keeps its existing workbook fixture.

Fixture redaction is part of the corpus contract: a redaction verifier confirms that no original
device, host, or site identifier remains.

### 12.4 New-seam rule

Test a new seam through `ImportEngine.plan`, `ImportEngine.execute`, and the HTTP surface. Do not test
a private helper. If a behavior is only reachable through a private helper, the seam is wrong and the
design needs revisiting before the test is written.

### 12.5 Execution conventions

Run Django tests inside the repository development container. Use a unique PostgreSQL database name
that starts with `test_` for each task, and a dedicated Redis sidecar for each task. NetBox's
`RQQueueTestMixin` calls Redis `FLUSHALL`, so a database number on a shared Redis service does not
isolate one task from another. The test helpers size the worker pool to the machine and cap it at
eight workers; each worker gets a private PostgreSQL database and private Redis task and cache
databases. Front-end tests run with the JavaScript unit runner, and browser tests run with the browser
runner.

### 12.6 Required coverage per area

| Area | Required tests |
| --- | --- |
| Catalog and registry | A profile rejects an inapplicable policy row; every surface derives its choices from the catalog |
| Profile cutover | The migration moves each column into `adapter_config` and drops it; YAML round-trips through a fresh instance |
| Import Engine | Deterministic identities and fingerprints; cycle rejection; identical-identity sharing; conflicting-identity invalidation |
| Selective execution | An unrelated unit change does not block a safe selection; a source or profile change invalidates every selection |
| Transactions | A precondition mismatch rolls back the complete selected transaction and marks the attempt failed |
| Idempotency | A duplicate submission returns the existing Import Execution |
| Pending-row recovery | A `pending` row whose linked Job is terminal or missing reads as `failed` with reason `abandoned`; a redelivery returns that row; a new submission with a new key succeeds |
| Source document retention | A referenced document survives housekeeping; an unreferenced one older than 30 days is deleted; a plan or execution against a deleted document raises the stale-source-document error |
| Trace adapter | The fixture expectations in section 5.8 |
| Cable module | No-op, creation-only, partial reuse, same-port continuation, ambiguous peer, occupied termination, unmapped CableClass, permission block |
| Patched Path Replacement end to end | One HTTP execution deletes the Logical Cable, creates every segment, and writes the expected provenance rows, all asserted against the real database |
| Shared-segment provenance | Two Source Traces claiming one identical segment create one Cable and two `CableImportSource` rows |
| Segment precedence | A single-segment trace with a matching direct Cable is a no-op and deletes nothing |
| Review Workspace | Every disabled action renders its reason; the drift strip appears when live state moved |
| Proposal lifecycle | Every forbidden transition is rejected; staleness blocks acceptance inside the transaction; a `no_match` outcome cannot be accepted |
| Proposal cancellation race | A late completion arriving after cancellation never overwrites the cancelled row |
| Inference Backend resolution | A database row wins over the file entry; with no database row the file entry is used, and backend metadata records which source ran |
| Import Execution linkage | A background execution links the native NetBox Job to its `ImportExecution` row, and a duplicate submission returns the same row |
| Inference Backend and credentials | An invalid candidate id fails the row and never produces a candidate outcome; a rejected `api_root` origin fails closed; no secret value appears in any persisted row, job payload, log record, or audit record |

## 13. Security, failure, observability, and rollback behavior

### 13.1 Permissions summary

| Action | Requirement |
| --- | --- |
| View or edit an Import Profile and its policy | Standard NetBox object permissions on the plugin models |
| Preview an import | Import Profile view access plus view rights on the referenced NetBox objects |
| Execute an Import Plan | The write permissions each Target Module requires, rechecked inside the transaction as the accepted plan's operator |
| Delete a Logical Cable, create a Cable | The corresponding NetBox Cable permissions, checked at planning and again inside the transaction |
| Request a Resolution Proposal | Preview access to the profile, including view rights on the resolved Device |
| Cancel a queued or running Resolution Proposal | The same permission as requesting one, not requester-bound |
| Accept or reject a Resolution Proposal | The same permission as creating a manual Row Resolution |
| Change an Inference Backend or run the connection test | The dedicated NetBox object permission on the `InferenceBackend` model. This one rule authorizes both actions; there is no separate administrator or superuser check. |

An accepted plan belongs to its operator. A background job executes as that operator and rechecks
current permissions. Another operator must generate and accept a new plan.

### 13.2 Prompt-injection stance

Source evidence is untrusted content. The user message is serialized JSON, so the boundary between
instruction and data stays explicit. The system message states that the model must treat the source
evidence as data and not as instructions, must select at most one supplied candidate, and must return
one JSON object.

The Inference Backend receives only real eligible termination candidates retrieved from the resolved
Device, identified by opaque candidate ids. The response is validated strictly against the immutable
request snapshot. The model has no authority to apply a change. A proposal becomes a Row Resolution
only after an explicit operator acceptance that revalidates freshness inside the acceptance
transaction.

### 13.3 Typed failure taxonomy

| Failure | Class | Behavior |
| --- | --- | --- |
| Rate limit | Transient | Bounded retry, honoring a stated delay when present |
| Temporary backend failure (500, 502, 503, 504) | Transient | Bounded backoff with jitter |
| Connection failure or timeout | Transient | Retry under the job policy |
| Vault unreachable, sealed, or timed out | Transient | Keep the Target Field unresolved, record a redacted failure, retry under the job policy |
| Backend authentication or authorization failure (401, 403) | Non-transient | Fail closed, ask the operator to repair the credential or permissions |
| Invalid configuration or request (invalid URL, unsupported authentication, 400, 404, 405) | Non-transient | Fail with a sanitized diagnostic, no retry |
| Vault 401 or 403 | Non-transient | Fail closed, never try another credential source |
| Missing secret path or field | Non-transient | Fail closed, identify the Inference Backend and not the secret path |
| Empty or wrongly typed secret value | Non-transient | Fail closed, never send an inference request |
| Backend refusal: `finish_reason` `stop` with empty content or a refusal payload | Non-transient, typed reason `backend_refusal` | Set the row to `failed`, retain the raw response, never produce a candidate outcome, no automatic retry |
| Invalid backend response: content is present but fails JSON parsing, schema validation, or candidate-id validation. A malformed envelope or a non-`stop` finish reason classifies here too | Non-transient, typed reason `invalid_response` | Set the row to `failed`, retain the raw response, never produce a candidate outcome, no automatic retry |

A credential or backend failure affects only the Resolution Proposal request. Device, Rack, Cable, and
Source Trace preview and import workflows stay available.

### 13.4 Audit records

Section 10.7 lists the audit records. Every selective and final execution writes an Import Execution
record. On success it commits atomically with its NetBox changes. On failure, NetBox changes roll back
before the attempt is marked failed, and the record identifies the failed Planned Change, the
rolled-back changes, and the dependent changes that were not attempted.

### 13.5 Rollback summary

One selective execution and one final execution each use one transaction, and any failure rolls back
the complete selected transaction. Inside one Source Trace unit, the Logical Cable deletion and the
segment creations commit together or not at all. A precondition mismatch rolls back and requires a
replan. A permission failure inside the transaction rolls back and produces a blocked unit on the next
plan.

### 13.6 Observability

Progress reporting counts selected Synchronization Units and Planned Changes for an execution, and
proposal state plus attempt count for an inference job. Logs never contain a secret value, an
authorization header, a Vault token, a Vault response body, or inference request headers. A backend
error body is diagnostic data and is stored redacted on the proposal row, not in application logs.

## 14. Sequenced implementation tickets

Every ticket is independently mergeable with the complete test suite passing. Every ticket deletes the
path it replaces in the same change.

### T1. Target-field catalog, adapter registry, and Import Profile cutover

**Scope.** Add the static Source Adapter registry, the target-field catalog, and the output-kind and
consumption declarations. Add `ImportProfile.source_adapter` (immutable after creation) and
`adapter_config` with an adapter-declared validation form. Run the ordered migration sequence from
section 9.2 that stamps the `flat_workbook` key, moves the device-format columns into
`adapter_config`, converts the primary contact role to a natural-key reference, and drops the columns.
Add the key-family mechanism with the Device Target Module's `extra_json` family. Move every existing
surface onto the catalog: `ColumnMapping` and `ColumnTransformRule` validation, profile forms, REST,
GraphQL, and YAML serialization. Add the policy applicability classification and reject inapplicable
rows. T1 owns the catalog cutover for surfaces that exist today; the surfaces introduced by T4, T5,
T7, and T8 are T10's scope.

**Acceptance criteria.**

- The registry is the only source of adapter choices in forms, REST, GraphQL, and YAML.
- A profile rejects a change to `source_adapter` after creation.
- The adapter-declared form rejects an unknown `adapter_config` key and an invalid value.
- The migration sequence runs as three ordered steps in one release, moves every listed column into
  `adapter_config` for every existing profile, and drops the columns; the data step contains data
  operations only; no dual read path remains.
- `ColumnMapping` and `ColumnTransformRule` accept exactly the values they accept today, resolved
  through the catalog, including a non-empty `extra_json:` name and the candidate-target exclusion.
- A dangling natural-key reference produces a validation error at the form boundary.
- YAML export from one instance imports into a fresh instance without an instance-local id.
- No surface keeps a local target-field list.
- The change is independently mergeable with all tests passing.

**Blocked by.** None.

### T2. Import Engine core with Device and Rack Target Modules

**Scope.** Implement the Import Plan model, dispositions, diagnostics, identities, fingerprints, the
merged dependency graph, semantic plan comparison, selective and final execution, the one outer
transaction per execution request, idempotency, progress, and the `ImportExecution` audit record with
its native Job link, pending reservation, and deleted-object snapshot. Add the `SourceDocument` model
with its permission and retention rules. Move existing Device and Rack behavior behind Target Modules
and move flat workbook parsing behind the `flat_workbook` Source Adapter in the same change. Remove
the fixed passes, `RowResult`, the `dry_run` flag, view-owned safety helpers, and every call from a
view or job into private engine behavior.

**Acceptance criteria.**

- Views and jobs call only `ImportEngine.plan`, `ImportEngine.execute`, and Review Workspace commands.
- Planning is deterministic: the same source, profile, actor, planning context, and NetBox state
  produce the same identities, ordering, and fingerprints.
- The coordinator rejects a missing dependency reference and a cycle.
- Identical Planned Change identities share and execute once; the same identity with different
  payloads makes the plan invalid.
- A change in an unrelated unit does not block a safe selection; a source, profile, actor, or
  planning-context change invalidates every selection.
- A precondition mismatch inside the transaction rolls back the complete selected transaction and
  records a failure that identifies the failed change.
- One outer transaction covers one execution request; no unit commits independently.
- The request inserts a `pending` `ImportExecution` row and commits it before the target transaction
  opens; a duplicate submission returns the existing row in any outcome, including `pending`.
- The applied-changes field records every applied Planned Change identity, and every deleted object
  with its type, id, display, and, for a Cable, its terminations.
- A background execution links the native NetBox Job to its `ImportExecution` row.
- A `SourceDocument` referenced by an `ImportExecution` is never deleted; an unreferenced one is
  deleted by housekeeping 30 days after creation and never sooner; reading one requires Import
  Profile view permission.
- A retained `ImportJob` row keeps its historical fields, has null new fields, and never satisfies an
  idempotency lookup.
- A serialized plan with an incompatible schema version fails before any write.
- User-visible Device and Rack behavior is unchanged, proven end-to-end through HTTP.
- `RowResult`, the fixed passes, the `dry_run` flag, and the view-owned safety helpers are deleted.
- The change is independently mergeable with all tests passing.

**Blocked by.** T1.

### T3. Trace Source Adapter

**Scope.** Implement the `trace_workbook` Source Adapter: block parsing, sheet pairing, corroboration,
canonical orientation, the canonical JSON trace identity, the content fingerprint, duplicate collapse,
the Endpoint Summary fallback, the Source Adapter rows of the section 5.6 taxonomy, and provenance
capture. Emit Source Traces as a typed output kind with source diagnostics. The adapter class and its
corpus tests ship in this ticket, but the `trace_workbook` key is not registered as a selectable
profile choice yet: an operator cannot create a trace profile until the Cable Target Module exists.
T5 registers the key.

**Acceptance criteria.**

- The adapter never queries NetBox, and no test grants it database access to NetBox target models.
- The adapter emits every Source Adapter row of the section 5.6 taxonomy, including
  `trace.unknown_port_class` for a PortClass value outside its fixed vocabulary, and it detects that
  row with no NetBox access.
- The adapter never emits `cable.unsupported_termination_kind`, `trace.device_unresolved`, or
  `trace.endpoint_evidence_only`, because all three depend on NetBox state.
- The `trace_workbook` key is absent from the profile form, REST creation, GraphQL, and YAML adapter
  choices after this ticket merges. A REST update keeps the whole registry, so a client that reads a
  stored profile the release cannot run can write it back unchanged.
- The copper fixture produces 10 Source Traces, all valid with 3 Segment Evidence entries each and
  zero duplicate conflicts.
- The fiber fixture produces 10 Source Traces, 8 valid with 4 to 9 segments and 4 ending at a rear
  port, exactly one `trace.non_linear_path` and exactly one `trace.pass_through_at_interface`, and
  zero duplicate conflicts.
- Cross-trace checks report zero shared terminations and zero CableClass conflicts on both fixtures.
- A reversed re-statement of a trace collapses into the same Source Trace and leaves the content
  fingerprint unchanged.
- Two labels that differ only in separator characters produce different canonical JSON identities.
- A workbook with neither recognized sheet produces a batch-level error and no units.
- An invalid trace does not stop the rest of the batch.
- The change is independently mergeable with all tests passing.

**Blocked by.** T1.

### T4. Trace decision models and policy

**Scope.** Add the two trace-side persistence models and their operator surfaces, so the Cable planner
in T5 has decisions and policy to read. Add `TerminationResolution` with both field-key roles
(`termination` and `mapped_peer`), the canonical JSON key, and the three value columns. Add the
manual-selection persistence path that writes those rows and requests a replan through
`ImportEngine.plan`, which is why this ticket needs the engine seam from T2. Add `CableClassMapping`
with its two tri-state dimensions, the runtime-choice validation, the single-and-duplex Cable Profile
restriction, its profile UI section, and its registration as Cable target policy in the catalog. This
ticket writes and validates decisions; it plans nothing.

**Acceptance criteria.**

- A `TerminationResolution` row stores the canonical JSON key with its role marker, the selected
  object type, the selected object id, and the display name at selection time.
- A `termination` row and a `mapped_peer` row for the same termination coexist without overwriting.
- The manual-selection path writes the row through its owning model and requests a replan through
  `ImportEngine.plan`; it never edits an Import Plan.
- The `CableClassMapping` form offers only Cable Type and Cable Profile values the running instance
  reports, and only Cable Profiles compatible with one termination per side.
- Validation distinguishes `cable.cableclass_stale_mapping` from `cable.profile_incompatible`.
- The CableClass mapping section appears only on a trace-adapter profile and is rejected on any other
  profile.
- The change is independently mergeable with all tests passing.

**Blocked by.** T1, T2, T3.

### T5. Cable Target Module with Patched Path Replacement

**Scope.** Implement the Cable Target Module: port resolution by claimed kind and unique exact port
name, eligible-candidate retrieval for the picker and for proposal requests, the Cable Target Module
rows of the section 5.6 taxonomy, PortMapping verification, same-port continuation, direction-
independent comparison, segment precedence, the no-op rule, Logical Cable identification and deletion,
segment creation policy, preconditions, permission checks, execution, and `CableImportSource`
provenance rows. Read the decisions and policy that T4 persists. Register the `trace_workbook` adapter
key as a selectable profile choice. Raise and document the NetBox 4.6 floor for the trace feature.
Until T10 lands, a trace profile is configurable through the UI only.

**Acceptance criteria.**

- The `trace_workbook` key becomes selectable in the profile form.
- A single-segment trace with a matching direct Cable plans as a `no-op`; that Cable is treated as a
  proven physical segment, is never classified as the Logical Cable, and is not deleted.
- A trace whose complete physical path already exists plans as a `no-op`.
- A trace with patching plans one unit that deletes the Logical Cable first and then creates each
  segment in canonical order, with each creation depending on the deletion.
- A trace with no direct Cable plans as a creation-only replacement without a deletion change.
- A unique mapped peer substitutes automatically and the substitution appears in the plan; several
  mapped peers block the unit and offer exactly the mapped peers for manual selection.
- A stored `TerminationResolution` row for either role is reused on a replan.
- A resolved object that is not an Interface, FrontPort, or RearPort makes the unit `invalid` with
  `cable.unsupported_termination_kind`.
- A contradicted Pass-Through Claim makes the unit `invalid` and names both ports and the actual
  mappings.
- A foreign cable on a desired termination blocks the unit; no third-party cable is ever deleted or
  edited.
- An unresolved CableClass dimension, a stale mapping, and an incompatible Cable Profile each block
  the unit with their own diagnostic.
- Eligible-candidate retrieval returns only terminations of the claimed kind on the resolved Device,
  scoped to the actor's view permission.
- A precondition mismatch inside the transaction rolls back the complete selected transaction.
- Every created Cable gets one provenance row per contributing Source Trace; two traces sharing one
  identical segment create one Cable and two rows. The deleted Logical Cable appears only in the
  `ImportExecution` deleted-object snapshot.
- CablePath is never written.
- The change is independently mergeable with all tests passing.

**Blocked by.** T2, T4.

### T6. Trace Review Workspace UI

**Scope.** Build the review workspace: summary strip, trace list with disposition badges, the three
per-trace panels, termination field state badges, the searchable candidate picker with its eligible
count, the `Sync with dependencies` selection, the drift warning strip with its re-read action, and
the disabled-with-reason pattern for every action. Consume the `TerminationResolution` write path from
T4 and the eligible-candidate retrieval from T5. Wire review commands through the Review Workspace
module so they persist decisions and request a new plan. Adopt NetBox theme variables.

**Acceptance criteria.**

- Every action is always visible; an illegal action renders disabled with its reason.
- The proposed panel shows a per-segment status of create, reuse existing, delete Logical Cable, or
  conflict.
- The picker lists only eligible candidates of the claimed kind on the resolved Device and shows the
  "N of M eligible" count.
- Selecting a candidate writes a `TerminationResolution` row through its owning model and triggers a
  replan; no review command edits an Import Plan.
- A termination matched by the exact-name rule shows `automatically resolved`; one selected by an
  operator shows `manually resolved`.
- The drift strip appears when the freshly computed plan fingerprint differs from the reviewed one,
  and the re-read action clears it.
- Synchronizing a blocked trace is disabled with its reason.
- The workspace uses NetBox theme variables in both light and dark themes.
- The change is independently mergeable with all tests passing.

**Blocked by.** T5.

### T7. Inference Backend configuration and credential boundary

**Scope.** Add the `InferenceBackend` model with at most one enabled row and the `inference_backend`
file fallback. Implement `api_root` validation with the origin allowlist, scheme rules, host resolution
checks, and redirect handling. Implement the OpenAI-compatible backend adapter for non-streaming Chat
Completions with typed error classification. Implement the credential backend seam and its Vault KV v2
implementation. Add the three plugin settings with the shapes in section 8.2.1, including the two
permitted `vault.auth_method` values. Add the connection test as a worker Job, authorized by the
dedicated `InferenceBackend` object permission in section 13.1.

**Acceptance criteria.**

- The enabled database row is the active backend; the `inference_backend` setting under the fixed key
  `file-fallback` is active only when no enabled database row exists; the two sources are never merged
  field by field.
- A malformed `inference_backend` mapping fails as `invalid_configuration` and never silently selects
  a different backend.
- The `vault` mapping rejects a `mount` key and any `auth_method` other than `proxy` or `token`; the
  KV v2 mount is read from the credential reference.
- The resolver reports which source it used, and that value reaches backend metadata.
- An `api_root` outside the allowlist, or one resolving to a private, link-local, loopback, or
  metadata address without explicit approval, is rejected at the form boundary and at request time.
- A redirect to a disallowed target is not followed.
- The backend adapter imports no NetBox, candidate, proposal, or job model.
- Each documented backend condition maps to its typed failure class.
- The Vault backend reads one field from one path, rejects a missing, empty, or non-string value, and
  omits the secret from every exception message.
- An allowlist entry without a scheme, host, and port is rejected at startup; startup never contacts
  Vault or the backend.
- No test can find a secret value in any model row, serializer output, YAML export, session, job
  payload, audit record, or log record. The typed credential reference exists in exactly one
  authoritative place.
- The connection test runs as a worker Job and returns one of the typed result categories, never a
  secret value and never a Vault response body.
- A user holding the dedicated `InferenceBackend` object permission can run the connection test, and a
  user without it cannot; no separate administrator or superuser check exists.
- The change is independently mergeable with all tests passing.

**Blocked by.** T1.

### T8. Resolution Proposal model, job, and acceptance

**Scope.** Add the `ResolutionProposal` model: the immutable request content (including the resolved
Device object type and id), the Candidate Snapshot, the five-value status enum, the completion
content, the typed failure reason, and the one-shot decision fields. Add the inference job with
bounded transient retries, cancellation, and typed failure storage. Use the eligible-candidate
retrieval from T5 to build the Candidate Snapshot. Implement read-time staleness, the strict response
validator, and the acceptance transaction that revalidates freshness and upserts the
`TerminationResolution` row that T4 delivers.

**Acceptance criteria.**

- A proposal binds to (Import Profile, task type, canonical JSON field key) and survives preview close
  and replanning.
- At most one active proposal exists per bound key, and a proposal can be requested only for an
  unresolved field.
- The status enum has exactly five values; a terminal status can never change; acceptance and
  rejection set the decision fields without touching the status.
- The decision fields can be set exactly once.
- The row is created at request time and exists before any Inference Backend call.
- A transient failure retries at most twice and then fails; a non-transient failure fails immediately
  with the typed reason stored.
- An invented, missing, duplicated, or malformed candidate id, or a wrong `finish_reason`, sets the
  row to `failed` with its typed reason and the raw response text, and never produces a candidate
  outcome.
- A structured-output refusal and an empty-content completion set the row to `failed` with typed
  reason `backend_refusal`; neither is recorded as `no_match`.
- A stale proposal cannot be accepted, and staleness is revalidated inside the acceptance transaction
  against the frozen resolved Device reference.
- A `no_match` outcome cannot be accepted.
- Acceptance upserts the `TerminationResolution` row for the bound key and links it from the proposal;
  the last explicit operator action wins.
- Cancellation is allowed for any operator with request permission, and a late response arriving after
  cancellation never overwrites the cancelled row.
- The lifecycle carries a task type and a generic Candidate Snapshot with nothing Contact-specific.
- The change is independently mergeable with all tests passing.

**Blocked by.** T5, T7.

### T9. Ask AI UI integration

**Scope.** Add the Ask AI action to the termination field in the review workspace. Render the proposal
card in all its states, the in-place pending progress with cancel, the per-field proposal history, and
the accept and reject actions with their disabled reasons. Enforce the request, view, and decision
permissions at the view boundary.

**Acceptance criteria.**

- Ask AI is disabled with its reason when the field is already resolved, when an active proposal
  exists, when no Inference Backend is enabled, or when the operator lacks the permission.
- A pending card shows progress in place, polls until a terminal state, and offers cancel.
- A completed card shows the "Proposal - not applied" badge, the candidate with its kind, the
  explanation, the backend metadata with attempt count, and explicit Accept and Reject buttons.
- A stale card shows the stale badge and a disabled Accept with its reason.
- A no-match card has no accept action; a failed card offers Ask AI again.
- Accepting writes a `TerminationResolution` row, marks the termination `accepted`, and triggers a
  replan.
- Cancel is offered to any operator with request permission, not only the requester.
- The per-field history lists every attempt with its status and outcome.
- The change is independently mergeable with all tests passing.

**Blocked by.** T6, T8.

### T10. REST, GraphQL, and YAML surface completion

**Scope.** Cover only the surfaces that T4, T5, T7, and T8 introduce. T1 already moved every existing
surface onto the catalog. Add the `CableClassMapping` REST endpoint, GraphQL type, and YAML section;
the `InferenceBackend` endpoint with its write-only credential reference; and the read-only Resolution
Proposal and Cable provenance endpoints. Until this ticket lands, a trace profile and its CableClass
mappings are configurable through the UI only.

**Acceptance criteria.**

- Every new REST and GraphQL choice list and writable field set for Target Fields derives from the
  target-field catalog.
- The `InferenceBackend` endpoint validates against the `InferenceBackend` form schema, not the
  target-field catalog. Inference Backend configuration holds no Target Field.
- Profile YAML for a trace-adapter profile round-trips across instances with natural-key references
  only, including CableClass mappings.
- YAML rejects a policy section that the profile's adapter does not support.
- The `InferenceBackend` serializer never returns a credential value, and the credential reference is
  write-only.
- Resolution Proposal and provenance endpoints are read-only.
- Import Plans, `ImportExecution` rows, Resolution Proposals, and Inference Backends are absent from
  GraphQL.
- The change is independently mergeable with all tests passing.

**Blocked by.** T5, T7, T8.

### T11. Hardening and end-to-end pass

**Scope.** Close the coverage matrix in section 12.6 end to end. Verify the permission summary, the
prompt-injection stance, the failure taxonomy, the rollback behavior, and progress reporting under
real HTTP and real database conditions. Delete every remaining test for an obsolete private helper.
Confirm that no dual code path, commented-out code, or untracked "remove later" marker remains.

**Acceptance criteria.**

- The section 12.6 matrix is fully covered, with end-to-end tests for upload through database outcome.
- A permission check failure inside a transaction rolls back and blocks the unit on the next plan.
- A redaction scan finds no secret value in any persisted row, log record, job payload, or audit
  record. The typed credential reference is expected in exactly one authoritative place, the enabled Inference Backend row or the file fallback when no row exists, and
  the scan asserts it appears nowhere else.
- A source-evidence string that looks like an instruction does not change the Inference Backend
  request structure or bypass response validation.
- No test targets a private engine helper.
- No obsolete runtime path, compatibility facade, or dual read path remains in the tree.
- The change is independently mergeable with all tests passing.

**Blocked by.** T6, T9, T10.

### Ticket dependency summary

| Ticket | Blocked by |
| --- | --- |
| T1 Target-field catalog, adapter registry, Import Profile cutover | None |
| T2 Import Engine core with Device and Rack Target Modules | T1 |
| T3 Trace Source Adapter | T1 |
| T4 Trace decision models and policy | T1, T2, T3 |
| T5 Cable Target Module with Patched Path Replacement | T2, T4 |
| T6 Trace Review Workspace UI | T5 |
| T7 Inference Backend configuration and credential boundary | T1 |
| T8 Resolution Proposal model, job, and acceptance | T5, T7 |
| T9 Ask AI UI integration | T6, T8 |
| T10 REST, GraphQL, and YAML surface completion | T5, T7, T8 |
| T11 Hardening and end-to-end pass | T6, T9, T10 |

## 15. Out of scope and future fog

Out of scope for this architecture:

- Multi-fiber and multi-lane modeling beyond NetBox's existing supported Cable Profiles.
- Trunk, breakout, and shuffle Cable Profiles for segment creation. A created segment has one
  termination per side, so the CableClass mapping offers only the single and duplex family
  (section 3.6).
- Whole-preview batch inference processing.
- Contact-field assistance through the Resolution Proposal lifecycle.
- Creating missing Devices or ports.
- Termination types other than Interface, FrontPort, and RearPort.
- Any Inference Backend applying a change without explicit operator acceptance.
- Creating or mutating CablePath state.
- Creating a NetBox Cable Profile at run time.
- Updating attribute drift on a reused Cable.
- A trace-level database record.
- A durable review-session model.
- Migrating an old executable Import Plan or keeping a compatibility executor.

Deferred and recorded as future fog:

| Item | Status |
| --- | --- |
| NetBox visual theme polish | Deferred to implementation; the prototype palette is not final (#83) |
| Third-party Source Adapter extensibility through entry points | Not in this delivery; the registry is static in-plugin (#85) |
| Per-Import Profile Inference Backend selection | Not in this delivery; the Inference Backend is plugin-level (#85) |
| Resolution Proposal row pruning and retention tooling | Future scope; all rows are retained now (#82) |
| Contact assistance as a second proposal task type | Future scope; the lifecycle is already task-type-generic (#82) |
| Direct AppRole Vault login without Vault Proxy | Future scope; Vault Proxy auto-auth is the baseline |
| A bounded in-process credential cache | Future scope; the plugin caches nothing now |
| `json_object` and `json_schema` response modes | Allowed only after an operator verifies the exact backend and model |
| Model discovery in the Inference Backend UI | Optional convenience, never a worker dependency |

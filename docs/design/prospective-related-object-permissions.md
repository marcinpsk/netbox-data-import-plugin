# Prospective related object permissions

## 0. Review record

- Round 1 accepted a missed caller state: Device execution assigns primary and out-of-band IP
  relations before its final Device permission check. Merged design r2 includes those relations.
- Round 2 found that r2 did not replace a physical `IPAddress` row after assignment and did not
  account for Device reverse rows created before the final check. Merged design r3 closes both gaps.

## Brief

The `object_permissions` module owns read-only permission assessment for a row that does not exist
yet. Its `assess_permission_scoped_save()` interface currently represents only the root row. A Device
plan can also create the Rack or Device Role that the Device will reference. The permission assessment
therefore sees null foreign keys although execution writes non-null relations.

The design must meet these observable conditions:

- A constraint that requires a planned relation to be null does not authorize the Device.
- A constraint that identifies the planned Rack by name or the planned Device Role by slug authorizes
  the matching Device.
- The assessment performs no writes, emits no model signals, and consumes no database sequence value.
- Existing cyclic, multi-valued, JSON, and generated-primary-key assessments keep their behavior.
- Target Modules provide planned model state. They do not interpret NetBox constraint syntax.

The seam belongs at `assess_permission_scoped_save()`. Permission constraint evaluation is already
hidden behind that interface, and moving constraint interpretation into Target Modules would reduce
locality. A regression test at this interface, plus target-module tests for planned Rack and Device
Role relations, mechanically guards the failure class.

## Candidate shapes

### Shape A: prospective relation world

Extend the permission assessment interface with named prospective relation objects. The module gives
each unsaved relation a private synthetic key, connects the root candidate to that key, and adds the
related row to the same read-only SQL world used for the root candidate. Existing database rows stay
visible. Target Modules pass the Rack and Device Role objects that their dependency changes will
create.

### Shape B: target-owned constraint projection

Keep the permission interface unchanged. Target Modules assign placeholder foreign keys and inspect
permission constraints to decide relation predicates from planned source values. This keeps the SQL
implementation smaller, but it makes each caller understand NetBox constraint syntax and splits one
permission rule across modules.

## Blind design

The primary designer used GPT-5 with the session reasoning level. The blind designer inherited the
same model and reasoning level in a separate agent context. The blind brief contained the problem,
constraints, and source file pointers. It did not contain either candidate shape or this record.

The blind design independently selected Shape A. It also required three details:

- Allocate synthetic keys per concrete model, including multiple candidates of one model.
- Treat each synthetic key only as relation plumbing. A constraint that needs its unknown generated
  value must fail closed, while a nullness constraint can use its presence.
- Build planned Rack and Device Role ORM objects from the same payloads that execution uses.

## Divergence table

| Decision | Primary design | Blind design | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Owning module | `object_permissions` | `object_permissions` | It already owns constraint parsing and prospective SQL. | Converged | Target Modules pass state only. |
| Interface name | `prospective_relations` | `related` | The longer name distinguishes planned rows from existing ORM relations. | Use `prospective_relations`. | Call sites state the temporal meaning. |
| Synthetic-key rules | Relation rows use negative keys. | Allocate per model and reject constraints that use unknown key values. | Existing root-key handling already makes synthetic keys non-authoritative. | Accept blind refinement. | Exact and range predicates on unknown generated keys fail closed. |
| Multiple rows of one model | Not specified. | Support them in one model world. | A reusable permission interface must not make table name a unique candidate key. | Accept blind refinement. | One world CTE can contain several candidate rows. |
| Caller representation | Pass unsaved Rack and Device Role rows. | Same, built from execution payloads. | The model defines database field preparation and relation metadata. | Converged | No parallel permission DTO exists. |
| Alternative projection | Target Modules interpret constraints. | Rejected. | Deleting the permission module would spread NetBox constraint knowledge across callers. | Reject Shape B. | Constraint syntax stays local. |

## Merged design r1

Add one optional keyword-only `prospective_relations` mapping to
`assess_permission_scoped_save()`. Each key names a concrete forward foreign-key or one-to-one field
on the root model. Each value is the unsaved final row for that relation.

The implementation copies all rows, assigns collision-free negative keys without saving, connects
the root foreign keys, and builds one typed candidate/world CTE pair per concrete model. ORM query
aliases keep their joins but read participating models from those worlds. Root selection reads only
the root candidate. A direct or traversed predicate that depends on an unknown generated key fails
closed. Nullness predicates continue to evaluate relation presence.

Invalid fields, wrong model types, conflicting foreign keys, unsupported schemas, and database or
constraint errors use the existing warning and `allowed=False` behavior. Execution remains
authoritative and checks permissions again after dependencies receive real keys.

Target Modules retain the exact unsaved Rack and Device Role objects described by their dependency
changes. Device create assessment passes those objects through the interface. Device Role create
planning also uses its exact candidate instead of a bare model permission.

Tests exercise the public permission interface, Device plans with matching and excluded planned
relations, Device Role candidate constraints, no model signals or sequence consumption, and the
existing matrix unchanged. An import guard keeps NetBox constraint parsing inside
`object_permissions`.

## Decision

The adversarial reviewer returned **RATIFY r1** for the original merged scope.

## Merged design r2

Implementation review found that Device execution also assigns `primary_ip4`, `primary_ip6`, and
`oob_ip` before it checks the final Device permission. The shared `ip_assignment` module already owns
address resolution for execution. Add one read-only function there that returns the physical or
unsaved `IPAddress` row a Device field will reference, or no row when execution will leave that field
unchanged. For a new Device, it derives interface availability from the Device Type templates and
uses the same address and VRF lookup as execution.

Device candidate construction applies those physical keys and passes unsaved address rows through
`prospective_relations`. It does this for creates and updates. The permission helper uses the
candidate primary key to select add or change assessment, so the same interface checks both current
and final update scope. Two fields that will reference one new address share one prospective row.

The Device permission check occurs after all Device-owned concrete state in both planning and
execution. Contact assignments and provenance records remain separate related writes with their own
permissions and preconditions.

Revision r2 is complete when planning blocks an assignable address under a null-only Device scope,
allows an unassignable address that leaves the field null, accepts a matching address constraint,
checks prospective updates, and keeps the existing apply-time rollback check as defense in depth.
The adversarial reviewer did not ratify r2.

## Merged design r3

`prospective_relations` accepts a final saved related row as well as an unsaved row. A saved row keeps
its real key, replaces its physical database version in the read-only world, and never mutates the
caller object. This lets IP planning represent an existing unassigned address after execution assigns
it to the selected interface.

For a new Device, the IP resolver instantiates the selected Interface Template in memory. It records
the known assignment type and a synthetic interface key on the prospective address. The permission
module treats that key as presence only, as it does for synthetic primary keys: nullness can use it,
but a value predicate fails closed. No interface row enters the database.

A root reverse-relation predicate fails closed during prospective create or update assessment. A
Device save can materialize component rows, and later import steps can materialize contact or
provenance rows. The planner does not claim to simulate those graphs. This rule prevents a missing
reverse row from authorizing a plan. It is derived from model metadata inside `object_permissions`;
Target Modules do not interpret constraints or enumerate reverse permission paths.

The final Device check remains after IP, contact, and provenance writes as defense in depth. Planning
can be conservative for a reverse-relation scope, but it cannot mark a plan actionable from a reverse
state that execution will change.

Revision r3 is complete when saved related rows are replaced in the prospective world, assignable
new and existing IPs produce the same Device forward relation state as execution, generated interface
keys are presence-only, root reverse predicates fail closed, and focused tests prove contact and
provenance absence cannot authorize a Device plan.

The adversarial reviewer ratified merged design r3.

The ratification covers saved-row replacement, synthetic assignment
keys, conservative reverse-relation handling, and final execution rechecks. Duplicate candidates for
one concrete model and key must coalesce only when their complete prepared state matches. A conflict
fails closed. A refused reverse-relation constraint arm does not prevent another independent
permission arm from authorizing the candidate.

Generated values stay private across every ORM traversal. A query alias that compares a generated
field, or joins from it into a model outside the prospective world, reads a world without candidate
rows that contain unknown values. A join between two represented candidate models can still use
their generated keys as internal relation plumbing. This keeps known related fields queryable while
preventing an internal key from authorizing a plan.

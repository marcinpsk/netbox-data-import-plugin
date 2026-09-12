# Proposal acceptance permission

Status: ratified

## Brief

The Review Workspace must disable proposal acceptance when the same actor and current database
state make the acceptance writer refuse. The previous presentation check used only the model-level
add permission. The writer also enforced object constraints against the saved
`TerminationResolution`, or change permission when the resolution already existed. A constrained
actor could therefore see an enabled Accept action that returned HTTP 403.

`object_permissions` owns permission-scoped writes. `termination_proposal` owns the values written
by an accepted termination proposal. `proposal_presentation` owns action reasons and must not copy
write policy.

The design must meet these observable conditions:

- A fresh proposal disables Accept when a new resolution falls outside the actor's add constraints.
- The same proposal enables Accept when the new resolution falls inside those constraints.
- An existing resolution enables Accept only when the actor can change the current and resulting
  row.
- The POST repeats the permission check under its existing lock and remains authoritative.
- The GET performs no write, emits no write-lifecycle signal, and consumes no primary key.
- One policy seam decides both the presentation state and write authorization.
- A permission or constraint change between GET and POST can make the POST refuse. The UI does not
  claim to remove this race.

Evidence:

- `netbox_data_import/proposal_presentation.py` derives the Accept reason.
- `netbox_data_import/termination_proposal.py` forms and writes a `TerminationResolution`.
- `netbox_data_import/object_permissions.py` contains the permission-scoped write seam.
- `netbox_data_import/tests/test_proposal_workspace.py` proves that a constrained add outside the
  actor's scope gets HTTP 403.
- NetBox evaluates object constraints through compiled Django querysets.

## Candidate shapes

### Generic prospective scoped-save assessment

Add a read-only assessment to `object_permissions`. It receives the actor, model, lookup, and
resulting values. It returns the required permission and whether the prospective save stays inside
that permission's object constraints. Proposal acceptance calls the same assessment under its
profile and proposal locks. The generic writer retains its saved-row check as the final authority.

This keeps generic add, view, and change policy in the existing deep module. The assessment must
preserve Django lookup and join semantics.

### Prepared termination-resolution operation

Make `termination_proposal` produce an immutable operation that owns both authorization and
execution. This would keep the resolution lookup and values together, but it would add a second
public execution interface for one caller.

### Transactional probe write

Save and roll back a temporary resolution during GET. This shape is rejected. A rolled-back insert
can emit write signals and consume a sequence value. It also makes presentation a writer.

## Blind design

The primary and blind designs independently selected `object_permissions` as the owner of a
read-only prospective-save assessment. The blind design also required `termination_proposal` to
construct the exact resolution lookup and values once for both assessment and execution. It
rejected a task-only permission helper because that helper would copy generic create and update
rules.

The merged design uses an immutable assessment result and a narrow task assessment method. The
task shares one private write specification between that method and execution.

## Adversarial revisions

The first assessment proposal used `Q.check()` against a scalar value map. Ratification rejected
it because scalar values cannot preserve reverse or multi-valued join correlation. `Q.check()` can
also convert a database error into a successful result.

The next proposal restricted constraints to direct fields and forward single-valued relations.
Ratification rejected that boundary because it changed the authorization semantics of valid
NetBox constraints.

The next proposal compiled the real constrained queryset against a one-row CTE that shadowed the
model table. This preserved ordinary relation correlation, but it also hid existing rows whenever
the query joined back to the root model. It denied every create constraint on the primary key,
including the known post-insert fact that an automatic primary key is not null.

The next proposal replaced only the query's base alias with the candidate CTE. It preserved saved
siblings and handled known primary-key nullness. Ratification found one remaining defect: a cyclic
relation could not see the prospective row itself.

The final design uses two CTEs:

- The candidate CTE contains only the prospective row.
- The world CTE selects the physical table without the candidate primary key, then adds the
  candidate row.

The cloned Django query maps its base alias to the candidate CTE. It normally maps every other
alias for the same model table to the world CTE. Other tables remain unchanged. The query root is
therefore one candidate, while cyclic paths see the database as it would exist after the save.

A create uses a collision-free negative automatic primary key. The assessment supports known
`isnull` and exact-null predicates for that key. A root predicate that depends on the unknown
numeric value cannot grant the operation. When a cyclic predicate depends on that value, the query
maps only the compiled alias that carries that predicate to the physical table. This prevents the
synthetic candidate from granting access while a matching saved sibling can still grant it. Other
cyclic aliases continue to see the prospective world. Another valid OR arm can also grant the
operation. Detection reads the compiled lookup tree, so explicit `__pk` paths and implicit terminal
relation lookups use the same rule.

All candidate and constraint values stay parameterized. Table names, columns, and casts come from
Django model metadata. Compilation or database errors fail closed. The assessment performs one
read-only SELECT and does not create a database object.

Three throwaway proofs ran on both supported NetBox versions. They covered a multi-valued related
filter, a correlated sibling join with primary-key nullness, and cyclic create and update paths
that had to see the prospective row. Each proof left the database unchanged and was removed after
the result was recorded.

## Ratified interface

`object_permissions.assess_permission_scoped_save()` returns an immutable result with `allowed`
and `permission`. A missing row uses add permission. An existing kept row uses view permission. An
existing updated row requires change permission against its current and prospective state.

Proposal acceptance calls the assessment while it holds the profile and proposal locks. The
generic `_scoped_write()` keeps its established transactional saved-row checks. It does not use a
raw prospective instance because model save hooks can derive fields that the generic seam cannot
predict. This keeps valid constraints on save-derived values effective.

`SelectTerminationTask` constructs and validates the `TerminationResolution` lookup and values in
one private method. Its assessment and write methods both use that specification. Presentation
asks the task for an assessment only when a selected candidate exists. Acceptance repeats the same
assessment before staleness evaluation and execution.

Tests use real ObjectPermission rows. They cover constrained creates, current and prospective
update scope, related and cyclic constraints, automatic primary-key predicates, no assessment
write, save-derived fields, card and POST agreement, and the existing create-to-update race.

Ratification verdict: RATIFY

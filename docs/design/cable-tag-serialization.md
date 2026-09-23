# Cable tag state during Logical Cable replacement

Status: RATIFIED at r5 by Astra high, after four adversarial rounds. Implemented on the Cable policy branch.

## Brief

Patched Path Replacement records the reviewed description and tag names in a
deletion fingerprint. Execution locks the Logical Cable, replans, checks that
fingerprint, and deletes the Cable in one transaction. NetBox stores Cable tags
in `extras_taggeditem`, where `(content_type_id, object_id)` is a generic
relation without a foreign key to `dcim_cable`. A concurrent tag writer can
therefore change the relation after the fingerprint check. A writer waiting on a
table lock could also add a relation after the Cable is deleted.

The Cable Target Module owns the reviewed deletion. The database must own the
serialization seam because NetBox UI, API, background jobs, and direct ORM
writes all use the same tag relation. A Python lock at one caller cannot cover
them. The migration is the mechanical guard for this failure class.

Acceptance conditions:

1. A tag association inserted, updated, or removed before execution locks the
   complete review set changes the replan fingerprint and stops the accepted
   plan. An uncommitted association cannot commit an orphan after deletion.
2. A tag association writer starting after the complete review lock set cannot
   change the reviewed state. If replacement commits, an insert or update
   targeting that deleted Cable fails by the end of its transaction and leaves
   no orphan relation. An update or removal of a deleted association can affect
   zero rows without an integrity error.
3. A tag rename cannot change the fingerprint between replan and deletion.
4. Unrelated tag writes do not wait for this Cable. Normal NetBox tag cleanup
   during Cable deletion still succeeds.
5. The behavior works on NetBox 4.6 and 4.7. Migration reversal removes only
   the database objects this plugin added.

Evidence: `cable_target.py` defines `_deleted_cable_review_fingerprint`,
`_load_existing_cables`, and `CableModule._delete`. The actual 4.7 PostgreSQL
schema has foreign keys from `extras_taggeditem` to content type and tag, but
none from `object_id` to Cable. `CableExecutionTest` exercises the real
`ImportEngine` and PostgreSQL with competing connections.

## Candidate shapes

| Shape | Mechanism | Limitation |
| --- | --- | --- |
| A | A trigger on tag association writes locks old and new Cable rows and rejects missing new targets | The plugin must implement referential integrity itself and prove behavior across transaction snapshots. |
| B | Derive a nullable Cable ID on each association and enforce it with a real foreign key | PostgreSQL owns existence checks. The plugin must lock existing association rows while it reviews them. |
| Rejected | Lock existing association rows or the tag table only during replacement | A missing association has no row to lock. A queued insert can land after deletion; a table lock delays unrelated tags. |

## Blind designs and divergence

The first design was drafted here with a plugin trigger that locks old and new
Cable rows on every association write. A fresh `gpt-6-astra` high-effort
read-only design, given only the factual brief and code pointers, proposed a
derived Cable foreign key. Neither designer saw the other's proposal before
both were complete.

| Decision | First design | Blind design | Disposition |
| --- | --- | --- | --- |
| Association integrity | Manual row-lock trigger and missing-row check | Derived Cable foreign key with PostgreSQL referential integrity | Use the foreign key with deferred `NO ACTION`. It prevents committed orphans and leaves cleanup to Django. |
| Existing association updates and removals | Trigger locks the parent Cable | Lock association rows during execution | Use association locks. The FK guards new targets; existing rows need their own lock to preserve reviewed membership. |
| Tag names | Lock Tag rows | Lock Tag rows | Shared requirement, not independent verification. |
| Reviewed display | Separate reads | One canonical review snapshot for display and fingerprint | Use one cached snapshot. Astra high round 1 reproduced a plan whose display and fingerprint disagreed. |

The interface affected by the choice is the database schema and the locks in
`CableModule._delete`. Both shapes expose the same ImportEngine behavior. The
foreign key has greater depth: it hides referential integrity and cleanup from
the plugin. Deleting it would force these rules back into a trigger.

## Merged design, r5

A reversible custom migration extends `extras_taggeditem` with nullable
`ndi_cable_id bigint`. A `BEFORE INSERT OR UPDATE` trigger derives the value
from `(content_type_id, object_id)` on every write, including attempts to set
`ndi_cable_id` directly. It resolves `dcim.cable` by natural content-type
identity at runtime, not an installation-specific numeric ID. An initially
deferred foreign key to `dcim_cable(id)` uses `ON DELETE NO ACTION` and
`ON UPDATE NO ACTION`. An index covers non-null projected IDs. The migration
backfills existing associations, rejects any pre-existing Cable orphan, and
keeps Django model state unchanged. It depends on the `__first__` migration
sentinel for `extras` and `dcim`, which resolves to the installed NetBox
version's first migration. Reverse SQL removes only this FK, trigger, function,
index, and column. DDL runs atomically; a lock timeout or validation failure
rolls it all back.

Installation locks `django_content_type`, `dcim_cable`, then
`extras_taggeditem` in one migration transaction. It adds the
column, installs the derivation trigger,
backfills every Cable-typed association from its `object_id` without joining
away missing Cable rows, adds the index, and validates the FK. A missing
content-type row causes the trigger to reject a new or updated association;
it never projects it to NULL. Every association insert or update reads its
content-type row with `FOR SHARE` before deriving the projection. A separate
trigger rejects changing a content type into or out of `dcim.cable` while it
has tagged items. The shared row lock makes the check and the projection
serialize: if an association commits first, the identity change sees it and
refuses; if the identity change commits first, the association reads its new
identity and derives the correct projection. This also covers an
administrator's multirow content-type update. Migration reversal removes both
triggers and both functions.

The `extras_taggeditem` lock is `ACCESS EXCLUSIVE`, because `ADD COLUMN`
requires it. It blocks tag reads as well as tag writes until the migration
commits. This cost is accepted. The migration runs in the upgrade window, and
one transaction keeps enforcement and backfill atomic. A staged migration could
release reads earlier, but it cannot roll back as one unit. On PostgreSQL 18,
with 5,000,000 tagged items of which 500,000 are on Cables, the migration held
the lock for about 10.3 seconds. The backfill took 9.5 seconds of that time.
The cost grows with the number of tagged items.

The content-type identity trigger rejects any transition into or out of
`dcim.cable` when `current_setting('transaction_isolation')` is not
`read committed`. This check runs before its association query. A transaction
snapshot established before a competing association commits cannot then make
the identity guard miss that association after waiting. Ordinary tag writes
retain their supported isolation behavior; a content-type identity change
under stronger isolation fails explicitly and must be retried under
`READ COMMITTED` after the administrator checks existing associations.

Execution already takes `FOR UPDATE` on the Cable during replan. Because the FK
is deferred, a concurrent insert can remain uncommitted while replan reads.
Its commit must check the Cable reference. If deletion commits first, that
writer fails with `23503`. If the writer commits first, replan sees its tag or
the deletion transaction fails safely and rolls back. Django's generic
relation collector remains responsible for removing associations. A direct
SQL Cable delete that leaves associations fails at commit instead of silently
leaving orphans. No hidden cascade changes collector or signal behavior.

After locking all relevant Cable and CableTermination rows, execution locks
the Cable's existing association rows `FOR UPDATE` in primary-key order. It
then locks their referenced Tag rows `FOR SHARE` in primary-key order. It
acquires this complete set before the first replan review read. Association
locks preserve membership and `tag_id`; Tag locks preserve names. A rename
that commits before these locks is seen by the fingerprint check. The preview
path remains read-only.

Planning caches one immutable Cable review snapshot per Cable per invocation.
The deletion fingerprint and operator-visible Logical Cable display derive
from that exact value. Execution builds a fresh snapshot under the complete
lock set and compares it with the accepted fingerprint. The reviewed metadata
does not enter the post-deletion audit row.

The public test seam is `ImportEngine.plan` and `ImportEngine.execute` against
the real database. The first red test covers a queued association insert at
Cable `pre_delete`: it must fail after committed deletion and leave no orphan.
Next slices cover existing-row removal or update, Tag rename, writer-first
stale review, and unrelated tag writes. A migration test covers backfill,
reversal, and a pre-existing orphan. Run these on both supported NetBox
versions. A real backend lock observation, SQLSTATE, and final database state
must prove a wait; catching any `OperationalError` as a timeout is inadequate.

Deadlocks remain possible when an external writer locks a Tag or association
before touching a Cable. A database abort is safe; the complete import
transaction rolls back. The guarantee concerns reviewed tag names, not every
Tag field or same-transaction application hooks after validation. Stronger
transaction isolation must abort safely or pass the same invariant.

## Astra high round 1: not ratified

The reviewer found that `ON DELETE CASCADE` can remove a TaggedItem before
Django's collector or signals observe it. It also reproduced a review display
whose tag names differed from the accepted fingerprint. Both findings are
accepted and changed r3. The reviewer verified that ImportEngine holds its
Cable locks through deletion and refuted the claim that a `BEFORE` trigger
cannot fill a foreign-key column. It also showed that an association update or
removal can complete between the Cable lock and association lock; r3 names
the complete review lock set as the serialization point. The migration order,
content-type lifecycle, and real PostgreSQL proof remain open for round 2.

Open review questions: Does the deferred `NO ACTION` FK preserve Django's
collector behavior on both installed NetBox versions? Does a queued writer
fail when deletion commits, including when its statement started earlier?
Does the migration work with fresh and existing databases and reverse cleanly?
The adversarial reviewer must verify the revised mechanism. PostgreSQL
regression tests must settle its runtime claims before the branch is pushed.

## Astra high round 2: not ratified

The deferred `NO ACTION` foreign key survived the reviewer's writer-timing
attack. The remaining blocker was a content-type rename racing an uncommitted
association: a plain identity lookup could project NULL, then the rename could
make that association Cable-typed before it commits. Revision r4 adds a shared
content-type row lock to the association trigger. The identity-change trigger
already takes the incompatible update lock. The reviewer closed the other
round-1 design findings and kept real PostgreSQL tests as an implementation
gate. Round 3 must verify this final locking protocol.

## Astra high round 3: not ratified

The reviewer closed the READ COMMITTED content-type race and the requested
multirow and upsert cases at design level. It found a REPEATABLE READ schedule
where an administrator's identity-change trigger can keep a stale snapshot
after waiting for an association writer. Revision r5 fails that identity
change before its association check when the transaction isolation level is
stronger than READ COMMITTED. The deletion core remains unchanged. Round 4
must verify that this explicit abort closes the last lifecycle blocker.

## Astra high round 4: ratified

The reviewer closed the REPEATABLE READ identity-change blocker because r5
rejects that transition before its association query. It also verified from
PostgreSQL's row-lock executor that a stronger-isolation association writer
which tries to lock a content-type row changed since its snapshot aborts with
`40001`; it cannot commit an obsolete NULL Cable projection. The reviewer
ratified r5 for the acceptance conditions above. Red ImportEngine tests proved
that a deleted Cable could gain a tag and that an existing tag association
could change during deletion review. The migration and execution locks made
both tests pass on NetBox 4.6 and 4.7. Separate tests cover a queued writer,
Tag rename, content-type identity changes, and upgrade failure on an existing
orphan. The complete test suites remain the final validation gate.

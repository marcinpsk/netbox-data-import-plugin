<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
-->

# netbox-branching compatibility

## 0. Review record

Round 4: RATIFY r4, for the current plugin models, NetBox 4.7.0 with netbox-branching 1.2.1, and
branches created after this release. The ratification is by source inspection; the CI job in the
design is what verifies it at runtime. Rounds 1 to 3: NOT RATIFIED (six, three and one blockers),
each closed or withdrawn below. After round 2 the operator removed D8 from scope (constraint below).

Blind co-design: Claude (Opus 5.5) drafted from the brief below. `gpt-6-astra` at reasoning effort
high drafted in a fresh read-only context from the same brief and evidence pointers, without the
Claude draft. Both chose main-only operation.

Corrected before round 1 (not a reviewer finding):

- The Claude draft said migration 0038 must run on a branch. Refuted: branch migrate runs `RunSQL`
  with the search path set to the branch schema only (`netbox_branching/models/branches.py`,
  `_isolated_runsql`), so its `LOCK TABLE django_content_type` fails there. Reopen if branching
  stops isolating `RunSQL`.
- The Claude draft missed that `ImportProfile.tags` and `InferenceBackend.tags` are plain
  `ManyToManyField`s. Their auto-created through tables hold a database foreign key to
  `extras_tag`, so a Tag delete in a branch cascades into main.

## Problem brief

NetBox Branching (netbox-labs/netbox-branching, v1.2.1, NetBox 4.7 only; v1.1.x covers 4.4.1 to
4.6) stages changes in a per-branch PostgreSQL schema. Its `BranchAwareRouter` sends queries for
branchable models to a `schema_<id>` connection while a branch is active. The connection uses
`search_path=<branch>,<main>`. A model is branchable when it inherits `ChangeLoggingMixin`, is in
`INCLUDE_MODELS` (for example `extras.taggeditem`, `dcim.portmapping`), or a registered resolver
says so. Merge and sync replay `ObjectChange` rows only. The plugin today has no code for
branching, and a user can install both.

The question is what the plugin guarantees when netbox-branching is installed, and what code
enforces it.

### Evidence

- Only `ImportProfile` and `InferenceBackend` are change-logged (`NetBoxModel`). The 18 other models
  are plain `models.Model`, and all of them have a foreign key to `ImportProfile`.
- `DeviceImportSource.device` and `CableImportSource.cable` are `CASCADE` foreign keys into DCIM.
  `ClassRoleMapping.rack_type` is `SET_NULL` into `dcim.RackType`. A Django cascade runs on the
  deleting instance's connection, where a table absent from the branch schema resolves to main.
- About 25 `transaction.atomic()` calls pass no `using=`, so they open a transaction on `default`.
  Inside them, `select_for_update()` runs on `ImportProfile` and on DCIM models, which route to the
  branch connection. `locked_profile_policy` (`models.py`) wraps every import this way.
- `ImportJobRunner` and `ResolutionProposalJob` are plain `JobRunner`s. NetBox applies request
  processors (which activate the branch) only in `ScriptJob`, `AsyncViewJob` and `AsyncAPIJob`.
  `ImportJobRunner` enters NetBox's `event_tracking` only, so it records ObjectChanges and
  activates no branch.
- Raw cursors on `django.db.connection` read tables at `object_permissions.py` (permission query)
  and `cable_target.py` (`extras_tag ... FOR SHARE`).
- Plugin code calls `snapshot()` before it updates an existing change-logged object (#188). A
  test-time guard (`tests/snapshot_guard.py`) fails a plugin write without a current snapshot.
- Migration 0038 adds a column, trigger and foreign key on `extras_taggeditem` through `RunSQL`.
  Branch provisioning copies triggers, but not foreign keys.
- The CI matrix runs NetBox 4.7.0 and 4.6.10, without netbox-branching.

### Constraints

- netbox-branching stays optional. Without it, behaviour does not change.
- No backwards-compatibility layers; this repository removes obsolete paths.
- Fail fast and visibly; no silent fallback to main.
- The plugin was never compatible with netbox-branching before this release, so a deployment runs
  both only from this release on, and no branch predates it (operator decision after round 2).

### Acceptance conditions

1. With a branch active, every plugin entry point (UI view, REST API, background job) has one
   documented outcome. No outcome is an unhandled 500, and none writes to a schema the user did not
   choose.
2. A NetBox core action executed in a branch (delete or edit a Device, Cable, RackType or Tag)
   does not change the plugin's rows in main. A change to a global object (User, core.Job,
   CustomField, ObjectType) lands in main, and so do its effects on plugin rows.
3. After a branch merges or is discarded, main's plugin rows are consistent with main's NetBox
   objects.
4. A CI job with netbox-branching on NetBox 4.7 runs tests that fail when 1 to 3 regress.
5. A mechanical guard stops a new call site from bypassing the chosen rule.

## Divergence table (blind merge)

| # | Decision | Claude | Codex | Evidence | Disposition |
|---|---|---|---|---|---|
| D1 | Support level | Main only | Main only | Full support needs all 18 plain models change-logged, and a rework of the default-connection locks (`models.py` `locked_profile_policy`) and raw cursors | Converged. Agreement is not verification |
| D2 | Which plugin models are branchable | Derived rule: a plugin model is branchable only when it has a database foreign key to a branchable model outside the plugin. Today: `DeviceImportSource`, `CableImportSource`, `ClassRoleMapping`, and the two `tags` through tables. The other 17, including `ImportProfile` and `InferenceBackend`, are forced non-branchable | All 20 plus both through tables are branchable | Every writer of plugin rows is a plugin view, API or job, and all of them are refused in a branch. So a branch copy of a policy row can only go stale. Sync selects by branchable type (`branches.py:424`), so main's profile edits are not replayed into branches. A core action reaches plugin rows only through a database foreign key from a plugin table | Claude's rule, with the through tables added. Differs in test: under Claude's rule a branch cannot delete an Import Profile, and branch reads of profiles show main |
| D3 | Branch revert | Allow it. Document that `DeviceImportSource` rows removed by a merged delete are not restored | Refuse every revert through a validator and a `pre_revert` backstop | Revert replays `ObjectChange` only, and plugin rows have none | Unresolved. Round 1 is to judge the r1 middle option (below) |
| D4 | GraphQL and the Device card in a branch | Allowed. GraphQL reads main (both exposed types are non-branchable under D2). The card reads the branch copy | Guard GraphQL fields; the card shows a notice | `graphql/schema.py` exposes only `ImportProfile` and `CableClassMapping`, read-only | Claude's, as a documented outcome. Round 1 may attack it |
| D5 | Stale branch selection (a cookie for a merged or archived branch resolves to main) | Not handled | Refuse: a selection must not authorize a write to main | `BranchMiddleware` returns 400 for an unknown or unready branch. For a stale cookie it runs the request on main and clears the cookie | Accepted in reduced form: refuse a plugin request that carries a branch selector while no branch is active, except the explicit switch to main (`?_branch=`) |
| D6 | Jobs | `refuse_branch()` at job start and in `locked_profile_policy` | Payloads carry an explicit main scope; an AST check forbids direct enqueue | Workers never activate a branch (`netbox/jobs.py` `JobRunner.handle`). The contextvar is the one fact; a payload field duplicates it | Claude's. Rejected: a second representation of the same fact, with one adapter |
| D7 | Data migrations on branch migrate | `fake_on_branch = True` on all seven | True on 0020, 0038 and 0039; the other four unset | 0015, 0022, 0030 and 0035 write `DeviceExistingMatch`, `ImportProfile` and `ResolutionProposal`. Under D2 these stay in main, and `RunPython` runs with the full search path, so a branch migrate would rewrite main | All seven `True`, following D2 |
| D8 | Branches that exist before the upgrade | Release note | Refuse to operate in them; validate the branch's physical tables first | Their schemas lack the new branchable tables, so the cascade leak survives in them | Unresolved. Round 1 is to choose (see r1) |
| D9 | Cable-tag foreign key inside a branch | None | Add it during provisioning | Branching excludes every foreign key from branch tables by design (`provisioning.py:262`). Nothing outside migrations reads `ndi_cable_id`, and the plugin never runs in a branch | Codex's rejected |
| D10 | Startup validation | None | Reject incompatible versions, and exemptions that override the resolver | A resolver `True` does not override `exempt_models` (`utilities.py` `supports_branching`). `netbox_data_import.*` in `exempt_models` would restore the leak | Codex's, accepted |
| D11 | Owner and seam | `branching.py`; plugin middleware for requests, plus a guard in jobs and `locked_profile_policy` | `branching.py` with `require_main(...)` at each call site | Middleware scoped by URL namespace covers every current and future view with no per-view code | Claude's |


### Round 1 dispositions (r1)

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| B1 | Provisioning copies an M2M table only through its parent's stored branchable ObjectType. NetBox creates ObjectTypes from `get_models()`, which excludes auto-created models. So resolver `True` never provisions the `tags` through tables | `netbox_branching/utilities.py:293-315`; NetBox `core/signals.py:52-75` | CLOSED in r2: `tags` moves to NetBox's standard `extras.TaggedItem`, which branching replicates (`INCLUDE_MODELS`). The through tables are deleted |
| B2 | Core handlers write non-branchable plugin rows in main while a branch is active: CustomField rename or delete rewrites `custom_field_data`; User and Job deletes null plugin foreign keys | NetBox `extras/signals.py`, `core/models/jobs.py`; `EXEMPT_MODELS` holds `core.*` and `extras.customfield` | REFUTED as a blocker. CustomField, Job and ObjectType are exempt, and User is not change-logged, so each change lands in main by design, and its effect on plugin rows lands with it. Under the rejected all-branchable shape, main's rows would instead keep stale keys. The r1 premise sentence was wrong; r2 restates it. Reopen if branching makes any of these objects branchable |
| B3 | A background core API bulk delete (`?background=true`) loses the branch: `AsyncAPIJob._build_request()` copies neither the `X-NetBox-Branch` header nor cookies, so it deletes main's Device, and the plugin rows cascade with it | NetBox `netbox/jobs.py:296-325`, `netbox/api/viewsets/mixins.py:340-350` | Mechanism confirmed. REFUTED as a plugin blocker: the Device itself is deleted from main, and the plugin rows stay consistent with main (condition 3). The plugin cannot intercept it. Follow-up: report it upstream. Reopen if the plugin gains a writer on that path |
| B4 | Sync records synthetic DELETE ObjectChanges for cascaded branchable models, including non-change-logged plugin models. A revert validator keyed on core types alone misses them, and revert then fails in `full_clean()` | `netbox_branching/models/branches.py:859-897` | CLOSED in r2: the validator also matches deletes of the plugin's branchable models |
| B5 | A middleware check does not quarantine a branch: sync, merge and migrate run in jobs, and a queued `AsyncViewJob` skips middleware | `netbox_branching/jobs.py`; `models/branches.py` `migrate()` checks no validator itself | CLOSED in r2: old branches are fenced through the pending-migrations gate. `sync()` and `merge()` raise unless the branch is READY, and activation needs READY. A migrate validator and a `pre_migrate` receiver refuse to migrate them |
| B6 | Guards too weak: a namespace walk misses views outside the namespace; a `fake_on_branch` attribute check accepts `False`; a discard test passes with no protection | Reading of the r1 guard list | CLOSED in r2 (guards rewritten) |
| N1 | GraphQL related fields (tags, journal, changelog) read the branch, so "returns main data" is wrong | NetBox `core/graphql/mixins.py:23-29`; branching `database.py:41-45` | CLOSED in r2: the plugin's GraphQL fields refuse in a branch |
| N2 | Resolver registration does not refresh stored `ObjectType.features`; only `post_migrate` does | NetBox `core/signals.py:52-75` | CLOSED in r2: the release ships a migration, so `migrate` refreshes them, and a test asserts the stored features |

### Changes r1 to r2

1. `ImportProfile.tags` and `InferenceBackend.tags` use NetBox's standard `extras.TaggedItem`. A data migration moves the existing rows. The two through tables are dropped.
2. The branchable plugin models are `DeviceImportSource`, `CableImportSource` and `ClassRoleMapping`.
3. The D2 premise now says: a non-branchable plugin row changes only through a plugin entry point (refused in a branch) or through a change to a global object, which itself lands in main.
4. The revert validator also matches deletes of the plugin's branchable models.
5. D8: the r1 middleware table check is removed. The tags migration is the fence. A migrate validator and a `pre_migrate` receiver refuse branches where a fence migration is pending.
6. D4: the plugin's GraphQL fields refuse in a branch, and the Device card shows a notice.
7. The guards are rewritten (see below).
8. Acceptance condition 2 now says "executed in a branch", and names global objects.

### Round 2 dispositions (r2)

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| R2-1 | The pending-migrations fence is not durable. `check_pending_migrations` scans only READY branches. Revert sets READY without checking migrations, and recovery resets SYNCING and MERGING to READY, also from the automatic recovery job | `netbox_branching/signal_receivers.py:276-292`; `models/branches.py:1228-1235`; `choices.py:68-86` | Accepted. It is the second blocker in the same mechanism (D8) after B5. D8 is the split candidate |
| R2-2 | A plugin bulk edit queued as an `AsyncViewJob` before the upgrade runs later without middleware. A pending branch then resolves to no branch, so the edit lands in main | NetBox `netbox/jobs.py:267-273`; branching `utilities.py:589-597` | Mechanism accepted. It needs a job queued before the upgrade: after it, middleware refuses every plugin request that carries an active branch or a stale selector, so no new such job is queued. Part of D8 |
| R2-3 | After a merge, NetBox's Tag `pre_delete` handler removes the tag from each non-branchable Import Profile, and records that change in main, not in the branch. So a revert restores the Tag without its assignments | NetBox `core/signals.py:215-247`; taggit's manager adds a reverse many-to-many relation on Tag | Accepted in r3: the revert validator also matches Tag deletes |
| R2-N1 | `BRANCHABLE_SINCE` equality does not validate its values, and conflicts with the CreateModel exception | Reading of r2 | Part of D8 |
| R2-N2 | The brief says only `ScriptJob` applies request processors; `AsyncViewJob` and `AsyncAPIJob` do too | NetBox `netbox/jobs.py:267-273,386-388` | Accepted: evidence line corrected |

Not usable as a fence: a plugin request processor. `apply_request_processors` swallows an exception
raised on entry (NetBox `utilities/request.py:136-141`), so a processor cannot refuse; it could only
switch to main silently.

### Operator decision after round 2

D8 had produced a blocker in both rounds (B5, then R2-1 and R2-2). The operator ruled it out of
scope: the plugin never worked with netbox-branching before this release, so no deployment has a
branch that predates it. R2-1, R2-2 and R2-N1 close with D8.

### Changes r2 to r3

1. Constraint added: no branch predates this release.
2. Removed: `BRANCHABLE_SINCE`, the migrate validator, the `pre_migrate` receiver, and the section
   on branches that predate the upgrade.
3. The revert validator also matches Tag deletes (R2-3).
4. Guard 2 pins the branchable set in the test instead of deriving the fence from a mapping.
5. The brief's evidence on request processors is corrected (R2-N2).

### Round 3 dispositions (r3)

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| R3-1 | Main deletes Tag T, and a branch syncs. Sync replays main changes only for branchable types (`branches.py:424`), so it skips main's removal of T from a non-branchable Import Profile. The branch copy of that `TaggedItem` then cascades, and sync records a synthetic `extras.TaggedItem` DELETE (`branches.py:859-897`) with no Tag DELETE. A revert re-creates the row against a missing Tag and fails in `full_clean()` | Branching `models/branches.py:424,859-897`; `models/changes.py:185-198` | Accepted. It is the second instance of one mechanism after B4. r4 states the general rule: the validator matches every delete that revert cannot restore for plugin data |
| R3-N1 | Guard 1 asserts a `code` for every response, but only the REST contract has one | Reading of r3 | Accepted: separate UI and REST assertions |

### Changes r3 to r4

1. The revert validator matches deletes of the plugin's referenced types, of the plugin's branchable
   models, and of `extras.TaggedItem` (the synthetic record of a tag assignment).
2. Guard 1 asserts the UI page and the REST `code` separately.
3. CI adds the case: main deletes a Tag, the branch syncs, merges, then a revert is refused.

## Design r4

### Contract

The plugin operates on main only. With netbox-branching installed and a branch active:

| Entry point | Outcome |
|---|---|
| Plugin-owned URL callbacks (UI) | HTTP 409 page naming the branch, with a link to switch to main |
| Plugin-owned URL callbacks (REST API) | HTTP 409, JSON `{"detail": ..., "code": "branch_not_supported"}` |
| Plugin GraphQL fields | GraphQL error with the same message |
| Device card | A notice that import data shows on main only |
| `ImportJobRunner`, `ResolutionProposalJob`, `SourceDocumentRetentionJob` | `JobFailed` before any domain read, if a branch is active in the worker |
| Any code path that takes `locked_profile_policy` (Custom Scripts, `nbshell`) | `BranchActive` raised before the lock |

A plugin request that carries a branch selector while no branch is active is refused the same way.
The exception is `?_branch=` with no value and no `X-NetBox-Branch` header: that is the explicit
switch to main. The header takes precedence, as it does upstream (`utilities.py:559-587`).

Without netbox-branching, every guard is a no-op and behaviour does not change.

### Owner: `netbox_data_import/branching.py`

- `active_branch()`: the active Branch, or `None` when netbox-branching is absent.
- `refuse_branch()`: raises `BranchActive` when a branch is active.
- `is_branchable(model)`: the resolver. It answers only for this plugin's models and returns
  `None` for every other model.
- `BranchRefusalMiddleware`: in `PluginConfig.middleware`. It refuses when the resolved callback's
  module is inside `netbox_data_import`. Branching activates the branch in `CoreMiddleware`'s
  request processors, which run before plugin middleware (NetBox `netbox/middleware.py:62-64`,
  `settings.py:1016-1019`).
- `register()`: called from `ready()`. When `netbox_branching` imports, it registers the resolver
  and the revert validator. It raises
  `ImproperlyConfigured` if `register_branching_resolver` is missing, or if `supports_branching()`
  disagrees with `is_branchable()` for any plugin model (an `exempt_models` entry).

### Branchability rule

`is_branchable(model)` is `True` when the plugin model has a concrete `ForeignKey` or
`OneToOneField` to a model outside the plugin that `supports_branching()` accepts, and `False`
otherwise. Today: `DeviceImportSource` (Device, CASCADE), `CableImportSource` (Cable, CASCADE),
`ClassRoleMapping` (RackType, SET_NULL).

Rows of those three: provision copies them. A core delete in the branch cascades or nulls only the
branch copy. Sync does not bring main's new rows (not change-logged; branch tables carry no foreign
keys). When sync replays a main delete, it records a synthetic DELETE for the branch copy
(`branches.py:859-897`). Merge replays the core delete in main, where Django's collector removes
main's `DeviceImportSource` and `CableImportSource` rows, or nulls `ClassRoleMapping.rack_type`. Discard drops the copy.

Every other plugin row is written only through a plugin entry point, refused in a branch, or
through a change to a global object (User, core.Job, CustomField, ObjectType), which lands in main
with its effects. Tags use `extras.TaggedItem`, which branching replicates.

### Revert

Revert replays the branch's `ObjectChange` rows only. Plugin data has none, except the synthetic
DELETE rows that sync writes for cascaded branchable rows (`branches.py:859-897`). So revert cannot
restore plugin data, and the validator refuses any revert that would need to. It refuses when
`branch.get_changes()` holds a DELETE of:

- a type a branchable plugin model references (Device, Cable, RackType), computed from the same
  `_meta` walk as the branchability rule;
- a branchable plugin model (a synthetic record);
- Tag, or `extras.TaggedItem` (a synthetic record of an assignment). NetBox's Tag delete handler
  records the removal of a tag from a non-branchable Import Profile in main, not in the branch. And
  sync does not replay that removal into the branch.

`revert()` checks `can_revert` whenever it commits, so the validator is the execution-time check. A
dry run rolls back and is not refused.

### Migrations

`fake_on_branch = True` on 0015, 0020, 0022, 0030, 0035, 0038 and 0039. The new tags migration
also gets `fake_on_branch = True`: it moves rows between main-only tables and `extras_taggeditem`.
It runs before netbox-branching can be installed alongside the plugin, so no branch misses it.

### Mechanical guards

1. A test walks the complete URL tree. For every callback whose module is inside
   `netbox_data_import`, it sends an authenticated request with a real branch active and asserts the
   409: for a UI callback, the refusal page; for a REST callback, the JSON `code`. A plugin view
   registered under any URL is covered.
2. A test computes the rule from `_meta` for every plugin model, asserts that `supports_branching()`
   agrees, and asserts that the branchable set equals a set pinned in the test. A change to the set
   fails the test. Its message says that open branches lack the new table, so the change needs a
   design decision.
3. A test fails when a migration module holds `RunPython` or `RunSQL` and `fake_on_branch is not True`.
   A migration that must run on branches is listed in the test by name, with the reason.
4. Lifecycle tests assert the branch-side effect before they assert main is unchanged.

### CI

A new `test.yaml` job: NetBox 4.7.0, netbox-branching 1.2.1, `DynamicSchemaDict`,
`BranchAwareRouter`, both plugins. It runs the branching test module, which other jobs skip through
`pytest.importorskip("netbox_branching")`. The tests provision a real branch, so they use
`TransactionTestCase`. They cover:

- the refusal of each entry point;
- that a provisioned branch holds the three plugin tables and `extras_taggeditem`;
- a Device, Cable, RackType and Tag delete in a branch: the branch copy changes, main does not;
- merge removing main's `DeviceImportSource` and `CableImportSource` rows and nulling
  `ClassRoleMapping.rack_type`, and discard keeping them;
- a revert refused after a Device, Cable, RackType or Tag delete, after a sync-recorded synthetic
  plugin-row delete, and after main deletes a Tag assigned to an Import Profile and the branch syncs
  and merges;
- stored `ObjectType.features` after `migrate`;
- startup validation under an `exempt_models` entry.

### Out of scope

- NetBox's `AsyncAPIJob` drops the branch selector (B3). Report it upstream.

## Next action

Implement in layers; each increment ends green in the new CI job.

1. **Data integrity.** The CI job with netbox-branching, `branching.py` with the resolver and startup
   validation, the tags move to `extras.TaggedItem`, and guard 2. Done when, in a provisioned
   branch, deleting a Device, Cable, RackType or Tag leaves main's plugin rows unchanged; merge
   applies the effect to main; discard keeps main as it was; and each test fails with the resolver
   removed.
2. **Refusal.** The middleware, GraphQL refusal, Device card notice, job and lock guards, and
   guard 1. Done when every plugin-owned callback returns the 409 contract in a branch, and the
   guard fails when the middleware is removed.
3. **Revert validator.** Done when each revert case in the CI list is refused, and an unrelated
   revert proceeds.
4. **Migrations.** `fake_on_branch = True` on the eight data migrations, and guard 3.

Follow-up outside this design: the upstream report on `AsyncAPIJob` dropping the branch selector.

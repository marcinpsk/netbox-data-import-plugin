# Agent instructions

Shared guidance for coding agents in this repository. Tool-specific files
(`CLAUDE.md`, and any Copilot or CodeRabbit configuration) link here instead of
repeating the rules, so there is one source of truth.

## Project overview

NetBox plugin for importing device inventory and rack layouts from external DCIM systems.
It configures import profiles (source format, column mappings, class/role mappings),
previews imports, and executes them with full result tracking.

Requires NetBox >= 4.2.0 and Python >= 3.12. Licensed under Apache-2.0 (REUSE-compliant).

## Architecture

Standard NetBox plugin pattern:

- **`catalog.py`** - Target-field catalog and policy applicability: the one source of Target Field keys
- **`adapters.py`**, **`adapter_forms.py`** - Source Adapter registry and the adapter-declared configuration forms
- **`models.py`** - ImportProfile, ColumnMapping, ClassRoleMapping, ImportJob
- **`engine.py`** - Core import logic: parse file, apply mappings, create/update NetBox objects
- **`views.py`** - CRUD for import profiles + import wizard (upload, preview, execute, results)
- **`api/`** - DRF REST API using NetBox's `NetBoxModelViewSet`/`NetBoxModelSerializer`
- **`jobs.py`** - Background job for running imports through NetBox's job system
- **`forms.py`, `tables.py`, `filters.py`, `navigation.py`, `urls.py`** - Standard NetBox UI

## Development environment

Uses a devcontainer (`.devcontainer/`) running the `netboxcommunity/netbox` Docker image.

```bash
# All aliases - type 'dev-help' inside the devcontainer
netbox-run          # foreground dev server
netbox-reload       # reinstall plugin + restart
netbox-manage migrate
netbox-manage makemigrations netbox_data_import
```

## Testing

Run Django tests inside the repository devcontainer. Use a unique PostgreSQL database and a
dedicated Redis sidecar for each task. NetBox's `RQQueueTestMixin` calls Redis `FLUSHALL`, so a
database number on the shared Redis service does not isolate one task from another.

Start a temporary Redis container on the devcontainer network. Pass its container name as
`TEST_REDIS_HOST`. Set `TEST_DB_NAME` to a unique name that starts with `test_`. The `netbox-test`
and `netbox-test-coverage` helpers size the pytest worker pool to the machine (`-n auto`) and cap it
at eight workers. Each worker gets a private PostgreSQL database and private Redis task and cache
databases. Set `NETBOX_TEST_WORKERS` to pin the count: `1` runs one worker, `0` runs the suite in
one process.

```bash
TEST_DB_NAME=test_unique_task TEST_REDIS_HOST=redis-sidecar-task netbox-test
```

JavaScript tests run on the host with `npm test` (Vitest). Browser tests run with
`npm run test:browser` (Playwright).

## Linting

```bash
ruff check .          # lint
ruff format .         # format
ruff check --fix .    # lint + auto-fix
```

## Licensing headers

Every file needs REUSE-compliant licensing. See [`docs/agents/licensing.md`](docs/agents/licensing.md).
Do not copy a year from another file: the rules there define which year to use.

## Issue tracker

Issues and specs use GitHub Issues through the `gh` CLI.
See [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md).

## Triage labels

Triage uses the five default canonical labels.
See [`docs/agents/triage-labels.md`](docs/agents/triage-labels.md).

## Domain docs

Domain documentation uses a single-context layout.
See [`docs/agents/domain.md`](docs/agents/domain.md).

## Key conventions

- All views, forms, serializers, and tables inherit from NetBox base classes.
- Use `NetBoxModel`, `NetBoxModelViewSet`, `NetBoxModelForm`, and similar. Never raw Django/DRF.
- Commits follow Conventional Commits format (enforced by a pre-commit hook).
- Never add a `Co-authored-by` trailer to commit messages.
- Schema migrations are generated artifacts. Change a model, then run
  `netbox-manage makemigrations netbox_data_import`. Do not hand-edit a generated migration.
- A data migration is written by hand, because `makemigrations` generates no `RunPython`. Start it
  with `netbox-manage makemigrations netbox_data_import --empty`. Give it no reverse callable when
  the change cannot be undone, so Django refuses the rollback instead of losing data.

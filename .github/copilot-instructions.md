# Copilot Instructions

## Project overview

NetBox plugin for importing device inventory and rack layouts from external DCIM systems.
Allows configuring import profiles (source format, column mappings, class/role mappings),
previewing imports, and executing them with full result tracking.

Requires NetBox ≥ 4.2.0 and Python ≥ 3.12. Licensed under Apache-2.0 (REUSE-compliant).

## Architecture

Standard NetBox plugin pattern:

- **`models.py`** — ImportProfile, ColumnMapping, ClassRoleMapping, ImportJob
- **`engine.py`** — Core import logic: parse file, apply mappings, create/update NetBox objects
- **`views.py`** — CRUD for import profiles + import wizard (upload → preview → execute → results)
- **`api/`** — DRF REST API using NetBox's `NetBoxModelViewSet`/`NetBoxModelSerializer`
- **`jobs.py`** — Background job for running imports via NetBox's job system
- **`forms.py`, `tables.py`, `filters.py`, `navigation.py`, `urls.py`** — Standard NetBox UI

## Development environment

Uses a devcontainer (`.devcontainer/`) running the `netboxcommunity/netbox` Docker image.

```bash
# All aliases — type 'dev-help' inside the devcontainer
netbox-run          # foreground dev server
netbox-reload       # reinstall plugin + restart
netbox-manage migrate
netbox-manage makemigrations netbox_data_import
netbox-test         # run plugin tests
```

## Linting

```bash
ruff check .          # lint
ruff format .         # format
ruff check --fix .    # lint + auto-fix
```

## Testing

```bash
netbox-test
# or: python manage.py test netbox_data_import
```

## REUSE/SPDX compliance

All source files must include SPDX headers:

```
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
```

## Key conventions

- All views, forms, serializers, tables inherit from NetBox base classes
- Use `NetBoxModel`, `NetBoxModelViewSet`, `NetBoxModelForm`, etc. — never raw Django/DRF
- Commits follow Conventional Commits format (enforced by pre-commit hook)
- Never add Co-authored-by trailer to commit messages

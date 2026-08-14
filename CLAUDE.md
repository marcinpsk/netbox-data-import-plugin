# Agent skills

## Issue tracker

Issues and specs use GitHub Issues through the `gh` CLI. See `docs/agents/issue-tracker.md`.

## Triage labels

Triage uses the five default canonical labels. See `docs/agents/triage-labels.md`.

## Domain docs

Domain documentation uses a single-context layout. See `docs/agents/domain.md`.

## NetBox tests

Run Django tests inside the repository devcontainer. Use a unique PostgreSQL database and a dedicated Redis sidecar for each task. NetBox's `RQQueueTestMixin` calls Redis `FLUSHALL`, so a database number on the shared Redis service does not provide isolation between tasks.

Start a temporary Redis container on the devcontainer network. Pass its container name as `TEST_REDIS_HOST`. Set `TEST_DB_NAME` to a unique name that starts with `test_`. The `netbox-test` and `netbox-test-coverage` helpers use eight pytest workers by default. Each worker gets a private PostgreSQL database and private Redis task and cache databases. Set `NETBOX_TEST_WORKERS=1` for a serial run.

```bash
TEST_DB_NAME=test_unique_task TEST_REDIS_HOST=redis-sidecar-task netbox-test
```

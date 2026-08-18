# Installation

```bash
pip install netbox-data-import
```

Add to `PLUGINS` in `configuration.py`:

```python
PLUGINS = ["netbox_data_import"]
```

Run migrations:

```bash
python manage.py migrate
```

## Upgrade notes

### Import Job becomes Import Execution

The Import Job history model is renamed to Import Execution. Existing history rows are kept and stay
display-only. The migration drops two columns permanently, and a rollback cannot restore them:

- `dry_run`
- `result_rows`, which holds the stored per-row results of every past import

Export those columns before you migrate if you still need them:

```bash
python manage.py dumpdata netbox_data_import.ImportJob > import-job-history.json
```

Three surfaces are renamed in the same release. Update any integration that uses them:

| Before | After |
| --- | --- |
| `GET /api/plugins/data-import/jobs/` | `GET /api/plugins/data-import/executions/` |
| `/plugins/data-import/jobs/` | `/plugins/data-import/executions/` |
| Permission `view_importjob` | Permission `view_importexecution` |

Re-grant the permission to every non-superuser group that needs the history page. NetBox resolves it
through an object permission, so edit the existing permission and add the Import Execution object
type to it.

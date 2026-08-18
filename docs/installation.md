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

If you still need those columns, export them **before you upgrade the package**. `dumpdata` reads the
model from the app registry, and the new release no longer defines `ImportJob`:

```bash
# On the old version, before you install the new package.
python manage.py dumpdata netbox_data_import.ImportJob > import-job-history.json
```

If the new package is already installed but you have not run `migrate` yet, the table still
carries its old name. Dump it with SQL instead:

```bash
psql -d netbox -c "\copy (SELECT id, dry_run, result_rows FROM netbox_data_import_importjob) \
  TO 'import-job-history.csv' WITH CSV HEADER"
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

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

### Transform patterns use RE2

The plugin now evaluates Column Transform Rule patterns with RE2. RE2 prevents a configured pattern
from consuming unbounded CPU time. It does not support Python regex backreferences or look-around.

Check existing rules before you upgrade the package. Install the new engine without replacing the
old plugin, then run this command from the NetBox application directory:

```bash
python -m pip install 'google-re2>=1.1.20251105'
python manage.py shell <<'PY'
import re2

from netbox_data_import.models import ColumnTransformRule

options = re2.Options()
options.log_errors = False
review_tokens = (r"\w", r"\W", r"\d", r"\D", r"\s", r"\S", r"\b", r"\B", "(?i")
for rule in ColumnTransformRule.objects.order_by("profile_id", "pk"):
    try:
        re2.compile(rule.pattern, options=options)
    except re2.error as error:
        print("UNSUPPORTED", rule.pk, rule.profile_id, rule.source_column, error)
        continue
    found = [token for token in review_tokens if token in rule.pattern]
    if found:
        print("REVIEW", rule.pk, rule.profile_id, rule.source_column, found)
PY
```

Replace or delete each `UNSUPPORTED` rule before you install the new plugin version. The rule form
rejects these patterns after the upgrade.

RE2 accepts the constructs in each `REVIEW` rule, but gives them ASCII semantics. Python regexes give
these constructs Unicode semantics. If a rule must match non-ASCII text, use a Unicode property or
an explicit source-specific character class. For example:

- Replace `\w` with `[\p{L}\p{N}_]`.
- Replace `\d` with `\p{Nd}`.
- Replace `\s` with a suitable class such as `[\p{Z}\t\r\n\f]`.
- Test word boundaries and case-insensitive matches with representative non-ASCII values.

Use a negated class or property for the uppercase forms. The command prints nothing when it finds no
unsupported syntax or common Unicode-sensitive constructs. Test every rule with representative
source values before you upgrade because successful compilation alone does not prove equal behavior.

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

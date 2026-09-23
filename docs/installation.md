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

RE2 accepts the constructs in each `REVIEW` rule. The `\w`, `\d`, `\s`, and `\b` families use ASCII
semantics, while Python regexes give these constructs Unicode semantics. If a rule must match
non-ASCII text, use a Unicode property or an explicit source-specific character class. For example:

- Replace `\w` with `[\p{L}\p{N}_]`.
- Replace `\d` with `\p{Nd}`.
- Replace `\s` with a suitable class such as `[\p{Z}\t\r\n\f]`.
- Test word boundaries with representative non-ASCII values.

RE2's `(?i)` flag uses Unicode simple case folding. It matches one-code-point case pairs, but does
not match expansions such as `ß` to `SS`. Test case-insensitive rules with representative non-ASCII
values.

Use a negated class or property for the uppercase forms. The command prints nothing when it finds no
unsupported syntax or common Unicode-sensitive constructs. Test every rule with representative
source values before you upgrade because successful compilation alone does not prove equal behavior.

### Cable tag integrity

Migration `0038_cable_tag_integrity` adds a foreign key from each Cable tag association to its
Cable. If a tag association names a Cable that no longer exists, the migration stops with a foreign
key violation on `ndi_taggeditem_cable_fk` and changes nothing.

Find these associations with `python manage.py dbshell` before you run `migrate`:

```sql
SELECT item.id, item.object_id, item.tag_id
FROM extras_taggeditem AS item
JOIN django_content_type AS kind ON kind.id = item.content_type_id
WHERE kind.app_label = 'dcim' AND kind.model = 'cable'
  AND NOT EXISTS (SELECT 1 FROM dcim_cable AS cable WHERE cable.id = item.object_id);
```

Each returned row tags a deleted Cable, so NetBox shows it nowhere. Delete all of them, then run
`migrate` again:

```sql
DELETE FROM extras_taggeditem AS item
USING django_content_type AS kind
WHERE kind.id = item.content_type_id
  AND kind.app_label = 'dcim' AND kind.model = 'cable'
  AND NOT EXISTS (SELECT 1 FROM dcim_cable AS cable WHERE cable.id = item.object_id);
```

The migration locks `extras_taggeditem` until it commits, so NetBox cannot read or write tags for
that time. The time grows with the number of tagged items: about 10 seconds for 5,000,000 tagged
items on PostgreSQL 18.

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

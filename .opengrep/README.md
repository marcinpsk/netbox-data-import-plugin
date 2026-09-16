<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
-->

# OpenGrep rules

Call-shape rules that ruff and mypy cannot express. Run them with `scripts/check-review-patterns`:

```bash
scripts/check-review-patterns scan   # the package; exits 1 on a finding
scripts/check-review-patterns test   # every rule against its fixture
```

Both run from pre-commit only. **Never add an OpenGrep step to CI**: CodeRabbit disables its own
OpenGrep pass for a repository whose CI already runs one. The ruleset is named `boundaries.yaml`
under `.opengrep/` for the same reason, because CodeRabbit also stands down for a root
`opengrep.yml`, `semgrep.yml`, or their variants.

The scan passes explicit file targets. OpenGrep's default ignore list drops `tests/`, and these
rules exist to check exactly that tree.

## Rules

| id | What it refuses |
| --- | --- |
| `nbdi-tests-use-public-coordinator` | a private attribute read, plain or through `getattr`, on `ImportEngine` or on any value bound from it |
| `nbdi-tests-use-public-coordinator-direct` | the same read off a fresh instance, or off the `import_engine` module itself |
| `nbdi-bounded-response-body` | a `request_to_resolved_address` call outside the tests that sets no `response_body_limit` |

Taint mode carries the first rule through any binding form, so an alias, a tuple target, a walrus,
an attribute target, and an `as` import are all covered without enumerating them. Its sink accepts
`getattr` too, because a bound receiver puts that form out of the second rule's reach. It is
intra-procedural, so a same-named variable in another function is not a false positive. These
replaced a hand-written AST scanner in `tests/test_module_boundaries.py` that missed all of those
forms and had no notion of scope.

## Adding a rule

One rule per class, one fixture named after the ruleset stem, carrying `# ruleid:` and `# ok:`
annotations. Prove it red on real code and green on the fixed tree before committing. Fixtures are
excluded from ruff: they are the violations, not source.

# Licensing headers (REUSE/SPDX)

This repository is REUSE-compliant. Every file carries licensing information, either as a
header in the file or through a path entry in `REUSE.toml`.

## Which year to use

Use **the year the file was first added**, which is the current year for a file you are
creating now. Never hardcode a year from an example, and never copy the header from an
existing file without changing the year.

Do **not** rewrite the year in a file that already exists. The copyright year records when
the content was authored, so bumping it on every edit is wrong and produces pointless diffs.
A file added in 2025 keeps `2025` even when it is edited later.

This means a pull request that adds new files will contain years that differ from the rest of
the tree. That is expected, not a mismatch to fix.

## Header format

Both tag styles below are valid REUSE and both appear in this tree. Use
`SPDX-FileCopyrightText` in new files; it is REUSE's own tag. Leave the older
`Copyright (C)` headers alone where they already exist.

Python, YAML, and other `#`-comment files:

```python
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: <year> Marcin Zieba <marcinpsk@gmail.com>
```

JavaScript and CSS:

```js
/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: <year> Marcin Zieba <marcinpsk@gmail.com> */
```

Django templates:

```django
{# SPDX-License-Identifier: Apache-2.0 #}
{# SPDX-FileCopyrightText: <year> Marcin Zieba <marcinpsk@gmail.com> #}
```

## Files covered by REUSE.toml

Some paths carry no inline header and are annotated in `REUSE.toml` instead: generated code,
lockfiles, fixtures, documentation, and `netbox_data_import/migrations/**`. Do not add headers
to files in those paths. Add the path to `REUSE.toml` when you introduce a new category of
generated or vendored file.

## Checking

The `reuse-lint` pre-commit hook gates every commit. Run it directly with:

```bash
uvx --native-tls reuse lint
```

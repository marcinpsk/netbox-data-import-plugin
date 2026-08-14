<!--
SPDX-License-Identifier: Apache-2.0
Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
-->
# Data Import

This context translates source-file rows into reviewed NetBox changes. Import profiles describe reusable source formats, while row resolutions preserve decisions that depend on one source row.

## Language

**Import Profile**:
A reusable description of one source-file format and its NetBox synchronization policy.

**Source Column**:
A named column in a source file.

**Target Field**:
A semantic NetBox value that the import can resolve or synchronize, including a field nested within a NetBox object such as a Contact email address.

**Direct Mapping**:
An import-profile rule that asserts one source column always supplies one target field.
_Avoid_: Hard mapping

**Candidate Mapping**:
An import-profile rule that makes one or more source columns eligible to supply a target field without asserting which value is correct for every row.
_Avoid_: Contact candidates, fallback columns

**Row Resolution**:
A saved decision that selects or derives target-field values for one source row. The import reapplies the decision when that row appears in a later file.
_Avoid_: Override, exception

<!--
SPDX-License-Identifier: Apache-2.0
Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
-->
# Data Import

This context translates source-file rows into reviewed NetBox changes. Import profiles describe reusable source formats, while row resolutions preserve decisions that depend on one source row.

## Language

**Import Profile**:
A reusable description of one source-file format and its NetBox synchronization policy.

**Import Plan**:
The reviewable set of NetBox changes and no-ops derived from one source file and the current NetBox state. Preview and execution use the same accepted plan.

**Synchronization Unit**:
The smallest independently reviewable and executable part of an Import Plan. It can originate from one source row or one complete Source Trace, and all its changes commit together.
_Avoid_: Executable row

**Planned Change**:
One target-specific NetBox mutation within a Synchronization Unit. It has a stable identity and records the target state that must remain unchanged before execution.
_Avoid_: Row action

**Import Execution**:
An audited attempt to apply selected Synchronization Units from an accepted Import Plan as one transaction.
_Avoid_: Import run

**Source Column**:
A named column in a source file.

**Source Batch**:
The typed source items and source diagnostics produced by one source adapter from one imported file. It contains no NetBox target matches or planned writes.
_Avoid_: Parsed rows

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

**Resolution Proposal**:
An unaccepted suggestion of Target Field values. It has no authority to change NetBox; accepted values become a Row Resolution.
_Avoid_: AI answer, automatic resolution

**Source Trace**:
An end-to-end connectivity path described by a source report. It can confirm an existing direct physical Cable or provide intermediate patching details; it is not a single NetBox object.
_Avoid_: Cable row

**Patched Path Replacement**:
A reviewed change that replaces one direct LLDP-derived Cable with physical Cable segments and patch-panel pass-throughs. The resulting path terminates on the same two endpoint Devices.
_Avoid_: Cable amendment

**Cable Type**:
The fixed NetBox Cable value that describes the physical medium or classification of one Cable segment.

**Cable Profile**:
The fixed NetBox Cable value that describes connector and lane topology within one Cable. It does not define reusable Cable creation defaults and cannot be created by the importer.

**Field Difference**:
One Target Field whose resolved Source Row value differs from the value on its matched NetBox object.

**Ignored Field Difference**:
A saved decision to preserve the NetBox value for one Field Difference while the exact Source Row value and NetBox value pair remains unchanged. If either value changes, the Target Field becomes a Field Difference again.
_Avoid_: Ignored field

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
-->

# Public refusal messages

## Decision

Public responses read authored refusal text through `PublicRefusal.operator_message`.
They do not convert the caught exception to a string. HTTP views and the Job formatter share
this model-free interface. Existing refusal constructors retain their message, status and reason. An unknown Source Adapter
retains its authored adapter name so the operator can repair the profile.

The Job formatter uses fixed text for database errors, unreadable plans, object-permission
failures, source-reader failures and unexpected exceptions. Django validation errors expose their
field messages. Internal source-planning failures remain in the server log.

An independent blind design and an adversarial design review accepted this interface. Its
acceptance conditions are preserved HTTP reasons and statuses, private internal failure details,
zero results from the standard Python CodeQL security suite, and the full native validation gates.

## Alternatives

Fixed text at every catch would erase useful refusal reasons. Reading `Exception.args` would
couple responses to diagnostic storage. Separate constructors with the same text attribute would
duplicate the interface across refusal types. A shared exception owns this contract once.

## Mechanical guard and limits

The existing response scanner rejects `str`, `repr` and `args` access for public refusal types.
It permits their public message, status and reason. Selected source fixtures cover these bad
cases and a valid public-message read. The full-tree scan runs in the normal test suite.

This is a bounded syntax check. It trusts the listed response helpers and does not follow
arbitrary calls across modules. The existing curated-type policy remains for unrelated plugin
exceptions. The guard does not prove that every constructor received safe text. Behavioral
request tests and CodeQL cover the affected HTTP and Job consumers.

---
status: accepted
date: 2026-08-17
---

# Source Trace identity comes from its endpoints

A Source Trace arrives as repeated workbook blocks, and the same physical path can be re-exported with changed patching, in either direction, or duplicated. Its Synchronization Unit identity must stay stable through all of that.

The identity of a Source Trace is the unordered pair of its two endpoint Termination References, each under the name identity every other name comparison uses (specification section 5.9), and ordered lexicographically by code point. The ordered path evidence, CableClass labels, and Pass-Through Claims form a separate direction-independent content fingerprint. Two ports carry one physical path, so the endpoint pair is the natural stable identity: a re-export with changed patching keeps the same Synchronization Unit and changes only the fingerprint. The source From/To direction is provenance and display data only.

## Considered options

- Full-path identity: rejected because any patching change would create a new unit identity, which breaks replanning stability and re-import matching.
- Endpoint identity without a content fingerprint: rejected because a changed physical claim would go undetected until planning diffs.

## Amendment (2026-10-01): one uppercase name identity

The endpoint names were casefolded in Python, while PostgreSQL compared NetBox names in uppercase, and the two forms disagree for some characters. Now one definition in `netbox_data_import/identity.py` serves both sides: explicit whitespace collapsed and trimmed, then the full Unicode uppercase mapping. It changes two equivalence classes. `ẞ`, the Kelvin, Angstrom and Ohm signs, `ϴ` and `İ` no longer share a key with the letter they casefold to, and `ı` now shares the key of `i`. Uppercase also sorts `[`, `\`, `]`, `^`, `_` and `` ` `` after the letters, so the canonical order of some endpoint pairs reverses. Migration `0045_rekey_name_identities` rekeys every stored identity, merges or drops colliding decisions, and states an unknown segment position where the order reversed. The installation upgrade notes list what an operator has to choose again.

---
status: accepted
date: 2026-08-17
---

# Source Trace identity comes from its endpoints

A Source Trace arrives as repeated workbook blocks, and the same physical path can be re-exported with changed patching, in either direction, or duplicated. Its Synchronization Unit identity must stay stable through all of that.

The identity of a Source Trace is the unordered pair of its two endpoint Termination References, normalized like every other source identity comparison (trim, collapse whitespace, casefold) and ordered lexicographically. The ordered path evidence, CableClass labels, and Pass-Through Claims form a separate direction-independent content fingerprint. Two ports carry one physical path, so the endpoint pair is the natural stable identity: a re-export with changed patching keeps the same Synchronization Unit and changes only the fingerprint. The source From/To direction is provenance and display data only.

## Considered options

- Full-path identity: rejected because any patching change would create a new unit identity, which breaks replanning stability and re-import matching.
- Endpoint identity without a content fingerprint: rejected because a changed physical claim would go undetected until planning diffs.

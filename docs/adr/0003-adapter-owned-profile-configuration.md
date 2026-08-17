---
status: accepted
date: 2026-08-17
---

# Import Profile configuration is adapter-owned

ImportProfile accreted device-format columns (sheet name, source-ID wiring, contact policy) that do not apply to other source formats. Each new adapter would widen the shared table with mostly-null fields.

An Import Profile selects exactly one Source Adapter from a static in-plugin registry, and the selection is immutable after creation. Scalar adapter settings live in one `adapter_config` JSON field that a form declared by the selected adapter validates at the boundary. Relational policy models stay separate tables, each tagged with the adapter output and target it applies to, and a profile rejects inapplicable rows. Object references inside `adapter_config` use natural keys, validated at the form boundary and again at planning; a dangling reference blocks the affected units. This replaces database-level PROTECT and keeps YAML profile export portable across NetBox instances. The cutover is one migration with no compatibility path.

## Considered options

- Typed nullable columns per adapter: rejected because every future adapter widens the shared table and scatters applicability rules.
- A separate config table per adapter: rejected as heavy for a handful of scalars with no relational consumers.
- Database ids for references inside the JSON: rejected because YAML exports would carry instance-local ids that dangle on import elsewhere.

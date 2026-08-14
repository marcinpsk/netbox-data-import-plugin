# Domain docs

This file defines how engineering skills consume this repo's domain documentation.

## Before exploring, read these

- Read `CONTEXT.md` at the repo root.
- If `CONTEXT-MAP.md` exists at the repo root, read it and each relevant context file.
- Read ADRs under `docs/adr/` that affect the area under investigation.

If a file does not exist, proceed silently. Do not propose creating it in advance. The `/domain-modeling` skill creates domain files when the project resolves terms or decisions.

## File structure

This repository uses a single-context layout:

```text
/
├── CONTEXT.md
└── docs/
    └── adr/
        ├── 0001-example-decision.md
        └── 0002-another-decision.md
```

## Use the glossary vocabulary

When output names a domain concept, use the term defined in `CONTEXT.md`. This applies to issue titles, refactor proposals, hypotheses, and test names. Do not replace defined terms with synonyms.

If the glossary does not define a required concept, reconsider whether the term belongs to the project. If it does, note the gap for `/domain-modeling`.

## Flag ADR conflicts

If output contradicts an existing ADR, report the conflict explicitly instead of silently overriding the decision.

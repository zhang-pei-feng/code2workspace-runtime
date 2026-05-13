---
name: data-governance-ops
description: Handle source governance briefs, snapshot refresh, latest snapshot queries, history compare, quality triage, and optional local database inspection for biological data governance tasks. Use when the user asks what a source is, how to refresh or inspect a latest snapshot, what changed between snapshots, which records have quality issues, or whether a local governance import database can answer a question.
---

# Data Governance Ops

Use this skill for governance-style data-source operations.

## Capabilities

- source brief / onboarding facts
- refresh latest snapshot
- query latest snapshot by field
- compare latest vs previous snapshot
- triage basic quality issues
- optional local DB SQL inspection when local DB credentials exist

## Entry point

Run the local helper directly:

```bash
python3 skills/capabilities/data-governance-ops/scripts/governance_ops.py --help
```

## Rules

1. Use only the local helper shipped in this repository.
2. Do not call the old `data_governance_agent` ACP runtime.
3. Save outputs under `results/skills/data-governance-ops/...`.
4. If a live path is unsupported or unavailable, say so plainly and fall back to
   registered sample metadata only when appropriate.
5. Never fabricate local DB results; if DB configuration is missing, return a
   structured error.

## Typical flows

- Source brief:
  `python3 skills/capabilities/data-governance-ops/scripts/governance_ops.py source-brief --source ncbi_virus`
- Refresh snapshot:
  `python3 skills/capabilities/data-governance-ops/scripts/governance_ops.py refresh --source ncbi_virus --limit 5`
- Query latest:
  `python3 skills/capabilities/data-governance-ops/scripts/governance_ops.py query-latest --source ncbi_virus --field accession --value PZ`
- Compare snapshots:
  `python3 skills/capabilities/data-governance-ops/scripts/governance_ops.py compare --source ncbi_virus`
- Quality triage:
  `python3 skills/capabilities/data-governance-ops/scripts/governance_ops.py quality --source ncbi_virus`

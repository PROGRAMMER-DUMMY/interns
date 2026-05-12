---
name: stakeholder-memory
description: >
  Capture durable user, team, and stakeholder preferences discovered during interviews or corrections.
  Use when the user states how they prefer decisions, reviews, risk handling, output style, naming,
  governance, or optimization tradeoffs.
---

# Stakeholder Memory

Store preferences that should affect future decisions. Do not store secrets, raw data, credentials,
or private personal information unrelated to the project.

## What To Capture

- User decision style: recommendation-first, options-first, source-backed, concise, detailed.
- Risk tolerance: correctness before speed, human approval required, rollback expectations.
- Team priorities: data team, business team, platform team, security team.
- Naming and domain vocabulary preferences.
- Review style: what evidence humans want before accepting a candidate.
- Repeated corrections: assumptions the user rejected.

## Output

Write or update:

```text
workspaces/<project>/interns/generated/memory/preferences.json
workspaces/<project>/interns/generated/memory/decision_history.md
```

Use append-only decision history. For JSON, merge conservatively and keep source notes.

## Schema

```json
{
  "user_preferences": {},
  "team_preferences": {},
  "decision_style": {},
  "rejected_assumptions": [],
  "source_notes": []
}
```

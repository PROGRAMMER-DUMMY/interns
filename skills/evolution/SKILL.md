---
name: evolution
description: >
  Learn from stakeholder interviews, user corrections, accepted decisions, rejected assumptions,
  optimization outcomes, and failed attempts. Use after meaningful project work, after user feedback,
  after governance decisions, or when patterns should improve future onboarding and optimization.
---

# Evolution

Capture durable lessons so future work on the same project starts smarter.

## Step 0: Active Workflow Setup

Before recording lessons, confirm which project/workspace the lesson belongs to. If unclear, inspect
`config/tasks.json`, recent files, and `workspaces/`, then ask the user to confirm the target
workspace.

## Capture Signals

- User corrections: "no", "not that", "actually", "instead", "my plan is".
- Accepted recommendations.
- Rejected assumptions.
- Stakeholder preferences and review expectations.
- KPI/data model conflicts discovered.
- Optimization attempts that improved or degraded performance.
- Governance decisions and why they happened.
- Repeated tool/test failures and the eventual fix.

## Record Format

Append to:

```text
workspaces/<project>/interns/generated/memory/evolution.md
```

Maintain or update:

```text
workspaces/<project>/interns/generated/memory/lessons.json
```

Use this structure:

```markdown
## [YYYY-MM-DD] [Topic]

**Trigger**: [What happened]
**Assumption**: [What was assumed, if any]
**Outcome**: [Accepted / rejected / failed / improved / degraded]
**Lesson**: [What to do next time]
**Applies To**: [requirements / KPI / data model / governance / optimization / git / UI]
```

## Rules

- Do not store secrets or raw data.
- Do not record unconfirmed guesses as facts.
- Prefer lessons tied to evidence, user correction, or successful verification.
- Keep lessons project-scoped unless they clearly apply across the whole platform.

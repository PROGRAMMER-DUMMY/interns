---
name: to-solution-brief
description: >
  Convert stakeholder interviews, KPI registry details, data model facts, and preferences into a
  concrete solution brief for a governed optimization task. Use after grill-requirements and
  domain-model have enough information.
---

# To Solution Brief

Generate a brief that is specific enough for implementation, evaluation, and governance.
If a required section is unknown, return to `grill-requirements` for the highest-impact missing
decision.

## Template

```markdown
# Solution Brief: [project/task]

## Problem
[What is slow, costly, incorrect, manual, or hard to govern.]

## Stakeholders
[Business/data/platform/security owners and review responsibilities.]

## Inputs
[Data, KPI registry, data model, source artifacts.]

## Optimization Target
[SQL, Polars, hybrid, workflow, prompt, or other scoreable artifact.]

## Semantic Guardrails
[KPI formulas, grain, tolerances, required columns, forbidden changes.]

## Success Metrics
[Primary metric, secondary metrics, SLA, correctness threshold.]

## Approval And Rollback
[Human review, evidence required, rollback expectations.]

## Out Of Scope
[Explicit exclusions.]

## Open Questions
[Only unresolved items that block implementation or promotion.]
```

## Output

```text
workspaces/<project>/interns/generated/requirements/solution_brief.md
```

---
name: grill-requirements
description: >
  Interview stakeholders to understand what they want optimized, what must not change, how success
  is measured, and what preferences or constraints should shape the solution. Use for new workspace
  onboarding, KPI/data model discovery, product scoping, or when business/data/platform requirements
  are incomplete.
---

# Grill Requirements

Interview one decision at a time until the project can be turned into a governed optimization task.
If a fact is discoverable from files, configs, KPI registries, data models, or code, inspect it
instead of asking.

## Step 0: Active Workflow Setup

Before requirements grilling, establish the active workflow:

1. Ask what the user wants to do right now.
2. Ask the user to point to the current active workflow/project if unclear.
3. Scan likely files under `workspaces/`, `config/`, and relevant source/test folders.
4. Summarize the likely active file set.
5. Ask for confirmation before continuing.

Confirmation prompt:

```text
I found this likely workflow: [workspace/files]. Should I use these for the interview and task setup?
```

## Order

1. Business goal and current pain.
2. Stakeholders and review/approval owners.
3. Data sources, catalog paths, and access constraints.
4. KPI registry, formulas, grain, tolerances, and non-negotiable guardrails.
5. Data model: facts, dimensions, keys, joins, cardinality, lineage.
6. Missing dictionaries, metadata files, catalog paths, contracts, or SLA files required for safe
   KPI-to-column mapping.
7. Optimization target: SQL, Polars, pipeline, prompts, workflow, or hybrid.
8. Success metrics: correctness, runtime, cost, SLA, maintainability.
9. Failure policy: fail-closed vs fail-soft by domain.
10. Human workflow: who reviews, what evidence they need, how rollback works.
11. Preferences: naming, output style, risk tolerance, explanation depth.

## Question Shape

Ask exactly one question. Include recommended answer and why.

```markdown
Question: ...

Options:
- Option A: ...
- Option B: ...

Recommended answer: ...

Why: ...
```

## Output

Record findings under:

```text
workspaces/<project>/interns/generated/requirements/
  stakeholder_interview.md
  requirements.json
workspaces/<project>/interns/reports/
  open_questions.md
```

`stakeholder_interview.md` should include the conversation summary, task options
shown to the user, recommended answers, accepted decisions, rejected options, and
remaining unresolved questions.

User answers that resolve KPI mappings, formula-derived features, temporal anchors, policy, SLA, or
contract questions must be recorded as accepted decisions before implementation relies on them.

Do not write outputs outside `workspaces/<project>/interns/`.

## Mode: Self-Grill (merged from the self-grill skill)

Before committing to a plan, design recommendation, or implementation approach,
turn the interrogation inward: generate 3-6 grilling questions tailored to the
current proposal, answer each with concrete evidence (file paths, samples,
prior decisions), and surface any unknown-unknowns. Emit a short Self-Grill
Audit block BEFORE the final recommendation. Trigger before "what's the best
approach for X" / "should I use A or B" style answers; skip for trivial or
mechanical responses.

## Mode: Clarify Ambiguity (merged from the clarify-ambiguity skill)

Clarify only when a wrong assumption would materially change the answer or
cause wasted work. If ambiguity is low-risk and reversible, proceed with the
most likely interpretation and STATE the assumption. For KPI/query mappings,
derivation formulas, temporal anchors, policy/SLA/contract, or
production-impacting choices, never proceed from assumption: inspect available
context first (files, configs, tests, panels, profiles), then ask ONE targeted
question before generating executable logic. Ask only for intent, preference,
permissions, or facts that are genuinely unavailable.

# Gemini Skill Adapter

This file is generated from canonical repo skills. Do not hand-edit it.

## Routing Rules

- Treat `skills/*/SKILL.md` as the source of truth.
- If the user explicitly names `$skill-name` or `skill-name`, load that skill.
- Otherwise match the request to skill descriptions and load the smallest relevant skill set.
- If multiple skills match, order them by dependency and keep context minimal.
- If local file access is available, open the listed `SKILL.md` before applying a skill.
- If local file access is unavailable, use embedded bodies only when this adapter was generated with full embedding.

## Available Skills

### clarify-ambiguity

- Path: `skills/clarify-ambiguity/SKILL.md`
- Description: Use when a request is underspecified, ambiguous, assumption-heavy, or likely to produce a wrong, unsafe, costly, or irrelevant answer without clarification. Trigger when missing context materially affects correctness, safety, user intent, implementation choices, or recommendation quality. Do not trigger for clear requests or minor ambiguities that can be handled by stating a reasonable assumption.

### data-engineering-pipeline-design

- Path: `skills/data-engineering-pipeline-design/SKILL.md`
- Description: Design source-to-target SQL, Polars, PySpark, ETL/ELT, and medallion-layer workflows from KPI requirements, data model evidence, profiles, and accepted workspace definitions.

### databricks-access-gates

- Path: `skills/databricks-access-gates/SKILL.md`
- Description: Use when Databricks work hits or may hit missing permissions, token scopes, Unity Catalog grants, workspace API access, SQL warehouse paths, storage policies, compute policies, Genie/dashboard/job creation permissions, data registration approvals, or any remote mutation gate. Ask the user for the exact missing access or approval before retrying remote Databricks actions.

### domain-model

- Path: `skills/domain-model/SKILL.md`
- Description: Align work with the project's domain language, KPI registry, data model, schema, relationships, grain, and business rules. Use before generating contracts, task configs, optimizations, or reports.

### evolution

- Path: `skills/evolution/SKILL.md`
- Description: Learn from stakeholder interviews, user corrections, accepted decisions, rejected assumptions, optimization outcomes, and failed attempts. Use after meaningful project work, after user feedback, after governance decisions, or when patterns should improve future onboarding and optimization.

### feature-derivation-library

- Path: `skills/feature-derivation-library/SKILL.md`
- Description: Use when KPI/query work needs reusable derived-feature patterns, candidate formulas, temporal anchors, join-derived features, taxonomy-derived features, or SQL/Polars derivation templates. This skill helps propose derivations while preserving the rule that candidates are not proof.

### grill-requirements

- Path: `skills/grill-requirements/SKILL.md`
- Description: Interview stakeholders to understand what they want optimized, what must not change, how success is measured, and what preferences or constraints should shape the solution. Use for new workspace onboarding, KPI/data model discovery, product scoping, or when business/data/platform requirements are incomplete.

### stakeholder-memory

- Path: `skills/stakeholder-memory/SKILL.md`
- Description: Capture durable user, team, and stakeholder preferences discovered during interviews or corrections. Use when the user states how they prefer decisions, reviews, risk handling, output style, naming, governance, or optimization tradeoffs.

### task-onboarding

- Path: `skills/task-onboarding/SKILL.md`
- Description: Turn a workspace project with data, KPI registry, data model, and source artifacts into a runnable optimization task. Use when adding a new project under workspaces/ or refreshing task config, contracts, profiles, and baseline setup.

### to-solution-brief

- Path: `skills/to-solution-brief/SKILL.md`
- Description: Convert stakeholder interviews, KPI registry details, data model facts, and preferences into a concrete solution brief for a governed optimization task. Use after grill-requirements and domain-model have enough information.

### workspace-governance

- Path: `skills/workspace-governance/SKILL.md`
- Description: Enforce workspace safety: keep project outputs under workspaces/<project>/interns/, avoid pushing raw data or generated artifacts, prevent secret leakage, and check staged files before commit/push. Use before git add/commit/push and whenever workspace files are modified.

### workspace-kpi-query-optimizer

- Path: `skills/workspace-kpi-query-optimizer/SKILL.md`
- Description: Build, validate, and optimize query logic for any workspace that contains data, a KPI/metric registry, and a data model. Use for SQL, Polars, or hybrid KPI/query optimization tasks where generated outputs must live under workspaces/<project>/interns/.

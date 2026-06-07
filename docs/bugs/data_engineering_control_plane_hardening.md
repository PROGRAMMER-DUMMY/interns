# Data Engineering Control Plane Hardening Brief

Date: 2026-05-24

Scope: Project-level hardening based on the end-to-end
`workspaces/Healthcare-RCM-Data-Platform` RCM session and follow-up architecture
review.

Status: Proposed implementation plan

## Summary

The project is a governed optimization control plane, but the RCM workflow exposed
that the current path is still too close to KPI/query generation. It can produce
executable SQL before fully proving catalog identity, layer state, source truth,
cleaning rules, grain, transformations, aggregation formulas, and harness coverage.

The required direction is to harden the system into separate governed tracks that
share contracts and harnesses:

- `kpi_only`
- `etl`
- `elt`
- `medallion`
- `oltp_ingestion`
- `existing_gold_validation`

All tracks must share catalog contracts, profile evidence, source-truth contracts,
relationship and grain contracts, evidence-order guardrails, full review panels,
and proof packets.

## Accepted Decisions

1. Use separate governed tracks, not one forced workflow.
2. Add a catalog-first gate before code generation.
3. Support external workspaces and remote warehouses as local-first, explicit
   deployment targets.
4. Treat catalog objects as dual-mode: logical contracts plus optional physical
   bindings.
5. Generated transform/KPI code must not reference raw paths outside
   ingestion/bootstrap stages.
6. For raw-file starts, ask table format per workflow and recommend Delta for
   Databricks/Spark, Iceberg for open lakehouse, and local DuckDB/parquet for
   smoke tests.
7. Build bronze/silver/gold only when the selected track requires pipeline
   delivery.
8. Continue from the highest trusted existing layer when silver/gold already
   exists.
9. Existing layers are trusted only with contracts, profile/schema evidence,
   lineage, grain, freshness, quality checks, and passing harnesses.
10. Cleaning rules are profile-driven, but business-impacting rules need proof or
    approval.
11. Only reversible/type-safe technical normalization can run automatically.
12. Deduplication detection is automatic, but application is approval-gated.
13. Preserve normalized entities in bronze/silver and denormalize only into
    approved analytical silver views or gold outputs.
14. Every table/view must declare grain, key candidates, duplicate behavior, and
    join cardinality expectations.
15. Aggregations must come from source truth or approved business rules.
16. Ambiguous percentage/ratio denominators block code generation.
17. Use a layered harness across catalog, bronze, silver, gold, and KPI outputs.
18. Auto-loop can fix implementation defects inside approved contracts only.
19. Remote writes require dry-run deployment plans and explicit approval.
20. Proof packets must include end-to-end source truth, layer, code, harness, and
    risk evidence.
21. Cross-agent behavior must be enforced with tool-evidence harnesses.
22. Chat review mode must show full panels, not compact one-line prompts.
23. Add strict evidence-order and time-budget guardrails.
24. Guardrail violations must fail and recover from the deterministic next step.
25. First milestone is the contract and guardrail foundation.
26. The first milestone is done only when enforcement and tests exist.

## Bugs And Gaps

### BUG-DE-001: No Catalog Contract Before Code Generation

Severity: Critical

Finding:
Generated code can reference raw workspace paths such as CSV files directly. That
is acceptable only in ingestion/bootstrap or local smoke-test adapters, not in
business transformations, KPI logic, silver logic, gold logic, or production
pipeline code.

Impact:

- Non-portable generated code.
- Weak lineage and layer ownership.
- Bypasses profile/schema contracts.
- Hard to switch to Delta, Iceberg, Databricks, or a warehouse catalog.
- Makes local file paths the data interface instead of governed logical objects.

Fix Direction:

- Generate `catalog_contract.json` and `catalog_contract.md` from workspace
  inventory, profiles, schemas, accepted scope, and physical bindings.
- Require code generators to use logical catalog object names.
- Allow raw paths only in ingestion/bootstrap adapters.
- Add a harness rule that fails generated transform/KPI code containing raw paths.

Acceptance Criteria:

- Catalog contracts are generated before source-to-target or pipeline code.
- KPI, silver, gold, and transform code use logical catalog objects.
- Raw paths appear only in ingestion/bootstrap code.
- Tests fail when raw paths appear in business logic.

### BUG-DE-002: Missing Data Engineering Route Gate

Severity: Critical

Finding:
The workflow does not first choose whether the user is doing KPI-only proof, ETL,
ELT, medallion, OLTP ingestion, or existing-layer validation.

Impact:
The system can skip required pipeline stages or rebuild layers unnecessarily.

Fix Direction:
Add a route panel that asks or infers:

- start point: raw, bronze, silver, gold, external catalog, unknown
- workflow track: KPI-only, ETL, ELT, medallion, OLTP ingestion, existing gold
- target language/runtime: SQL, Polars, PySpark, hybrid
- table format: Delta, Iceberg, local/parquet, warehouse native

Acceptance Criteria:

- Every code-generation path records a selected track.
- Existing gold/silver paths validate and continue instead of rebuilding by
  default.
- Raw-file medallion paths require bronze/silver/gold contracts.

### BUG-DE-003: Pipeline Middle Is Not First-Class

Severity: Critical

Finding:
The RCM flow jumped from profiles and mappings to KPI SQL. It did not explicitly
model cleaning, casting, deduplication, normalization, denormalization,
transformation, aggregation, quarantine, or layer quality checks.

Fix Direction:
Add `pipeline_plan.json` and `pipeline_plan.md` with:

- cleaning rules
- type normalization rules
- dedup candidates and approved dedup rules
- normalized source entities
- denormalized analytical views
- bronze/silver/gold outputs
- aggregation contracts
- quality gates
- harness plan

Acceptance Criteria:

- ETL, ELT, and medallion tracks produce a pipeline plan before code.
- Pipeline plans include cleaning, dedup, transform, aggregation, and layer checks.
- Code generation is blocked when required pipeline contracts are missing.

### BUG-DE-004: Bronze/Staging Table Format Is Not Governed

Severity: High

Finding:
Raw CSVs are queried directly. For production-like medallion workflows, raw files
should be ingested into governed bronze/staging objects.

Fix Direction:
Add a bronze table format decision:

- Delta Lake for Databricks/Spark-first
- Iceberg for open lakehouse
- local DuckDB/parquet for smoke tests
- warehouse-native table when the target warehouse is explicit

Bronze objects should include audit metadata such as source file, ingest time,
source row number, source hash, batch id, schema version, and quarantine reason.

Acceptance Criteria:

- Raw-file medallion/ETL/ELT tracks define bronze targets.
- Local-only KPI proof can use local catalog views without pretending they are
  production bronze.
- Remote bronze writes require a dry-run deployment plan and approval.

### BUG-DE-005: Existing Layer Trust Is Undefined

Severity: High

Finding:
The workflow does not define what makes an existing silver or gold layer trusted
enough to continue from.

Fix Direction:
Trust requires:

- catalog contract
- schema/profile evidence
- lineage or source mapping
- declared grain
- quality checks
- freshness metadata
- passing validation harness

Acceptance Criteria:

- Existing silver/gold layers are validated before downstream use.
- If trust checks fail, the workflow blocks or falls back to earlier layers.
- User confirmation alone does not replace machine-checkable evidence.

### BUG-DE-006: Workbook KPI Truth Was Not Enforced Early Enough

Severity: Critical

Finding:
The RCM session initially generated SQL that passed execution but violated
`docs/Sample KPI.xlsx`:

- KPI 1 used row counts instead of `SUM(PaidAmount)`.
- KPI 1 missed Medicare, Age > 50, and monthly ServiceDate trend semantics.
- KPI 2 returned rows instead of percentage distinct lives.
- KPI 3 counted rows instead of Commercial top 10 by `SUM(PaidAmount)`.

Fix Direction:
Treat workbook/KPI registry truth as a source-truth contract. Preserve metric
rows, cut rows, continuation rows, filters, dimensions, and time grain.

Acceptance Criteria:

- Harness rejects metric substitutes such as `COUNT(*)` for `SUM(PaidAmount)`.
- Harness rejects missing filters and time grains.
- KPI proof packet displays workbook source truth beside generated logic.

### BUG-DE-007: Percentage Denominator Ambiguity Is Not Blocked

Severity: High

Finding:
KPI 2 percentage share can mean global total, within department, within gender,
within visit type, or selected population.

Fix Direction:
If denominator scope is not proven by source truth, ask a blocker panel with
concrete denominator options. Do not generate production code until resolved.

Acceptance Criteria:

- Ambiguous ratio/percentage KPIs block code generation.
- Panel options preserve denominator ids and business summaries.
- Accepted denominator becomes a reusable KPI contract.

### BUG-DE-008: Grain Contracts Are Not Required Everywhere

Severity: High

Finding:
Tables and outputs can be used downstream without explicit grain, key candidates,
duplicate behavior, or join cardinality expectations.

Fix Direction:
Require grain contracts for catalog objects, bronze, silver, gold, and KPI
outputs.

Acceptance Criteria:

- Every generated table/view has declared grain.
- Join harness checks detect row multiplication against expected cardinality.
- Duplicate behavior is explicit before aggregations are trusted.

### BUG-DE-009: Deduplication Is Not Governed

Severity: High

Finding:
Duplicates are not first-class profile/harness findings, and no approval boundary
exists for dropping or merging duplicate-looking rows.

Fix Direction:
Detect duplicates automatically, propose dedup keys/rules, but require source
contracts, data model proof, or user approval before applying deduplication.
Applied dedup should produce cleaned outputs and duplicate/rejected evidence.

Acceptance Criteria:

- Duplicate candidates are reported per layer.
- Dedup application requires approval or proof.
- Rejected/duplicate rows are traceable.

### BUG-DE-010: Cleaning And Semantic Transformation Boundaries Are Blurred

Severity: High

Finding:
The workflow does not clearly distinguish type-safe technical normalization from
business-changing transformations.

Fix Direction:
Allow automatic technical normalization such as trimming strings, empty-to-null,
date parsing with failure reporting, numeric casting with quarantine, internal
alias normalization, and audit metadata. Require approval for category mapping,
deduplication, imputation, payer attribution, age bands, denominator selection,
and business filters.

Acceptance Criteria:

- Technical cleaning can run with reported failures.
- Business-impacting transformations appear in review panels.
- Quarantined rows are counted and traceable.

### BUG-DE-011: Normalization And Denormalization Are Not Explicit

Severity: Medium

Finding:
The flow can join operational entities directly into KPI SQL without a declared
decision about normalized source preservation versus analytical denormalization.

Fix Direction:
Preserve normalized source entities in bronze/silver. Denormalize only into
approved analytical silver views or gold outputs with grain and lineage.

Acceptance Criteria:

- Gold outputs expose business labels such as department name when required.
- Key-only outputs are rejected when the KPI asks for business names.
- Denormalized views declare lineage and join assumptions.

### BUG-DE-012: Layered Harness Is Missing

Severity: Critical

Finding:
Execution success alone can pass code that is semantically wrong.

Fix Direction:
Add layered harness checks for:

- catalog contracts
- bronze schema/types/audit metadata
- silver cleaning/dedup/conformance
- gold grain/metric formulas
- KPI source-truth alignment
- joins and cardinality
- result shape and sample outputs
- freshness and unresolved risks

Acceptance Criteria:

- Final output harness cannot pass if upstream layer checks fail.
- Harness distinguishes hard failures from warnings.
- RCM-style metric mistakes are hard failures.

### BUG-DE-013: Auto-Loop Is Not Bound To Approved Contracts

Severity: Medium

Finding:
The repo has optimization/autoloop concepts, but the workflow does not clearly
restrict auto-fixes to implementation defects within approved contracts.

Fix Direction:
Auto-loop may fix syntax, aliases, engine functions, catalog bindings, casts, and
missed declared filters/formulas. It must not invent or change business rules,
joins, metric formulas, or denominator scopes.

Acceptance Criteria:

- Auto-loop repairs code that violates known contracts.
- Unknown business semantics route to blocker panels.
- Repairs are recorded in optimization memory/evidence.

### BUG-DE-014: Remote Storage And Warehouse Mutation Policy Needs Product-Level Enforcement

Severity: High

Finding:
The project supports external/lakehouse/warehouse scenarios, but remote writes
must be consistently guarded.

Fix Direction:
Remote inspection can be allowed when credentials and policies permit. Creating
tables, moving data, writing bronze/silver/gold, running jobs, or changing
warehouse objects requires a dry-run deployment plan and explicit approval.

Acceptance Criteria:

- Deployment plan lists target catalog/schema/table/path, operation, write mode,
  data movement, risk, cost, and rollback.
- No remote mutation happens without explicit approval.
- Local smoke tests remain available without remote writes.

### BUG-DE-015: Full Review Panels Are Hidden Or Over-Compacted

Severity: High

Finding:
Important review content was compacted into short prompts or hidden behind
artifact paths.

Fix Direction:
Chat review mode must show decision-critical evidence:

- source truth
- proposed logic
- mappings
- transformations
- joins
- grain
- risks
- options
- recommendation
- harness checks

Acceptance Criteria:

- Full panel Markdown renders in chat/reports by default for review mode.
- Compact JSON requires explicit machine mode.
- Anti-compaction tests fail one-line replacement panels.

### BUG-DE-016: Cross-Agent Tools Can Simulate Workflow State

Severity: High

Finding:
Gemini monitoring showed that an agent can simulate panels or reuse context
instead of running required project tools.

Fix Direction:
Add a tool-evidence harness for Gemini, Claude, Codex, and other CLIs. Required
commands, file reads, panel ids, option ids, and trajectory events must appear in
the evidence stream.

Acceptance Criteria:

- Simulated panels fail the harness.
- Missing required project tool calls fail the harness.
- Unsupported claims fail unless backed by repo artifacts.

### BUG-DE-017: Evidence Order And Time Budget Are Not Enforced

Severity: High

Finding:
Agents can take long detours or read unrelated files after a tool output already
declares the deterministic next artifact to read.

Fix Direction:
Add per-stage evidence policies and budgets. Examples:

- If a command says read `current.md`, read that panel before inspecting unrelated
  contracts.
- Dataset questions use profiles first.
- Raw data reads require a reason and bounded sampling.
- Long detours fail the workflow guardrail.

Acceptance Criteria:

- Trajectory records commands, file reads, reason codes, elapsed time, and policy
  matches.
- Out-of-policy reads fail guardrails and recover to the deterministic next step.
- Raw reads are blocked or flagged when profiles are sufficient.

### BUG-DE-018: Proof Packet Is Not End-To-End Enough

Severity: High

Finding:
Proof output can show SQL and results without enough upstream evidence.

Fix Direction:
Proof packets must include source truth, catalog objects, layer contracts,
cleaning/dedup decisions, joins, grain, metric formulas, code, harness results,
sample outputs, warnings, and unresolved risks.

Acceptance Criteria:

- Business reviewers can see workbook truth and generated output together.
- Data engineers can see catalog/layer lineage and quality checks.
- Platform reviewers can see deployment and remote mutation status.

## First Implementation Milestone

Name: Contract and guardrail foundation

Goal:
Prevent the known RCM failure modes before building broader medallion/ETL/ELT
generation.

Required scope:

1. Generate catalog contracts from profile and inventory evidence.
2. Enforce no raw paths in non-ingestion generated code.
3. Render full review panels in chat/Markdown review mode.
4. Add evidence-order and time-budget guardrail tests.
5. Add tests for wrong evidence order, slow detours, missing panel reads, and raw
   path leakage.
6. Keep remote writes behind dry-run deployment plans and explicit approval.

Done criteria:

- Code generation is blocked from using raw paths outside ingestion/bootstrap.
- `catalog_contract.json` is generated from profiles and scope.
- Full review panels render without one-line compaction.
- Guardrail tests fail wrong evidence order and slow/out-of-policy detours.
- The RCM workbook-truth bugs cannot recur without failing the harness.

## Recommended Command/Artifact Additions

Proposed commands:

```powershell
uv run prepare-data-engineering-route --workspace workspaces/<project>
uv run build-catalog-contract --workspace workspaces/<project>
uv run prepare-pipeline-plan --workspace workspaces/<project> --track medallion --target-engine sql
uv run validate-code-generation-contracts --workspace workspaces/<project>
uv run run-layered-pipeline-harness --workspace workspaces/<project>
```

Proposed artifacts:

```text
interns/generated/contracts/catalog_contract.json
interns/reports/catalog_contract.md
interns/generated/contracts/pipeline_plan.json
interns/reports/pipeline_plan.md
interns/generated/contracts/bronze_contracts.json
interns/generated/contracts/silver_contracts.json
interns/generated/contracts/gold_contracts.json
interns/generated/evidence/layered_pipeline_harness.json
interns/reports/proof_packet/end_to_end.md
```

## Product Risk

Current risk is high for full data-engineering delivery. The project can present
reasonable generated outputs while skipping catalog abstraction, medallion layer
planning, transformation contracts, and source-truth semantic checks. The first
hardening milestone should turn these expectations into enforceable contracts and
tests before expanding code generation.

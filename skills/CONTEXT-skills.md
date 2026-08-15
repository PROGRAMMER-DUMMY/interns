# Skills Directory Context: `skills/`

This document provides an exhaustive, file-by-file context map and architectural reference for all 19 skill modules located within the [`skills/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills) directory.

---

## 🌲 Directory Structure & Subagent Maps

```
skills/
├── CONTEXT-skills.md                                # Master context map for the skills directory
├── context-map-sync/                                # Keeps CONTEXT-<folder>.md true to the code beside it
│   └── SKILL.md                                     # Fire conditions, procedure, verification, anti-patterns
├── dashboard-agent/                                 # Conversational front-end over dashboard surface
│   └── SKILL.md                                     # DashboardAgent specification and instructions
├── dashboard-design/                                # BI dashboard design, spec contracts, and visual verification
│   ├── SKILL.md                                     # Engine instructions, chart quality rules, and verify gate
│   └── agents/
│       └── dashboard-team.yaml                      # Subagent team definitions for dashboard tasks
├── data-engineering-pipeline-design/                # Source-to-target SQL/Polars/PySpark & medallion pipelines
│   ├── SKILL.md                                     # Intake rules, medallion architecture standards, and design rules
│   └── agents/
│       └── specialized-data-team.yaml               # Subagent team definitions for data engineering tasks
├── data-model-creation/                             # Interactive data model creation & ERD generation
│   └── SKILL.md                                     # Interview flow, understanding score, and diagram export
├── databricks-access-gates/                         # Databricks access, token scope, and UC policy gates
│   └── SKILL.md                                     # Access failure classification and user approval prompts
├── domain-model/                                    # Domain vocabulary, KPI registry alignment, and entity extraction
│   └── SKILL.md                                     # Inspection targets, extraction rules, and contract output
├── evolution/                                       # Lessons learned, user corrections, and memory capture
│   └── SKILL.md                                     # Memory format, capture triggers, and evolution rules
├── feature-derivation-library/                      # Reusable derived-feature patterns & formula candidates
│   └── SKILL.md                                     # Pattern detection, feature resolution states, and search gate
├── green-gate/                                      # CI test suite runner and regression detector
│   └── SKILL.md                                     # Test invocation rules, venv interpreter rules, and sweep mode
├── grill-requirements/                              # Requirement grilling, self-grilling, and ambiguity resolution
│   └── SKILL.md                                     # Interview order, single-question format, self-grill, and clarify modes
├── handoff/                                         # Session context compaction & cross-session bridge
│   └── SKILL.md                                     # Handoff structure, OS temp storage rules, and PII redaction
├── kpi-analyst/                                     # KPI sheet parsing, metric SQL generation, and result validation
│   ├── SKILL.md                                     # Metric decomposition, CTE query rules, and classification
│   └── agents/
│       └── openai.yaml                              # LLM configuration for KPI analyst subagent
├── kpi-clarification/                               # Structuring ambiguous KPI descriptions into metric specs
│   └── SKILL.md                                     # 8-step metric decomposition, standard output block, and rules
├── stakeholder-memory/                              # User preferences, risk tolerance, and decision history
│   └── SKILL.md                                     # Schema, preference capture targets, and decision history
├── task-onboarding/                                 # Onboarding fresh workspaces into runnable optimization tasks
│   └── SKILL.md                                     # Onboarding steps, workspace scanning, and task config setup
├── to-solution-brief/                               # Converting interviews and models into structured solution briefs
│   └── SKILL.md                                     # Brief template, section definitions, and output location
├── workspace-governance/                            # Data safety, gitignore enforcement, and pre-commit checks
│   └── SKILL.md                                     # File safety rules, pre-commit inspection, and secret prevention
└── workspace-kpi-query-optimizer/                   # General-purpose workspace KPI & query optimization loop
    ├── SKILL.md                                     # Full optimization workflow, blocker grilling, and evidence tracking
    └── agents/
        └── enterprise-workflow.yaml                 # Multi-agent enterprise optimization workflow spec
```

---

## 📚 Detailed Skill Documentation

### 1. [`dashboard-agent`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/dashboard-agent/SKILL.md)

- **Path**: [`skills/dashboard-agent/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/dashboard-agent/SKILL.md)
- **Purpose**: Acts as a named, conversational front-end (`DashboardAgent`) for workspace BI dashboards. It translates natural language plot requests into governed spec edits (writing strictly to `user_overrides`) followed by mandatory visual re-verification using `dashboard-verify`.
- **Parameters & Triggers**:
  - **Triggers**: Messages starting with `DashboardAgent,`, mentioning "DashboardAgent", or requesting plot additions/modifications (e.g., "add a plot...", "change KPI-2 to a donut").
  - **Argument Hint**: `"DashboardAgent, <request>"`
- **Workflows**:
  1. **Advise**: Reads target KPI specs and live result columns (`DuckDB` execution of `interns/generated/solutions/<kpi_id>.sql`). Recommends chart types using `core/dashboard/chart_knowledge.py`.
  2. **Edit**: Invokes `apply_panel_override` in `core/dashboard/agent_panel.py` to append panel dicts to `user_overrides["panels"]` in `workspaces/<ws>/dashboard/<kpi_id>.json`.
  3. **Verify**: Runs browser gate `tools.dashboard_verify` / `workspace-dashboard --screen` to confirm zero layout overflow or visual errors.
- **Key Instructions & Guardrails**:
  - Edits MUST write to `user_overrides` ONLY, never `machine_defaults`.
  - Always re-verify after modifications via browser gate.
  - Never expose display-redacted (PII/PHI/PCI) columns on chart axes.
  - Stay workspace-agnostic (resolve columns from live views and data types).
  - Do NOT touch upstream contracts (`kpi_registry.json`, SQL, data models).

---

### 2. [`dashboard-design`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/dashboard-design/SKILL.md)

- **Path**: [`skills/dashboard-design/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/dashboard-design/SKILL.md)
- **Subfiles**: [`agents/dashboard-team.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/dashboard-design/agents/dashboard-team.yaml)
- **Purpose**: Engine for designing, customizing, debugging, and visually verifying per-workspace BI dashboards. Controls JSON spec contracts (`machine_defaults` + `user_overrides`), data-driven chart inference (`core/dashboard/profile.py`), Dash rendering, and static HTML exports.
- **Parameters & Triggers**:
  - **Triggers**: Requests to change chart types, add filters, debug blank charts, export HTML/PDF decks, or open dashboard ports.
  - **Argument Hint**: `"What dashboard work? (e.g. 'change kpi_001 to bar chart', 'add a region filter', 'why is kpi_002 blank')"`
- **Workflows**:
  1. **Evidence-Driven Panel Selection**: Uses `decide_panels()` in `core/dashboard/profile.py` to derive charts from column data shapes (constants excluded).
  2. **Spec Merging**: Merges `user_overrides` on top of `machine_defaults` at render time (`merge_spec`).
  3. **Visual Verification Gate**: Runs `uv run workspace-dashboard --workspace workspaces/<ws> --screen` or `uv run dashboard-verify` to ensure zero container overflow, non-blank rendering, and legend inclusion.
- **Key Instructions & Guardrails**:
  - Re-executes live SQL (`interns/generated/solutions/<kpi_id>.sql`) on DuckDB on page load (no stale snapshots).
  - Blocked KPIs without SQL must render as informative blocker cards, not be hidden.
  - Enforces chart-quality defaults: no duplicate inline titles, trend/line charts aggregated by date, share charts normalized to true 0–100%, top-N ranked on non-constant columns, corporate BI styling applied.

---

### 3. [`data-engineering-pipeline-design`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/data-engineering-pipeline-design/SKILL.md)

- **Path**: [`skills/data-engineering-pipeline-design/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/data-engineering-pipeline-design/SKILL.md)
- **Subfiles**: [`agents/specialized-data-team.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml)
- **Purpose**: Designs source-to-target SQL, Polars, PySpark, ETL/ELT, and medallion-layer (Bronze/Silver/Gold) loading pipelines driven by KPI requirements, data model contracts, and profiles. Also handles external data lake intake.
- **Parameters & Commands**:
  - `uv run discover-external-sources --workspace workspaces/<project> --external-root <external-root>`
  - `uv run prepare-bronze-silver-standards --workspace workspaces/<project> --domain <domain>`
- **Workflows**:
  1. **Required Evidence Order**: External discovery -> KPI requirements -> Data model docs -> Profile evidence (`profiles/`) -> Contracts -> Bounded samples.
  2. **External Intake**: Registers raw files as Bronze candidates, parses docs, profiles schemas before Silver/Gold design.
  3. **Medallion Separation**: Bronze (source-preserving + metadata), Silver (conformed, typed, normalized, quarantine-backed), Gold (business aggregates, star-schema facts/dimensions).
- **Key Instructions & Guardrails**:
  - Never infer source truth from column-name similarity alone.
  - Deduplication is strictly forbidden in Bronze; Silver deduplication requires an approved JSON decision and retained rejected-row lineage.
  - Keep KPI-specific formulas out of default Silver; place them in Gold.
  - Flag anti-patterns: Gold built directly from Bronze, business logic in Bronze, missing fact grain, nullable fact FKs without unknown-member policy.

---

### 4. [`data-model-creation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/data-model-creation/SKILL.md)

- **Path**: [`skills/data-model-creation/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/data-model-creation/SKILL.md)
- **Purpose**: Interactively builds and refines data models with users through guided conversation. Clarifies grain, entities, keys, facts/dimensions, relationships, cardinality, temporal anchors, and SCD policies, calculating a continuous Understanding Score before generating governed specs and ERD diagrams.
- **Parameters & CLI Execution Sequence**:
  - `prepare-data-model-generation` -> `apply-data-model-answer` -> `prepare-data-model-blocker-panel` -> `apply-data-model-blocker-answer` -> `finalize-data-model-generation --approve-final-preview` -> `export-data-model-diagram`
- **Workflows**:
  1. **Routing & Drafting**: Routes input mode (text docs / image ERD / profiles) and writes draft model packs (`interns/generated/requirements/`).
  2. **Structured Interview**: Sequentially resolves Purpose -> Entities -> Grain -> Keys -> Facts vs Dims -> Relationships -> Temporal Anchor -> SCD Policy -> Gaps.
  3. **Understanding Score**: Computes 0–100 score; relationship inferred only by column name counts as candidate (<=50).
  4. **Finalization & ERD Export**: On approval, generates `docs/data-model.md`, `docs/erd.md`, `data_model_contract.json`, and exports native SVG + Mermaid ERDs.
- **Key Instructions & Guardrails**:
  - Hard rule: Never assert a relationship, grain, or fact/dimension role purely from column-name matching.
  - Draft model packs are not user-facing until explicitly approved.
  - Image/name-derived relationship links remain non-executable until promoted by `build-relationship-contracts`.

---

### 5. [`databricks-access-gates`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/databricks-access-gates/SKILL.md)

- **Path**: [`skills/databricks-access-gates/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/databricks-access-gates/SKILL.md)
- **Purpose**: Handles Databricks remote access errors, token scope deficiencies, Unity Catalog grant requirements, workspace API locks, and compute policy constraints, converting remote failures into clear user access requests.
- **Parameters & Classifications**:
  - Operation types: `read_only`, `workspace_mutation`, `uc_mutation`, `compute_mutation`, `ai_asset_mutation`, `data_movement`.
  - Required Environment Variable: `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`.
- **Workflows**:
  1. **Operation Classification**: Categorizes the intended remote Databricks action.
  2. **Local Gate Verification**: Verifies explicit user permission and environment variable flags.
  3. **Access Request Formatting**: On failure, emits a structured prompt detailing what succeeded, what failed, minimum required grants, and safe retry steps.
- **Key Instructions & Guardrails**:
  - Presence of credentials does NOT equal user approval for remote execution.
  - Never print or leak token values, secrets, or bearer headers in chat or logs.
  - Do not retry mutating operations until the user confirms missing access/policy is granted.

---

### 6. [`domain-model`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/domain-model/SKILL.md)

- **Path**: [`skills/domain-model/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/domain-model/SKILL.md)
- **Purpose**: Extracts project domain vocabulary, entity relationships, primary/foreign keys, temporal anchors, and metric mapping constraints from context files, data models, and profiler outputs prior to contract generation.
- **Parameters & Input Sources**:
  - Reads: `CONTEXT.md`, `config/tasks.json`, `workspaces/<project>/docs/`, KPI registries, profile outputs (`profiles/*.profile.json`).
  - Code-Generated Output Paths: `workspaces/<project>/interns/generated/contracts/domain_model.json`, `kpi_feature_mapping.json`, `interns/reports/open_questions.md`.
- **Workflows**:
  1. **Inspection**: Scans workspace documentation and dataset profiles using Polars.
  2. **Extraction**: Identifies entities, facts, dimensions, PK/FK links, valid grains, temporal anchors, and KPI term mappings.
  3. **Contract Alignment**: Validates mappings against generated contracts.
- **Key Instructions & Guardrails**:
  - Use Polars for dataframe/schema inspection. Keep pandas conversions strictly local if forced by external APIs.
  - Read code-generated contracts (`domain_model.json`, `kpi_feature_mapping.json`); NEVER hand-write them.
  - Self-authored notes must be written to `interns/reports/`, never directly into `interns/generated/contracts/`.

---

### 7. [`evolution`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/evolution/SKILL.md)

- **Path**: [`skills/evolution/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/evolution/SKILL.md)
- **Purpose**: Captures durable lessons, stakeholder corrections, accepted recommendations, rejected assumptions, and tool failure fixes to continuously improve future workspace onboarding and optimization loops.
- **Parameters & Triggers**:
  - Triggers: User corrections ("no", "actually", "instead"), governance decisions, accepted recommendations, optimization performance shifts.
  - Files Modified: `workspaces/<project>/interns/generated/memory/evolution.md` and `lessons.json`.
- **Workflows**:
  1. **Target Confirmation**: Verifies active workspace context.
  2. **Signal Capture**: Extracts triggers, assumptions, outcomes, lessons, and application areas.
  3. **Memory Record Update**: Appends structured markdown entries to `evolution.md` and updates `lessons.json`.
- **Key Instructions & Guardrails**:
  - Do not store secrets, tokens, credentials, or raw PII data.
  - Do not record unconfirmed guesses as facts.
  - Lessons must be tied to verified evidence or explicit user corrections.

---

### 8. [`feature-derivation-library`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/feature-derivation-library/SKILL.md)

- **Path**: [`skills/feature-derivation-library/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/feature-derivation-library/SKILL.md)
- **Purpose**: Provides reusable derived-feature candidate patterns (e.g., age, age bands, AR aging, net paid amount, duration buckets, recurrence windows) for non-physical KPI terms while ensuring candidates are not treated as proof.
- **Parameters & Commands**:
  - Command: `uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>`
  - Feature States: `proven_direct`, `proven_alias`, `proven_join`, `proven_formula`, `proven_taxonomy`, `user_confirmed`, `blocked_missing_evidence`, `blocked_ambiguous`, `candidate_unconfirmed`, `candidate_pattern`, `rejected`.
- **Workflows**:
  1. **Mapping Review**: Reads `kpi_feature_mapping.json` to identify unmapped/blocked features.
  2. **Pattern Search**: Uses `core/onboarding/features/derivation_search.py` to match candidate formulas.
  3. **Candidate Attachment**: Attaches candidates as `candidate_pattern` or `candidate_unconfirmed`.
  4. **User Confirmation**: Asks user when evidence is missing, recording accepted answers in `kpi_feature_mapping.json` and `decision_history.md`.
- **Key Instructions & Guardrails**:
  - Reusable patterns are candidates, never proof. Authoritative solution SQL requires `proven_*` or `user_confirmed` states for all features.
  - Never present semantically mismatched derivation patterns as selectable options.
  - Includes deterministic detection for `duration_bucket` and `recurrence_within_window`.

---

### 9. [`green-gate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/green-gate/SKILL.md)

- **Path**: [`skills/green-gate/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/green-gate/SKILL.md)
- **Purpose**: Runs the repository's portable green gate test suite (curated CI tests + enterprise suite) to evaluate codebase health before committing or finalizing work.
- **Parameters & Execution Commands**:
  - `green-gate` (strict curated + enterprise gate)
  - `green-gate --sweep` (sweeps resolver/pipeline modules, classifying regressions vs known baselines)
  - `green-gate --json` (outputs machine-readable test execution JSON)
  - Venv Fallback: `.venv\Scripts\python.exe -m core.dev.green_gate --sweep`
- **Workflows**:
  1. **Test Execution**: Runs test suite via the dedicated venv Python interpreter.
  2. **Result Classification**: Evaluates pass (`[ok]`), fail (`[x]`), or baseline failure (`[~] known`).
  3. **Baseline Maintenance**: Removes fixed tests from `KNOWN_BASELINE` in `core/dev/green_gate.py`.
- **Key Instructions & Guardrails**:
  - **HARD RULE**: NEVER use `uv run` for running tests (it forces `pyspark` 4.1.1 reinstall which breaks Delta tests). Always use the venv interpreter.
  - Output uses ASCII markers only (`[ok]/[x]/[~]`); no emojis allowed.

---

### 10. [`grill-requirements`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/grill-requirements/SKILL.md)

- **Path**: [`skills/grill-requirements/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/grill-requirements/SKILL.md)
- **Purpose**: Conducts structured stakeholder interviews one decision at a time to clarify business goals, guardrails, data models, and optimization targets. Integrates Self-Grill (inward audit) and Clarify Ambiguity modes.
- **Parameters & Output Artifacts**:
  - Outputs: `workspaces/<project>/interns/generated/requirements/stakeholder_interview.md`, `requirements.json`, `interns/reports/open_questions.md`.
  - Question Format: Single question with Question, Options, Recommended Answer, and Why.
- **Workflows**:
  1. **Active Workflow Confirmation**: Verifies active workspace before interviewing.
  2. **Sequential Grilling Order**: Business goal -> Stakeholders -> Data sources -> KPI formulas -> Data model -> Gaps -> Target -> Metrics -> Failure policy -> Human workflow -> Preferences.
  3. **Self-Grill Mode**: Emits 3-6 self-interrogation questions with evidence before making major proposals.
  4. **Clarify Mode**: Asks ONE targeted question only when ambiguity introduces material risk.
- **Key Instructions & Guardrails**:
  - Inspect files and context before asking discoverable facts.
  - Ask exactly ONE question at a time.
  - Record user answers as accepted decisions under `interns/` before implementation starts.

---

### 11. [`handoff`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/handoff/SKILL.md)

- **Path**: [`skills/handoff/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/handoff/SKILL.md)
- **Purpose**: Compacts the current session's un-derivable context, state snapshot, open decisions, and suggested skills into a temporary markdown handoff document for a fresh agent to continue execution.
- **Parameters & Storage Rules**:
  - Triggers: `/handoff`, "wrap this up for the next session", or approaching context limits.
  - Save Location (OS Temp ONLY):
    - Windows: `$env:TEMP\autoresearch-handoff-<UTC-timestamp>.md`
    - Linux/macOS: `${TMPDIR:-/tmp}/autoresearch-handoff-<UTC-timestamp>.md`
- **Workflows**:
  1. **State Snapshot**: Records active workspace, allowlists, stage, in-flight jobs, and uncommitted git state references.
  2. **Anchor Indexing**: References minimal load-bearing files (e.g. `current.md`, PRDs, commits).
  3. **Redaction**: Scrubs API keys, tokens, `.env` content, and PII/PHI.
  4. **Path Emission**: Prints absolute path and load command to the user.
- **Key Instructions & Guardrails**:
  - NEVER write handoff files into the workspace directory or working tree.
  - Do NOT re-paste file contents or full diffs; reference paths and `git status`/`git diff`.
  - Do NOT print the full handoff text directly in chat.

---

### 12. [`kpi-analyst`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/kpi-analyst/SKILL.md)

- **Path**: [`skills/kpi-analyst/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/kpi-analyst/SKILL.md)
- **Subfiles**: [`agents/openai.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/kpi-analyst/agents/openai.yaml)
- **Purpose**: Parses, interprets, operationalizes, and validates KPI definition documents (metrics, dimensions, cuts, filters, grain). Generates CTE-structured SQL queries and result demo tables.
- **Parameters & Classifications**:
  - Output Types: Trend, Ranking, Share/percentage, Snapshot, Scorecard, Cohort.
  - Artifact Lifespans: Ad hoc (single use), Reporting query (weeks/months), View/dbt model (reusable), Stored procedure (permanent).
- **Workflows**:
  1. **Decomposition**: Extracts business question, metric formula, dimensions, filters, grain, output type.
  2. **Ambiguity & Typo Check**: Detects unproven columns, invalid joins, or formula typos.
  3. **Query Generation**: Constructs CTE-based SQL queries using explicit `WHERE` filters and `COUNT(DISTINCT)`.
  4. **Governed Validation**: Validates generated artifacts against `kpi_registry.json`, `kpi_feature_mapping.json`, `source_to_target_plan.json`, `relationship_contracts.json`.
- **Key Instructions & Guardrails**:
  - Default to ANSI SQL unless DuckDB/Databricks/Spark SQL is specified.
  - Make denominator scope explicit with CTEs or window partitions for share/percentage metrics.
  - Never map column-name similarity alone to physical columns without evidence or confirmation.

---

### 13. [`kpi-clarification`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/kpi-clarification/SKILL.md)

- **Path**: [`skills/kpi-clarification/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/kpi-clarification/SKILL.md)
- **Purpose**: Converts ambiguous, informal, or loosely defined KPI descriptions into precise, structured business metric specifications with explicit calculation logic and clarification questions.
- **Parameters & Triggers**:
  - Triggers: "define this metric", "clarify this measure", "document our conversion rate", or any informal business measure presentation.
- **Workflows**:
  1. **Decomposition (8 Dimensions)**: Subject Metric -> Numerator -> Denominator -> Aggregation -> Grouping Dimensions -> Filters -> Time Grain -> Output Type.
  2. **Output Block Formatting**: Renders standardized markdown block with Original KPI, Business Definition, Calculation Logic, Aggregation, Dimensions, Filters, Time Grain, Output Type, Explicit Assumptions, and Targeted Clarification Questions.
- **Key Instructions & Guardrails**:
  - Never assume unstated dimensions, filters, or time periods—flag them as ambiguity questions.
  - If multiple valid interpretations exist, list all plausible ones.
  - Render one full output block per input KPI.
  - If input is under 5 words and highly ambiguous, ask one targeted question before full output generation.

---

### 14. [`stakeholder-memory`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/stakeholder-memory/SKILL.md)

- **Path**: [`skills/stakeholder-memory/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/stakeholder-memory/SKILL.md)
- **Purpose**: Persists user, team, and stakeholder preferences (decision styles, risk tolerance, naming conventions, review requirements, rejected assumptions) across sessions.
- **Parameters & Schema**:
  - Output Files: `workspaces/<project>/interns/generated/memory/preferences.json` and `decision_history.md`.
  - JSON Schema Keys: `user_preferences`, `team_preferences`, `decision_style`, `rejected_assumptions`, `source_notes`.
- **Workflows**:
  1. **Preference Signal Extraction**: Detects stakeholder preferences during interviews or feedback loops.
  2. **Conservative Merging**: Merges new preferences into `preferences.json` and appends entries to `decision_history.md`.
- **Key Instructions & Guardrails**:
  - Do NOT store secrets, tokens, raw workspace data, credentials, or unrelated personal information.
  - Keep decision history strictly append-only.

---

### 15. [`task-onboarding`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/task-onboarding/SKILL.md)

- **Path**: [`skills/task-onboarding/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/task-onboarding/SKILL.md)
- **Purpose**: Transforms raw workspace project inputs (data files, KPI registries, data models) into structured, runnable optimization tasks and semantic contracts under `interns/`.
- **Parameters & Commands**:
  - Primary Command: `uv run onboard-workspace --workspace workspaces/<project>`
  - Output Locations: `interns/generated/contracts/`, `profiles/`, `requirements/`, `reports/open_questions.md`, `config/tasks.json`.
- **Workflows**:
  1. **Active Setup & Scan**: Recursively scans workspace files using `rg` or PowerShell fallback and gets user confirmation.
  2. **Runtime Layout Creation**: Establishes `workspaces/<project>/interns/` structure.
  3. **Profiling & Contract Generation**: Runs data profiling, parses registries, and updates `config/tasks.json`.
- **Key Instructions & Guardrails**:
  - Do not assume active workspace based on file names alone; require explicit confirmation.
  - Use Polars for file and schema inspection.
  - Keep executable KPI logic blocked until physical mappings are proven or confirmed.

---

### 16. [`to-solution-brief`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/to-solution-brief/SKILL.md)

- **Path**: [`skills/to-solution-brief/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/to-solution-brief/SKILL.md)
- **Purpose**: Synthesizes findings from stakeholder interviews, KPI registry details, data models, and preferences into a formal Solution Brief for governed optimization tasks.
- **Parameters & Output File**:
  - Output Path: `workspaces/<project>/interns/generated/requirements/solution_brief.md`
- **Workflows**:
  1. **Information Synthesis**: Gathers details from `grill-requirements`, `domain-model`, and `stakeholder-memory`.
  2. **Template Population**: Populates sections: Problem, Stakeholders, Inputs, Optimization Target, Semantic Guardrails, Success Metrics, Approval & Rollback, Out of Scope, Open Questions.
- **Key Instructions & Guardrails**:
  - Solution brief must be detailed enough to guide implementation, governance, and evaluation.
  - If a key section cannot be resolved, return to `grill-requirements` for missing decisions.

---

### 17. [`workspace-governance`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/workspace-governance/SKILL.md)

- **Path**: [`skills/workspace-governance/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/workspace-governance/SKILL.md)
- **Purpose**: Protects workspace data and repo integrity by enforcing `.gitignore` rules on `interns/`, preventing accidental staging of raw datasets, databases, `.env` files, or secrets.
- **Parameters & Inspection Commands**:
  - `git status --short`
  - `git diff --cached --stat`
  - `git diff --cached --name-only`
- **Workflows**:
  1. **Workspace Verification**: Confirms active workspace context.
  2. **Pre-Commit Verification**: Scans staged git files for `workspaces/<project>/interns/`, `.env`, `.duckdb`, `.sqlite`, `.csv`, `.parquet`, `.pdf`, or `config/lock.toml`.
- **Key Instructions & Guardrails**:
  - `workspaces/**/interns/` MUST remain ignored by git.
  - Never stage raw data, local databases, binary files, or enterprise config locks.
  - Immediately flag and stop execution if secret tokens or un-ignored workspace artifacts appear in git status.

---

### 18. [`workspace-kpi-query-optimizer`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/workspace-kpi-query-optimizer/SKILL.md)

- **Path**: [`skills/workspace-kpi-query-optimizer/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/workspace-kpi-query-optimizer/SKILL.md)
- **Subfiles**: [`agents/enterprise-workflow.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml)
- **Purpose**: General-purpose engine for building, profiling, validating, and optimizing SQL, Polars, or hybrid query logic across any workspace containing datasets, KPI registries, and data models.
- **Parameters & Runtime Layout**:
  - Layout: `workspaces/<project>/interns/` (`state/`, `runs/`, `reports/`, `generated/`).
  - Feature States: `proven_direct`, `proven_alias`, `proven_join`, `proven_formula`, `proven_taxonomy`, `user_confirmed`, `blocked_missing_evidence`, `blocked_ambiguous`.
- **Workflows**:
  1. **Workspace Setup & Scan**: Verifies active workspace and creates `interns/` structure.
  2. **Profiling & Contract Mapping**: Profiles datasets with Polars and generates semantic contracts.
  3. **Automatic Blocker Grilling**: Asks one blocker question at a time using workspace-level blocker inventories.
  4. **Baseline & Optimization Loop**: Establishes baseline SQL (`interns/generated/solutions/`), runs optimizations (predicate pushdown, join rewrites, downcasting), and verifies correctness before acceptance.
- **Key Instructions & Guardrails**:
  - All generated/runtime outputs MUST live under `workspaces/<project>/interns/`.
  - Use Polars by default for dataframe and file work.
  - Remote Databricks execution requires explicit `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` approval.
  - NEVER accept an optimization without proving semantic correctness against the baseline.

---

### 19. [`context-map-sync`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/context-map-sync/SKILL.md)

- **Path**: [`skills/context-map-sync/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/context-map-sync/SKILL.md)
- **Purpose**: Keeps every `CONTEXT-<folder>.md` true to the code beside it, enforcing `CONTEXT-MAP.md` rule 3 — a code change and its CONTEXT update are ONE change, staged together. Agents read these files *instead of* the code to decide what to call, so a stale entry produces wrong code that fails at runtime, not merely a misleading doc.
- **Parameters & Triggers**:
  - **Triggers**: In any mapped directory — a file added/deleted/renamed; a public function, class or constant added/removed/renamed; a signature change; a CLI flag added or removed; a changed failure mode. Also before staging any commit touching `core/`, `tools/`, `tests/`, `config/`, `docs/`, `skills/` or `vendor/`.
  - **Does not fire** for a private helper used once inside its own module.
- **Workflows**:
  1. **Map** touched files to their CONTEXT owner (one per directory, named for it).
  2. **Read before editing** — entries drift independently, so expect pre-existing errors beside the line you came for; fix what you can verify from code you actually read.
  3. **Update** in the established shape (`Exact Purpose` / `Key Functions / Classes` / `Inputs & Outputs` / `Failure Modes & Edge Cases`), using `file://` links with forward slashes.
  4. **Stage** the CONTEXT file in the same commit as the code.
- **Key Instructions & Guardrails**:
  - Verification is an **import of every symbol just documented** — the check that would have caught the `DatabricksExecutionClient` drift (F19).
  - `tools/context_status.py` is **not** a coverage checker despite the name: it estimates chat-context size, has no `main()`, and prints nothing. A real coverage/drift verifier does not exist yet.
  - Never copy a signature from another CONTEXT file; that is how drift propagates. Never write a line-number anchor you did not verify — a wrong one is worse than none because it looks precise.

---

## 🧹 Code Hygiene & Integrity Audit (`skills/`)

- 💀 **Dead Code**: None found. All 18 skill directories contain active, load-bearing `SKILL.md` specifications wired into repo tools and prompt templates.
- 🔌 **Unwired Components**: None. The 4 `agents/*.yaml` files ([`dashboard-team.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/dashboard-design/agents/dashboard-team.yaml), [`specialized-data-team.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml), [`openai.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/kpi-analyst/agents/openai.yaml), [`enterprise-workflow.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml)) are referenced by subagent orchestration logic.
- 👯 **Duplication & Overlap**:
  - `grill-requirements` merges self-grill and clarify-ambiguity modes.
  - `kpi-clarification` provides NL-to-spec decomposition, complementing `kpi-analyst` (which focuses on SQL/code generation).
  - `workspace-kpi-query-optimizer` acts as the master orchestrator chaining `workspace-governance`, `domain-model`, `feature-derivation-library`, `task-onboarding`, `grill-requirements`, `stakeholder-memory`, `to-solution-brief`, and `evolution`.
- ⚠️ **Mismatches & Risks**: None. All internal tool calls and CLI references match current repo entry points.

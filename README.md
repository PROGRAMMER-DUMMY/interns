# Autoresearch

Autoresearch solves a specific enterprise problem: **business teams have KPI questions written in business language, but translating those questions into correct, trusted SQL from complex multi-table datasets requires deep domain + data engineering knowledge that most teams don't have time to apply carefully.**

The platform automates that translation pipeline with governance — from a KPI question like *"What is the trend for amount paid for Medicare LOB by gender and payer for patients above 50?"* to a verified, executable SQL query against the right tables, joins, and filters — with every mapping decision traced and approved.

```
Business KPI question + raw datasets
          ↓
  [Onboard] Profile datasets, normalize KPI registry
          ↓
  [Resolve] Map business terms → physical columns (evidence-backed scoring)
          ↓
  [Govern] Ask blocking questions when mappings are ambiguous
          ↓
  [Prove]  Build FK/relationship contracts between tables
          ↓
  [Generate] Emit executable SQL (DuckDB local or Databricks enterprise)
          ↓
  [Results] KPI definition + SQL + result table stored in runs/
```

Every decision is captured. Every mapping has evidence. Bad data never silently reaches Gold.

## Current Capabilities

- **Workspace onboarding** — profiles datasets (row counts, schema, quality), normalizes KPI registries, builds semantic contracts
- **Evidence-backed feature resolution** — maps KPI business terms to physical columns using dataset profiles, data dictionaries, lexicon, and scoring; auto-proves high-confidence mappings
- **Governed blocker question panels** — when a mapping is ambiguous, generates a structured question with JSON-backed options ranked by evidence; accepts answers through governed wrappers
- **Relationship/FK contracts** — proves or requires approval for every multi-table join before any SQL is generated; profile-only candidates are advisory only
- **KPI SQL generation** — emits executable DuckDB SQL locally; swaps to catalog table references for Databricks enterprise; generates only from fully proven or user-confirmed mappings
- **Grain-bucketing for share metrics** — a share/percentage metric cut by a raw continuous dimension (exact age, days-since) blocks pending a decision instead of fragmenting into one tiny row per value; `apply-pipeline-decision --grain-bucketing band_continuous_cuts` emits fixed-width bands (readable `20-29` labels, numeric sort), or `exact_value_grain` keeps the exact grain
- **Tamper-evident execution evidence** — the artifact validator re-executes generated result views and compares columns/row counts to the recorded harness, so a hand-edited or fabricated result manifest is rejected rather than trusted
- **Run reports** — every `generate-kpi-sql` call writes `interns/runs/{date}/results.md` containing KPI definition, full SQL, and executed result table
- **Workspace allowlisting** — scope any workspace to specific dataset subsets without touching code
- **Workspace memory** — accepted decisions (feature mappings, relationship approvals) persist across runs so the same questions are never asked twice
- **Decision history** — every feature mapping decision and relationship approval is appended to `interns/generated/memory/decision_history.md`
- **Databricks enterprise path** — generate Databricks-dialect SQL targeting Unity Catalog tables; deploy asset manifests, Genie workspace specs, and deployment plans
- **KPI generation mode** — BA/product-led KPI definition interview that scores, refines, and governs KPI creation before implementation
- **Dashboard** — run history, reviewer proof, intern activity, governance decisions, and human alerts
- **Validation harnesses** — artifact validators, workflow guardrail checks, AI app harness, AI CLI harness, and reliability suite

## Core Layout

The platform engine is organized by responsibility:

**Primary pipeline** (`core/onboarding/`) — this is where the KPI-to-SQL work lives:
- `core/onboarding/kpi/` — feature resolver, blocker question panel, SQL generator, execution harness, KPI generation workflow
- `core/onboarding/features/` — feature expression parsing, derived feature markdown, blocker detection
- `core/onboarding/relationships/` — FK/relationship contract builder and approval
- `core/onboarding/workspace/` — workspace onboarding, kickstart, delegation, flow orchestration
- `core/onboarding/lexicon/` — workspace vocabulary and term normalization
- `core/onboarding/memory/` — workspace definitions, decision history, user decisions
- `core/onboarding/data_model/` — data model document parsing and image extraction
- `core/onboarding/sources/` — source catalog ingestion and external source intake
- `core/onboarding/databricks/` — Databricks asset manifests and Genie workspace specs

**Supporting infrastructure:**
- `core/medallion/` — Bronze/Silver/Gold medallion architect (design and build pipeline)
- `core/execution/` — local DuckDB and Databricks execution backends
- `core/governance/` — contracts, policies, approvals, and promotion gates
- `core/context/` — bounded context indexes, task manifests, and context wiki reports
- `core/storage/` — workspace layout, metadata store, SQLite/Delta state
- `core/optimization/` — optimization memory and diff classification
- `core/observability/` — metric parsing and telemetry
- `core/agents/` — LLM routing and intern activity
- `core/presentation/` — console tables, markdown rendering

## Agent Guidance

Agents should start with `AGENTS.md`. Repo-native operating skills live in `skills/` and cover
ambiguity handling, stakeholder interviews, preference memory, domain modeling, task onboarding,
workspace governance, and evolution.

## CLI Enterprise Guide

The CLI version is the canonical workflow interface. It is designed for data teams, analytics
engineers, product leads, business analysts, and platform teams that need traceable KPI and
data-engineering work.

The fast-moving workflow source of truth is `AGENTS.md`, `TOOLS.md`, `.agents/tools.json`, and the
current JSON/Markdown panels written under `workspaces/<project>/interns/reports/`. This README is
an orientation document, not the policy authority for agent behavior.

The operating model is:

```text
workspace inputs -> governed artifacts -> validated executable outputs
```

For agent-led sessions, prefer the quiet workflow front door:

```powershell
uv run workspace-flow start --workspace workspaces/<project> --intent kpi_generation --domain healthcare
uv run workspace-flow answer --session <session-id> --answer option_a
uv run workspace-flow results --session <session-id>
```

`workspace-flow` persists session state and returns compact questions/results while running the
lower-level onboarding, blocker, planning, SQL generation, validation, and preview steps in the
backend.

The CLI is not a loose query generator. It is a governed control plane that:

- inventories workspace inputs without reading raw data too early;
- scores and improves KPI requirements before implementation;
- profiles datasets and records schema/value evidence;
- maps KPI/business terms to physical columns, formulas, or accepted definitions;
- asks blocker questions when mappings, formulas, joins, grain, or ownership are not proven;
- builds production-grade relationship/FK contracts before multi-dataset execution;
- builds source-to-target plans for SQL, Polars, PySpark, and medallion/ETL work;
- generates executable artifacts only after evidence and approval gates pass;
- stores decisions, memory, reports, and proof under the workspace.

Current operator path for a fresh KPI/query workspace:

```powershell
# 1. Scope the workspace (optional — limit to specific datasets)
#    Set workspace_settings.json with dataset_allowlist before onboarding.

# 2. Onboard — profiles datasets, normalizes KPI registry, writes contracts
uv run onboard-workspace --workspace workspaces/<project>

# 3. Resolve KPI features — maps business terms to physical columns
uv run resolve-kpi-features --workspace workspaces/<project> --domain <domain> --include-candidates

# 4. Answer blockers — if blocked_kpi_count > 0, read the panel and answer
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
uv run apply-kpi-panel-answer --workspace workspaces/<project> --domain <domain> --answer option_a

# 5. Build relationship contracts — prove FK joins between tables
uv run build-relationship-contracts --workspace workspaces/<project>
uv run apply-relationship-answer --workspace workspaces/<project> --relationship-id <id> --answer approve

# 6. Generate SQL — emits executable SQL and result table
uv run generate-kpi-sql --workspace workspaces/<project> --kpi-id kpi_001

# 7. Review results
#    interns/runs/{date}/results.md  ← KPI definition + SQL + result table
```

Use the generated panel files for stakeholder questions. Do not hand-edit generated contracts such
as `kpi_feature_mapping.json`, `workspace_feature_definitions.json`, or blocker panel JSON. Apply
answers through the supported wrappers, especially `apply-kpi-generation-answer` and
`apply-kpi-panel-answer`.

For an all-KPI review packet before bulk approval or execution, run:

```powershell
uv run kpi-proof-packet --workspace workspaces/<project> --domain healthcare
```

The first version is read-only. It writes source-row traceability, mapping recommendations,
readiness gates, generated SQL when present, output previews when present, and sample values under
`workspaces/<project>/interns/reports/kpi_proof_packet/`.

Dependency-free AI application tests can run from workspace-scoped JSONL datasets:

```powershell
uv run run-ai-app-harness --workspace workspaces/<project> --dataset workspaces/<project>/interns/ai_harness/datasets/happy_path.jsonl
```

The default path is local and CI-safe. Cases can target `local_stub` or `http_ai`; HTTP cases are
blocked unless `--allow-remote-ai` is passed. Store only non-secret config fields and env var names,
using `config/ai_harness.example.json` as the template. KPI/SQL suites can assert feature mappings,
SQL semantics, result-table shape, pinned metric values, and baseline result regressions. Example
KPI suite rows are in `config/ai_harness.kpi_suite.example.jsonl`.

Workflow guardrails can be checked with:

```powershell
uv run validate-workflow-guardrails --workspace workspaces/<project>
```

This catches workflow failures around the tools themselves: invented generic KPI features, blocker
panels without source-backed provenance, raw dataset reads before profiles, non-portable shell
commands, and failed commands that were not recovered with a safer project tool.

Replayable workflow steps can be recorded with:

```powershell
uv run record-workspace-trajectory --workspace workspaces/<project> --event-type command --status ok --summary "Listed workspace."
```

The append-only trajectory is written to `interns/state/trajectory.jsonl` and summarized under
`interns/reports/trajectory/`. `validate-workflow-guardrails` consumes it by default when present.
Controlled tools such as `workspace-flow`, `prepare-kpi-blocker-panel`, and
`apply-kpi-panel-answer` also record best-effort trajectory events automatically.

`validate-project-harness` includes workflow guardrail health in the top-level project score, so
agent process failures such as unsupported commands or unrecovered failed steps can block readiness
even when static artifacts look valid.

Build a local evidence graph when you need impact or traceability across artifacts:

```powershell
uv run build-workspace-evidence-graph --workspace workspaces/<project>
uv run query-workspace-evidence-graph --workspace workspaces/<project> --term Payer
```

It writes `interns/generated/evidence_graph/graph.json` and
`interns/reports/evidence_graph/current.md`, linking KPI terms, mappings, profile columns, SQL,
trajectory events, and harness findings.

The dashboard includes a `Review` page for the same proof trail. It reads the selected workspace's
reliability suite, workflow guardrails, evidence graph, memory health, trajectory, blocker panel,
project harness, and KPI proof packet artifacts without reading raw datasets. Run it locally with:

```powershell
uv run python dashboard.py
```

CLI agents can be regression-tested against the governed workflow with:

```powershell
uv run run-ai-cli-harness --workspace workspaces/<project> --dataset workspaces/<project>/interns/ai_cli_harness/datasets/governed_suite.jsonl
```

Real subprocess execution of tools such as Claude, Gemini, or Codex is blocked unless
`--allow-cli-exec` is passed. The default stub mode checks command transcripts, project-tool
compliance, generated artifacts, JSON fields, and workflow guardrails without calling an external
AI CLI.

Bugfinder support is intentionally conservative. `prepare-workspace-bug-report` catches workflow
contradictions such as selection/onboarding disagreement, blocker panels that ask about parser
artifacts or operator fragments, and scoped workspace definitions that appear to overwrite one
another. It is not a substitute for reviewer judgment on business semantics, relationship approval,
or production readiness.

### Team Roles

- Business analysts and product leads use KPI generation, quality scoring, competitive review, and
  blocker panels to define or revise metrics.
- Data engineers use relationship contracts, source-to-target plans, generated SQL, and validation
  reports to build trusted pipelines.
- Reviewers use reports, evidence files, decision history, and production proof artifacts to approve
  or reject generated logic.
- Platform teams use Databricks manifests, deployment plans, governance gates, and metadata stores
  to operationalize approved work.

### Quiet CLI Behavior

Agents and operators should keep the main chat or terminal summary short. Show the stage, key
result, blocker/risk, recommendation, and next command. Detailed logs, full JSON, validation traces,
and long reports belong in workspace artifacts under `workspaces/<project>/interns/`.

## Workspace Output Layout

New projects live under `workspaces/<project>/`. Platform-generated outputs for
that project are grouped under `workspaces/<project>/interns/` so the project
root stays readable:

```text
workspaces/<project>/
  interns/
    state/        # workspace.db, run.log
    runs/         # per-run artifacts
    generated/    # contracts, profiles, evidence, solutions, requirements, memory, context
    reports/      # human-readable reports
```

`workspaces/**/interns/` is ignored by git because it contains local run output.

Fresh workspace onboarding:

```powershell
uv run onboard-workspace --workspace workspaces/<project>
```

KPI generation route after workspace confirmation:

```powershell
uv run prepare-kpi-generation --workspace workspaces/<project>
uv run apply-kpi-generation-answer --workspace workspaces/<project> --answer option_a
uv run finalize-kpi-generation --workspace workspaces/<project> --approve-final-preview
```

This workflow always presents two paths: KPI generation / BA-product interview, or the usual
onboarding workflow. KPI generation scores existing KPIs, accepts optional stakeholder context,
records decisions, creates a draft registry under `interns/generated/requirements/`, and only writes
a user-facing KPI registry after explicit final-preview approval.

Enterprise kickstart for a new governed workspace:

```powershell
uv run kickstart-workspace --workspace workspaces/<project>
```

Local hardware/resource preflight:

```powershell
uv run resource-preflight --workspace workspaces/<project>
```

This writes CPU, memory, disk, budget, worker, and run-mode evidence under
`workspaces/<project>/interns/generated/evidence/resource_preflight.json` and
`workspaces/<project>/interns/reports/resource_preflight.md`. Heavy local workflows use this
resource layer to choose safer defaults or block before exhausting disk/RAM. Onboarding uses the
resource decision to reduce profiling sample rows and disable expensive checks under pressure.
Medallion local builds use strict resource gating and recommend remote execution when the local run
is unsafe. Source-to-target planning writes `resource_transform_settings` into
`source_to_target_plan.json`; SQL generation includes the resource mode/strategy in generated SQL
and blocks local DuckDB SQL when the plan says local execution is unsafe. Local execution backends
also record resource decisions and stop before launching subprocesses when resource preflight blocks.
SQL/Polars/PySpark stage outcomes can be recorded with `record-engine-evolution`; this writes
structured `engine_evolution.json` plus a human-readable `evolution.md`, and hybrid source-to-target
planning reads those lessons before recommending an engine. Engine records include workload shape,
decision analysis, rejected alternatives, bottlenecks, validation, promotion state, and next
experiment suggestions so the planner learns from detailed evidence rather than one shallow timing.

Bounded context packs:

```powershell
uv run context-router build --workspace workspaces/<project> --task plan-source-to-target --budget standard
```

The context router builds a compact page index and task manifest from canonical workspace artifacts
instead of flooding chat or prompts with whole files. It writes:

```text
workspaces/<project>/interns/generated/context/context_index.json
workspaces/<project>/interns/generated/context/context_pages.jsonl
workspaces/<project>/interns/generated/context/manifests/<task>_<budget>.json
workspaces/<project>/interns/reports/context/<task>_<budget>.md
```

Budgets can be `small`, `standard`, or `deep`, with optional numeric caps for sections, bytes, and
estimated tokens. `plan-source-to-target` now creates a context manifest automatically and records
the manifest/wiki paths in `source_to_target_plan.json`.

External source-root discovery:

```powershell
uv run prepare-external-source-intake --external-root <external-source-root> --proposed-workspace workspaces/cms
uv run apply-external-source-intake --external-root <external-source-root> --proposed-workspace workspaces/cms --answer option_a
uv run discover-external-sources --workspace workspaces/<project> --external-root <external-source-root>
```

Use this when the user points to a large folder outside the repo. The intake workflow first asks
whether to create a new workspace or attach to an existing one, saves repo-level routing defaults,
asks for a reason when the user changes a saved default, then runs metadata-only discovery. The
discovery command keeps generated output under the repo workspace, classifies the external root by
paths and metadata only, groups raw data with nearby dictionaries/methodology docs, detects Delta
tables, DuckDB/SQLite files, specs, logs, and system state, then drafts
`docs/source_selection.generated.json` with review-gated local sources and medallion/ETL
recommendations.

Governed source catalog route for API, local/external, and Databricks Unity Catalog inputs:

```powershell
uv run source-catalog plan --workspace workspaces/<project>
uv run source-catalog preflight --workspace workspaces/<project>
uv run source-catalog api-fetch --workspace workspaces/<project> --source <source-id>
uv run source-catalog local-stage --workspace workspaces/<project> --source <source-id>
uv run source-catalog uc-inspect --workspace workspaces/<project> --source <source-id>
uv run source-catalog discover-docs --workspace workspaces/<project>
uv run source-catalog index-catalog --workspace workspaces/<project> --source <catalog-source-id>
uv run source-catalog match-catalog --workspace workspaces/<project> --source <catalog-source-id> --keyword claims
uv run source-catalog draft-selection --workspace workspaces/<project> --source <catalog-source-id>
uv run source-catalog finalize-selection --workspace workspaces/<project> --source <catalog-source-id> --approve-final-preview
uv run source-catalog process --workspace workspaces/<project>
uv run source-catalog validate --workspace workspaces/<project> --strict
```

Compatibility wrappers:

```powershell
uv run prepare-source-catalog --workspace workspaces/<project>
uv run ingest-source-catalog --workspace workspaces/<project>
```

Reusable source templates live under `config/source_catalogs/`. Workspace-approved selections live
under `workspaces/<project>/docs/source_selection.json`. Ingestion writes only into the workspace
and uses a concurrent API scheduler with shared per-host QPS throttling, retries, checkpoints,
quarantine artifacts, and atomic materialization. Large JSON catalogs are indexed from JSONL/NDJSON
or streamable JSON arrays without flooding the agent context; generated selections are promoted only
with `finalize-selection --approve-final-preview`.
`datasets/` or `docs/` trees and records provenance sidecars before normal onboarding. API sources
use conservative defaults for QPS, retries, `Retry-After`, byte caps, checkpoints, and quarantine.
Auth is bound by environment variable name only; secret values are not written to artifacts.
Processing writes staged Parquet/profile/drift evidence under `interns/generated/evidence/`.
Large source catalogs are indexed into compact JSONL, matched against workspace signals, and turned
into review-only source-selection drafts instead of being pasted into prompts.

Resolve KPI features after onboarding, optionally attaching reusable derivation candidates:

```powershell
uv run resolve-kpi-features --workspace workspaces/<project> --domain healthcare --include-candidates
```

For day-to-day KPI blocker work, prefer the governed wrapper that runs resolution, derived-feature
review generation, blocker panel generation, and artifact validation in one step:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain healthcare
```

Apply an accepted panel answer through the supported answer wrapper:

```powershell
uv run apply-kpi-panel-answer --workspace workspaces/<project> --domain healthcare --answer option_a
```

For Gemini/Claude/Codex launch guidance and short user intents such as `prepare blockers`,
`show blocker json`, and `accept option A`, see `.agents/CLI_LAUNCH.md`.

SQL, Polars, PySpark, and ETL/medallion generation must be data-model driven. KPI requirements
define the business question, but the data model and generated profiles choose the eligible source
datasets, joins, grain, temporal anchors, and loading layer. If a requested target engine or
medallion step cannot be proven from those artifacts or an accepted workspace definition, generation
should stop at a blocker question or solution brief instead of producing executable logic.

Build the source-to-target contract before executable generation:

```powershell
uv run build-relationship-contracts --workspace workspaces/<project>
uv run plan-source-to-target --workspace workspaces/<project> --target-engine sql
```

Relationship contracts gate trusted multi-dataset generation. Profile-only join candidates are
advisory; SQL/Polars/PySpark/ETL generation should use only data-model-proven or user-confirmed
relationships.

Before committing generated work or workspace changes, run:

```powershell
uv run validate-git-hygiene
```

This blocks raw data, generated workspace output, runtime state, logs, local databases, and
oversized files from being staged.

Kickstart scans workspace inputs, runs local-safe onboarding, writes or updates
the hybrid task entry in `config/tasks.json`, and records discovered enterprise
documents, accepted defaults, and open questions under `workspaces/<project>/interns/`.
It also runs a conservative first-pass feature resolver that writes
`interns/generated/contracts/kpi_feature_mapping.json`.

The onboarding command discovers data, KPI/metric registries, and data model
artifacts, profiles datasets with the canonical profiler, and generates baseline
contracts, reports, runner/evaluator scripts, and query artifacts under
`workspaces/<project>/interns/`. Executable KPI logic should be generated only
from evidence-backed mappings or explicit user decisions; unresolved features
remain in manifest/open-question form.

## CLI Workflow Reference

For agent-led conversations on the checked-in RCM workspace and fresh-start scenarios, see
`workspace_scenarios.md`.

### 1. Workspace Selection

```powershell
uv run list-workspace-files --workspace workspaces/<project>
```

Use this first when a user says `set workspace` or names a project. It lists workspace files up to
the configured cap and classifies likely KPI files, data model files, datasets, docs, and existing
`interns/` state. It does not parse raw datasets, profile data, or write artifacts. The listed file
set is the confirmation boundary.

### 2. KPI Generation Or Usual Workflow

```powershell
uv run prepare-kpi-generation --workspace workspaces/<project>
```

This creates a two-path panel:

1. KPI generation / BA-product interview
2. Usual workflow / existing KPI + data model

It also calculates KPI quality/readiness if KPI files exist. Optional stakeholder context can be
included with repeated `--context-file` values:

```powershell
uv run prepare-kpi-generation --workspace workspaces/<project> --context-file workspaces/<project>/docs/meeting_transcript.md
```

Advance the interview with:

```powershell
uv run apply-kpi-generation-answer --workspace workspaces/<project> --answer option_a
```

Finalize only after preview approval:

```powershell
uv run finalize-kpi-generation --workspace workspaces/<project> --approve-final-preview
```

### 3. Onboarding

```powershell
uv run onboard-workspace --workspace workspaces/<project>
```

Onboarding discovers KPI registries, data model docs, and datasets; profiles data; writes normalized
contracts; and creates baseline evaluation scaffolding under `interns/`.

### 4. Feature Resolution And Blocker Grilling

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

This is the preferred wrapper. It runs feature resolution, attaches derivation candidates, renders
derived-feature review files, creates a blocker question panel, and validates generated artifacts.

Apply a user answer from the current panel:

```powershell
uv run apply-kpi-panel-answer --workspace workspaces/<project> --domain <domain> --answer option_a
```

### 5. Relationship/FK Contracts

```powershell
uv run build-relationship-contracts --workspace workspaces/<project>
```

This is required before trusted multi-dataset generation. It converts data model and profile
evidence into relationship contracts. Profile-only relationships are advisory; executable
generation can use only `proven_data_model` or `user_confirmed` relationships.

### 6. Source-To-Target Planning

```powershell
uv run plan-source-to-target --workspace workspaces/<project> --target-engine sql
```

Use this before SQL, Polars, PySpark, ETL, or medallion generation. It explains selected datasets,
rejected datasets, feature mappings, joins, grain, temporal anchors, validation checks, blockers,
and target layer assumptions.

### 7. Executable SQL Generation

```powershell
uv run generate-kpi-sql --workspace workspaces/<project> --kpi-id kpi_001
```

Generated SQL uses ready feature mappings and executable relationship contracts. If a required join
is not proven, SQL generation must fail instead of guessing.

### 8. Databricks Planning And Deployment

```powershell
uv run prepare-databricks-assets --workspace workspaces/<project>
uv run prepare-genie-workspace --workspace workspaces/<project>
uv run deploy-databricks-workspace --workspace workspaces/<project>
```

These commands prepare manifests, workspace specs, runbooks, and dry-run deployment plans. Remote
mutation requires explicit approval and environment flags.

## Generated Artifact Catalog

All generated project artifacts live under:

```text
workspaces/<project>/interns/
```

The table below explains the important files, what is inside them, and who should inspect them.

For quick review, use this split:

| Artifact group | Human-readable? | Normal use |
|---|---:|---|
| `reports/*.md` | Yes | Reviewer and stakeholder summaries, plans, blockers, and proof. Start here before opening JSON. |
| `reports/**/current.md` | Yes | Current stakeholder-facing panel or question. Use this in chat/CLI reviews. |
| `generated/solutions/*.sql` | Yes | Generated executable KPI/query logic. Review before production use. |
| `evaluation/*.py` | Mostly | Workspace-local runner/evaluator scaffolding for scoreable execution. |
| `generated/contracts/*.json` | Partly | Machine-readable source of truth for KPI registry, mappings, relationships, and source-to-target plans. |
| `generated/profiles/*.json` | Partly | Profile-first dataset evidence: schema, row counts, nulls, sample values, and warnings. |
| `generated/requirements/*.json` | Partly | Workflow/session state for KPI generation, data-model generation, source intake, and production proof. |
| `generated/context/*` | Partly | Bounded context packs that help agents avoid reading every artifact into prompts. |
| `generated/evidence/*.json` | Partly | Preflight, bug, or audit evidence for debugging and governance. |
| `generated/memory/*` | Partly | Accepted decisions, rejected assumptions, and reusable workspace preferences. |
| `state/*.duckdb`, `state/*.db`, `state/delta_metadata/`, `state/metadata_store/` | No | Runtime databases and metadata caches. These are not normal reviewer documents. |

For simple KPI/query review, the usual starting files are:

```text
workspaces/<project>/interns/reports/source_to_target_plan.md
workspaces/<project>/interns/reports/relationship_contracts.md
workspaces/<project>/interns/generated/solutions/kpi_001.sql
workspaces/<project>/interns/reports/open_questions.md
```

### Requirements Artifacts

`generated/requirements/input_inventory.json`

Contains the discovered workspace inputs: dataset files, KPI registry files, and data model files.
Use it to confirm what the system treated as source material during onboarding.

`generated/requirements/stakeholder_interview.md`

Human-readable starter interview generated from workspace inputs and KPI definitions. It captures
initial assumptions and clarification prompts for business stakeholders.

`generated/requirements/kpi_generation_session.json`

State file for the KPI generation / BA-product interview workflow. It contains current stage,
detected files, optional context files, existing KPIs, quality scores, accepted decisions,
preferences, proof policy, draft KPIs, and competitive review output.

`generated/requirements/kpi_registry_draft.json`

Draft KPI registry created by KPI generation mode. It is safe generated output and is not the final
user-facing KPI registry. It includes draft KPI rows, advisor notes, per-KPI evidence proof
requirements, and competitive review findings.

`generated/requirements/kpi_generation_production_proof.json`

Final production-readiness proof for a generated KPI set. It records checks such as data quality
tests, edge-case tests, reconciliation checks, performance baseline, owner, SLA, governance
approval, and required actions before publish.

`generated/requirements/databricks_asset_manifest.json`

Databricks planning artifact for datasets, target Unity Catalog tables, workspace files, source
hashes, edit policy, execution truth, and non-Genie fallback behavior. It is a plan, not remote
mutation.

`generated/requirements/genie_workspace_spec.json`

Spec for Databricks workspace folders, Genie spaces, allowed tables, starter prompts, permissions,
drift detection, and guardrails. Genie remains an operator interface, not the source of truth.

`generated/requirements/databricks_workspace_deployment_plan.json`

Dry-run deployment plan describing proposed Databricks operations. It separates supported apply
operations from spec-only operations such as jobs, dashboards, and Genie spaces.

### Contract Artifacts

`generated/contracts/kpi_registry.json`

Normalized KPI registry extracted from source KPI files. It contains KPI names/questions,
descriptions, metrics, cuts/grain hints, refinement notes, source path, and mapping status. It is
the contract used by feature resolution.

`generated/contracts/domain_model.json`

Machine-readable summary of discovered data model docs and profiled datasets. It lists dataset
paths, formats, row counts, schemas, and profile paths. It is used as data model evidence, not as a
complete semantic model by itself.

`generated/contracts/semantic_contract.json`

Rules and expectations for term resolution. It records the evidence order: KPI registry, data
model docs/diagrams, profile evidence, dictionaries/metadata, then user clarification.

`generated/contracts/kpi_feature_mapping.json`

Main KPI mapping artifact. It lists every KPI, every extracted feature/business term, its state,
resolution type, source columns, evidence, candidates, blockers, decision history, and summary
counts. SQL/code generation should rely only on ready or user-confirmed features.

`generated/contracts/workspace_feature_definitions.json`

Reusable definitions accepted for the workspace, such as `paid = PaidAmount` or a derived formula.
Definitions include state, resolution type, source columns, evidence note, decision history, and
verification status.

`generated/contracts/relationship_contracts.json`

Production-grade relationship/FK contract. It contains relationships between datasets, join columns,
state, confidence, evidence sources, cardinality checks, null behavior, uniqueness checks,
referential-integrity checks, grain impact, approval state, owner, review date, source-system scope,
lineage export policy, promotion policy, rollback policy, and executable usage policy. Trusted
multi-dataset generation can use only executable-approved relationships.

`generated/contracts/source_to_target_plan.json`

Implementation plan for SQL, Polars, PySpark, or medallion/ETL generation. It records selected
datasets, rejected datasets, feature mappings, join plan, relationship contracts, grain, temporal
anchor, medallion layers, validation checks, blockers, and target engine.

### Profile Artifacts

`generated/profiles/profile_index.json`

Index of all dataset profiles. It includes dataset path, format, row count, schema, sources used,
warnings, and the path to each detailed profile. This is the first place to inspect before reading
raw datasets.

`generated/profiles/*.profile.json`

Detailed profile for one dataset. It contains schema, sample value evidence, null counts,
min/max-style diagnostics when available, warnings, and profiling metadata. These files are used for
column mapping, relationship/FK inference, and validation evidence.

### Solution Artifacts

`generated/solutions/kpi_metrics.sql`

Baseline KPI manifest from onboarding. It is a starter artifact and may be manifest-only until
feature mappings are resolved.

`generated/solutions/kpi_001.sql`, `kpi_002.sql`, ...

Generated SQL for individual KPIs after mappings and relationship contracts are ready. These files
create KPI feature views using resolved source columns and executable-approved relationships. They
should be treated as generated code and reviewed before production use.

`generated/solutions/*_databricks.sql`

Databricks-dialect SQL generated with catalog/schema table references instead of local DuckDB CSV
reads. Remote execution still requires explicit approval.

### Evidence And Review Reports

`reports/onboarding_report.md`

Human-readable onboarding summary: discovered inputs, KPI count, profile count, artifacts written,
and warnings. This is usually the first report a reviewer reads.

`reports/open_questions.md`

Open blockers and questions that could not be resolved from files, profiles, dictionaries, metadata,
or prior decisions. Agents should ask from these only after inspecting available evidence.

`reports/blocker_question_panel/current.json`

Machine-readable current blocker question. It includes blocker, question, options, recommended
answer, why, evidence files, answer type, reuse scope, and JSON-backed derived-feature options when
valid.

`reports/blocker_question_panel/current.md`

Human-readable version of the current blocker question. This is the preferred file to show to
stakeholders in CLI/chat.

`reports/blocker_question_panel/index.json`

Index of all generated blocker panels. Useful when reviewing what questions existed and which
feature/KPI each applied to.

`reports/derived_feature_reviews/md/*.md`

Human-readable derived-feature review files. They explain formulas, inputs, examples, evidence
state, confidence, remaining risk, and whether user confirmation is required.

`reports/derived_feature_reviews/json/*.json`

Machine-readable strict derived-feature evidence. These JSON files back the Markdown reviews and
prevent prose-only formula approval.

`reports/relationship_contracts.md`

Human-readable relationship/FK contract summary. It shows relationship IDs, state, confidence,
join columns, executable SQL allowance, and approval state.

`reports/source_to_target_plan.md`

Human-readable implementation plan for each KPI. It shows selected sources, feature mappings,
validation checks, blockers, grain, and output target.

`reports/kpi_generation/current.json`

Machine-readable current KPI generation panel. It contains the current question, options,
recommendation, quality score, draft preview, proof notes, and competitive review depending on the
stage.

`reports/kpi_generation/current.md`

Human-readable KPI generation panel. This is the preferred artifact to show business analysts and
product leads during KPI generation.

`reports/kpi_generation/production_proof.md`

Human-readable final production-readiness proof for the generated KPI set. It lists checks and
required actions before publishing.

`reports/databricks_workspace_deployment_dry_run.md`

Human-readable Databricks deployment plan. It explains proposed operations and which ones are
spec-only versus supported for apply after approval.

`reports/genie_operator_runbook.md`

Runbook for Genie/Databricks operators. It explains the workspace layout, guardrails, permissions,
and how Genie should be used without becoming the source of truth.

### Memory Artifacts

`generated/memory/kpi_generation_workspace_memory.json`

Workspace-level memory for KPI generation preferences, accepted decisions, final registry path, and
production readiness. These facts apply only to the current workspace.

`generated/memory/genie_workspace_decisions.json`

Accepted decisions and defaults for Databricks/Genie workspace setup.

`generated/memory/evolution.md`

Human-readable lessons, accepted decisions, rejected assumptions, and future optimization hints.

`generated/memory/lessons.json`

Machine-readable lessons captured during workspace setup or deployment planning.

`state/team_memory/kpi_generation_preferences.json`

Repo-level/team-level preference memory. It stores stable preferences such as proof policy,
advisor style, final-write policy, and preferred KPI format behavior. Business definitions should
usually remain workspace-scoped unless explicitly promoted.

### Evaluation And Runtime Artifacts

`evaluation/experiment.py`

Workspace-local experiment runner scaffold. It executes the configured SQL/baseline path and prints
timing/success fields for the optimization loop.

`evaluation/evaluator.py`

Workspace-local evaluator scaffold. It checks result shape and provides a scoreable interface for
the loop.

`state/workspace.db`

SQLite runtime state for experiments, logs, intern activity, governance decisions, and alerts when
the loop/dashboard is used.

`state/run.log`

Workspace run log. Useful for debugging but should not be treated as source of truth.

`state/delta_metadata/`

Local Delta-backed structured metadata store for generated collections when available.

`state/metadata_store/`

JSON fallback metadata store used when Delta writes are unavailable.

## Metadata Store

Autoresearch uses hybrid storage:

- executable artifacts stay as files under `workspaces/<project>/interns/`
  (`kpi_metrics.sql`, evaluator scripts, reports, logs, DuckDB state);
- structured JSON state is written through a metadata store
  (`contracts`, `profiles`, `requirements`, `bootstrap`, mappings, decisions).

Local mode requires no setup and stores structured metadata as local Delta tables under:

```text
workspaces/<project>/interns/state/delta_metadata/
```

If Delta writes fail, the system falls back to JSON under:

```powershell
workspaces/<project>/interns/state/metadata_store/
```

Enterprise Databricks deployments can map the same collections to Delta tables
in Unity Catalog. MongoDB remains optional for document-store environments:

```powershell
$env:AUTORESEARCH_METADATA_BACKEND = "mongo"
$env:AUTORESEARCH_MONGO_URI = "mongodb://..."
$env:AUTORESEARCH_MONGO_DB = "autoresearch"
uv sync --extra enterprise-metadata
```

If MongoDB is unavailable, writes fall back to the local JSON metadata store and
record a warning under the workspace reports.

The experiment loop also performs local-safe auto-bootstrap. If required
`interns/` artifacts are missing or stale, it fingerprints workspace inputs,
reruns onboarding, and then executes the local baseline. Existing artifacts are
reused when the input fingerprint is current.

Databricks is never used for remote execution just because credentials exist.
The system may health-check Databricks, but remote execution requires explicit
approval via:

```powershell
$env:AUTORESEARCH_ALLOW_REMOTE_EXECUTION = "1"
```

Use strict Databricks validation when a remote test must fail closed instead of
falling back to DuckDB:

```powershell
$env:AUTORESEARCH_DATABRICKS_STRICT = "1"
```

The governed execution modes are `sql`, `polars`, `sql_polars_hybrid`, and
`pyspark`. Databricks hardening starts with SQL Warehouse execution; generated
KPI SQL can target Databricks tables only after feature mappings are fully
proven or user-confirmed:

```powershell
uv run generate-kpi-sql --workspace workspaces/<project> --kpi-id kpi_001 --dialect databricks --catalog main --schema autoresearch
```

Before executing that SQL remotely, create the Databricks asset manifest and
register/upload the listed datasets only after explicit approval:

```powershell
uv run prepare-databricks-assets --workspace workspaces/<project> --environment dev --domain rcm --catalog dev --schema rcm
```

## Enterprise Databricks Operating Model

Autoresearch uses a federated enterprise model:

- Git owns reusable engine code, tests, skills, policies, prompts, and deployment manifests.
- Databricks owns operational assets: Unity Catalog tables, jobs, notebooks, dashboards,
  Genie spaces, MLflow traces, and Delta evidence tables.
- Genie is an interactive Databricks workspace operator, not the source of truth.
- API/CI automation must be able to run the same workflows without Genie.
- Local DuckDB execution is a syntax/layout/tiny-fixture smoke test path only.
  Enterprise promotion evidence must come from Databricks.

Recommended environment layout:

```text
shared non-prod Databricks workspace
  /Workspace/Autoresearch/dev/<domain>
  /Workspace/Autoresearch/stage/<domain>
  dev.<domain>
  stage.<domain>

isolated prod Databricks workspace
  /Workspace/Autoresearch/prod/<domain>
  prod.<domain>
```

The Databricks asset manifest records dataset registration targets, generated
workspace file targets, source hashes, edit policies, promotion gates, Genie
usage rules, and the non-Genie API fallback contract.

Generate the local Genie workspace setup bundle from that manifest:

```powershell
uv run prepare-genie-workspace --workspace workspaces/<project>
```

This writes:

```text
workspaces/<project>/interns/generated/requirements/genie_workspace_spec.json
workspaces/<project>/interns/reports/genie_operator_runbook.md
workspaces/<project>/interns/generated/memory/genie_workspace_decisions.json
workspaces/<project>/interns/generated/memory/evolution.md
workspaces/<project>/interns/generated/memory/lessons.json
```

The Genie workspace spec is review-only by default. It does not create remote
Databricks folders, Genie spaces, jobs, dashboards, or permissions until a
future deployer is explicitly approved.

Plan Databricks deployment from the reviewed Genie workspace spec:

```powershell
uv run deploy-databricks-workspace --workspace workspaces/<project>
```

This writes:

```text
workspaces/<project>/interns/generated/requirements/databricks_workspace_deployment_plan.json
workspaces/<project>/interns/reports/databricks_workspace_deployment_dry_run.md
state/databricks/deployments/latest.md
state/databricks/deployments/deployment_index.json
```

Remote mutation is disabled by default. The deployer can apply supported
workspace folder/file operations plus Unity Catalog schema and evidence-table
setup only when all approval signals are present:

```powershell
$env:AUTORESEARCH_ALLOW_REMOTE_EXECUTION = "1"
uv run deploy-databricks-workspace --workspace workspaces/<project> --apply --confirm-remote-mutation
```

Raw dataset registration, Jobs, dashboards, and Genie spaces remain spec-only
until their API payloads, compute policy, storage-location policy, and
permissions have been reviewed.

Deployment evidence is written in two places. The active workspace keeps the
project audit copy under `workspaces/<project>/interns/`, while repo-level
runtime state under `state/databricks/deployments/` records the latest and recent
Databricks deployment attempts across workspaces. This lets operators switch
active projects without losing sight of platform connection and deployment
status.

For a team-oriented overview of ingestion patterns, Bronze/Silver/Gold responsibilities, data
cleaning techniques, serving patterns, and modern data tooling, see
`docs/reference/data_workflow_medallion_reference.md`.

For the current Databricks production practices reference, including Unity Catalog, Lakeflow,
Auto Loader, Delta maintenance, SQL warehouses, CI/CD, security, cost, and observability guidance,
see `docs/reference/databricks_production_practices.md`.

## Tool-Agnostic Skills

Repo skills live once under `skills/*/SKILL.md`. They can be exposed to Codex,
Claude Code, Gemini CLI, or other agents through generated adapters instead of
manual per-tool copies:

```powershell
uv run generate-skill-adapters
```

This writes:

```text
.agents/skills_index.json
.agents/generic/SKILLS.md
.agents/claude/SKILLS.md
.agents/gemini/SKILLS.md
.agents/codex/SKILLS.md
```

Adapters are lightweight by default: they include each skill name, description,
path, and routing rules. Use full embedding only when a tool cannot read local
files:

```powershell
uv run generate-skill-adapters --embed-full
```

The generated files are tool-agnostic routing instructions. `skills/*/SKILL.md`
remains the source of truth.


## Promotion Model

Candidate optimizations are evaluated in a sandbox first. An improved metric is
not enough to promote a change: the run must pass semantic, SLA, correctness,
mode, and policy gates. Production defaults require human review; global
exploration can discover options but cannot auto-promote.

The default downcast policy is conservative:

- Integer downcasts require exact min/max proof.
- Float and decimal downcasts require explicit approval.
- Sample min/max values are diagnostic only; exact bounds are authoritative for production.

## Fresh Start

This repository intentionally excludes local datasets, state databases, run logs, credentials, caches, and generated profiler outputs. Add enterprise data through catalog/backend configuration, not by committing raw data files.

Never commit `.env`, tokens, local `state/`, DuckDB databases, CSV/PDF source dumps, parquet outputs, or generated hotspot/profile artifacts.

## Verify

```bash
uv run python -m unittest tests.test_enterprise_optimization
uv run python -m compileall core interns tools tests dashboard.py
```

Workspace-specific benchmarks require local/catalog data to be provisioned first.

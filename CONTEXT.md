# Domain Model

## Core Concepts
- **LLMEngine**: A strategy interface for AI generation that abstracts away the underlying execution method (API vs CLI).
- **Workspace**: A unified state manager that encapsulates both persistent version control (Git operations) and ephemeral execution state, backed by a single SQLite datastore (`workspace.db`). Always active; never replaced by remote backends.
- **MetricParser**: A strategy interface responsible for extracting and structuring metrics from raw process logs. The single authoritative metric extraction point.
- **DecisionStrategy**: A strategy interface encapsulating the logic for evaluating whether an experiment's metrics constitute a success or failure, allowing multi-objective tracking.
- **InsightsIntern**: A consolidated sub-agent that performs both data analysis and deep research, eliminating overlap and reducing token expenditure.
- **ExecutionBackend**: A strategy interface for running one experiment iteration, abstracting DuckDB (local default), Databricks SQL Warehouse, Databricks Connect (local→remote Spark), and Databricks Jobs (submit + poll) as interchangeable implementations. Selected via `config/lock.toml [databricks] execution`. Databricks remote execution still requires `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`; strict validation can set `AUTORESEARCH_DATABRICKS_STRICT=1` to fail closed instead of falling back to DuckDB.
- **TelemetryBackend**: A strategy interface for experiment observability. `LocalTelemetry` wraps `Workspace` (SQLite, always active). `DatabricksTelemetry` mirrors writes to MLflow 3 (experiment tracking, LLM tracing, GenAI evaluation) and Delta tables when Databricks is configured. Both run simultaneously — Databricks is additive, not a replacement.
- **DatabricksClient**: A thin wrapper around the Databricks SDK `WorkspaceClient`. Reads credentials from `DatabricksConfig` (sourced from `lock.toml` + env vars). Provides health checks, job submission, Delta writes, and MLflow experiment setup. All imports are lazy — loads with zero cost when Databricks is disabled.

## Enterprise Optimization Concepts

- **SemanticContract**: A machine-readable guardrail bundle derived from KPI registries, methodology JSON, data dictionaries, and task-level rules. It defines what optimization must preserve before performance improvements can be accepted.
- **ChangeClassifier**: A deterministic diff classifier that labels candidate patches as optimization patterns such as `predicate_pushdown`, `join_rewrite`, `cte_rewrite`, `aggregation_rewrite`, `case_simplification`, or `column_pruning`.
- **OptimizationMemory**: Structured experiment memory stored in `workspace.db`. It records what changed, why it was expected to help, what happened, which guardrails passed or failed, and whether the candidate was kept or discarded.
- **OptimizationPlanner**: A strategy ranker that combines hotspot reports, semantic contracts, and optimization memory to recommend the next optimization pattern. This is the first step toward adaptive optimization.
- **OptimizationPolicy**: Versioned execution, failure, approval, downcast, and SLA policy loaded from task config. Production mode is bounded; global exploration is evidence-only unless explicitly allowed.
- **DataModelProfiler**: Metadata-first profiler that records schema, source stats, sample bounds, exact bounds, and conservative downcast recommendations for local file-backed datasets.
- **WorkspaceKickstarter**: Enterprise onboarding bridge that scans workspace inputs, classifies likely KPI, policy, SLA, contract, dictionary, model, and methodology documents, runs local-safe onboarding, and writes a hybrid task entry plus discovery artifacts.
- **DatabricksAssetManifest**: Environment/domain deployment contract for Databricks-first enterprise operation. It maps workspace datasets to Unity Catalog tables, generated files to Databricks Workspace paths, source hashes to drift detection, Genie to interactive operation, and API/CI to non-Genie fallback.
- **GenieWorkspaceSpec**: Review-only deployment bundle generated from the Databricks asset manifest. It defines workspace folders, Genie spaces, starter prompts, permissions, jobs, dashboards, non-Genie fallback, drift checks, and evolution memory before any remote Databricks mutation is allowed.
- **DatabricksWorkspaceDeploymentPlan**: Dry-run-first deployment boundary for Databricks workspace setup. It can apply reviewed workspace folder/file operations and Unity Catalog governance schema/evidence-table setup only with explicit remote approval; raw dataset registration, Jobs, dashboards, and Genie spaces remain spec-only until reviewed.
- **GovernanceEvaluator**: Promotion gatekeeper that converts run evidence into `approved`, `needs_review`, or `rejected` decisions with evidence packs and alert events.
- **Polars-first data handling**: Dataframe work uses Polars by default for profiling,
  schema inspection, sampling, KPI preparation, and file processing. Pandas is allowed
  only at narrow third-party integration boundaries that require it, with the reason
  documented and conversion back to Polars kept local.

## Core Package Layout

`core/` is split by platform responsibility:

- `core/orchestration/`: experiment loop and runner.
- `core/execution/`: local and Databricks execution backends.
- `core/governance/`: contracts, policies, semantic rules, mode planning, and promotion gates.
- `core/optimization/`: diff classification, strategy planning, decision strategy, and adaptive memory.
- `core/profiling/`: data model profiling and downcast diagnostics.
- `core/agents/`: intern routing, registry, and LLM engine abstractions.
- `core/observability/`: metric parsing and telemetry backends.
- `core/storage/`: SQLite/Git workspace state.

Project-specific runtime output belongs under `workspaces/<project>/interns/`,
not directly in the project root. The loop stores task-scoped `workspace.db`,
`run.log`, run artifacts, generated evidence, and reports there.

Fresh workspace setup is local-safe by default. The loop fingerprints workspace
inputs and auto-runs onboarding when generated `interns/` artifacts are missing
or stale. Remote execution such as Databricks requires explicit approval and
falls back to local DuckDB without that approval.

Structured JSON artifacts use a pluggable metadata store. Local mode stores
metadata as Delta tables under `interns/state/delta_metadata/`, with JSON fallback
under `interns/state/metadata_store/`. Enterprise Databricks deployments can map
the same contracts, profiles, requirements, bootstrap state, mappings, decisions,
and evidence to Delta tables in Unity Catalog. Executable SQL, scripts, logs,
reports, images, and raw workspace inputs remain file-based.

Databricks deployment reporting has both project and platform scope. Project
audit artifacts stay under the active workspace's `interns/` tree. Cross-workspace
operator state, including the latest Databricks deployment report and deployment
index, is written under repo-local `state/databricks/deployments/` so switching
workspaces does not hide the current platform status.

Skills are tool-agnostic. `skills/*/SKILL.md` is the canonical skill source, and
`generate-skill-adapters` creates `.agents/<tool>/SKILLS.md` plus a shared
`.agents/skills_index.json` for Claude Code, Gemini CLI, Codex, and generic
agents. Adapters should be lightweight unless a target tool cannot read local
files and requires `--embed-full`.

Enterprise deployments are Databricks-primary. DuckDB/local execution is a
developer smoke-test harness for syntax, layout, and tiny fixtures; production
evidence, scoring, promotion, lineage, and governance must come from Databricks.
The recommended team model is federated: platform owns standards, CI/CD,
identity, manifests, and promotion gates, while domain teams own notebooks,
jobs, dashboards, Genie spaces, and KPI logic inside approved environment and
domain boundaries.

Agents should read `AGENTS.md` for operating rules. Repo-native skills in `skills/`
define the stakeholder interview, preference memory, task onboarding, workspace
governance, and evolution process.


## Enterprise Data Flow

```
KPI registry / data model / methodology
  -> WorkspaceKickstarter / WorkspaceOnboarder
  -> SemanticContract
  -> OptimizationPolicy + ModePlan
  -> DataModelProfiler metadata/sample/exact evidence
  -> OptimizationPlanner
  -> intern suggestions / candidate patch
  -> ExecutionBackend
  -> evaluator + profiler guardrails
  -> ChangeClassifier
  -> GovernanceEvaluator + EvidencePack + AlertEvent
  -> OptimizationMemory
  -> dashboard review / approval queue
  -> next-run strategy ranking
```

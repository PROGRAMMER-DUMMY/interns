# Domain Model

## Core Concepts
- **LLMEngine**: A strategy interface for AI generation that abstracts away the underlying execution method (API vs CLI).
- **Workspace**: A unified state manager that encapsulates both persistent version control (Git operations) and ephemeral execution state, backed by a single SQLite datastore (`workspace.db`). Always active; never replaced by remote backends.
- **MetricParser**: A strategy interface responsible for extracting and structuring metrics from raw process logs. The single authoritative metric extraction point.
- **DecisionStrategy**: A strategy interface encapsulating the logic for evaluating whether an experiment's metrics constitute a success or failure, allowing multi-objective tracking.
- **InsightsIntern**: A consolidated sub-agent that performs both data analysis and deep research, eliminating overlap and reducing token expenditure.
- **ExecutionBackend**: A strategy interface for running one experiment iteration, abstracting DuckDB (local default), Databricks SQL Warehouse, Databricks Connect (local→remote Spark), and Databricks Jobs (submit + poll) as interchangeable implementations. Selected via `config/lock.toml [databricks] execution`.
- **TelemetryBackend**: A strategy interface for experiment observability. `LocalTelemetry` wraps `Workspace` (SQLite, always active). `DatabricksTelemetry` mirrors writes to MLflow 3 (experiment tracking, LLM tracing, GenAI evaluation) and Delta tables when Databricks is configured. Both run simultaneously — Databricks is additive, not a replacement.
- **DatabricksClient**: A thin wrapper around the Databricks SDK `WorkspaceClient`. Reads credentials from `DatabricksConfig` (sourced from `lock.toml` + env vars). Provides health checks, job submission, Delta writes, and MLflow experiment setup. All imports are lazy — loads with zero cost when Databricks is disabled.

## Enterprise Optimization Concepts

- **SemanticContract**: A machine-readable guardrail bundle derived from KPI registries, methodology JSON, data dictionaries, and task-level rules. It defines what optimization must preserve before performance improvements can be accepted.
- **ChangeClassifier**: A deterministic diff classifier that labels candidate patches as optimization patterns such as `predicate_pushdown`, `join_rewrite`, `cte_rewrite`, `aggregation_rewrite`, `case_simplification`, or `column_pruning`.
- **OptimizationMemory**: Structured experiment memory stored in `workspace.db`. It records what changed, why it was expected to help, what happened, which guardrails passed or failed, and whether the candidate was kept or discarded.
- **OptimizationPlanner**: A strategy ranker that combines hotspot reports, semantic contracts, and optimization memory to recommend the next optimization pattern. This is the first step toward adaptive optimization.
- **OptimizationPolicy**: Versioned execution, failure, approval, downcast, and SLA policy loaded from task config. Production mode is bounded; global exploration is evidence-only unless explicitly allowed.
- **DataModelProfiler**: Metadata-first profiler that records schema, source stats, sample bounds, exact bounds, and conservative downcast recommendations for local file-backed datasets.
- **GovernanceEvaluator**: Promotion gatekeeper that converts run evidence into `approved`, `needs_review`, or `rejected` decisions with evidence packs and alert events.

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


## Enterprise Data Flow

```
KPI registry / data model / methodology
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

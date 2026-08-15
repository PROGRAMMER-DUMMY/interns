# Harness Architecture Context: `core/onboarding/harness`

This document provides an exhaustive, file-by-file reference for all test and evaluation harnesses in [`core/onboarding/harness`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness).

---

## Executive Overview & ASCII Architectural Model

The `harness` package implements the governed quality-assurance, reliability, trajectory, and evaluation control plane for workspace onboarding, KPI execution, multi-engine code generation, and agent workflow compliance.

```
                                  ┌─────────────────────────────┐
                                  │      Reliability Suite      │
                                  │   (reliability_suite.py)    │
                                  └──────────────┬──────────────┘
                                                 │
                  ┌──────────────────────────────┴──────────────────────────────┐
                  ▼                                                             ▼
┌───────────────────────────────────┐                         ┌───────────────────────────────────┐
│          Project Harness          │                         │      Workflow Guard Harness       │
│        (project_harness.py)       │                         │    (workflow_guard_harness.py)    │
└─────────────────┬─────────────────┘                         └─────────────────┬─────────────────┘
                  │                                                             │
 ┌────────────────┴────────────────┐                                            ▼
 │                                 │                          ┌───────────────────────────────────┐
 ▼                                 ▼                          │        Trajectory Recorder        │
┌───────────────────┐    ┌───────────────────┐                │     (trajectory_recorder.py)    │
│Intent Coverage    │    │Engine Generation  │                └─────────────────┬─────────────────┘
│(intent_coverage.py)│    │(engine_gen.py)    │                                  │
└───────────────────┘    └───────────────────┘                                  ▼
                                                              ┌───────────────────────────────────┐
┌───────────────────┐    ┌───────────────────┐                │       State Health Monitor        │
│Layered & Exec     │    │AI App & CLI       │                │         (state_health.py)       │
│(pipeline_*.py)    │    │(ai_app/cli_*.py)  │                └───────────────────────────────────┘
└───────────────────┘    └───────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/__init__.py#L1-L3)

- **Exact Purpose**: Package initialization file for top-level project harness commands.
- **Key Functions / Classes**: None (Docstring only).
- **Inputs & Outputs**: None.
- **Failure Modes & Edge Cases**: None.

---

### 2. [`ai_app_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_app_harness.py#L1-L914)

- **Exact Purpose**: Dependency-free test harness for evaluating local stub and remote HTTP AI model outputs (eval types: `exact_match`, `schema_check`, `keyword`, `sql_semantic`, `kpi_mapping`, `result_table`).
- **Key Functions / Classes**:
  - [`AITestRecord`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_app_harness.py#L36-L54): Dataclass recording single AI test execution metrics and evaluation results.
  - [`AIAppHarnessResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_app_harness.py#L56-L81): Dataclass encapsulating run-level evaluation statistics.
  - [`AIAppHarness(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_app_harness.py#L83-L271): Core harness class that loads JSONL test datasets, executes test cases, checks baseline regressions, and writes report artifacts.
  - [`_call_http_ai(prompt, config)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_app_harness.py#L279-L312): Performs HTTP POST requests to remote AI APIs using `urllib.request`.
  - [`_evaluate(eval_type, output, case)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_app_harness.py#L334-L346): Dispatches evaluation to specific assertion logic.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_app_harness.py#L889-L914): CLI entry point for AI app harness.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, dataset path (`.jsonl`), optional config file, `--allow-remote-ai` flag.
  - *Outputs*: JSON and Markdown report artifacts written under `interns/reports/ai_app_harness/` and `interns/evidence/ai_app_harness/`.
- **Failure Modes & Edge Cases**:
  - `http_ai` targets are safely blocked unless `--allow-remote-ai` is explicitly passed.
  - Config files containing inline `api_key` raise `ValueError` (must use `api_key_env`).

---

### 3. [`ai_cli_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_cli_harness.py#L1-L593)

- **Exact Purpose**: Governed CLI-agent harness testing whether CLI agents follow project tool workflows, avoid raw dataset reads, pass command policies, and satisfy `WorkflowGuardHarness`.
- **Key Functions / Classes**:
  - [`CLIHarnessRecord`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_cli_harness.py#L51-L69): Dataclass holding CLI command execution records and transcript evaluations.
  - [`AICLIHarnessResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_cli_harness.py#L71-L94): Summary dataclass for CLI harness execution.
  - [`AICLIHarness(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_cli_harness.py#L96-L289): Executes test commands, validates command policies, and invokes workflow guardrails.
  - [`_command_policy(transcript, case)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_cli_harness.py#L343-L370): Evaluates captured CLI commands against required/forbidden lists, bad command markers, and raw dataset read rules.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/ai_cli_harness.py#L570-L593): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace, dataset path, optional CLI tool config, `--allow-cli-exec` flag.
  - *Outputs*: `AICLIHarnessResult`, `.commands.jsonl` transcripts, JSON and Markdown reports.
- **Failure Modes & Edge Cases**:
  - Real CLI execution (`target="cli"`) requires `--allow-cli-exec`; otherwise records `status="blocked_cli_execution"`.

---

### 4. [`engine_generation_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/engine_generation_harness.py#L1-L414)

- **Exact Purpose**: Static coherence validator for multi-engine KPI code outputs (`interns/generated/solutions/`) across DuckDB SQL, Polars, and PySpark without executing remote Spark/Java engines.
- **Key Functions / Classes**:
  - [`EngineCheck`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/engine_generation_harness.py#L58-L77): Single engine artifact check dataclass.
  - [`KPIEngineRecord`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/engine_generation_harness.py#L80-L104): Aggregated checks for all engines of one KPI.
  - [`EngineGenerationHarness(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/engine_generation_harness.py#L128-L327): Scans solution directory, verifies SQL view definitions, compiles Python scripts, checks for fatal Polars patterns (`pl.today()`) and PySpark preflight markers.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/engine_generation_harness.py#L400-L414): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path.
  - *Outputs*: `EngineGenerationHarnessResult`, JSON/Markdown reports under `interns/reports/engine_generation_validation/`.
- **Failure Modes & Edge Cases**:
  - Python syntax errors in Polars or PySpark scripts cause check failure.
  - Missing claimed engines reported by `engine_generation/current.json` trigger record failure.

---

### 5. [`intent_coverage_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/intent_coverage_harness.py#L1-L284)

- **Exact Purpose**: High-level guardrail verifying that generated KPI SQL realizes all declared intent (grain dimensions, metric aggregations, filters) independently of the generator.
- **Key Functions / Classes**:
  - [`KPICoverageRecord`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/intent_coverage_harness.py#L36-L46): Intent coverage result for a single KPI.
  - [`IntentCoverageResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/intent_coverage_harness.py#L49-L73): Harness result dataclass.
  - [`KPIIntentCoverageHarness(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/intent_coverage_harness.py#L76-L210): Loads KPI registries, feature mappings, and SQL files, then runs `evaluate_intent_coverage`.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/intent_coverage_harness.py#L253-L284): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, contracts directory.
  - *Outputs*: `IntentCoverageResult`, JSON/Markdown reports under `interns/reports/kpi_intent_coverage/`.
- **Failure Modes & Edge Cases**:
  - Missing generated SQL file for a registered KPI generates an `error` severity finding and fails the harness.

---

### 6. [`layered_pipeline_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/layered_pipeline_harness.py#L1-L91)

- **Exact Purpose**: Validates presence of medallion-layer pipeline contracts (`catalog_contract.json`, `data_engineering_route.json`, `pipeline_plan.json`) and verifies silver layer deduplication approval gates.
- **Key Functions / Classes**:
  - [`LayeredPipelineHarnessResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/layered_pipeline_harness.py#L13-L21): Summary dataclass.
  - [`LayeredPipelineHarness(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/layered_pipeline_harness.py#L23-L68): Runs contract presence and deduplication checks.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/layered_pipeline_harness.py#L84-L91): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path.
  - *Outputs*: JSON/Markdown reports under `interns/reports/layered_pipeline_harness/`.
- **Failure Modes & Edge Cases**:
  - Missing contract files or blocked pipeline plans set `ok=False`.

---

### 7. [`pipeline_execution_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/pipeline_execution_harness.py#L1-L112)

- **Exact Purpose**: Validates generated medallion ETL/ELT SQL scripts (`pipeline_layers.sql`) and redacts PII/PHI sample columns (e.g. SSN, phone, address, patient ID).
- **Key Functions / Classes**:
  - [`PipelineExecutionRecord`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/pipeline_execution_harness.py#L14-L20): Dataclass for single layer execution sample.
  - [`PipelineExecutionHarnessResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/pipeline_execution_harness.py#L22-L33): Harness output dataclass.
  - [`PipelineExecutionHarness(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/pipeline_execution_harness.py#L35-L76): Checks existence of `pipeline_layers.sql` and generates redacted sample tables.
  - [`_redacted_sample(sql, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/pipeline_execution_harness.py#L86-L102): Inspects SQL and referenced CSV sample lines for sensitive patterns, replacing them with `<redacted>`.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/pipeline_execution_harness.py#L105-L112): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path.
  - *Outputs*: JSON/Markdown reports under `interns/reports/pipeline_execution_harness/`.
- **Failure Modes & Edge Cases**:
  - Non-existent `pipeline_layers.sql` records error and sets `ok=False`.

---

### 8. [`project_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/project_harness.py#L1-L753)

- **Exact Purpose**: Central, scoreable project harness aggregator that runs artifact validation, KPI execution, git hygiene, workflow guardrails, evidence graph, and benchmark release gates.
- **Key Functions / Classes**:
  - [`ProjectHarnessResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/project_harness.py#L41-L62): Aggregated score and check result dataclass.
  - [`ProjectHarness(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/project_harness.py#L64-L375): Executes and aggregates all sub-harnesses, calculates weighted overall score, and determines release blocker status.
  - [`_score(checks)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/project_harness.py#L377-L402): Computes weighted numerical score (validation=17%, execution=21%, core=21%, gates=17%, hygiene=9%, workflow=10%, graph=5%).
  - [`_collect_blockers(checks)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/project_harness.py#L500-L539): Consolidates hard blockers across all sub-checks.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/project_harness.py#L699-L753): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, domain, threshold (default 95.0), `--cross-engine` flag.
  - *Outputs*: `ProjectHarnessResult`, `project_harness.json`, `project_harness.md`.
- **Failure Modes & Edge Cases**:
  - Note (Security S4): Project harness evaluates evidence completeness and data quality, NOT security vulnerabilities.
  - Hard blockers fail the run even if numerical score exceeds threshold.

---

### 9. [`reliability_suite.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/reliability_suite.py#L1-L411)

- **Exact Purpose**: Local-safe reliability suite running workflow guardrails, evidence graph builder, project harness, and validating harness evidence artifacts without external network dependencies.
- **Key Functions / Classes**:
  - [`ReliabilitySuiteResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/reliability_suite.py#L26-L46): Suite summary dataclass.
  - [`ReliabilitySuite(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/reliability_suite.py#L49-L316): Executes suite checks and validates artifact health.
  - [`_run_harness_artifact_check(name, artifact_rel, *, required)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/reliability_suite.py#L240-L290): Inspects JSON evidence files for malformed payloads or missing required contracts.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/reliability_suite.py#L384-L411): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, domain, project harness execution mode (`auto`/`run`/`skip`).
  - *Outputs*: JSON and Markdown reports under `interns/reports/reliability_suite/`.
- **Failure Modes & Edge Cases**:
  - Missing required harness evidence artifacts cause check failure.

---

### 10. [`state_health.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/state_health.py#L1-L278)

- **Exact Purpose**: Read-only health monitor scanning workspace state/audit files (`trajectory.jsonl`, `audit_chain.jsonl`, `session.json`, `run.log`) to surface unrotated file growth and stale sessions.
- **Key Functions / Classes**:
  - [`StateHealthResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/state_health.py#L122-L130): Dataclass encapsulating file size and stale session metrics.
  - [`scan_workspace_state(layout, *, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/state_health.py#L87-L119): Recursively scans `state_dir`, categorizing files by kind, flagging large files (>=10MB) and stale sessions (>90 days).
  - [`scan_all_workspaces(repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/state_health.py#L214-L241): Scans all workspaces under `workspaces/` for cross-workspace reporting.
  - [`record_state_health(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/state_health.py#L171-L211): Generates current JSON/MD reports.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/state_health.py#L243-L278): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path or `--all-workspaces`.
  - *Outputs*: Read-only health reports written to `interns/reports/state_health/`.
- **Failure Modes & Edge Cases**:
  - Unreadable or missing files are safely skipped without mutating disk state.

---

### 11. [`trajectory_recorder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/trajectory_recorder.py#L1-L507)

- **Exact Purpose**: Workspace-scoped, cross-platform thread-safe trajectory and tamper-evident audit chain recorder for tracking agent actions, CLI tool invocations, and workflow decisions.
- **Key Functions / Classes**:
  - [`TrajectoryRecordResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/trajectory_recorder.py#L23-L40): Summary dataclass for trajectory events.
  - [`WorkspaceTrajectoryRecorder(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/trajectory_recorder.py#L42-L186): Manages `workspace_lock`-protected appends to `trajectory.jsonl` and `audit_chain.jsonl`, maintains incremental summary counters (`trajectory_summary_state.json`), and renders bounded tail reports.
  - [`record_trajectory_event_safe(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/trajectory_recorder.py#L194-L238): Safe helper that catches exceptions to prevent trajectory logging from crashing primary workflows.
  - [`load_trajectory(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/trajectory_recorder.py#L257-L273): Reads full trajectory event history.
  - [`_tail_lines(path, n)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/trajectory_recorder.py#L276-L293): Efficient seek-backward reader for retrieving recent trajectory lines.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/trajectory_recorder.py#L469-L507): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Event type, status, summary, command, exit code, decision, metadata.
  - *Outputs*: JSONL appends to `state/trajectory.jsonl`, `state/audit_chain.jsonl`, and report artifacts under `interns/reports/trajectory/`.
- **Failure Modes & Edge Cases**:
  - Sensitive values in event fields are automatically scrubbed via `core.observability.log_redaction.redact`.
  - Audit chain write failures degrade silently to preserve main trajectory logging.

---

### 12. [`workflow_guard_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/workflow_guard_harness.py#L1-L1731)

- **Exact Purpose**: Exhaustive workflow reliability and guardrail validator detecting non-portable shell commands, unprofiled raw data reads, invented/unproven blocker features, unrecovered failures, incomplete sessions, and unreviewed dashboard screeners.
- **Key Functions / Classes**:
  - [`WorkflowGuardResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/workflow_guard_harness.py#L113-L132): Guardrail evaluation result dataclass.
  - [`WorkflowGuardHarness(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/workflow_guard_harness.py#L133-L1233): Comprehensive guard class implementing all check routines (`_check_artifacts`, `_check_command_log`, `_check_trajectory`, `_check_stalled_steps`, `_check_failed_without_retry`, `_check_completion_after_unrecovered_failures`, etc.).
  - [`_finding(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/workflow_guard_harness.py#L1244-L1262): Formats structured severity/code/message finding dictionaries.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/workflow_guard_harness.py#L1707-L1731): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, optional command log, `--roster-severity` option.
  - *Outputs*: `WorkflowGuardResult`, JSON/Markdown report artifacts under `interns/reports/workflow_guard_harness/`.
- **Failure Modes & Edge Cases**:
  - `failed_without_recovery` and `completion_claim_over_unrecovered_failures` raise `error` severity and set `ok=False`.
  - Non-portable Unix commands (`cat`, `grep`, `sed`) on Windows trigger errors.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - `_VOLATILE_COMMAND_FLAGS` in [`workflow_guard_harness.py:80`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/workflow_guard_harness.py#L80) includes flags (`--quiet`, `--verbose`) stripped during command normalization.
- 🔌 **Unwired Components**: None. Every harness script is exposed as a CLI entry point via `pyproject.toml` or `ProjectHarness`/`ReliabilitySuite` invocations.
- 👯 **Logic & Code Duplication**:
  - Path normalization helper `_rel(path, root)` is duplicated in 10 separate files in this package (`ai_app_harness.py`, `ai_cli_harness.py`, `engine_generation_harness.py`, `intent_coverage_harness.py`, `layered_pipeline_harness.py`, `pipeline_execution_harness.py`, `reliability_suite.py`, `state_health.py`, `trajectory_recorder.py`, `workflow_guard_harness.py`).
  - JSON file loader `_load_json(path)` is re-implemented in `ai_app_harness.py`, `ai_cli_harness.py`, `engine_generation_harness.py`, `intent_coverage_harness.py`, `layered_pipeline_harness.py`, `project_harness.py`, and `workflow_guard_harness.py`.
- ⚠️ **Broken References & Mismatches**:
  - Note S4: Security scope boundaries are explicitly documented in `project_harness.py` and `workflow_guard_harness.py`; data quality and evidence completeness pass gates do not replace security deployment gates.

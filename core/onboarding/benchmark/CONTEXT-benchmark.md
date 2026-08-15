# Benchmark Package Architecture Context: `core/onboarding/benchmark`

This document provides an exhaustive, file-by-file architectural and technical reference for all components in [`core/onboarding/benchmark`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark).

---

## Executive Overview & Architectural Model

The `benchmark` package provides project-native evaluation scorecards and release gate status checks (`agent_benchmark_scorecard.json` and `release_gate_status.json`) for AI data-agent workspaces.

It evaluates 10 workspace capability components grouped into Core Readiness and Product Maturity scores, evaluates 7 operational release gates, and outputs human-readable markdown reports (`current.md`) and machine-readable JSON contracts (`current.json`).

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 Workspace Contract Artifacts                                │
│   (KPI Registry/Mapping, Data Model Contract/Draft, Relationship Contracts, Source-to-Target │
│  Plan, Execution Harness Evidence, Validation Status, Presentation, Wiki Memory, Workflow)   │
└──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     agent_benchmark.py                                      │
│                               AgentBenchmarkScorecardBuilder                                │
│  - Evaluates 10 capability components across Core Readiness & Product Maturity               │
│  - Verifies exact KPI execution harness records (kpi_id_results, table-form samples)        │
│  - Computes weighted readiness scores & evaluates 7 operational release gates               │
│  - Generates blocker remediation routes for non-ready components                            │
└──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     Output Artifacts                                        │
│  - contracts/agent_benchmark_scorecard.json & release_gate_status.json                       │
│  - reports/benchmarks/current.json & current.md                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/__init__.py)

- **Exact Purpose**: Package initialization exporting `AgentBenchmarkResult` and `AgentBenchmarkScorecardBuilder`.
- **Key Functions / Classes**:
  - Exports [`AgentBenchmarkResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L19), [`AgentBenchmarkScorecardBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L33).
- **Inputs & Outputs**:
  - *Inputs*: None.
  - *Outputs*: Module exports (`__all__`).
- **Failure Modes & Edge Cases**: None.

---

### 2. [`agent_benchmark.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py)

- **Exact Purpose**: Evaluates workspace readiness components, calculates weighted benchmark scores, evaluates release gates, and generates scorecard reports.
- **Key Functions / Classes**:
  - [`AgentBenchmarkResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L19-L30): Result dataclass returning readiness scores, gate counts, and artifact paths.
  - [`AgentBenchmarkScorecardBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L33-L470): Main class orchestrating component evaluation, scoring, gate evaluation, and report emission.
  - Component evaluation methods:
    - [`_kpi_readiness_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L134-L160): Evaluates `kpi_feature_mapping.json` / `kpi_registry.json`.
    - [`_data_model_readiness_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L162-L194): Evaluates `data_model_contract.json` / `data_model_draft.json` / `domain_model.json`.
    - [`_relationship_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L196-L217): Evaluates `relationship_contracts.json`.
    - [`_source_to_target_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L219-L240): Evaluates `source_to_target_plan.json`.
    - [`_kpi_execution_harness_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L242-L333): Strict validator for `kpi_execution_harness.json` (verifies `ok=True`, exact `<kpi_id>_results` view naming, non-placeholder columns, and table-form markdown samples).
    - [`_validation_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L335-L355): Runs `WorkspaceArtifactValidator`.
    - [`_presentation_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L357-L368): Evaluates SVG/Mermaid/XLSX bundle in `presentation_manifest.json`.
    - [`_wiki_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L370-L397): Evaluates wiki cards and conflict counts from `reports/wiki_memory/current.json`.
    - [`_workflow_component()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L399-L415): Checks `reports/workflow/current.json`.
    - [`_autopilot_component(validation)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L417-L435): Evaluates mandatory autopilot stop boundaries.
  - [`_release_gates(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L437-L458): Evaluates 7 operational release gates (`business_review`, `presentation_review`, `source_to_target_planning`, `executable_sql_generation`, `medallion_or_etl_generation`, `bounded_autopilot`, `production_promotion`).
  - Helper functions: [`_component`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L472-L487), [`_missing_component`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L490-L497), [`_gate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L500-L506), [`_blocker_routes`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L509-L576), [`_weighted_score`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L578-L583), [`_ratio_score`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L586-L589), [`_render_markdown`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L592-L647), [`prepare_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L677-L686).
- **Inputs & Outputs**:
  - *Inputs*: Workspace layout, generated contracts, reports, evidence JSON files.
  - *Outputs*: `agent_benchmark_scorecard.json`, `release_gate_status.json`, `reports/benchmarks/current.json`, and `reports/benchmarks/current.md`.
- **Failure Modes & Edge Cases**:
  - Missing contracts result in `status="blocked"` components with 0.0 scores and specific unblocker commands.
  - Placeholder result columns (e.g. `ready_marker`) or missing table sample formatting cause `kpi_execution_harness` component failure.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - `external_benchmarks` dict in `prepare()` ([`agent_benchmark.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L78-L81)) explicitly records `"status": "not_executed_in_v1"` as a documented placeholder for future TPC/Spider/BIRD benchmark plug-ins.
- 🔌 **Unwired Components**:
  - None. `AgentBenchmarkScorecardBuilder` is wired to the `prepare-agent-benchmark` CLI entry point.
- 👯 **Logic & Code Duplication**:
  - `_rel` is reimplemented in [`agent_benchmark.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/agent_benchmark.py#L666-L670).
- ⚠️ **Broken References & Mismatches**:
  - None. Component loading handles missing artifacts gracefully without throwing unhandled exceptions.

# Core Optimization Architecture Context: `core/optimization`

This document provides an exhaustive reference for all components in [`core/optimization`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization).

---

## Executive Overview & Architectural Model

The `core/optimization` package handles workspace state optimization, diff classification, optimization planning, memory management, and engine selection strategies.

---

## File Details

### 1. [`change_classifier.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/change_classifier.py)

- **Exact Purpose**: Classifies code, data model, or schema changes between optimization iterations.
- **Key Functions / Classes**:
  - [`classify_changes(old_state, new_state)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/change_classifier.py#L15-L45): Categorizes modifications into breaking, additive, or cosmetic changes.
- **Inputs & Outputs**:
  - *Inputs*: Prior and updated state objects.
  - *Outputs*: Change classification category.
- **Failure Modes & Edge Cases**:
  - Treats ambiguous modifications conservatively as breaking changes requiring re-validation.

### 2. [`engine_evolution.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/engine_evolution.py)

- **Exact Purpose**: Manages engine evolution iterations, tracking rule updates and SQL optimization strategies over time.
- **Key Functions / Classes**:
  - [`EngineEvolutionManager`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/engine_evolution.py#L25-L80): Records performance gains and evolution trajectories across execution runs.
- **Inputs & Outputs**:
  - *Inputs*: Iteration run results and engine performance data.
  - *Outputs*: Evolution trajectory report.
- **Failure Modes & Edge Cases**:
  - Reverts optimization iterations if accuracy drops below baseline.

### 3. [`memory.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/memory.py)

- **Exact Purpose**: Optimization memory store holding prior successful query plans and resolution decisions.
- **Key Functions / Classes**:
  - [`OptimizationMemory`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/memory.py#L15-L50): Persists and retrieves optimization lessons in `interns/generated/memory/`.
- **Inputs & Outputs**:
  - *Inputs*: Resolution key, decision payload.
  - *Outputs*: Cached decision object.
- **Failure Modes & Edge Cases**:
  - Invalidates cached entries when source dataset schema changes.

### 4. [`planner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/planner.py)

- **Exact Purpose**: Formulates optimization step sequences for refining dataset models and query pipelines.
- **Key Functions / Classes**:
  - [`PlanOptimizationSteps(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/planner.py#L20-L65): Generates ordered list of optimization tasks.
- **Inputs & Outputs**:
  - *Inputs*: Workspace state and goals.
  - *Outputs*: Optimization plan list.
- **Failure Modes & Edge Cases**:
  - Detects infinite loops in iterative optimization plans.

### 5. [`strategy.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/strategy.py)

- **Exact Purpose**: Strategy selector choosing optimal execution engines and query rewrite rules.
- **Key Functions / Classes**:
  - [`select_optimization_strategy(context)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/strategy.py#L10-L35): Recommends execution strategies (e.g. pushdown filtering, partitioning).
- **Inputs & Outputs**:
  - *Inputs*: Workload profile and target engine parameters.
  - *Outputs*: Strategy configuration dict.
- **Failure Modes & Edge Cases**:
  - Defaults to safe standard join strategies when stats are incomplete.

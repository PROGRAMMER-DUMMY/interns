# Core Evolution Architecture Context: `core/evolution`

This document provides an exhaustive reference for all components in [`core/evolution`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution).

---

## Executive Overview & Architectural Model

The `core/evolution` package manages schema drift detection, evolution snapshots, and drift resolution panels (`prepare-drift-panel` -> `apply-drift-answer`).

---

## File Details

### 1. [`cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/cli.py)

- **Exact Purpose**: CLI entry point for evolution subcommands (`prepare-drift-panel`, `apply-drift-answer`).
- **Key Functions / Classes**:
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/cli.py#L15-L50): Dispatches drift panel management commands.

### 2. [`drift.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/drift.py)

- **Exact Purpose**: Detects schema drift, column type changes, and newly added source tables across dataset runs.
- **Key Functions / Classes**:
  - [`detect_schema_drift(baseline_profile, current_profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/drift.py#L20-L75): Compares dataset profiles and returns drift events.

### 3. [`panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/panel.py)

- **Exact Purpose**: Prepares and renders schema drift resolution question panels.
- **Key Functions / Classes**:
  - [`prepare_drift_panel(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/panel.py#L25-L95): Generates `current.json` and `current.md` drift panel artifacts.

### 4. [`snapshot.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/snapshot.py)

- **Exact Purpose**: Creates and persists schema state snapshots for evolution tracking.
- **Key Functions / Classes**:
  - [`create_evolution_snapshot(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/snapshot.py#L15-L50): Persists snapshot in `interns/state/snapshots/`.

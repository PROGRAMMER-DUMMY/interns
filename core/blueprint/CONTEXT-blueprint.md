# Core Blueprint Architecture Context: `core/blueprint`

This document provides an exhaustive reference for all components in [`core/blueprint`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint).

---

## Executive Overview & Architectural Model

The `core/blueprint` package generates and manages data architecture intake blueprints, decision tables, playbooks, and blueprint rendering templates.

```
┌─────────────┐        ┌──────────────┐        ┌─────────────┐
│   cli.py    ├───────►│ decisions.py ├───────►│ renderer.py │
└─────────────┘        └──────────────┘        └─────────────┘
                                                      │
                                                      ▼
                                               ┌─────────────┐
                                               │ playbook.py │
                                               └─────────────┘
```

---

## Subdirectories & Context Maps

- [`tables/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables): Blueprint decision table schemas and definitions. See [`tables/CONTEXT-tables.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/CONTEXT-tables.md).

---

## File Details

### 1. [`cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/cli.py)

- **Exact Purpose**: Command-line entry points for `prepare-blueprint`, `confirm-blueprint`, and blueprint status queries.
- **Key Functions / Classes**:
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/cli.py#L20-L65): Dispatches CLI subcommands for intake blueprint lifecycle.
- **Inputs & Outputs**:
  - *Inputs*: `--workspace`, `--domain` parameters.
  - *Outputs*: Exit status codes and generated blueprint status.
- **Failure Modes & Edge Cases**:
  - Rejects confirmation attempts from automated agent identities (`confirm-blueprint` requires human user).

### 2. [`decisions.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/decisions.py)

- **Exact Purpose**: Manages architectural decision matrices, storage choices, and engine selection rules.
- **Key Functions / Classes**:
  - [`BlueprintDecisionEngine`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/decisions.py#L30-L110): Evaluates intake rules against catalog capabilities.
- **Inputs & Outputs**:
  - *Inputs*: Intake answers and source catalog specs.
  - *Outputs*: Decision graph object.
- **Failure Modes & Edge Cases**:
  - Flags conflicting architectural decisions for user review.

### 3. [`playbook.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/playbook.py)

- **Exact Purpose**: Loads architecture transformation playbooks and rules.
- **Key Functions / Classes**:
  - [`load_playbook(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/playbook.py#L15-L50): Parses playbook YAML configuration.
- **Inputs & Outputs**:
  - *Inputs*: File path to playbook.
  - *Outputs*: Playbook dictionary.
- **Failure Modes & Edge Cases**:
  - Returns default playbook rules if file is unreadable.

### 4. [`renderer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/renderer.py)

- **Exact Purpose**: Renders human-readable markdown blueprints (`interns/reports/intake_blueprint/current.md`) and JSON specifications.
- **Key Functions / Classes**:
  - [`render_blueprint_markdown(blueprint_data)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/renderer.py#L35-L120): Formats architectural blueprint report.
  - **Legacy-artifact preservation — do not delete.** Before writing `interns/reports/solution_blueprint/current.{json,md}`, the renderer copies any existing artifact whose `generated_by` is not `prepare-blueprint` to `current.legacy.{json,md}`. Task D1 retired `prepare-solution-blueprint` (it now redirects here and writes nothing), and the flip plan called for removing this block as unreachable — **it is not.** `apply-blueprint-answer` (`core/onboarding/blueprint.py::apply_main` -> `build_blueprint`) is a second entry into the legacy producer and still stamps `generated_by: prepare-solution-blueprint`. Removing the copy would let `prepare-blueprint` silently overwrite an approved legacy plan. It can only go once `apply-blueprint-answer` is retired, which needs an answer for what replaces blueprint EDITS (`exclude`/`include`/`as_volume`/`as_managed`) — `prepare-blueprint` has no equivalent.
- **Inputs & Outputs**:
  - *Inputs*: Blueprint data structure.
  - *Outputs*: Markdown string and JSON string.
- **Failure Modes & Edge Cases**:
  - Handles missing optional metadata fields with default placeholder strings.

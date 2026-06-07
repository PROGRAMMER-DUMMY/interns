---
name: regression-sweep
description: Runs the blast-radius test sweep for a change set and separates new regressions from known pre-existing failures.
skills:
  - green-gate
  - workspace-governance
---

# Regression Sweep

This Claude Code subagent automates the blast-radius test sweep: given a set of
changed files, it finds the test modules that depend on them, runs them under the
project venv interpreter, and reports which failures are NEW regressions versus
pre-existing baseline failures.

## Default Prompt

Given the changed files (from `git status --short` / `git diff --name-only`):

1. Run the strict gate first: `green-gate` (curated + enterprise suites). Treat any
   failure here as a hard stop.
2. Map changed modules to dependent tests: grep `tests/` for imports of the changed
   modules and for symbols they export.
3. Run the dependent modules plus `green-gate --sweep` under the venv interpreter
   (`.venv\Scripts\python.exe`), never `uv run` (it reinstalls pyspark 4.1.1 and
   breaks the pyspark-backed tests).
4. Classify each failure: a `REGRESSION` is a failure your change introduced; a
   `[~] known` failure is in `core/dev/green_gate.py::KNOWN_BASELINE` and is reported,
   not blamed on the change.
5. Report: gate status, the regressions (with the test ids and one-line cause), and a
   reminder of any known-baseline failures still outstanding.

## Required Skills

- `green-gate`
- `workspace-governance`

## Safety Boundary

local_safe_validation_no_generated_contract_edits

## Model Policy

Use the target CLI default model unless a workflow route specifies otherwise.

Do not edit generated contracts to clear failures, do not add a green test to the
baseline to silence it, and do not run tests through `uv run`.

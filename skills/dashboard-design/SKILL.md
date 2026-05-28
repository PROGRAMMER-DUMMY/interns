---
name: dashboard-design
description: >
  Design, customize, debug, and verify per-workspace BI dashboards.
  Owns the dashboard/ directory in any workspace: JSON spec contracts
  (machine_defaults + user_overrides), chart-type inference, Dash
  renderer, static HTML export, dialect dispatch, and live callback
  testing. Use whenever the user wants a chart, a layout change, a new
  filter, a customization, or a dashboard bug investigated.
argument-hint: "What dashboard work? (e.g. 'change kpi_001 to bar chart', 'add a region filter', 'why is kpi_002 blank')"
---

# Dashboard Design

Per-workspace BI dashboards. Generic across workspaces. The dashboard
contract has three durable shapes — anyone editing this surface must
respect them.

## The three contracts that must hold

1. **Spec format** — every chart is a JSON file at
   `workspaces/<ws>/dashboard/<kpi_id>.json` with two top-level keys:

   ```json
   {
     "machine_defaults": { "chart_type": "line", "x": "...", "y": "...", ... },
     "user_overrides":   { "chart_type": "bar" }
   }
   ```

   `machine_defaults` is rewritten on every regeneration. `user_overrides`
   is preserved verbatim. The renderer merges overrides on top of defaults
   at render time. **Never put both kinds of data in the same key.**

2. **Live SQL re-execution** — the Dash renderer reads each KPI's
   generated SQL from `interns/generated/solutions/<kpi_id>.sql`, runs
   it via DuckDB on every page load, and renders the resulting view
   `<kpi_id>_results`. Stale snapshots are not acceptable.

3. **Blocked KPIs are rendered as cards, not hidden** — KPIs without
   executable SQL render a blocker card showing the KPI definition,
   the blocker reason from `workspace-flow status --diff`, and the
   exact `apply-*` recovery commands. Index page never silently
   filters them out.

## Files this skill owns

- `core/dashboard/spec.py`         — JSON spec contract + merge + refresh
- `core/dashboard/inference.py`    — chart-type rules (date→line, top-N→ranked-bar, etc.)
- `core/dashboard/renderer.py`     — Dash app + Plotly figure builder
- `core/dashboard/export.py`       — static HTML export
- `tools/workspace_dashboard.py`   — `workspace-dashboard` CLI entry
- `tests/test_dashboard_*.py`      — regression tests
- `workspaces/<ws>/dashboard/`     — per-workspace specs + exports

## What this skill does NOT own

- The KPI registry / feature mapping / source-to-target plan —
  upstream contracts in `interns/generated/contracts/`. If the chart
  is wrong because the data is wrong, escalate to `data-engineer` or
  `kpi-analyst`.
- Relationship contracts (`build-relationship-contracts`). If a chart
  is blocked because joins are missing, the recovery is in the data
  layer.
- Whole-workspace workflow (`workspace-flow start`). Dashboards refresh
  on its `complete` branch but do not drive it.

## Triggers

Use this skill when the user says any of:

- "change `<kpi_id>` to <chart type>"
- "add a filter / date range / dropdown"
- "this chart is blank / wrong colors / sorting wrong"
- "export the dashboard as HTML"
- "open the dashboard on port X"
- "why is `<kpi_id>` missing from the dashboard?"
- "add a `<chart type>` to the inference rules"
- "write a test for this callback"

## How to make a change safely

1. **Customize a chart** → edit `dashboard/<kpi_id>.json` → set keys
   under `user_overrides`. Never edit `machine_defaults` by hand.
2. **Add a new chart type** → add an `infer_chart` branch in
   `core/dashboard/inference.py`, add a renderer branch in
   `_figure_from_spec`, ship a regression test that exercises both.
3. **Change the spec schema** → bump `SPEC_VERSION` in `spec.py`,
   document the new field, ensure `merge_spec` semantics stay
   backward-compatible. Existing user_overrides must continue to work.
4. **Debug a callback** → boot the live app on a non-default port
   (`--port 8073`), curl `/_dash-dependencies` to see callback graph,
   `/_dash-layout` to see the component tree. Don't reach for a
   browser test until you know the wiring is wrong.

## Regression tests this skill must keep green

- `tests/test_dashboard_spec_preservation.py` — user_overrides
  survival is the load-bearing UX promise. If you change `save_kpi_spec`
  or `refresh_workspace_dashboard`, run this test FIRST.
- `tests/test_dashboard_callback_live.py` (if present) — Selenium-style
  test that a date-filter change actually re-renders. Requires
  `dash[testing]` + a browser driver.

## Genericity reminder

Workspace-agnostic. Never hardcode `Healthcare-RCM` / `healthcare` /
dataset names. All paths via `WorkspaceLayout`. The chart inference
ladder must work for any workspace shape — if you find yourself adding
a special case for one workspace's data, you're solving the wrong
problem.

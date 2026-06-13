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

## Derive from data, don't curate from text (the default selector)

Chart selection is EVIDENCE-DRIVEN: `core/dashboard/profile.py` executes the
result view, profiles each column (type, distinct count, temporal, constant),
and `decide_panels()` derives the visualization from that shape — NOT from KPI
keywords. A KPI with more than one informative dimension becomes SEVERAL simple
panels (one chart per dimension: the measure broken down by it), not one
overloaded chart. Constant (filter-pinned) columns are excluded automatically.
When adding behavior, extend the data-driven engine; only touch the text-based
`inference.py` for the no-rows fallback.

## Judgment skills (bounded firing)

Two repo-native skills wrap the work — bounded so a fragile agent doesn't stall:

- **`grill-requirements`** — at the INPUT, and ONLY when the request is genuinely
  ambiguous (which KPI / which dimension / which chart type / fit-to-screen vs
  detail). Do NOT fire it on a clearly-specified request. Stops the agent guessing
  wrong and building the wrong dashboard.
- **`grill-requirements`** — at the OUTPUT, before presenting: emit a short audit of the
  assumptions made (why log scale, which dimensions were dropped, why this chart
  type). It is **advisory** — it surfaces confidence/assumptions to the user but
  **never overrides the `dashboard-verify` gate**, which stays the sole pass/fail
  authority. A confident grill-requirements cannot wave through a failed overflow/color
  check; a low-confidence one is a signal to the user, not a silent block.

(Use the REPO skills `grill-requirements` / `grill-requirements` — not the plugin
`clarify`/`self-score`/`grill-me`, which aren't reliably present for repo agents.)

## Chart-quality defaults (auto-apply on every regeneration)

`machine_defaults` must produce a chart that is *correct and readable by
default* — not just structurally valid. These rules are generic (no
workspace/domain specifics) and are the baseline the agent owns:

1. **No duplicate titles.** The card/page header already shows the KPI
   title. Do NOT repeat it as an in-figure Plotly title in the grid/inline
   view — pass `title=""` for inline cards. (A title on the standalone
   detail page is fine.)
2. **Trend/line charts aggregate by the date axis.** Sum the measure to one
   value per period (split by at most one color dimension). Never scatter
   every raw result row — granular rows produce vertical dot-stripes and a
   zig-zag line.
3. **Share/percentage charts are true 0–100%.** Aggregate by `(x, color)`
   before normalizing so each stack sums to 100% (a raw-row stack shows a
   broken 5000%/10000% axis). Cap dense categoricals to a readable top-N on
   the x-axis (rest → "Other") and keep the color-series count small.
4. **Ranked/top-N charts rank by the right dimension.** Use the
   highest-cardinality NON-constant categorical column (the entity being
   ranked) — never a column pinned to a single value by a WHERE filter
   (that collapses the chart to one bar).
5. **Responsive sizing + margins.** Charts must never clip the card; rotate
   or truncate long category ticks. Use the shared theme's margins.
6. **One styling seam.** Apply the shared clean-corporate-BI theme
   (`_apply_corporate_theme`) — navy/steel-blue/grey colorway, white canvas,
   light gridlines, percent-axis formatting. Do not hand-style per chart.

When you add or change a chart-type rule, the regression test must assert
the *quality* property (aggregated, percent 0–100%, ranked by non-constant
column), not merely that a figure was produced.

## Visual verification (MANDATORY GATE — run before showing the user)

A dashboard can have correct markup, the right `chart_type`, and sensible
headline strings and still render badly: plots overflowing their card (a real
618px overflow was shipped this way), broken percent axis, single-bar "top-N",
clipped labels, missing legend. **Verifying HTML classes / spec JSON / headline
text is NOT verifying the dashboard.** You MUST load it in a real browser and
assert it before claiming it works.

Run the gate — it drives a real browser (`agent-browser`) and FAILS (exit 1) on
overflow, blank charts, or a multi-series chart missing its legend:

```bash
# 1. export (or boot the live Dash app and use its URL)
uv run workspace-dashboard --workspace workspaces/<ws> --export
# 2. GATE: browser-verify before presenting
uv run dashboard-verify \
  --url "file:///C:/ABS/.../dashboard/exports/index.html" \
  --screenshot "<abs>/dashboard/exports/_shot.png"
```

- The gate checks: every plot rendered (not blank), nothing overflows its
  container (bounding-box), and multi-series charts carry a legend. A non-zero
  exit is a BLOCKER — fix and re-run; do not present a failing dashboard.
- THEN read the screenshot it captured and judge it visually too (the gate
  catches structural breakage; you still confirm it reads well). Only claim a
  dashboard "looks good / professional" after the gate passes AND you have seen it.
- For the live Dash app, boot it on a port and pass `--url http://127.0.0.1:<port>`;
  agent-browser can also `click`/`hover` to confirm legend-toggle and panel
  select/deselect actually work.
- `tools/dashboard_verify.py` owns the gate; setup is `npm i -g agent-browser &&
  agent-browser install` (one-time Chrome-for-Testing download).

## Files this skill owns

- `core/dashboard/profile.py`      — EVIDENCE-DRIVEN engine: profiles the executed
  result rows and `decide_panels()` derives one panel per informative dimension
  (constants excluded). This is the default selector — derive from data, not text.
- `core/dashboard/spec.py`         — JSON spec contract + merge + refresh (stores `panels`)
- `core/dashboard/inference.py`    — name/text fallback when result rows are unavailable
- `core/dashboard/renderer.py`     — Dash app + Plotly figure builder
- `core/dashboard/export.py`       — static HTML export (board grid + theme)
- `tools/workspace_dashboard.py`   — `workspace-dashboard` CLI entry (`--export`)
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

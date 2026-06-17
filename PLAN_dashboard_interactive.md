# Plan — Interactive Dashboard (Compare-Explorer + Drill-down + DashboardAgent)

Board feedback: replace many flat panels with (a) an interactive **compare-explorer**
(pick any column-by-column comparison from dropdowns, incl. on a timeline), (b)
**hierarchical drill-down** (e.g. KPI-2: click an age band → see VisitType + Gender
within it), and (c) a conversational **DashboardAgent** for quick plot add/change.

Branch: `feature/dashboard-explorer-drilldown` (off `main`). Commit incrementally;
one commit per sub-step below. Background agents may run the phases.

## Hard constraints (every phase must honor)
- **Live app is the deliverable.** Interactivity (dropdowns, click-drill, live data)
  works only in the served Dash app (`build_dash_app`), never in the static export.
- **Static export = QA proxy only** (used for screenshot self-review, not shown to
  the board). Where a feature can't be interactive there, bake a sensible
  *non-interactive equivalent* (combined / faceted chart) so the export still reads well.
- **Workspace-agnostic** — no Healthcare/RCM/column-name hardcoding; derive from spec
  result columns + dtypes.
- **Governance intact** — reuse display redaction (`pii_redaction`) + data_policy on
  every rendered surface; never expose masked/redacted columns raw.
- **Engine parity untouched** — these are presentation-only; do NOT change generated
  SQL/Polars/PySpark or KPI results.
- **Tests**: run with the venv interpreter, NOT `uv run` (hook blocks it):
  `./.venv/Scripts/python.exe -m pytest <targets> -q`. Live-callback tests use the
  `dash[testing]` extra (`tests/test_dashboard_callback_live.py` is the pattern).
- **Verify visually**: `./.venv/Scripts/python.exe -m tools.workspace_dashboard
  --workspace workspaces/Healthcare-RCM-Data-Platform --screen` (0 findings) and
  read the PNGs under `interns/reports/dashboard_screener/shots/`.
- Never commit to `main`. Never run `uv run pytest` / engine gen (hook).

## Key files
- `core/dashboard/renderer.py` — live Dash app (`build_dash_app`), `_figure_from_spec`,
  controls, callbacks. **Owner of Phase 1 & 2 (serial — same file).**
- `core/dashboard/export.py` — static export (QA fallback charts).
- `core/dashboard/spec.py` — spec contract (`machine_defaults` + `user_overrides`).
- `core/dashboard/inference.py` / `profile.py` / `chart_knowledge.py` — panel/chart pick.
- `skills/dashboard-agent/SKILL.md` (new) + `.agents/claude/SKILLS.md` — Phase 3
  (independent of renderer → safe to build in parallel via worktree).

---

## Phase 1 — Compare-Explorer (live app)  [priority]
Goal: per-KPI "Explore" pane with dropdowns: **X (dimension)**, **Breakdown/series
(optional)**, **Measure**, **Chart type**; rebuilds a figure from the KPI's own result
columns. If X is a date/month column → timeline (line). Reuses `_figure_from_spec` by
building a transient spec from the dropdown values.

- **1a** `_classify_columns(rows)` helper → (dimensions, measures, date_cols) from dtype;
  unit test in `tests/test_dashboard_inference.py`. *(commit)*
- **1b** Explore controls (`dcc.Dropdown` x4) added to the detail region, options from the
  selected KPI's columns; default to the KPI's primary x/measure. *(commit)*
- **1c** `_explore_figure(kpi_id, x, series, measure, chart_type)` callback → transient
  spec → `_figure_from_spec`; redaction-aware; guards empty/invalid combos. *(commit)*
- **1d** Static fallback: one representative combined panel (grouped bar x=first dim,
  color=second dim) appended in `export.py` so QA screenshots show the richer view. *(commit)*
- **1e** Live-callback test (`tests/test_dashboard_callback_live.py`) for ≥3 column combos
  + screener 0-findings. *(commit)*
Acceptance: live app lets you pick any X/measure/series/chart and the figure updates;
date X → timeline; tests green; screener clean.

## Phase 2 — KPI-2 drill-down (after Phase 1 — same file)
Goal: click a category bar → detail shows the within-category breakdown by the other
dims (age band → VisitType donut + Gender donut, filtered to that band).

- **2a** Spec drill hierarchy (optional `drill: {by, into[]}` in spec; default derive
  from dims) + `clickData` wiring + a drill-state `dcc.Store`. *(commit)*
- **2b** Sub-panel render for the clicked category (reuse `_figure_from_spec` on filtered
  rows); "← back" to clear. *(commit)*
- **2c** Static faceted fallback (small-multiples per top-N categories) in `export.py`. *(commit)*
- **2d** Callback test (simulate clickData → filtered sub-figures) + screener. *(commit)*
Acceptance: clicking an age-band bar shows VisitType + Gender for that band only; back
restores; tests green.

## Phase 3 — DashboardAgent skill (parallel, worktree — independent files)
Goal: named skill triggered by `DashboardAgent, <request>` that turns NL into a spec
edit + verify loop, wrapping the existing `dashboard-engineer` subagent + `dashboard-design`
skill knowledge (spec contract, chart_knowledge axes, KPI results, redaction).

- **3a** `skills/dashboard-agent/SKILL.md`: trigger, capabilities (advisor + editor),
  knowledge map (spec/renderer/results/axes), guardrails (write to `user_overrides`
  only; never touch `machine_defaults`; re-verify). Register in `.agents/claude/SKILLS.md`. *(commit)*
- **3b** Helper to apply an NL-described panel to a spec's `user_overrides` + re-export +
  `tools.dashboard_verify`; preserve-on-regen test (`test_dashboard_spec_preservation.py`). *(commit)*
- **3c** Demo: run the example "DashboardAgent, i want a new plot stating Percentage Share
  by Visittype for each department" end-to-end; screenshot proof. *(commit)*
Acceptance: the example request adds a valid persisted panel that survives regen and
renders; skill registered + discoverable.

## Integration / done
- Merge Phase 3 worktree into the feature branch.
- Full pass: `./.venv/Scripts/python.exe -m pytest tests/ -q -k "dashboard or render or chart or spec or inference"` green; screener 0 findings.
- Open PR (only when the user asks).

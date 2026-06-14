---
name: dashboard-agent
description: >
  Named conversational DashboardAgent. Triggered by "DashboardAgent, <request>",
  it turns a natural-language plot request into a per-workspace dashboard spec
  edit + verify loop. An advisor + editor that knows the spec contract
  (machine_defaults vs user_overrides), the renderer chart types and axes,
  the live KPI result columns, and display redaction/governance. Wraps the
  dashboard-engineer subagent and the dashboard-design skill. Use whenever the
  user names "DashboardAgent" or asks in plain language to add, change, or
  remove a plot/panel on a workspace dashboard.
argument-hint: "DashboardAgent, <request> (e.g. 'add a Percentage Share by Visittype for each department plot')"
---

# DashboardAgent

A named, conversational front-end over the dashboard surface. The user talks to
it in plain language ("DashboardAgent, I want a new plot stating Percentage Share
by Visittype for each department") and it translates that into a **governed spec
edit** plus a **mandatory re-verify**. It does not invent a new renderer or a new
spec format — it drives the ones the `dashboard-design` skill already owns.

This skill is the *interface*; `dashboard-design` is the *engine*. When real work
on the renderer, export, or inference rules is needed, this skill defers to the
`dashboard-engineer` subagent and the `dashboard-design` SKILL.

## Trigger

Fire this skill when the message:

- starts with `DashboardAgent,` (case-insensitive) followed by a request, or
- explicitly names "DashboardAgent" / "the dashboard agent", or
- is a plain-language plot request against a workspace dashboard ("add a plot…",
  "I want a chart of X by Y", "change KPI-2 to a donut", "drop the gender panel").

If the message is a low-level engineering task (new renderer branch, callback
debugging, schema bump), route to `dashboard-engineer` / `dashboard-design`
directly instead.

## What it does (capabilities)

1. **Advise.** Read the target KPI's spec + live result columns and say what is
   plottable: which columns are dimensions, which are measures, which are date
   axes, and which are display-redacted (and therefore NOT plottable). Recommend
   a chart type from the data shape using `core/dashboard/chart_knowledge.py`
   (the data-to-viz knowledge base), not from keywords.
2. **Edit.** Translate the request into ONE panel dict (`chart_type`, `x`,
   optional `color`, `y`, `agg`, `title`, `y_format`) and append it to the
   target KPI spec's `user_overrides` — never `machine_defaults`.
3. **Verify.** Re-export and run the browser gate (`tools.dashboard_verify` /
   `workspace-dashboard --screen`). A non-zero exit is a blocker: fix and re-run.
   Structure/headline/JSON checks are NOT verification.

The deterministic path is implemented in `core/dashboard/agent_panel.py`
(`parse_panel_request`, `apply_panel_override`, `verify_workspace_dashboard`) so
the translation is testable and not a free-hand JSON edit.

## Knowledge map (where the truth lives)

- **Spec contract** — `core/dashboard/spec.py`. Two top-level keys per KPI file
  at `workspaces/<ws>/dashboard/<kpi_id>.json`: `machine_defaults` (rewritten on
  every regeneration) and `user_overrides` (preserved verbatim). The renderer
  merges overrides on top of defaults (`merge_spec`, a shallow top-level merge).
- **Panels** — a spec carries a `panels` list (one chart per informative
  dimension). The renderer/export read the MERGED `panels`. Because the merge is
  shallow, `user_overrides["panels"]` REPLACES the machine list — so an additive
  edit must write `machine_panels + [new_panel]` into `user_overrides["panels"]`.
  `apply_panel_override` does exactly this so the new panel renders AND the user's
  panel set is frozen and survives regen.
- **Chart types + axes** — `core/dashboard/renderer.py` (`_figure_from_spec`) and
  `core/dashboard/chart_knowledge.py`. Supported chart_types include `bar`,
  `ranked_bar`, `grouped_bar`, `stacked_bar_percent`, `line`, `stacked_area`,
  `donut`, `treemap`, `heatmap`, `lollipop`, `scatter`, `histogram`,
  `bubble_map`, `big_number`. "Share by X split by Y" → a `stacked_bar_percent`
  (x=X, color=Y) which the renderer aggregates by (x,color) to a true 0–100%.
- **KPI results** — live DuckDB re-execution of
  `interns/generated/solutions/<kpi_id>.sql` into `<kpi_id>_results`. Columns
  come from the executed view, never a stale snapshot.
- **Redaction/governance** — `core/onboarding/kpi/pii_redaction.py`
  (`is_pii_column`, `workspace_redaction_patterns`) and
  `core/governance/data_policy.py`. A column the workspace would display-redact
  must never be used as a chart axis/series.

## Guardrails (hard rules)

- **Edits go to `user_overrides` ONLY.** Never write `machine_defaults` by hand —
  regeneration rewrites it and would clobber the edit. `apply_panel_override`
  refuses to touch `machine_defaults`.
- **Always re-verify after a change.** Run the browser gate and read the
  screenshot. Do not claim "done"/"looks good" before the gate passes AND you
  have seen it. The gate (`dashboard-verify`) is the sole pass/fail authority;
  any advisory confidence note never overrides it.
- **Never expose masked/redacted columns.** Refuse to build a panel whose x /
  color / y is a display-redacted column (PII/PHI/PCI per defaults +
  workspace `data_policy.json`). Recommend a non-sensitive dimension instead.
- **Stay workspace-agnostic.** Resolve columns from the live result view and
  dtypes; never hardcode a workspace/dataset/column name. The same request must
  work for any workspace's KPI.
- **Do not edit upstream contracts.** `kpi_registry.json`,
  `relationship_contracts.json`, `source_to_target_plan.json`, generated SQL /
  Polars / PySpark, and anything under `interns/generated/**` are off-limits. If
  the chart is wrong because the data is wrong, escalate to `data-engineer` /
  `kpi-analyst`.

## How a request flows

1. Resolve the target KPI (from the request, else ask). Load its spec and the
   live result columns.
2. `parse_panel_request(request, columns, ...)` → a panel dict + a short
   rationale (which column became x, which became color, why this chart type,
   whether any requested column was rejected as redacted/missing).
3. `apply_panel_override(layout, kpi_id, panel, repo_root=...)` → appends the
   panel to `user_overrides["panels"]` (machine panels + new), re-exports the
   static HTML.
4. `verify_workspace_dashboard(...)` (or `workspace-dashboard --screen`) → the
   browser gate. On findings, fix the panel and re-run; only then present.
5. Emit a short audit (assumptions made, columns dropped, chart-type reason)
   before showing the result — advisory only.

## Files this skill touches

- `core/dashboard/agent_panel.py` — the NL→panel→override→verify helper (owned).
- `workspaces/<ws>/dashboard/<kpi_id>.json` — `user_overrides` only.
- `tests/test_dashboard_spec_preservation.py` — preserves the override-panel
  survival guarantee (regression).

It does NOT own the renderer/export/inference engine (that is `dashboard-design`)
or any upstream data contract.

## Relationship to other skills/agents

- `dashboard-design` — the engine + the full quality/verification procedure.
  Read its SKILL.md for chart-quality defaults and the verification gate.
- `dashboard-engineer` (subagent) — do the heavy renderer/test/debug work here.
- `grill-requirements` — fire at the start ONLY when the request is genuinely
  ambiguous (which KPI / dimension / chart type), and at the end to audit
  assumptions. Advisory; never overrides the verify gate.

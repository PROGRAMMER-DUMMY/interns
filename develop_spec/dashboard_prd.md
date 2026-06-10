# PRD — Generic, self-verifying dashboard capability

Status: planned (2026-06-10). Derived from a grill-me session. Supersedes ad-hoc
dashboard fixes; the build order below is the contract.

## Goal
A dashboard capability that auto-designs correct, readable, complex dashboards
for ANY workspace and PROVES they render in a real browser before showing the
user. No workspace-specific styling or hardcoded thresholds.

## Locked decisions (from grilling)
1. **Unverifiable == blocked.** If the verify gate cannot run (browser/server
   down), hard-stop and report "unverified"; never present as success.
2. **Diagnosis = deterministic hard gate + advisory judgment.** Hard gate
   (reproducible, names the offender): plot overflow, blank chart, multi-series
   missing legend, **series color too-similar (perceptual distance)**, low
   contrast vs background, tick-label collision. Advisory (cannot block):
   `self-grill` audit + optional vision read of the screenshot.
3. **Generic + adaptive.** Look comes from a swappable **DESIGN.md** (Stitch
   format: palette/type/spacing/do-don'ts). Encoding (log vs linear, color
   count, normalization, when to show variation) is DERIVED from each result
   set's measured distribution — no fixed thresholds, no domain words.
4. **Legibility wins within the design language.** DESIGN.md sets hue family +
   tokens; the engine generates as many perceptually-distinct, colorblind-safe
   colors as the data needs, and picks log scale from distribution regardless of
   aesthetics. A pretty-but-unreadable chart is a failure.
5. **Correctness lives in deterministic, tested code + the gate** — runnable
   headless with no LLM. The agent is a thin orchestrator/judge and is NOT
   required for a correct dashboard (it died twice in testing; correctness must
   survive that).
6. **Repo-native judgment skills, bounded firing.** `clarify-ambiguity` at input
   (only if the request is ambiguous); `self-grill` at output (advisory audit
   before presenting). NOT the plugin `self-score`/`grill-me`. self-grill never
   overrides the gate.

## Non-goals
- No per-workspace hardcoded palettes/thresholds. No editing upstream contracts
  (kpi_registry / relationship_contracts / source_to_target). Data-correctness of
  server-side aggregation is a kpi-analyst/data-engineer concern — the dashboard
  escalates, never self-certifies the numbers.

## Build order (each phase: deterministic code + tests + gate-verified)

### Phase 1 — deterministic engine + full gate (no live app, no LLM)
1a. **Gate: add visual-correctness checks** to `tools/dashboard_verify.py` —
    extract rendered trace colors + legend labels from the SVG (proven feasible),
    compute pairwise perceptual distance (CIEDE2000) and flag series that are too
    similar BY NAME; contrast-vs-background; tick-label bounding-box collision.
    Make "gate could not run" exit a distinct BLOCKED status (decision 1).
1b. **Adaptive encoding** in `core/dashboard/profile.py` — derive log-vs-linear
    from the measure's distribution (orders-of-magnitude span / skew), choose
    color count from series cardinality, decide normalization from share-ness.
    No constants exposed as knobs; all derived. Tests assert the decision from
    synthetic distributions.
1c. **Palette generation** — a colorblind-safe categorical ramp generated to N
    distinct hues within the DESIGN.md hue family (decision 4). Single-series
    keeps the accent; multi-series extends.
1d. **DESIGN.md layer** — read `workspaces/<ws>/DESIGN.md` if present else a
    shipped repo-default; parse palette/type/spacing/rules into the theme.
    Export/renderer consume tokens from it, not hardcoded CSS.

### Phase 2 — live Dash app + fit-to-viewport
2a. Rebuild `build_dash_app` on the panel engine + DESIGN.md theme.
2b. **Overview + drill** layout: compact KPI summary tiles (headline + sparkline)
    fit one viewport; drill into a KPI shows its panels in an auto-fit grid that
    shrinks to fit (no inner scroll). Thematic tabs/sections for grouped KPIs.
2c. Controls: show/hide panels, KPI/dimension pickers, legend-toggle, hover,
    info/units affordance.
2d. **Nested KPIs** — explicit hierarchy representation in the layout (the "~20
    nested" case).
2e. **Scale**: server-side aggregation/sampling so raw (multi-TB) never reaches
    the browser; lazy/paged panel rendering.
2f. Verify each step with `dashboard-verify --url http://127.0.0.1:<port>` plus
    agent-browser click/hover to PROVE select/deselect + legend-toggle work.

### Phase 3 — agent wiring
3a. Add `clarify-ambiguity` + `self-grill` to the dashboard-engineer skills list
    (decision 6); document the bounded firing policy in SKILL.md + default_prompt.
3b. Regenerate adapters; confirm all three CLIs carry it.

## Acceptance
- `dashboard-verify` passes (no overflow/blank/legend/color-clash/contrast/
  collision) on the sample workspace AND a synthesized many-dimension / nested
  KPI fixture; a deliberately-broken fixture FAILS it (gate proves it can fail).
- Swapping the DESIGN.md visibly changes the look with zero code change.
- Encoding adapts: a wide-dynamic-range measure renders log; a share renders
  0-100%; both verified by gate + screenshot.
- Dashboard regenerates correctly with NO agent in the loop (decision 5).
- green-gate 346+/0.

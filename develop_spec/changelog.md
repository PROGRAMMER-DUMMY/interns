# develop_spec/changelog.md — what changed (newest first)

Append a dated entry after every change. Keep entries short: what / why / files /
tests / verification. This is the dev history of the PLATFORM code; it is not the
end-user `session-snapshot` audit and not git itself.

Entry template:

```
### YYYY-MM-DD  <short title>
- what:   <one or two lines>
- why:    <the bug/goal>
- files:  <key files touched>
- tests:  <suites added/run> (result)
- verify: <command(s) run to confirm>
```

---

### 2026-06-11  Fix the 3 pre-existing test_failure_contracts failures (pipeline-SQL validator)
- what: `WorkspaceArtifactValidator._validate_pipeline_harnesses` now enforces the
  pipeline-SQL content contract the 3 long-red tests assert: (1) empty `pipeline_layers.sql`
  -> "generated pipeline SQL must be non-empty"; (2) raw dataset-path reads (read_csv_auto/
  read_parquet/delta_scan/'datasets/' literal) with NO bootstrap markers -> "missing
  BEGIN/END CATALOG BOOTSTRAP"; (3) raw paths OUTSIDE the BEGIN/END CATALOG BOOTSTRAP region
  -> "raw dataset path references are only allowed inside CATALOG BOOTSTRAP". Added
  `test_failure_contracts` to green-gate (now green).
- why: These 3 were pre-existing failures parked in the backlog. The validator had the
  pipeline harness-JSON checks but never validated the SQL content itself.
- files: `core/onboarding/workspace/validation.py` (+content checks, `import re`),
  `core/dev/green_gate.py` (gate test_failure_contracts).
- tests: test_failure_contracts 14/14 green; green-gate 420 -> 434, 0 failing.
- verify: confirmed a LEGITIMATE pipeline (raw paths INSIDE bootstrap, downstream reads the
  view) produces NO content error — no false positive. The 3 target errors fire on the bad cases.

### 2026-06-11  Quick wins: gate the new tests, fix cp1252 crash, reconcile backlog
- what: (1) Added the 7 dashboard/token/wiki test modules to `green_gate.CURATED_MODULES`
  (test_dashboard_inference/profile/design_md/nested/verify, test_token_report, test_wiki_writer)
  — they passed on demand but weren't gating. (2) cp1252 fix: `workspace-flow` main() reconfigures
  stdout/stderr to UTF-8 errors=replace, and the generated Gold-results comment uses ASCII `->`
  instead of `→`. (3) Reconciled follow_ups.md (3 stale items checked off; cp1252 closed).
- why: The green checkmark meant less than it should (74 tests on the bench); a non-ASCII char
  crashed `workspace-flow results` on a piped Windows console; the backlog overstated open work.
- also (honesty): INVESTIGATED the "kpi_002 age via CURRENT_DATE" item I'd flagged as a bug — it
  is NOT one. The as-of-event anchor logic already exists and works (kpi_001 anchors on ServiceDate);
  kpi_002 has NO date column in its features (PatientID/Name/VisitType/Gender/DOB), so there's no
  event date to anchor on — CURRENT_DATE is the only/defensible option for an age-snapshot and is
  audited via `_age_fallback`. Retracted the claim; left the code unchanged rather than force a
  wrong anchor. Logged a possible future improvement (broaden detection to a lone date feature col).
- files: `core/dev/green_gate.py` (+7 modules), `core/onboarding/workspace/flow.py` (stdout
  reconfigure), `core/onboarding/kpi/sql_generator.py` (-> ASCII), `develop_spec/follow_ups.md`.
- tests: none new (wiring + 2-line fixes); green-gate now 420 tests, 0 failing (was 346 — the
  +74 are the newly-gated modules). Confirms the cp1252 changes broke nothing.
- verify: `green-gate` 420/0.

### 2026-06-10  DESIGN.md: consume the REAL awesome-design-md schema (was cosmetic-only)
- what: `design_md.py` now parses the actual awesome-design-md / Stitch schema — YAML
  frontmatter with a `colors:` map in the brand's own vocabulary + a nested `typography:`
  block — and maps it onto our tokens via a synonym table (`primary->accent`,
  `canvas->paper`, `canvas-soft/surface->card`, `ink->ink`, `ink-secondary->ink_soft`,
  `hairline->rule`, `primary-deep->accent_deep`, ...). Picks body/display/mono font stacks
  from `typography.*.fontFamily`; proprietary fonts keep their fallback stack and are NOT
  Google-loaded. Falls back to the loose token-block parser (our own default) when there's
  no YAML frontmatter.
- why: The earlier Phase-1d integration adopted the DESIGN.md *concept* but invented its own
  flat vocabulary, so dropping a real repo file (e.g. stripe/DESIGN.md) changed almost nothing
  (only a coincidental `ink` match) — cosmetic, not functional. The user gave that repo to use.
- files: `core/dashboard/design_md.py` (synonym map, `_parse_frontmatter`, font-stack picker;
  parse_design_md tries YAML first), `tests/test_dashboard_design_md.py` (+3: vocabulary map,
  card fallback, non-frontmatter fallback).
- tests: 11 design-md tests green; green-gate 346/0.
- verify: downloaded the live stripe/DESIGN.md -> parsed to accent #533afd (indigo) / paper
  #ffffff / ink #0d253d (navy) / card #f6f9fc / rule #e3e8ee / accent_deep #4434d4 / sohne
  font stack. Dropped it into the workspace, re-exported, screenshotted: dashboard genuinely
  restyled to Stripe's white/navy/indigo (headlines indigo, was sienna). Removed it -> back to
  editorial default. Now honors "use this repo" at the file level, not just the concept.

### 2026-06-10  Dashboard correctness: categorical axis ordering (ordinal/banded + nominal)
- what: `_figure_from_spec` now orders vertical-bar x categories generically: ORDINAL/banded
  categories (age bands "0-9".."100-109", numeric buckets) sort by their natural leading
  number; NOMINAL categories (departments, gender, visit type) sort by measure descending;
  temporal/ranked unchanged. New `_categorical_order` helper + Plotly categoryorder/categoryarray.
- why: User flagged data-ordering correctness. Browser inspection of the rendered charts showed
  the kpi_002 "% share by Age Band" x-axis in arbitrary first-appearance order
  (20-29, 80-89, 50-59, 0-9, ...) instead of natural 0-9, 10-19, ... — a real correctness bug;
  nominal bars were also unsorted.
- files: `core/dashboard/renderer.py` (`_categorical_order` + ordering in the bar path),
  `tests/test_dashboard_inference.py` (+2: ordinal natural-number order, nominal value-desc).
- tests: dashboard inference 34 green; green-gate 346/0.
- verify: re-exported + browser-read the rendered axes — age bands now
  0-9,10-19,20-29,...,100-109; visit-type/gender sorted by value. Screenshot confirms the
  age-band chart reads as a coherent distribution. (Static export `exports/index.html` is the
  design the user endorsed; this fixes its data ordering.)

### 2026-06-10  Dashboard PRD Phase 3: wire clarify-ambiguity + self-grill (bounded) — PRD COMPLETE
- what: Added the repo-native judgment skills to the dashboard-engineer with a bounded
  firing policy: `clarify-ambiguity` at INPUT (only when the request is genuinely
  ambiguous), `self-grill` at OUTPUT (advisory assumptions audit before presenting,
  NEVER overrides the dashboard-verify gate). Added to the agent skills list + default_prompt
  + a SKILL.md "Judgment skills (bounded firing)" section; regenerated all 3 CLI adapters.
- why: PRD decision 6. Uses REPO skills (clarify-ambiguity / self-grill), not the plugin
  clarify/self-score/grill-me which aren't reliably present for repo agents.
- files: `skills/dashboard-design/agents/dashboard-team.yaml` (skills + default_prompt),
  `skills/dashboard-design/SKILL.md` (firing-policy section); regenerated
  `.claude`/`.gemini`/`.codex` dashboard-engineer adapters + `.agents/*` indexes.
- tests: none (doc/config + generated adapters); regen verified all 3 adapters carry
  "JUDGMENT SKILLS (bounded firing)" + clarify-ambiguity + self-grill. green-gate 346/0
  (routing-coverage unaffected).
- verify: grep across .claude/.gemini/.codex adapters = present in all.
  DASHBOARD PRD COMPLETE (Phases 1a-1d, 2a-2e, 3). The dashboard capability is now generic,
  data-driven, browser-verified, scale-aware, and agent-orchestrated with bounded judgment.

### 2026-06-10  Dashboard PRD Phase 2d+2e: nested-KPI grouping + lazy/server-side scale
- what: 2d — an optional generic `group` field on a KPI organizes the overview into
  sections with inline separators (`.gsep`), while the strip stays one fit-to-viewport
  row; KPIs ordered by (group, id). Absent group -> flat (no behavior change for the
  sample). 2e — LAZY rendering: `build_dash_app` queries each KPI's result view ONCE for
  its headline + panel titles but builds the heavy Plotly figures only on drill (cached in
  `_fig_cache`); fetch capped at `_SAMPLE_CAP=5000`. Raw/large data never reaches the
  browser — KPI views are pre-aggregated server-side (DuckDB/Delta GROUP BY), the small
  result is sampled, and figures build per drilled KPI rather than all up front.
- why: PRD Phase 2 scale — handle ~20 nested KPIs / 100+ features / multi-TB without
  flooding the browser or building dozens of figures at startup.
- files: `core/dashboard/renderer.py` (meta/lazy refactor of build_dash_app, `_panel_figure`
  cache, `_SAMPLE_CAP`, group ordering + `.gsep`), `tests/test_dashboard_nested.py` (new).
- tests: new nested test proves 2 group separators + 4 tiles AND zero figures built at
  construction (lazy); full dashboard suites + green-gate 346/0.
- verify: re-booted live app (port 8072, HTTP 200) -> dashboard-verify PASS (4/4 rendered,
  legend, 0 overflow), confirming lazy figure-building renders correctly on drill. Phase 2
  COMPLETE; Phase 3 (wire clarify-ambiguity + self-grill into the agent) remains.

### 2026-06-10  Dashboard PRD Phase 2a-2c: live Dash app — overview+drill, controls, theme
- what: Rebuilt `build_dash_app` from the legacy single-chart layout into an
  overview+drill app on the data-driven panel engine + DESIGN.md theme. (2a) applies
  `set_active_design(load_design_tokens(ws))`, renders each KPI's panels as figures
  (`_kpi_render_data`), editorial CSS shell generated from tokens via
  `_dash_index_string`. (2b) FIT-TO-VIEWPORT: a fixed masthead + a one-row overview
  strip of clickable KPI tiles (headline + title) + a controls row + a flex detail
  region (the only scroll area); `#app{height:100vh;overflow:hidden}`. (2c) controls:
  KPI dropdown, per-panel show/hide checklist, legend-toggle + hover (Plotly native),
  metric/cuts info line. Pattern-matching callbacks wire tile-click->select,
  select->panel-checklist, select+checklist->detail grid, select->tile highlight.
- why: PRD Phase 2 — the live Dash app is the real target surface (not static export).
- files: `core/dashboard/renderer.py` (`_kpi_render_data`, `_dash_index_string`, new
  `build_dash_app`; removed legacy `_kpi_chart_card`/`_index_card_grid`/date-filter
  callback), `tests/test_dashboard_design_md.py` (+1 index-string structural test).
- tests: dashboard suites green; green-gate 346/0.
- verify: BOOTED the live app (port 8071, HTTP 200) and BROWSER-VERIFIED via
  dashboard-verify -> PASS (4/4 panels rendered, legend, 0 overflow) + agent-browser
  interaction proof: clicking the KPI-002 tile switched the detail to its 4 share
  panels; unchecking "by Gender" dropped the panel (4->3). Screenshot confirms editorial
  masthead + tiles + CVD-safe drill charts. Phase 2d (nested KPIs) + 2e (server-side
  aggregation/lazy) + Phase 3 (agent wiring) remain.

### 2026-06-10  Dashboard PRD Phase 1d: swappable DESIGN.md design language
- what: The dashboard look (palette/fonts) now comes from a swappable DESIGN.md, not
  hardcoded CSS. New `core/dashboard/design_md.py` (`DesignTokens` + forgiving parser +
  resolver: `workspaces/<ws>/DESIGN.md` -> shipped `core/dashboard/default_design.md` ->
  built-in editorial defaults). `renderer` reads active tokens (`set_active_design`,
  module-level `_ACTIVE`) for colorway/accent/fonts/text; `export` builds the `:root` CSS
  vars + Google-fonts link from tokens and applies them before rendering. Follows the
  Stitch/awesome-design-md convention (prose + a machine-readable token block).
- why: PRD decision 3 (generic, any workspace; no hardcoded styling). A workspace can carry
  its own DESIGN.md and get a different aesthetic with zero code change.
- files: `core/dashboard/design_md.py` (new), `core/dashboard/default_design.md` (new),
  `core/dashboard/renderer.py` (active tokens, removed hardcoded colorway/accent),
  `core/dashboard/export.py` (token-driven :root + fonts), `tests/test_dashboard_design_md.py`
  (new, 8), `tests/test_dashboard_inference.py` (theme test reads tokens).
- tests: 58 dashboard tests green; green-gate 346/0.
- verify: default export passes the browser gate (9/9, 0 overflow/clashes). SWAP proof —
  dropped a workspace DESIGN.md with teal accent + dark paper, re-exported: exported CSS
  showed `--accent: #0d7d74` / `--paper: #0f1417` with NO code change (acceptance met).
  Phase 1 (deterministic engine + gate) COMPLETE; Phase 2 (live Dash app) + 3 (agent wiring) remain.

### 2026-06-10  Dashboard PRD Phase 1b + 1c: adaptive log scale + colorblind-safe palette
- what: 1b — `profile.decide_panels` now derives log-vs-linear from the measure's
  DISTRIBUTION: `_should_log_scale` sets `log_scale` on a breakdown panel when the
  aggregated positive values span >=50x (~1.7 decades), so small categories aren't
  invisible slivers next to large ones; never for share/percent (bounded 0-100) or
  non-positive data. `renderer` applies it to the measure axis (x for ranked_bar, else
  y). 1c — multi-series charts now use a COLORBLIND-SAFE categorical palette
  (Okabe-Ito) instead of the near-monochrome editorial colorway; single-series keeps the
  editorial sienna accent. The verify gate's delta-E check enforces the separation.
- why: PRD decisions 3 (data-driven encoding, not fixed) + 4 (legibility wins within the
  design language). Directly addresses the user's "colors not separated / monochrome" and
  "use log when it shows the difference better" asks — both derived, generic, no knobs.
- files: `core/dashboard/profile.py` (`_should_log_scale` + log_scale on panels),
  `core/dashboard/renderer.py` (`_ACCENT`/`_CATEGORICAL_SAFE`, palette-by-series-count,
  log-axis application), `tests/test_dashboard_profile.py` (+3 log tests),
  `tests/test_dashboard_inference.py` (+3 palette/log-axis tests).
- tests: dashboard inference/profile/services green; green-gate 346/0.
- verify: re-exported + browser-gated PASS (9/9, 0 overflow, 0 color clashes); screenshot
  confirms the trend's Female/Male now render CVD-safe blue+vermillion (was sienna+slate),
  single-series breakdowns stay sienna. PRD 1d (DESIGN.md) + Phase 2 (live app) remain.

### 2026-06-10  Dashboard PRD + Phase 1a: perceptual color/contrast checks in the gate
- what: Wrote `develop_spec/dashboard_prd.md` (6 grilled decisions -> phased build).
  Phase 1a executed: `tools/dashboard_verify.py` now extracts per-plot rendered series
  colors + chart background from the SVG and runs PERCEPTUAL checks — pairwise CIE76
  delta-E between multi-series colors (flags pairs below the ~16 JND band that read as
  the same color, naming the colors) and WCAG contrast vs background (flags near-invisible
  series). Added a distinct `blocked` status: if the gate cannot run (browser/eval fails)
  it is BLOCKED, never silently passed (PRD decision 1). delta-E/contrast thresholds are
  documented as human-vision constants, not tunable/workspace knobs.
- why: The structural gate passed dashboards whose series were the same near-monochrome
  tint (the user's "colors not separated" complaint) — DOM checks can't catch that. Now the
  gate diagnoses color clashes deterministically and by value.
- files: `tools/dashboard_verify.py` (sRGB->Lab delta-E + WCAG contrast + per-plot color
  probe + blocked status), `tests/test_dashboard_verify.py` (new, 8 — parse/delta-E/contrast).
  `develop_spec/dashboard_prd.md` (new).
- tests: 8 verify tests green; green-gate 346/0. Live gate on the Healthcare board PASSES
  (9/9 rendered, 1 multi-series w/ legend, 0 overflow, 0 color clashes, 0 low-contrast).
- verify: delta-E sienna-vs-slate=75.7 (ok), near-sienna pair=2.2 (would flag); pale-on-white
  contrast 1.3 (flags), slate-on-white 10.15 (ok). Remaining PRD phases 1b-1d + 2 + 3 open.

### 2026-06-10  Interactive browser verification gate (agent-browser) + overflow fix
- what: New `tools/dashboard_verify.py` (entry `dashboard-verify`) — an interactive
  browser gate that drives `agent-browser` (vercel-labs CLI) to load a dashboard
  (static file:// or live Dash URL), run in-page bounding-box checks, and FAIL (exit 1)
  on: a plot that overflows its container, a chart that didn't render (blank), or a
  multi-series chart missing a legend. It also captures a screenshot to view. This is the
  "verify in a real browser before showing the user" gate.
- caught + fixed a real defect: the gate measured 6 elements overflowing by up to 618px
  (Plotly renders at a default pixel width and only refits its container on a resize event,
  which never fires on a static load). Fix: `export.py` dispatches `resize` + `Plotly.Plots.resize`
  after load (×3 for late layout/fonts) so every chart honors its cell + CSS containment
  (`overflow:hidden`, `min-width:0`, plotly width 100% !important). Gate re-run: PASS, then
  visually confirmed via screenshot (charts contained, trend legend present).
- baked into the agent: SKILL.md "Visual verification" section + dashboard-team.yaml
  default_prompt now mandate `dashboard-verify` as a blocking gate before presenting;
  regenerated .claude/.gemini/.codex adapters (all carry it).
- setup: `npm i -g agent-browser && agent-browser install` (one-time Chrome-for-Testing).
- files: `tools/dashboard_verify.py` (new), `pyproject.toml` (entry), `core/dashboard/export.py`
  (resize-dispatch + containment CSS), `skills/dashboard-design/SKILL.md` + `agents/dashboard-team.yaml`
  (+ regenerated adapters).
- tests: green-gate 346/0 (the gate is a runtime tool, exercised live, not a unit test).
- verify: `dashboard-verify` on the Healthcare board -> PASS (9/9 rendered, legend present, 0 overflow).

### 2026-06-10  Dashboard visual redesign: editorial "data desk" aesthetic
- what: Replaced the plain corporate-BI shell with a distinctive financial-broadsheet
  design (via the frontend-design skill). Masthead nameplate in Fraunces (display serif)
  with a mono eyebrow + dateline and a heavy rule; KPI cards as editorial articles with
  serif headlines, mono uppercase panel labels, sienna serif headline figures with tabular
  numerals, "01/02/03" index marks, hairline section rules, a hover lift, and a staggered
  fade-up load. Warm paper palette (cream `#f3efe6`, ink `#1b1a17`, sienna accent `#b4441c`,
  slate `#2f4452`), SVG grain overlay, Google-fonts (Fraunces / Hanken Grotesk / Spline Sans
  Mono). The Plotly theme was retoned to match: editorial sienna/slate colorway (passed via
  `color_discrete_sequence` so single-series bars use it, not Plotly default blue), transparent
  chart canvas so the warm card shows through, hairline gridlines, Hanken chart font.
- why: User invoked frontend-design to make the dashboard distinctive/production-grade rather
  than generic. Chose an editorial/data-desk direction — bold yet credible for a serious data
  product (no AI-slop fonts, no purple-on-white).
- files: `core/dashboard/export.py` (fonts link, grain, masthead, card system, CSS),
  `core/dashboard/renderer.py` (editorial colorway/font/transparent canvas + per-px
  color_discrete_sequence), `tests/test_dashboard_inference.py` (theme test -> editorial
  colorway + transparent canvas).
- tests: dashboard 45 (inference/services/profile) green; green-gate 346/0.
- verify: screenshotted the rendered board (headless Chrome) — Fraunces masthead, sienna charts
  cohesive with the shell, multi-panel cards readable. Two-section spec contract + data-driven
  panels unchanged underneath the restyle.

### 2026-06-10  Generic data-driven dashboard engine: panels derived from result shape
- what: Replaced text/keyword-curated chart selection with an evidence-driven engine that
  profiles the ACTUAL executed result rows and DERIVES the visualization. New
  `core/dashboard/profile.py`: `profile_columns` (per-column type / distinct / temporal /
  constant), `choose_measure`, and `decide_panels` — which emits ONE PANEL PER INFORMATIVE
  DIMENSION (measure broken down by that dimension), ordered by how much the dimension varies
  the measure, capped at 4. Constant (filter-pinned) columns are excluded automatically.
  `spec.py` executes the result view at build time and stores `machine_defaults.panels`
  (SPEC_VERSION 2; first panel mirrored to top level for back-compat; falls back to the old
  single-chart inference when rows can't be obtained). `renderer.py` renders each panel
  (`render_kpi_inline` returns a `panels` list; `_panel_spec`/`_panel_html`); `export.py`
  lays panels as a sub-grid in each KPI card + detail page.
- why: User: the dashboard wasn't good and must be GENERIC so it auto-decides per KPI. The old
  inference keyed on KPI text and crammed a 4-dimension KPI (kpi_002: dept x visit x gender x age)
  into one unreadable stacked bar. "Derive, don't curate": chart now emerges from the data's shape,
  and multi-dim KPIs become several simple charts ("more charts for variation", per user).
- also fixed (visible once panels rendered): a share metric broken down by ONE dimension must be
  re-expressed as share-of-total (`_normalize_percent`, sums to 100%) instead of summing the
  pre-computed share column (which showed 6000%/10000%). Percent axes now use a plain "%" suffix
  on percent-unit (0-100) data, never tickformat ".0%" (which x100s again).
- files: `core/dashboard/profile.py` (new), `core/dashboard/spec.py` (panels + SPEC_VERSION 2 +
  repo_root), `core/dashboard/renderer.py` (panel rendering + _normalize_percent + percent-suffix
  axes), `core/dashboard/export.py` (panel sub-grid + CSS), `tests/test_dashboard_profile.py` (new, 8).
- tests: profile 8 + dashboard_inference 37 + services; green-gate 346/0.
- verify: screenshotted kpi_002 detail — 4 clean panels (share by age band / department / visit-type /
  gender), each correctly scaled 0-50%/0-25%/0-10% summing to 100%. kpi_001 -> 4 panels (trend +
  breakdowns); kpi_003 -> 1 ranked top-10 (constant LOB excluded). Generic; no per-KPI rules.

### 2026-06-10  Dashboard chart fixes V1-V6 (satisfy the quality defaults; visually verified)
- what: Made the renderer meet the encoded quality defaults, verified by screenshotting the
  rendered board (not just structure). V1 no duplicate titles (inline figures `show_title=False`
  + axis titles dropped on compact cards). V2 line/trend aggregates the measure by the date axis
  (`_aggregate_rows`) — one point per period, no raw-row dot-stripes. V3 share charts aggregate by
  (x,color) so each stack is a true 0-100%, cap dense categoricals to top-12 x + top-6 series with
  an 'Other' bucket (`_cap_categories`), and use a `%`-suffixed 0-100 axis (NOT tickformat '.0%',
  which double-scaled to 5000%/10000%). V4 ranked/top-N ranks by the highest-cardinality NON-constant
  categorical (`_first_non_constant_categorical`) so a filter-pinned constant no longer collapses the
  chart to one bar; headline aligned. V5 responsive `to_html` + `automargin` + rotated x ticks so
  labels never clip. V6 horizontal top legend (no title collision).
- why: A screenshot of the D2 board showed it rendered badly despite valid structure (un-aggregated
  scatter, 10000% axis, single-bar 'top 10', duplicate titles, clipped 'mon' label). Fixed each and
  re-screenshotted until the board reads as a correct, clean corporate-BI dashboard.
- files: `core/dashboard/renderer.py` (`_aggregate_rows`, `_first_non_constant_categorical`,
  `_cap_categories`, `_figure_from_spec` show_title + per-type aggregation/percent/rank, theme
  automargin/autosize, responsive to_html, headline aligned to V4), `tests/test_dashboard_inference.py`
  (stacked-percent axis test updated to ticksuffix/range).
- tests: dashboard 37 + services green; `green-gate` 346/0.
- verify: screenshot of `dashboard/exports/index.html` — kpi_001 single clean line; kpi_002 0/50/100%
  stacked bars over 12 depts + Other with tidy legend; kpi_003 top-10 ranked payers (PAYOR8395 ->
  PAYOR4165, headline "PAYOR8395: $1.8K"). Resolves the ranked_bar dimension defect (was a follow-up).

### 2026-06-10  Dashboard quality + visual-verification defaults baked into the agent
- what: Encoded the chart-quality rules and a MANDATORY visual-verification loop as
  durable defaults the dashboard-engineer agent auto-applies (so dashboards self-resolve
  to a correct/readable state instead of needing per-run hand-fixing). Source of truth:
  `skills/dashboard-design/SKILL.md` (new "Chart-quality defaults" + "Visual verification
  (mandatory)" sections) and `skills/dashboard-design/agents/dashboard-team.yaml`
  (`default_prompt` extended). Regenerated the native agent adapters.
- why: A re-screenshot showed the D2 board actually rendered badly (un-aggregated trend
  scatter, broken 5000%/10000% percent axis, single-bar 'top-N', duplicate titles, clipped
  labels) even though structure/headline checks passed. Root lesson: structural verification
  is not visual verification. The agent must (1) build correct-by-default charts and (2)
  screenshot+view before claiming quality.
- defaults encoded: no duplicate titles (header carries it; inline figures title=''); line/
  trend aggregates measure by date axis (no raw-row scatter); share charts aggregate by
  (x,color) -> true 0-100% + cap dense categoricals to top-N + 'Other'; ranked/top-N ranks by
  highest-cardinality NON-constant categorical (never a filter-pinned constant); responsive
  sizing/margins; one corporate-theme styling seam; quality-asserting regression tests.
  verification: zero-install Chrome `--headless=new --virtual-time-budget --screenshot` of an
  absolute `file:///C:/...` URL, then VIEW the PNG; only claim 'professional' after seeing it.
- files: `skills/dashboard-design/SKILL.md`, `skills/dashboard-design/agents/dashboard-team.yaml`;
  regenerated `.claude/agents/dashboard-engineer.md`, `.gemini/agents/dashboard-engineer.md`,
  `.codex/agents/dashboard-engineer.toml` via `uv run generate-skill-adapters`.
- tests: none (guidance/doc + generated adapters); regeneration verified all 3 adapters carry
  the "VISUAL VERIFICATION IS MANDATORY" + "CHART-QUALITY DEFAULTS" text.
- verify: grep confirmed both rules present in .claude/.gemini/.codex adapters. The actual chart
  fixes (V1-V6) that satisfy these defaults are the next implementation step (tracked in follow_ups).

### 2026-06-10  Dashboard professionalism pass (D2): corporate-BI theme + single-board grid
- what: Static dashboard export is now a clean-corporate-BI product, not a dev artifact.
  (1) Shared Plotly theme (`_apply_corporate_theme` in `renderer.py`): navy/steel-blue/grey
  colorway, white canvas, light gridlines, consistent font/margins/legend, percent-axis
  formatting — applied at a single styling seam to every figure. (2) `export.py` rewritten to
  a single-board responsive CSS grid: a navy header bar (workspace + "Generated <date> · N KPIs
  [· M blocked]"), each KPI a card with the chart inline + a headline number + chart-type badge,
  click-through to a full detail page. (3) `render_kpi_inline` + `_kpi_headline` + `_format_measure`
  helpers: one SQL run feeds chart + headline; headline is currency/percent/count-aware. Share
  metrics headline a SEGMENT COUNT (summing percentages is meaningless and fraction-vs-percent
  units can't be inferred); x-column resolved with the same fallback the figure uses when the
  spec's `x` is a display label not present in the result columns.
- why: User asked to make the dashboard professional (chose "clean corporate BI" + "single-board
  grid"). The old export was a flat link list with bare default Plotly styling.
- files: `core/dashboard/renderer.py`, `core/dashboard/export.py`, `tests/test_dashboard_inference.py`
  (+7: CorporateThemeTests x2, HeadlineTests x5).
- tests: dashboard 37 + services green on venv; `green-gate` 346/0.
- verify: exported Healthcare-RCM board — header "Generated 2026-06-10 · 3 KPIs", 3 inline charts
  (line / stacked-percent / ranked-bar) with headlines $474.2K / 20 segments / "Commercial: $1.8K".
  user_overrides preserved; no upstream contracts edited.
- KNOWN DEFECT surfaced (D1, not D2): kpi_003 "top 10 payers" ranks by `lineofbusiness` (constant
  'Commercial' after the LOB filter) instead of `payorid`, so the ranked bar collapses to one bar
  and the headline label reads "Commercial". Logged in follow_ups as a ranked_bar dimension-selection
  fix.

### 2026-06-10  Dashboard chart-type inference (D1) + Wiki lean/linked pages (W1+W2)
- what: Two parallel subsystem refinements (built in isolated worktrees, integrated here).
  DASHBOARD (D1): correct chart-type inference by KPI shape — date/time cut -> `line`;
  top-N question -> `ranked_bar` (horizontal, sorted desc, limited to N); share/percentage
  metric -> `stacked_bar_percent` (100%-stacked, percent axis); single cut -> `bar`; 2+ cuts
  -> `grouped_bar`; no usable dimension -> `big_number` card. Measure column resolved via
  `_MEASURE_NAME_RE`; `validate_spec_columns` keeps a spec from referencing a column the SQL
  didn't emit (renderer falls back to a recovery card, never crashes). machine_defaults
  rewritten each run; user_overrides preserved verbatim. WIKI (W1+W2): KPI completion pages
  no longer inline the full generated SQL — Definition links the `.sql`, Current state shows
  only the `<kpi>_results` result-shaping body + preview table; new Lineage and Decision
  History sections (datasets->joins->grain->filters, grain/denominator choices, relationship
  approvals with provenance) and `[[..]]` cross-links, all derived from workspace contracts;
  human-authored sections preserved across regeneration.
- why: User asked to refine the dashboard (right charts) and wiki (lean, richly-linked,
  context-bearing) — see project plan. Both are generic (no domain vocabulary; genericity
  guard clean).
- files: `core/dashboard/inference.py`, `core/dashboard/spec.py`, `core/dashboard/renderer.py`,
  `tests/test_dashboard_inference.py` (new, 22); `core/wiki/writer.py`, `core/wiki/lineage.py`
  (new), `tests/test_wiki_writer.py` (new).
- tests: dashboard 22 (parse/measure/trend/topN/share/categorical/card/validation + 4 renderer
  branches, Plotly-skip aware) + dashboard_services 8; wiki_writer + wiki = 8. 38 green on the
  venv interpreter; `green-gate` 346/0 (see follow_ups: add the 2 new modules to the gate's
  discovery list so they gate, not just pass when run directly).
- verify: `.venv/Scripts/python -m unittest tests.test_dashboard_inference tests.test_dashboard_services
  tests.test_wiki_writer tests.test_wiki` -> 38 OK; green-gate all green. Two-section contract
  (machine/user) intact in both subsystems; no upstream contracts edited.

### 2026-06-10  `results` compact-by-default (+ `--full`); panel markdown never inlines SQL
- what: `workspace-flow results` is now COMPACT by default (SQL linked, tables shown);
  pass `--full` for inlined SQL (also always in the per-KPI .sql files). The results-stage
  session panel markdown's `## KPI Results` block is now compact too (was a second full-SQL
  dump that leaked `\`\`\`sql` even when the packet was compact).
- why: Live re-run (2026-06-10) showed agents reflexively call `workspace-flow results`
  to see results; with results full-by-default it truncated (~500 lines / 6 fences for 3
  KPIs), so the agent re-read `current.md` repeatedly and paraphrased the final summary
  instead of forwarding tables. Making the reflexive path compact removes the truncation
  trigger entirely. Drill-down preserved via `--full`.
- files: `core/onboarding/workspace/flow.py` (`results --full` arg; `_print_cli_panel`
  stage/full -> compact logic + threading; `_render_panel_markdown` `## KPI Results` block
  `include_sql=False`).
- tests: updated `ResultsPanelInlineRenderTests.test_results_panel_shows_definition_table_and_sql_pointer_compact`
  (panel markdown compact: pointer + table, no inline SQL); extended
  `test_complete_compact_explicit_full_and_kpi_filter` (default compact / `--full` has SQL).
  Full `test_workspace_flow` green.
- verify: live — `results` default 77 lines / 0 sql-fences, `--full` restores SQL, `--kpi`
  one KPI, `complete` auto 0 fences. (Honest caveat: whether the agent paraphrases its final
  message is its presentation choice; compact-default maximally enables verbatim forwarding.)

### 2026-06-09  Compact result auto-surface + single-KPI selection (right-size output)
- what: (1) Auto-surface at `complete` / `kpi_analyst_review` now emits a COMPACT packet
  — per-KPI definition + result table + `SQL: <path>` pointer, no inlined SQL. (2) New
  `workspace-flow results --session <id> --kpi <kpi_id>` forwards just that KPI's per-KPI
  run file. (3) Explicit `workspace-flow results` (no --kpi) stays FULL (SQL inlined) for
  drill-down. Mechanics: `render_kpi_block(..., include_sql=False)`; `_render_results_markdown(compact=)`;
  `_write_result_preview` writes a `current_compact.md` sibling; `_emit_result_packet(compact=, kpi_id=)`
  selects source; `_print_cli_panel` picks compact by stage + threads `--kpi`; the `complete`
  panel's `## Completed KPIs` block also renders compact (was a second full-SQL dump); the
  run-kpi-pipeline review-gate emit is compact too.
- why: Even after the subagent path was closed, the result PRESENTATION still failed — the
  auto-emitted packet inlined the full catalog-bootstrap SQL for every KPI (~500 lines for 3
  KPIs), so the CLI truncated it, the agent misread truncation as "can't display", and
  PARAPHRASED the tables (BUG-015-style reconstruct) and/or made the user type "show results".
  Root = oversized output causing truncation. Fix removes the cause: compact auto-surface is
  small enough to never truncate, so the agent forwards it verbatim. Single-KPI selection serves
  "give me just this KPI's solution" from the per-KPI files that already exist on disk.
- files: `core/onboarding/kpi/registry_loader.py` (`include_sql`), `core/onboarding/workspace/flow.py`
  (`_render_results_markdown` compact, `_write_result_preview` compact sibling, `_emit_result_packet`
  compact+kpi_id, `_print_cli_panel` stage-compact + kpi_filter, `## Completed KPIs` compact, `--kpi`
  CLI + threading, wrapper review-gate emit compact).
- tests: new — `ResultsPanelInlineRenderTests.test_render_kpi_block_include_sql_false_*`;
  `EmitResultPacketTests` (4: full/compact/kpi-id/not-found); `BugFixTests.test_complete_compact_explicit_full_and_kpi_filter`
  (e2e). Existing inline-render / bug013 / bug016 stay green. Full `test_workspace_flow` green.
- verify: compact packet 225->83 lines, 2599->1011 tok (-61%, bytes/4); auto-surfaces (complete +
  review-gate) emit 0 inline-SQL fences; explicit `results` keeps SQL; `results --kpi kpi_001`
  prints only that KPI.

### 2026-06-09  Stop the truncation-escalation at the blocker panel + disable Gemini subagents
- what: (1) `prepare-kpi-blocker-panel` `next_step` now carries a "truncation = success"
  guard: render the panel ONCE, do NOT re-read in another form OR delegate to a subagent,
  because `... first N lines hidden ...` means the read succeeded. (2) `.gemini/settings.json`
  `experimental.enableAgents` -> false.
- why: Re-test of a fresh workspace: onboarding + blocker panel rendered fine, then Gemini
  misread the long panel's UI-truncation banner as an incomplete read and ESCALATED by
  spawning a `generalist` subagent to "read it properly". That subagent can't read git-ignored
  `interns/` via ReadFile (subagents hard-respect .gitignore; the main agent uses
  respectGitIgnore=false), its shell fallbacks were policy-denied, so it looped until cancelled.
  Same root cause as the first-session re-read loop (truncation-as-failure), escalating via the
  subagent feature this time. The Active-Run "truncation=success" guard only covered
  results/complete/review output, not the blocker panel — so the disease recurred there.
  next_step is short and always fully visible even when the panel body truncates, so it is the
  right carrier. enableAgents=false removes the escalation path and matches the Agent Delegation
  Rule (subagents only when explicitly asked); the main agent handles the flow without them.
- files: `core/onboarding/kpi/blocker_workflow.py` (next_step guard), `.gemini/settings.json`
  (enableAgents=false + rollback note).
- tests: `tests.test_enterprise_optimization.EnterpriseOptimizationTests`
  `test_prepare_kpi_blocker_panel_next_step_carries_truncation_guard` (new); existing
  prepare/apply blocker-panel tests still green. settings.json JSON re-validated.
- verify: new test asserts the guard phrases in next_step; settings enableAgents=False.

### 2026-06-09  token-report tool: generic before/after token-cost tracing
- what: New `tools/token_report.py` (entry point `token-report`). Measures the two
  real per-session token sinks — (1) CLI **fixed context** (the files each CLI pins
  every turn, discovered generically from the CLI's own config: Gemini's
  `.gemini/settings.json` `context.fileName`, Claude's `CLAUDE.md`) and (2) optional
  **workspace run outputs** (result packet / dated run / open blocker panel). Counts
  tokens via tiktoken when present, else a deterministic utf8-bytes/4 heuristic
  (method recorded in every snapshot). Supports `--save <label>` + `--baseline` for a
  before/after delta table.
- why: The user wants token-cost tracked as a development tracing aid, and every
  change bracketed with a before/after measurement instead of eyeballing. Complements
  `tools/context_status.py` (workspace-state bytes for a live session) which does not
  cover CLI fixed context, tokens, or before/after.
- files: `tools/token_report.py` (new), `pyproject.toml` (`token-report` entry point),
  `tests/test_token_report.py` (new).
- tests: `tests.test_token_report` (5) — deterministic counter, file measure
  (present/missing), generic CLI discovery (gemini found / claude skipped when absent),
  grand-total rollup, compare delta. Green on the venv interpreter.
- verify: real before/after of the 2026-06-08 settings.json cut — gemini fixed context
  55,057 -> 16,336 tok (-38,721, -70%), grand total 62,378 -> 23,657 (-62.1%) on the
  bytes/4 heuristic (excludes the also-removed includeDirectoryTree runtime cost, so
  the true saving is larger). settings.json round-trip restored + JSON re-validated.

### 2026-06-09  Auto-emit KPI result packet at the kpi-analyst review gate
- what: The full result packet + Active Run pointer now print when the flow STOPS
  at the `kpi_analyst_review` gate, not only on `complete`/`results`. Added
  `kpi_analyst_review` to the emit condition in `_print_cli_panel`, and `run-kpi-pipeline`
  now emits the packet before its review-gate `_gate_stop`. Refactored the duplicated
  packet+pointer printing into one shared `_emit_result_packet(repo_root, workspace_rel)`.
- why: Operator complaint — had to type "show results" to see the tables. The flow
  stops at the enforced human review gate (BUG-014) BETWEEN generation and `complete`;
  results are already executed by then (preview is written before the gate) but the
  packet only printed on `complete`, so a CLI driving `workspace-flow start` saw a
  bare gate and no rows until a separate call. The reviewer needs the SQL + rows to
  judge intent anyway — so surface them AT the gate. (KPI Result Packet Forwarding
  Rule: needing to ask is a bug.) The review gate itself is unchanged (still enforced).
- files: `core/onboarding/workspace/flow.py` (`_emit_result_packet` new shared helper;
  `_print_cli_panel` + `pipeline_main` review-gate stop call it).
- tests: `tests.test_workspace_flow.BugFixTests.test_results_auto_emit_at_kpi_analyst_review_gate`
  (new — `status` alone prints packet + Active Run at the gate); full `test_workspace_flow`
  (34) green on the venv interpreter. bug013/bug016 still green (refactor parity).
- verify: new test drives `start` -> asserts stop at `kpi_analyst_review`, then a plain
  `status --session` prints "KPI Result Packet" + "## Active Run" + "results.md".

### 2026-06-08  Active-Run surface so completion can't loop on UI truncation
- what: Added an `## Active Run` block at the visible tail of `complete`/`results`
  CLI output naming the stable dated surface (`interns/runs/<date>/results.md` +
  per-KPI `kpi_*.md`) with explicit "read ONCE; `... first N lines hidden ...` means
  the read SUCCEEDED; do not re-read with -TotalCount/-Head/-Tail/Select-String or
  open the evidence JSON" guidance. New module helper `_active_run_paths` resolves
  today's run dir (fallback: latest dated dir; None if no snapshot).
- why: In the 2026-06-08 Hospital-A run the pipeline completed and wrote the packet,
  but the CLI frontend truncated the big inline packet ("first N lines hidden"); the
  driving agent misread truncation as a read failure and looped re-reading results in
  many forms + opened 109KB/384KB machine JSON, never presenting (user cancelled). The
  doc rule (GEMINI.md:180-192) already said this and still failed — so make it a
  structural tail pointer at the always-visible end of output.
- files: `core/onboarding/workspace/flow.py` (`_active_run_paths` + Active Run block
  in `_print_cli_panel`).
- tests: `tests.test_workspace_flow.ActiveRunPathsTests` (3, new) — today/fallback/none;
  full `tests.test_workspace_flow` (33) green on the venv interpreter.
- verify: live `workspace-flow results --session wf_20260608T145532Z` prints the Active
  Run block with the dated runs/2026-06-08/results.md + 3 per-KPI paths. (Noted a
  separate pre-existing cp1252 UnicodeEncodeError on `→` in generated SQL comments when
  stdout isn't UTF-8 — logged in follow_ups.md, not fixed.)

### 2026-06-08  Cut Gemini per-turn context bloat (~39k tok/turn)
- what: Removed `TOOLS.md` (~16k) and `.agents/tools.json` (~23k) from the pinned
  `context.fileName` array in `.gemini/settings.json` and set `includeDirectoryTree:false`.
  Pinned context dropped from 6 files (~56k tok) to 4 files (~17k tok) loaded every turn.
- why: Diagnosing the 2026-06-08 Hospital-A run (user-reported token burn). The visible
  cost was a post-completion re-read loop, but the bigger silent tax was ~56k tokens of
  fixed context carried across ~40 tool calls. TOOLS.md + tools.json are read-on-demand
  references (GEMINI.md:4 / AGENTS.md:468) and `jitContext:true` loads them when needed —
  pinning a 93KB machine registry into every turn was pure waste. Claude only pins
  CLAUDE.md (~2.5k) for comparison.
- files: `.gemini/settings.json` (context block only; fileFiltering/tools/model untouched).
- tests: none (config-only). JSON validity re-checked with `json.load`.
- verify: `python -c "import json; json.load(open('.gemini/settings.json'))"` -> valid;
  pinned list now GEMINI.md / AGENTS.md / workspace-workflow-prompt.md / gemini-cli-reference.md.
  Rollback baseline + result-forward-loop follow-up recorded in develop_spec/follow_ups.md.

### 2026-06-08  Grain-blocker panel route works end-to-end (follow_ups #1 fixed)
- what: Two coupled fixes so a share-by-continuous-cut KPI's grain decision is
  answerable via the panel instead of looping. (1) `_load_registry_with_features`
  (intent_contract.py) now backfills the positional `kpi_{idx:03d}` the rest of the
  system uses, so intent questions/answers carry a real kpi_id (was "" -> question_id
  `intent__<facet>`, decisions mirrored to `pipeline_decisions[""]` the generator never
  read). (2) `BlockerQuestionPanelBuilder` now promotes the one HARD-blocking intent
  facet (`grain_bucketing`, via new `_HARD_BLOCKING_INTENT_FACETS`) to `current` when
  there are no feature-mapping blockers, so apply-kpi-panel-answer resolves it; advisory
  facets (denominator_scope, temporal_anchor) stay set-only, preserving flow-stop design.
- why: a Gemini run looped on the kpi_002 grain blocker -- the blocker panel reported
  "no options" (grain was set-only + mis-keyed to ""), so apply-kpi-panel-answer /
  workspace-flow answer errored and the agent thrashed (~7% quota). Verified live: the
  real workspace's current.json is now the grain_bucketing question for kpi_002 with
  options; apply-kpi-panel-answer option_a records grain_bucketing_decisions[kpi_002].
- files: core/onboarding/kpi/intent_contract.py (kpi_id backfill),
  core/onboarding/kpi/blocker_question_panel.py (_HARD_BLOCKING_INTENT_FACETS + current
  selection), tests/test_kpi_intent_contract.py (3 regression tests).
- tests: tests.test_kpi_intent_contract 52/52 (added TestRegistryWithoutKpiIdBackfill x2,
  TestHardBlockingFacetBecomesCurrent x1 -- prior fixtures always set kpi_id, masking the
  bug). green-gate: all green.
- verify: .venv python -m core.dev.green_gate (all green); live apply on the RCM
  workspace recorded grain_bucketing_decisions[kpi_002].

### 2026-06-08  GEMINI.md guardrails: results-read discipline + grain-blocker routing
- what: Added two guardrails to GEMINI.md. (1) "Results read discipline" under the KPI
  Result Packet Forwarding Rule: read `kpi_results/current.md` ONCE, never `Get-Content` the
  ~2000-line evidence `current.json`, never use `-Wait`, and treat "first N lines hidden" as a
  UI truncation (read succeeded) rather than retrying. (2) New "Grain-Bucketing Blocker Rule":
  when the execution harness blocks a share KPI on a grain decision the blocker panel has no
  options, so route to the deterministic `apply-pipeline-decision --kpi-id <id>
  --grain-bucketing band_continuous_cuts` + re-run, instead of looping on the broken
  apply-kpi-panel-answer / workspace-flow answer routes.
- why: a fresh Gemini run burned ~7% of quota in one "show me the results" turn by re-reading
  the results ~8 ways (incl. the evidence JSON whole and a hanging `-Wait`), and separately
  looped on the kpi_002 grain blocker via panel/answer routes that error out ("current panel
  has no options" / "not waiting for a supported answer"). These are operator-side mitigations;
  the underlying panel-surfacing bug stays open (follow_ups #1).
- files: GEMINI.md
- tests: n/a (operating-doc guidance, not platform code)
- verify: next Gemini run should read the packet once and use apply-pipeline-decision for the
  grain blocker; quota burn per results turn should drop sharply.

### 2026-06-07  Gemini reads git-ignored interns/ artifacts (BUG-018 actually fixed)
- what: Set `.gemini/settings.json` fileFiltering.respectGitIgnore=false +
  respectGeminiIgnore=true, and rewrote `.geminiignore` from an (inert) negation
  list into a self-contained denylist that hides secrets/PHI/heavy files on its own.
  Lightweight interns/ artifacts (reports/**, generated/**, current.json|md,
  handoffs, metadata_store) are now readable by native ReadFile/Glob/SearchText.
- why: `.gitignore:15 workspaces/**/interns/` is a DIRECTORY exclusion, which a child
  `!negation` cannot undo per gitignore rules -- so the old `.geminiignore` re-includes
  never took effect. The main agent only read these files via shell Get-Content (the
  documented context-burning workaround); the built-in read-only `generalist` subagent
  has no shell, hit "ignored by configured ignore patterns" then "denied by policy",
  spun, and was cancelled (observed when the operator typed "show me the results").
  git commit behavior is UNCHANGED -- git still ignores interns/.
- files: .gemini/settings.json, .geminiignore
- tests: n/a (Gemini CLI config, not platform code; not covered by green-gate)
- verify: `git check-ignore -v <interns report .md>` still matches `.gitignore:15`
  (git unchanged); Gemini-side proof is a subagent ReadFile of
  `workspaces/**/interns/reports/**/*.md` now succeeding.

### 2026-06-07  band_continuous_cuts now actually bands; results render inline
- what: (1) A recorded `band_continuous_cuts` grain decision now emits banded SQL
  (`FLOOR(value / width) * width AS <cut>_band`, default width 10, overridable via
  `band_continuous_cuts:<n>`) instead of silently unblocking and still grouping by
  the exact continuous value. `exact_value_grain` keeps the exact grain. Applies to
  both the window/mismatched-grain share path and the general GROUP BY path; unit-
  agnostic (age years / days-since days). (2) The `workspace-flow results` panel
  markdown now renders the full packet (definition + SQL + result table) inline via
  the shared `render_kpi_block`, not just artifact paths.
- why: a Gemini session hit the grain-bucketing block on a share-by-age KPI, then
  HAND-EDITED the generated kpi_002.sql to bypass it (violating "never hand-edit
  generated contracts") and produced the exact ~7.4k-row 0.2%-each fragmentation
  the block warns about. Root cause: `grain_bucketing` was only read to decide
  whether to block (result_view_builder.py) — no code path consumed the band width,
  so the "proper" answer was a no-op identical to the hand-edit. Separately, the
  operator had to type "show results" because the `results` command emitted only
  paths, not the rendered tables.
- files: core/onboarding/kpi/result_view_builder.py (`_band_expr`,
  `_band_width_from_decision`, band_width threaded through `_detect_date_arithmetic`
  + both call sites + `age_band` skip-set); core/onboarding/workspace/flow.py
  (`_render_panel_markdown` renders `summary.kpis` inline when `completed_kpis`
  absent); tests/test_result_view_builder.py (+4 banding tests);
  tests/test_workspace_flow.py (+2 inline-render tests).
- tests: tests.test_result_view_builder (40 ok), tests.test_workspace_flow (30 ok),
  combined 70 ok; green-gate 345/0.
- verify: `.venv/Scripts/python.exe -m unittest tests.test_result_view_builder
  tests.test_workspace_flow` and `.venv/Scripts/python.exe -m core.dev.green_gate`.

### 2026-06-07  Break grain-decision deadlock; reject fabricated harness
- what: three fixes that together make a share-by-continuous-cut KPI actually
  completable end-to-end (verified live on RCM kpi_002: 1732 banded rows, all four
  cuts, within-department share):
  (1a) `apply-pipeline-decision` gains `--grain-bucketing` (mirrors
  `--percentage-denominator-scope`) — a direct path to record the grain decision
  under the correct `kpi_id` that does NOT run the heavy validator, so it cannot
  deadlock.
  (1b) the execution harness classifies an intent-decision-blocked KPI as
  `blocked_pending_decision` (new status) instead of `failed`; the artifact
  validator treats that as non-fatal (only a genuinely failed record means "did
  not pass"). This breaks the deadlock where a grain-blocked KPI made the
  validator fail, which blocked applying the answer that unblocks it.
  (2) the validator now RE-EXECUTES the result views and compares
  status/columns/row_count to the on-disk harness manifest — a hand-faked manifest
  (status flipped to passed, dummy sample row) is rejected as a possible tamper.
  Also: harness semantic checks (`parse_kpi` metric check + `grain_coverage`) are
  now grain-aware so the banded `age_band` form is not mis-flagged as a dropped
  cut or an unimplemented metric (verifier must parse the KPI the same way the
  generator did).
- why: a post-fix Gemini run could not record `band_continuous_cuts` (validator
  deadlock), so it hand-edited `kpi_execution_harness.json` to fake a pass with a
  `Dummy` row and marched on. Root causes: blocked-pending misclassified as
  failed; no `--grain-bucketing` direct path; validator trusted the harness JSON
  without re-execution.
- files: core/onboarding/pipeline_plan.py (decision_main: --grain-bucketing);
  core/onboarding/kpi/execution_harness.py (BLOCKED_PENDING_STATUS,
  sql_is_intent_blocked, execute_only, grain-aware metric check, counts);
  core/onboarding/workspace/validation.py (blocked_pending tolerance +
  _verify_harness_against_execution); core/onboarding/kpi/intent_coverage.py
  (age cut accepts age_band); tests in test_kpi_execution_harness.py (+3) and
  test_pipeline_plan.py (+2).
- tests: test_kpi_execution_harness + test_pipeline_plan (30 ok); broad sweep
  (workspace_flow / nl_chain / relationship / pipeline harness, 63 ok); green-gate
  345/0. Live RCM e2e: deadlock broken, kpi_002 passes banded, tampered manifest
  rejected.
- verify: `.venv/Scripts/python.exe -m unittest tests.test_kpi_execution_harness
  tests.test_pipeline_plan` + `.venv/Scripts/python.exe -m core.dev.green_gate`.

### 2026-06-07  Residuals: readable band labels + cross-CLI forwarding docs
- what: (1) banded continuous cuts now display a readable `20-29` range label
  (`CONCAT` of the band bounds) while GROUP BY / ORDER BY / PARTITION BY use the
  numeric `CAST(FLOOR(v/width) AS BIGINT)*width` lower bound — so bands sort
  numerically (100-109 after 20-29, not lexically). New optional
  `Dimension.display_expression` decouples the SELECT projection from the group
  key. (2) Documented the auto-forward result-packet behavior cross-CLI:
  strengthened AGENTS.md's forwarding rule to "present automatically on
  completion" and added a matching section to GEMINI.md (which had none).
- why: follow-ups from the 2026-06-07 banding fix — a bare numeric band reads
  poorly, and the "show results" friction was tool-fixed but only doc-enforced for
  Claude (CLAUDE.md), not Gemini.
- files: core/onboarding/kpi/result_view_builder.py (`Dimension.display_expression`,
  `_band_label_expr`, BIGINT cast in `_band_expr`, triple return from
  `_detect_date_arithmetic` + callers, SELECT uses display_expression);
  AGENTS.md; GEMINI.md; tests/test_result_view_builder.py (+1 numeric-sort lock,
  updated 2 band tests).
- tests: tests.test_result_view_builder (41 ok); green-gate 345/0.
- verify: `.venv/Scripts/python.exe -m unittest tests.test_result_view_builder` +
  DuckDB exec spot-check (bands render `30-39`/`100-109`, sorted numerically).

### 2026-06-06  Complete partial-completion threading for mixed KPI sets (Issue #2)
- what: finished threading the deferred (undefined) KPI set through the
  feature-blocker panel and the source-to-target gate so a MIX of defined +
  undefined KPIs reaches generation and yields partial results instead of stalling
  at the feature-blocker stage. Blocker panel now filters out deferred KPIs'
  unresolved features (and empty clusters); source-to-target planner marks
  undefined KPIs `deferred` (excluded from `blocked_kpi_count`, new
  `deferred_kpi_count`); `compute_workflow_diff` records deferred KPIs as deferred
  gaps with no recovery commands. Both the panel and planner self-derive the
  deferred set (consistent with flow._undefined_kpis / the validator), so no flow
  plumbing is required.
- why: Phase 5 (partial) only handled the all-undefined definition gate; a mix
  still stopped at the feature-blocker stage asking about the undefined siblings.
- files: core/onboarding/kpi/blocker_question_panel.py (deferred_kpi_ids threading
  + new `_deferred_kpi_ids_from_registry` helper -- the missing piece that left the
  interrupted Wave-2a stash red); core/onboarding/relationships/
  source_to_target_planner.py (deferred status + count, `_kpi_is_undefined`);
  core/onboarding/workspace/flow.py (compute_workflow_diff deferred gap);
  tests/test_partial_completion_deferred.py (new).
- tests: 8 (deferred-id derivation incl. enumerated ids; feature filtering;
  undefined detection).
- verify: enterprise + workspace_flow suites (106) OK; green-gate 345/0.
- note: panel/planner restored from the interrupted Wave-2a stash and completed;
  stash@{0} now fully salvaged (safe to drop).

### 2026-06-06  Wire parallel-completion planner into run-kpi-pipeline (Issue #4)
- what: `pipeline_main` (run-kpi-pipeline) now calls `dispatch_parallel_completion`
  between the relationship gate and `workspace-flow start`: it builds the
  dependency-aware completion plan and records the parallel-vs-sequential decision
  (instead of the plan only being emitted on demand). Above the ready-KPI threshold
  it recommends the `parallel_kpi_completion` delegation route; at/under it the
  pipeline runs sequentially as before. Advisory + defensive (try/except) so a
  planning failure degrades to a note and never breaks the run. Actual worker
  spawning stays with the delegation layer per the module's contract.
- why: the planner previously only emitted an artifact; the fan-out decision was
  never made/recorded in the deterministic chain.
- files: core/onboarding/workspace/flow.py (pipeline_main wiring);
  core/onboarding/kpi/parallel_completion.py (resolve_parallel_threshold,
  count_ready_kpis, decide_worker_count, DispatchDecision,
  dispatch_parallel_completion -- salvaged from the interrupted Wave-2a stash);
  tests/test_parallel_completion.py.
- tests: tests.test_parallel_completion (16) OK.
- verify: flow.py compiles; green-gate 345/0; pipeline-wrapper test shows only the
  documented pre-existing relationship-gate failure (stash-confirmed, not mine).
- remaining: autonomous concurrent dispatch of the parallel route from the runner
  (today it records the recommendation; the delegation layer performs fan-out).

### 2026-06-06  Verify grain_bucketing facet end-to-end (panel -> apply -> decision)
- what: confirmed #1's `grain_bucketing` facet flows through the live path without
  any panel code change: `blocker_question_panel` -> `intent_facet_panel_questions`
  surfaces the routed facet; `record_intent_answer(facet="grain_bucketing")` mirrors
  to `pipeline_decisions.json` (re-read by the SQL generator); the panel converges
  after the answer is recorded.
- why: #1 unit-tested the facet + recorder in isolation; this closes the
  panel-emission and apply-dispatch seam in-process.
- files: tests/test_kpi_intent_contract.py (TestGrainBucketingPanelE2E).
- tests: 1 in-process e2e (emit -> persist -> converge).
- verify: green-gate 345/0.

### 2026-06-06  Align executable-relationship default (conservative, fail-safe)
- what: `contracts.py::_executable_allowed` defaulted `allowed_in_sql_generation`
  to True when absent; the validator's `validation.py::_relationship_executable`
  defaulted to False. Aligned the contract side to the conservative default
  (`... is True`) so an absent/unknown policy means NOT executable in both places.
- why: a single source of truth for "is this relationship usable in generated SQL."
  Builders always emit the key, so this only hardens malformed/partial contracts;
  101 relationship/enterprise tests stayed green (nothing relied on the True default).
- files: core/onboarding/relationships/contracts.py;
  tests/test_relationship_contracts.py (ExecutableDefaultAlignmentTests).
- tests: 4 (absent->not-exec in both; explicit true/false agree; non-exec state).
- verify: green-gate 345/0.

### 2026-06-06  Workflow guard: completion claim no longer masks unrecovered apply failures (Issue #6)
- what: a run of failed mutation/apply commands followed by a completion/results
  claim was not blocked. The recovery heuristic counted any later `uv run ...`
  (including the completion command itself) as recovery, silencing
  `failed_without_recovery`. Added `_check_completion_after_unrecovered_failures`
  emitting error `completion_claim_over_unrecovered_failures`; tightened
  `_has_retry_or_recovery` + added `_is_mutation_command` / `_is_completion_claim`
  and MUTATION/COMPLETION/RECOVERY token sets.
- why: an "all KPIs proven / complete" claim could be made over unrecovered mutation
  failures; readiness was never blocked. Now propagates workflow-guard -> reliability
  -> project harness as a blocker.
- files: core/onboarding/harness/workflow_guard_harness.py;
  tests/test_workflow_guard_failed_apply_before_completion.py (new).
- tests: 4 (masking reproduction + new error + genuine-retry clean + no-claim clean)
- verify: green-gate 345/0; project_harness + reliability suites green. Generic
  (no domain/CLI-brand hardcoding).

### 2026-06-06  Triage two pre-existing test failures (pytest module + sql-gen stub)
- what: (A) converted tests/test_relationship_state_preservation.py from a pytest
  module to unittest (venv has no pytest; assertions unchanged 1:1). (B) implemented
  the real catalog-bootstrap contract in core/onboarding/pipeline_sql_generator.py
  ::generate() (was a stub) — emits `-- BEGIN/END CATALOG BOOTSTRAP` wrapping
  `catalog_raw_<stem>` readers + per-object bronze_/silver_/gold_<stem> layer views
  referencing only bootstrap views. Removed the two now-passing
  test_pipeline_sql_generator entries from green_gate.py KNOWN_BASELINE.
- why: (A) import error in venv; (B) generator never emitted the bootstrap markers /
  layer names the tests and the kpi/sql_generator.py contract require.
- files: tests/test_relationship_state_preservation.py,
  core/onboarding/pipeline_sql_generator.py, core/dev/green_gate.py.
- tests: 11 (2 + 9) OK
- verify: green-gate 345/0; sweep 0 regressions / 1 known-baseline.

### 2026-06-06  Close derivation-pattern detection gaps (duration-threshold + within-N-days recurrence)
- what: Phase 4 patterns were wired but did not FIRE for the named phrasings.
  Duration: added comparator verbs (exceeds/exceeding/beyond/past/at most/lasting/
  up to/no more|less than) + new explicit-subtraction form `_DURATION_DIFF_RE`
  ("STOP - START > 24 hours"). Recurrence: replaced the `_RECURRENCE_HINTS` substring
  list with `_RECURRENCE_RE` word-stem regex covering noun+verb forms
  (recurrence/recurring/reorder/repeat/readmission/readmitted) without
  false-positiving on release/review/region.
- why: detectors missed exactly the two KPI shapes the follow-up targeted; "readmit"
  is not a substring of "readmission" and noun forms were uncovered.
- files: core/onboarding/features/derivation_patterns.py;
  tests/test_derivation_patterns.py (new cases + genericity guard on the module).
- tests: duration verb/subtraction, recurrence noun-form, re-prefix non-false-positive,
  GenericityGuardTest (31 OK with test_metric_derivation)
- verify: green-gate 345/0.

### 2026-06-06  Hard-block exploded grain for share metrics cut by raw continuous dimensions
- what: escalate the raw exact-age/days-since continuous-cut grain explosion from a
  non-blocking WARN (e9d9d2c) to a hard BLOCKER for share/percentage metrics; the
  generator now PROPOSES fixed-width bands instead of a per-exact-value GROUP BY,
  surfaced via a new low-confidence `grain_bucketing` intent facet routed into the
  blocker panel and persisted to pipeline_decisions like `denominator_scope`.
- why: a share KPI cut by exact integer age fragmented results into ~7,400 rows each
  ~0.2% (meaningless denominator); the WARN did not stop the bad output.
- files: core/onboarding/kpi/result_view_builder.py (share-metric + raw-continuous-cut
  detectors; grain_bucketing param + grain_bucketing_block on ParsedKPI; block before
  GROUP BY; blocked-marker render), core/onboarding/kpi/intent_contract.py (grain_bucketing
  facet + answer mirror), core/onboarding/pipeline_plan.py (record_grain_bucketing),
  core/onboarding/kpi/sql_generator.py (load+thread decision),
  tests/test_result_view_builder.py, tests/test_kpi_intent_contract.py.
- tests: GrainBucketingBlockTests (8) + TestGrainBucketingFacet (6) (OK; re-run by parent)
- verify: tests.test_result_view_builder + tests.test_kpi_intent_contract (14 OK);
  green-gate 345 tests, 0 failing; genericity guard OK.

### 2026-06-06  relationship-approval count non-monotonic (display-only, fixed at envelope)
- what: VERDICT display-only. On-disk counts were always correct (apply recomputes the
  summary from the full persisted list inside workspace_lock); the governed CLI
  idempotent-replay branch echoed the FIRST apply's cached payload, so a re-issued
  approval reported a stale lower executable_relationship_count (e.g. 3->7->4->5->6).
  Fixed at source: on replay, re-run fn() under workspace_lock and report current
  persisted state; fall back to cached payload only if the refresh raises.
- why: not a race (cross-process mutex verified) and not a bad recompute (sequential
  return verified) — only the replay echo was stale.
- files: core/onboarding/workspace/cli_runner.py; tests/test_relationship_apply_count.py
  (new); tests/test_workspace_lock.py.
- tests: test_sequential_apply_count_is_correct_and_monotonic,
  test_replay_reports_current_disk_count_not_stale_payload (reproduces+guards),
  test_concurrent_apply_no_lost_update, test_cross_process_mutual_exclusion (8 OK).
- verify: tests.test_relationship_apply_count + tests.test_workspace_lock (8 OK);
  green-gate 345 tests, 0 failing.

### 2026-06-05  Activation/reliability loop + semantic grill + derivation patterns (Phases 0-5)
- what: convert advisory/CI-only guards into in-envelope hooks, generic + pipeline-wide.
  - P0/1/2: core/governance/op_signals.py + cli_runner hooks (live tripwire
    reliability + signal->skill activation) + empty-panel carries routing.
  - P3: verify_kpi_output non-blocking semantic gloss mismatch; generation passes
    dictionary glosses into derivation.
  - P4: core/onboarding/features/derivation_patterns.py (duration bucket +
    recurrence self-join), wired into the resolver's undefined-KPI branch.
  - P5 (partial): definition gate blocks only when ALL undefined; generation skips
    deferred. Full partial-result threading deferred (see follow_ups).
- why: skills/reliability tools lived BESIDE the pipeline, never fired on the live
  path (the "why didn't anything activate" gap); engine ignored its own kpi-analyst
  rule; feature-derivation-library had no built-in patterns.
- tests: test_op_signals, test_verify_semantic_gloss, test_derivation_patterns
  (new) + flow/derivation/panel suites green on the venv interpreter.
- commits: 4596bef, 05ad704, c43c663, e3e1ff0 (after base commit 981a0f7).
- NOTE: parallel worktree agents were attempted first but failed -- worktrees
  branch from the last commit and the whole session was uncommitted; committed the
  base (981a0f7) then built inline.

### 2026-06-05  Dictionary-grounded + specificity-aware measure selection
- what: derivation now obeys AGENTS.md "Data Model Driven Generation" (don't map
  on column-name similarity alone). Two generic, scenario-based rules:
  (1) SPECIFICITY tie-break — a non-entity question term outranks the entity/table
      word when ranking measures (`_measure_specificity` in metric_derivation.py);
  (2) GLOSS grounding — column data-dictionary descriptions are wired into
      derivation via onboarding `_load_column_glosses` (+ derive's existing
      dictionary_entries). No file/column hardcoded; convention-based discovery.
- why: kpi_007 "average total claim cost" picked `BASE_ENCOUNTER_COST` because
  "encounter" matched by name; the dictionary that defines `Total_Claim_Cost` was
  never consulted (the system violated its own kpi-analyst skill rule).
- files: core/onboarding/kpi/metric_derivation.py, core/onboarding/workspace/
  onboarding.py; tests/test_metric_derivation.py (MeasureSpecificityTests).
- tests: 63 across derivation-dependent suites green.
- verify: removed the human override and re-onboarded -> engine self-derives
  `avg(TOTAL_CLAIM_COST)` / `PAYER`.

### 2026-06-05  Fix phantom age dimension in result-view SQL
- what: `_AGE_PATTERN` "age of/from <col>" alternative had no leading `\b`, so
  "percent`age of` total" matched and treated "total" as a date column ->
  `date_diff('year', CAST("total" AS DATE)) AS age` on a percentage KPI.
- why: redefined kpi_004 (zero payer coverage %) generated broken, ungrouped SQL.
- files: core/onboarding/kpi/result_view_builder.py; tests/test_result_view_builder.py
  (AgeAsOfEventDateRegressionTests.test_percentage_of_total_name_does_not_emit_phantom_age_dimension).
- tests: result-view suite green; new regression added.
- verify: regenerated kpi_004 -> clean `COUNT(*)/COUNT(*) OVER ()*100 WHERE
  PAYER_COVERAGE = 0`; executed on real data -> 13,586/27,891 = 48.71%.

### 2026-06-05  apply-kpi-definition loop-close (human-confirmed KPI defs)
- what: new `apply-kpi-definition` CLI + decision store
  (`interns/generated/decisions/kpi_definitions.json`) + onboarding re-apply.
  Persists a human-confirmed metric/grain (source: human, --confirmed-by),
  mirrors into the live contract, survives re-onboarding. Closes the loop so the
  `kpi_definition_required` blocker can actually be answered.
- why: the panel could ask for a KPI definition but nothing could apply it ->
  NL KPIs could never complete (the Gemini session's deeper blocker).
- files: core/onboarding/kpi/kpi_definition.py (new), core/onboarding/workspace/
  onboarding.py (_apply_accepted_kpi_definitions), pyproject.toml; tests/
  test_kpi_definition_apply.py (new).
- tests: 64 across affected suites green (.venv).
- verify: applied kpi_004/005/006 on Hospital_Patient_Records ->
  ready_kpi_count 5 -> 8 (kpi_003/010 deferred).
- FOUND (pre-existing, not from this change): generation stamps one global
  result_format on all KPIs -> broken SQL for non-time-series KPIs. See
  follow_ups.md (per-KPI result_format) — this blocks REAL result tables.

### 2026-06-05  KPI dedupe + string-date derivation + parallel-completion planner
- what:
  - Dedupe KPIs by business question across registries (fixes 20-vs-10 double
    count when a generated registry + its source `.sql` were both ingested).
  - Recognize string-typed ISO timestamps as dates via sample-value evidence, and
    recover the count grain from the entity's own table name when distinct-counts
    are absent; onboarding now derives empty metric/cuts from profile evidence.
  - Run the `kpi_definition_incomplete` gate before the feature-blocker panel; the
    resolver emits an answerable definition blocker for empty KPIs (no more silent
    "0 questions" dead-end on the direct panel path).
  - New `plan-kpi-completion` CLI + `core/onboarding/kpi/parallel_completion.py`:
    dependency-aware parallel plan (shared blockers resolved once; independent
    components fanned across 2/4/6 workers). New `parallel_kpi_completion` routing
    stage.
- why: workspace showed 20 blocked KPIs / 0 questions (dead end) + duplicate
  count; goal also to complete many KPIs faster.
- files: core/onboarding/workspace/onboarding.py, core/onboarding/kpi/
  metric_derivation.py, core/onboarding/kpi/feature_resolver.py, core/onboarding/
  kpi/blocker_question_panel.py, core/onboarding/workspace/flow.py, core/
  onboarding/kpi/parallel_completion.py, core/onboarding/workspace/delegation.py,
  pyproject.toml; tests/test_metric_derivation.py, tests/test_kpi_registry_dedupe.py
  (new), tests/test_parallel_completion.py (new), tests/test_workspace_flow.py.
- tests: 75 across the touched + adjacent suites green (.venv unittest).
- verify: re-onboard + resolve on a sample workspace -> 10 KPIs (was 20),
  5 ready_for_sql, blocker panel non-silent; `validate-workspace-artifacts` ok=true;
  `plan-kpi-completion` -> 4 components / 2 workers.
- notes: two PRE-EXISTING failures unrelated to this change — see testing.md.

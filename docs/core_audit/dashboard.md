# dashboard — audit

## Purpose
`core/dashboard/` builds the per-workspace BI dashboard from KPI evidence. It owns: a two-section
JSON spec contract (`machine_defaults` rewritten on regen, `user_overrides` preserved); chart-type
inference from KPI registry text (`inference.py`) and — preferred — from the *actual* executed result
shape (`profile.py` + `chart_knowledge.py`, a data-to-viz knowledge base); a live Dash/Plotly renderer
(`renderer.py`) that re-executes each KPI's generated SQL via in-memory DuckDB and draws one panel per
informative dimension; a static-HTML export mirroring the live app with vanilla JS interactivity
(`export.py`); a swappable DESIGN.md design-token layer (`design_md.py`); and a headless-browser
screener that screenshots and deterministically checks every page (`screener.py`). It auto-runs on
`workspace-flow complete` (via `core/onboarding/workspace/flow.py`) and from the
`uv run workspace-dashboard` CLI (`tools/workspace_dashboard.py`).

Note: the repo-root `dashboard.py` (4913 lines) is an UNRELATED operator/medallion artifact console
built on `core.dashboard_services`. It does not import `core.dashboard` and shares no functions — no
functional duplication (see Cross-package coupling).

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 51 | Package facade; re-exports spec + `infer_chart` | (re-exports) |
| `chart_knowledge.py` | 260 | data-to-viz chart-selection knowledge base (thresholds + chooser fns) | `ChartChoice`, `choose_categorical_chart`, `choose_trend_chart`, `choose_two_categorical_chart`, `detect_geo_columns`, `is_ordinal_categories`, `value_spread` |
| `design_md.py` | 222 | DESIGN.md token parser (YAML frontmatter + loose key:value) | `DesignTokens`, `parse_design_md`, `load_design_tokens`, `_parse_frontmatter`, `_font_family_stack` |
| `export.py` | 570 | Static HTML export (index + per-KPI page) w/ vanilla JS tiles, view toggles, data viewer | `export_static_html`, `_wrap_page`, `_panels_html`, `_data_view_html`, `_tile_html` |
| `inference.py` | 416 | Text-based chart inference + SQL alias parsing + spec validation | `infer_chart`, `parse_result_view_columns`, `infer_measure_column`, `validate_spec_columns`, `SpecColumnError` |
| `profile.py` | 368 | Evidence-driven panel selection from executed rows; cycle-free DuckDB executor | `ColumnProfile`, `profile_columns`, `choose_measure`, `decide_panels`, `execute_result_view` |
| `renderer.py` | 1199 | Dash app + Plotly figure builder + live SQL re-exec + headline/data-view | `build_dash_app`, `render_kpi_inline`, `_figure_from_spec`, `_execute_sql_view`, `_kpi_headline`, `load_kpi_statuses` |
| `screener.py` | 381 | Export + headless screenshot + deterministic page checks + vision-review gate | `screen_dashboard`, `record_vision_review`, `vision_review_pending`, `_screenshot`, `_check_html` |
| `spec.py` | 314 | Spec build/save/load/merge; workspace refresh entrypoint | `DashboardSpec`, `build_kpi_spec`, `refresh_workspace_dashboard`, `save_kpi_spec`, `load_kpi_spec`, `merge_spec` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [DEAD] | renderer.py:951 `render_kpi_html` | Exported in `__all__` but called nowhere (export uses `render_kpi_inline`/`_panel_html`; no test references it). | Remove, or wire it where a single-fragment render is genuinely needed. |
| [DEAD] | renderer.py:575,602,641,659 | `_detect_artifact_dialect`, `_non_sql_dialect_card`, `_build_kpi_figure`, `_kpi_render_data` are defined but never invoked internally or in tests. The non-SQL (polars/pyspark) dialect card is thus never shown — those KPIs silently fall to "no rows" / blocked. | Either wire `_non_sql_dialect_card` into `build_dash_app`/export for non-SQL dialects, or delete the dead quartet. |
| [NOT-PROD] | renderer.py:31-39,55; profile.py:317-325,348 | `_at_repo_root` does a process-wide `os.chdir(repo_root)` around DuckDB exec. Not thread-safe and not safe under the Dash dev server with multiple workers / concurrent callbacks; a second render mid-chdir sees the wrong cwd. Generated SQL relies on relative `read_csv_auto` paths. | Pass an absolute base path into the SQL (or `SET file_search_path`) instead of mutating global cwd; at minimum guard with a lock. |
| [NOT-PROD] | screener.py:54-71 | `_screenshot` runs headless Edge/Chrome with `--virtual-time-budget=12000` + 90s timeout but ignores the subprocess return code (`capture_output=True` then discarded); success is inferred only from file existence+size. A browser that writes a partial/old PNG then errors is treated as success. agent-browser mentioned in docstring but never actually used. | Check `proc.returncode`; surface stderr tail on failure; remove the agent-browser claim or implement it. |
| [BUG] | renderer.py:317-319,1000-1001 | `big_number` headline/value uses `next(iter(first_row.values()))` — the FIRST column. If the result view emits a label/dim column before the measure (common), the card shows a non-measure (or a string coerced to 0 in the Indicator at line 323). | Use the resolved measure column (`choose_measure`/`infer_measure_column`) for `big_number`, not positional first value. |
| [BUG] | renderer.py:976-977 | `_format_measure` percent branch: `f"{value:.1f}%" if value > 1 else f"{value*100:.1f}%"`. A genuine share of exactly/just under 1% expressed in percent-units (e.g. 0.8 meaning 0.8%) is mis-scaled to 80.0%. The fraction-vs-percent-unit ambiguity is unresolved here even though `_kpi_headline` avoids summing shares elsewhere. | Carry an explicit unit flag from the spec (`y_format`/share detection already exists) rather than guessing from the `>1` magnitude. |
| [NOT-PROD] | profile.py:352, renderer.py:59 | `LIMIT {int(limit)}` (5000) is applied to the result view BEFORE profiling/aggregation. A high-cardinality view truncated at 5000 yields wrong panel selection and wrong headline sums (silent partial aggregation), with no "capped" signal to the user on charts (only the data-viewer table notes capping). | Detect truncation (fetch limit+1) and annotate charts/headline as partial, or aggregate server-side in SQL. |
| [BUG] | export.py:324-326 | Data-viewer note `'+' if capped and not total_hint` only appends "+" when uncapped-but-hinted; the screener regex `(\d+) of ([\d,]+)\+? rows` and the `shown > total` consistency check can disagree when `total_hint` is a non-numeric string (note interpolates `total_hint` verbatim into `{total_hint or len(rows)}`). | Coerce/validate `total_hint` to an int before formatting; keep the note machine-parseable for the screener. |
| [BUG] | inference.py:51-54 | `_SELECT_ALIAS_RE` matches `AS alias` only when followed by `,` or end-of-string. A final SELECT item like `... AS total FROM` (alias immediately before `FROM`, no comma) is missed because `FROM` is neither `,` nor `$` within the captured SELECT slice — works only because the slice ends at `FROM`. Robust for the generator's style but brittle for hand-written/multi-line SQL with trailing comments. | Anchor on the SELECT-clause boundary already extracted (group(1) ends at FROM), or add `\s+FROM` to the lookahead. |
| [NOT-PROD] | renderer.py:772-948 `build_dash_app` | All KPI SQL is executed eagerly at app-build time (line 806) for headline + panel meta; only the heavy Plotly figures are lazy. For a 50-KPI workspace that is 50 DuckDB full re-executions at startup before the first paint, serial. | Defer per-KPI SQL exec to first drill (or batch/parallelize); cache result rows keyed by SQL mtime. |
| [MISSING] | profile.py:328-358; renderer.py:42-66 | Two near-identical DuckDB executors (`execute_result_view` vs `_execute_sql_view`) intentionally duplicated to stay cycle-free, but they diverge: profile caps at 5000 default, renderer at 5000 via `_SAMPLE_CAP` elsewhere uses 5000 hard-coded in `_build_kpi_figure`. No shared timeout; a pathological SQL hangs the export/screener with no DuckDB statement timeout. | Extract a shared `core/dashboard/_duckdb_exec.py` with a statement timeout (`SET statement_timeout`) and one cap constant. |
| [NOT-PROD] | renderer.py:1042, export.py:39-42 | Every chart embeds Plotly via `include_plotlyjs="cdn"`. Export/screenshot determinism and offline rendering depend on `cdn.plot.ly` being reachable; the screener even warns when Plotly assets aren't referenced. Static HTML "auto-opens at completion" but is non-functional offline. | Offer `include_plotlyjs=True` (inline) or a vendored local copy for the export path; keep CDN for the live app. |
| [NOT-PROD] | export.py:457-530 | Export re-executes each KPI's SQL up to THREE times (inline card height 360, page card height 560, then again for the data-view rows at lines 481/486). Screener then re-exports. 4x redundant execution per KPI per screen. | Execute once per KPI, reuse rows across both render heights and the data view. |
| [BUG] | chart_knowledge.py:166-168 | `__import__("re").compile(r"^(lat|latitude)$", 2)` passes the literal int `2` as flags (happens to equal `re.IGNORECASE`) via a fragile `__import__` at module scope instead of a normal `import re`. Obscure and breaks if `re` flag values ever change semantics; also bypasses linting. | Use a top-level `import re` and `re.IGNORECASE`. |
| [NOT-PROD] | screener.py:86-93 `_looks_blank` | Blank detection is a byte-size heuristic (`< width*height*0.005`). A real but minimal dashboard (one tile, mostly paper background) can fall under threshold and be flagged blank; a corrupt-but-large PNG passes. | Decode a few pixels / use the existing `dashboard_verify` helpers for a content check, not raw byte count. |
| [BUG] | renderer.py:443 | Lollipop `pad = max((hi-lo)*0.18, hi*0.005) or 1.0` — when all values are equal (`hi==lo`) and `hi==0`, pad falls to 1.0 (ok), but when `hi<0` (all-negative measure) `hi*0.005` is negative and `(hi-lo)*0.18`=0, so `max(0, neg)`=0 → `0 or 1.0`=1.0 (ok) — but the axis range `[lo-pad, hi+pad*2.2]` can invert for negative data. | Use `abs(hi)` for the floor and clamp the range to ascending order. |
| [NOT-PROD] | spec.py:283, export/screener | `build_kpi_spec(... repo_root=layout.project_root.parents[1])` assumes a fixed `workspaces/<ws>` depth (parents[1]). A workspace nested at a different depth resolves repo_root wrongly → SQL relative paths break silently (empty rows). | Resolve repo_root from a known anchor (git root / `WorkspaceLayout`), not positional `parents[1]`. |

## Cross-package coupling
- Inbound integration (all healthy): `core/onboarding/workspace/flow.py` calls `refresh_workspace_dashboard`,
  then `screen_dashboard`/`export_static_html` on completion; `core/onboarding/harness/workflow_guard_harness.py`
  gates on `vision_review_pending`; `tools/workspace_dashboard.py` is the CLI for refresh/export/screen/serve/record-vision-review.
- Depends on: `core.onboarding.kpi.registry_loader.load_kpi_definitions`, `core.onboarding.workspace.flow.compute_workflow_diff`,
  `core.storage.workspace_layout.WorkspaceLayout`, `core.governance.data_policy` (PII redaction patterns),
  `core.onboarding.kpi.pii_redaction` (redaction), and `tools.dashboard_verify._delta_e` (palette contrast).
- Engine: in-memory DuckDB only (`duckdb.connect(":memory:")`). No Polars/PySpark path despite repo Polars-first stance;
  non-SQL dialects are detected by dead `_detect_artifact_dialect` but never surfaced.
- [DUP] vs root `dashboard.py`: NONE functionally. Root `dashboard.py` is the medallion/operator artifact
  console on `core.dashboard_services`; disjoint code. The only overlap is the word "dashboard" and both
  produce HTML — they are two separate products. No shared rendering, spec, or chart logic.
- Internal cycle-avoidance: `profile.py` deliberately keeps its own DuckDB executor and lazy-imports
  `chart_knowledge` inside `decide_panels` to avoid importing `renderer`/`spec` (clean, but see duplicate-executor finding).

## Verdict
Architecturally strong and unusually well-reasoned: the evidence-driven panel selection (`profile.py` +
`chart_knowledge.py`) is genuinely principled, the spec preservation contract is correct, error handling
in the executors is defensive (empty-on-failure, never-raises), and the integration into workflow-complete +
guard gates is clean. Chart inference handles the empty/no-dimension/share/ordinal edge cases well.

It is NOT fully production-ready for a multi-user or large-workspace setting. The top risks are: (1) the
process-wide `os.chdir` executor is unsafe under concurrent Dash callbacks; (2) eager + 3-4x redundant SQL
re-execution makes export/screen and app startup O(KPIs x re-execs) with no statement timeout — a single
pathological view hangs the pipeline; (3) the silent 5000-row pre-aggregation cap can produce wrong headline
numbers and wrong chart selection with no on-chart signal; (4) headless-screenshot success is inferred from
file size, not the browser exit code, so partial renders pass; (5) CDN-only Plotly defeats offline/deterministic
export. Plus a cluster of dead code (`render_kpi_html` + the non-SQL-dialect quartet) and two real headline/percent
mis-scaling bugs. Fixable without redesign: extract a shared timeout-bounded executor, run SQL once per KPI,
annotate truncation, check the screenshot exit code, and delete the dead functions.

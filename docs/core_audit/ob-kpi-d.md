# ob-kpi-D (results/proof/generation/pii) — audit

## Purpose
This slice owns the back half of the KPI pipeline's "prove it" surface: composing the
result-view SQL from a KPI's structured fields (`result_view_builder.py`), assembling the
read-only all-KPI proof packet (`proof_packet.py`), self-grilling the generated SQL for
executability + intent alignment (`verify_kpi_output.py`), driving the BA-style
create/revise/challenge/score KPI-generation interview (`generation_workflow.py` +
`generation_cli.py` + `generation_quality.py`), planning dependency-aware parallel KPI
completion (`parallel_completion.py`), and redacting PHI/PCI for *display* surfaces
(`pii_redaction.py`). Everything is intended to be workspace-agnostic (no domain
vocabulary) and artifact-first (panels/proofs/sessions written under `interns/`).

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| result_view_builder.py | 1547 | Parse KPI metric/cuts into structured aggregations/dimensions/filters and compose `CREATE OR REPLACE VIEW <kpi>_results`; handles share-of-total/group, mismatched-grain %, single-attribution shares, banding, age/date arithmetic, grain-bucketing hard block. | `parse_kpi`, `build_result_view_sql`, `ParsedKPI`, `Aggregation`, `Dimension`, `_resolve_group_column`, `_band_expr`, `_detect_window_intent` |
| proof_packet.py | 703 | Read-only all-KPI recommendation/proof packet: per-KPI reliability gates, mapping rows, planned/generated SQL, execution summary, next action; writes `kpi_proof_packet/current.{md,json}`. | `KPIProofPacketBuilder`, `_reliability_gates`, `_kpi_status`, `_render_markdown` |
| verify_kpi_output.py | 690 | Self-grill gate: executes generated SQL (DuckDB/warehouse-aware), checks result view + columns + rows, static intent cross-checks (metric/cuts/filters/garbage-literal/gloss/grain), optional cross-engine Polars/PySpark parity. | `KPIOutputVerifier`, `VerifyRecord`, `VerifyResult`, `_check_intent`, `_execute`, `_cross_engine_check` |
| generation_workflow.py | 1341 | Deterministic KPI-generation interview state machine (route→context→orientation→format→result-format→final-preview→finalize); writes session/panels/draft registry/proofs/memory. | `KPIGenerationWorkflow`, `_advance`, `_build_draft_kpis`, `_production_proof`, `finalize` |
| generation_cli.py | 89 | argparse wrappers (`prepare/apply/finalize-kpi-generation`) routed through `run_workspace_command`. | `prepare_main`, `apply_main`, `finalize_main` |
| generation_quality.py | 435 | KPI scoring (implementation/business/understanding), missing-discussion detection, seed-KPI suggestion, result-format candidate tables. | `score_kpis`, `understanding_score`, `missing_discussion_points`, `result_format_candidates` |
| parallel_completion.py | 540 | Inter-KPI dependency graph from shared blockers/joins; union-find components; worker assignment; fan-out dispatch decision + plan artifact. | `build_completion_graph`, `plan_parallel_completion`, `dispatch_parallel_completion`, `decide_worker_count`, `_UnionFind` |
| pii_redaction.py | 212 | Display-only PHI/PCI column redaction + HIPAA Safe-Harbor age>89 bucketing; dependency-free, regex column-name matching. | `is_pii_column`, `redact_rows`, `redact_row_dict`, `redact_sample_values`, `bucket_age_value` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | flow.py:1346 + verify_kpi_output.py:416 vs pii_redaction.py | The canonical `kpi_results/current.md` packet (CLAUDE.md says forward verbatim) and the verifier's `sample_output_table` render rows via `render_query_result_table` with **no PII redaction**. `pii_redaction` is wired ONLY into `blocker_question_panel.py`. So a KPI whose result view projects a name/SSN/age/PAN column (e.g. a `top N` over a person column, or cuts that pass through DOB/name) emits raw PHI/PCI into the packet the agent forwards to the user. The module's own docstring scopes it to the panel, but the highest-traffic display surface is unprotected. | Apply `redact_rows`/`is_pii_column`+`bucket_age_value` (honoring `data_policy` patterns) inside the result-packet renderer in `flow._write_result_preview` and in `verify_kpi_output._execute` before building the markdown table. |
| [DUP] | pii_redaction.py:38-56 vs phi_gate.py:71-97 | PCI column patterns are hand-duplicated from `PCI_IDENTIFIER_PATTERNS` ("kept in sync by tests"). The copies have **drifted**: phi_gate has `^cid$`, `^magstripe([_ ]?data)?$`, `^exp[_ ]?(month|year)$`, `^(aba|swift|bic)...$` which pii_redaction lacks. The sync test (`GateRedactionSyncTests`) only checks ~7 *representative* columns, so the drift passes. A `cid`/`magstripe`/`exp_month`/`swift_code` column is gated upstream but NOT redacted on display. | Derive `DEFAULT_PII_COLUMN_PATTERNS`' PCI subset from `phi_gate.PCI_IDENTIFIER_PATTERNS` at import (acceptable: phi_gate is stdlib-only there) OR make the sync test assert full set-equality of flattened PCI patterns, not representatives. |
| [BUG] | result_view_builder.py:1450-1453 | The extra-select dedupe uses `any(alias in term for term in select_terms)` — a substring test on the whole `expr AS alias` term. A short alias (e.g. `age`) is a substring of `age_band`, `coverage`, `percentage_share`, etc., so a legitimately distinct extra-select expr can be silently dropped when another term merely contains its alias as a substring. | Compare against the projected alias token exactly (parse the trailing `AS <alias>` or track a `set[str]` of emitted aliases) instead of `alias in term`. |
| [NOT-PROD] | result_view_builder.py:1187-1209, 1228-1250 | Prose filter extraction scrapes categorical filter literals out of free-text KPI names via regexes (`for <Word> <col>`, quoted-literal scavenging, first-letter abbreviation e.g. LOB→LineOfBusiness, `.title()`-casing the value). This is heuristic value-fabrication: it can invent `WHERE col = 'Medicare'` from prose and mis-case real values. `verify_kpi_output._check_garbage_filters` only catches >=4-word fragments, so a plausible 1-2 word wrong filter passes. | Gate prose-derived equality filters behind a confirmation panel (or require the literal to be an observed value in the column profile) rather than emitting them silently. |
| [BUG] | result_view_builder.py:38 / _AGG_FN_PATTERN | The agg regex hardcodes a misspelling alternative `disitnct\s+` in the DISTINCT group, but `_parse_aggregation` only sets `distinct=bool(match.group(2))` — it never strips the misspelled token from the column, and more importantly the regex bakes in one specific typo. Fragile and surprising; a different typo silently produces `distinct=False`. | Drop the typo alternative; normalize KPI text upstream, or treat any `dist\w*` prefix as distinct. Low severity but indicates parser is tuned to specific bad inputs. |
| [INTEGRATION] | result_view_builder.py:1096 (`__time_order__`) | running_total/moving_average windows emit `ORDER BY __time_order__`, a placeholder column name that must be rewritten downstream. Nothing in this slice defines/replaces `__time_order__`; if a metric text triggers `running`/`moving avg` and the downstream generator does not substitute it, the view is non-executable. Verify the sql/polars/pyspark generators handle this token (out of slice). | Confirm `__time_order__` substitution exists in `sql_generator`/engine generators; otherwise these window kinds are dead/broken paths. |
| [INTEGRATION] | proof_packet.py:53-56, 489-499; generation_workflow.finalize | proof_packet only supports `mode="recommend"`; `apply-safe`/`execute` modes are advertised in `_next_commands` but raise `ValueError` if invoked, and `--refresh-from-source` is accepted but explicitly unsupported (`refresh_from_source_supported: False`). Advertised-but-unimplemented commands will confuse operators. | Mark future commands clearly as "not yet available" in the rendered packet, or drop them from `next_commands` until implemented. |
| [BUG] | proof_packet.py:169-170, 184 | KPI id reconciliation assumes registry order == `kpi_NNN`. `all_ids` mixes mapping `kpi_id`s with synthesized `kpi_{idx:03d}` from registry order; `_kpi_index` parses the numeric suffix to index back into `registry_kpis`. If the mapping uses non-positional ids (e.g. `kpi_revenue`) or registry order differs from mapping order, registry text is attached to the wrong card or dropped. | Join registry↔mapping on a stable shared key, not positional index; fall back only when ids are genuinely absent. |
| [NOT-PROD] | generation_workflow.py:1044, generation_quality.py / _production_proof | "Production proof" is almost entirely static `needs_review` placeholders (data-quality/edge-case/reconciliation/SLA/governance all hardcoded `needs_review`; only `owner` is data-driven). It is a checklist template, not evidence — calling the artifact a "production_readiness_proof" overstates what was verified. | Either compute these checks from real artifacts (tests defined, SLA in task config, governance decision recorded) or rename to "production_readiness_checklist (manual)". |
| [BUG] | verify_kpi_output.py:396, 559-572 | `_execute` and `_sql_rows_and_sum` `os.chdir(self.repo_root)` for in-memory execution and only restore cwd in the non-warehouse branch's `finally`. The chdir is process-global and not thread-safe; concurrent verification (and the parallel-completion fan-out this slice plans!) can race the working directory, making `read_csv_auto('workspaces/...')` resolve against the wrong cwd. | Pass absolute paths / use DuckDB `SET file_search_path` instead of `os.chdir`; at minimum guard with a lock and document non-reentrancy. |
| [BUG] | verify_kpi_output.py:367-392 | `_execute` swallows `LOAD delta;` failures with a bare `except Exception: pass`, and the warehouse branch never `os.chdir`s, so warehouse-relative reads differ from memory-mode. If delta isn't loaded but the SQL needs it, the later `conn.execute(sql)` error is reported as a generic "execution failed" with no hint it was the delta extension. | Log/record the delta-load failure into `record.warnings` instead of silently passing. |
| [DUP] | generation_workflow.py:892-947 (`_load_session_glosses`) vs verify_kpi_output.py:237-268 (`_workspace_glosses`) | Two near-identical dictionary-CSV gloss scanners (rglob `*dictionary*.csv`, field/description column discovery, `/interns/` skip). Divergence risk: workflow also reads `data_dictionary/index.json`, verifier does not. | Extract one shared gloss-loader helper consumed by both. |
| [NOT-PROD] | generation_workflow.py:857-858 | `_build_draft_kpis` hardcodes the draft path string `interns/generated/requirements/kpi_registry_draft.json` (and `_write_draft_if_present` independently uses `layout.requirements_dir`). Two sources of truth for the same path; the hardcoded `.as_posix()` bypasses `WorkspaceLayout`. | Use `self.layout.requirements_dir / "kpi_registry_draft.json"` consistently; drop the literal. |
| [BUG] | parallel_completion.py:295-310 | `component_payload` uses `component_{i+1}` where `i` indexes the **sorted** components list, but `_assign_components_to_workers` labels with `component_{comp_index+1}` where `comp_index` is the **original enumerate index** before re-sorting by size. The `component_ids` recorded on workers therefore may not line up with the `component_id`s in `components`/`component_payload`. | Use a single canonical component-id assignment (assign ids once on the sorted list, pass the mapping into the worker assigner). |

## Cross-package coupling
- `result_view_builder` is the parser core reused by `verify_kpi_output`, `kpi_intent`,
  `intent_coverage`, `sql_generator`, `polars_generator`, `pyspark_generator` (parser
  parity is locked by `tests/test_parser_parity.py`). It does NOT itself execute or write
  the `current.md` packet — that is `core/onboarding/workspace/flow.py`
  (`_write_result_preview`, `_share_sum_check`). The share-sum invariant referenced in
  builder comments lives in flow, keyed on a `(percent|share|pct)` column-name regex.
- `pii_redaction` is consumed only by `blocker_question_panel.py` (and `data_policy`/
  `phi_gate` are the governance siblings). The result packet and verifier sample tables
  bypass it — see [BUG] above.
- `proof_packet` depends on `WorkspaceArtifactValidator`, `READY_STATES`,
  `console_tables`. `generation_*` depend on `WorkspaceOnboarder`, `metric_derivation`,
  `text_parser`, `panel_contract.normalize_decision_panel`, `lexicon.vocabulary`,
  `delegation.routing_for`. `parallel_completion` depends on `features.blockers.normalize`
  and `delegation.routing_for`; `dispatch_parallel_completion` is invoked from
  `workspace/flow.py` and registered in `delegation`/`project_harness`. All CLIs are wired
  in `pyproject.toml` and `TOOLS.md` (no [DEAD] entrypoints found).

## Verdict
Functionally substantial and well-commented; the result-view parser is genuinely
workspace-agnostic and has clearly absorbed many real bugs (BUG-001/005/011/012/024,
share single-attribution). The grain-bucketing hard block and share-sum surfacing are
good governance. **Not production-clean** on two fronts that matter for a PHI/PCI control
plane: (1) the canonical `current.md` result packet and verifier sample tables render raw
rows with NO PII redaction even though a redaction module exists — the highest-traffic
display surface is the one that leaks; (2) the pii_redaction PCI list has drifted from
`phi_gate` and the sync test only spot-checks representatives. Secondary correctness
risks: substring-based extra-select dedupe, prose-scraped equality filters emitted without
confirmation, positional registry↔mapping joins in proof_packet, component-id mismatch in
parallel_completion, and process-global `os.chdir` during execution that is unsafe under
the very parallel fan-out this slice plans. The generation "production proof" is a static
checklist, not evidence. Recommend addressing the two PII findings before any real-data
deployment.

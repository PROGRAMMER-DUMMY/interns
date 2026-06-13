# ob-kpi-A (intent/feature-resolve/metric-derive/parse) — audit

## Purpose
This slice covers the INTENT/PARSE/FEATURE-RESOLVE/METRIC-DERIVE front of the KPI
onboarding pipeline: it parses KPI specs from spreadsheets/SQL/text, detects the
role of each column in a KPI sheet, builds the per-facet KPI intent contract,
independently verifies generated SQL realizes that intent (intent coverage),
resolves KPI feature tokens against profiled columns (the core blocker-producing
step), derives a metric/cuts proposal from a bare business question, and loads
the shared KPI registry. Outputs feed the blocker question panel, SQL/Polars/
PySpark generators, and the execution-harness coverage gates.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| feature_resolver.py | 1868 | Resolve KPI metric/cuts tokens against profiled columns, aliases, contextual dictionary matches, collisions, derived patterns; emit `kpi_feature_mapping.json` + open questions + panel | `KPIFeatureResolver`, `_resolve_kpi`, `_resolve_direct_collision`, `contextual_column_candidates`, `_contextual_score`, `_dedupe_features_by_physical_column`, `_column_identity_groups` |
| metric_derivation.py | 988 | Derive `{metric, cuts, filters, intent}` proposal from a business question + profile evidence; deterministic, low-confidence cases left for the gate | `derive_metric_and_cuts`, `_derive_metric`, `_count_entity_column`, `_best_measure_column`, `_scored_temporal_anchor`, `columns_from_profile_index` |
| intent_contract.py | 1510 | Per-facet KPI intent contract (metric/grain/filters/denominator/bucketing/temporal/shape/null-zero) + low-confidence routing into the blocker panel + answer store | `build_intent_contract`, `_facet_*`, `low_confidence_facets`, `intent_facet_panel_questions`, `record_intent_answer`, `_load_registry_with_features` |
| intent_coverage.py | 790 | Independent (non-generator) verifier that generated result-view SQL realizes declared grain/metric/filters/joins/denominator/temporal/output-shape | `evaluate_intent_coverage`, `grain_coverage_findings`, `join_correctness_findings`, `denominator_scope_findings`, `temporal_anchor_findings`, `output_shape_findings`, `CoverageFinding` |
| kpi_intent.py | 274 | Engine-neutral structured KPI intent so Polars/PySpark agree with SQL | `parse_intent`, `parse_metric`, `MetricIntent`/`DimIntent`/`FilterIntent`/`ShareIntent`/`KPIIntent`, `_parse_window_or_ratio` |
| kpi_definition.py | 530 | Apply a human-confirmed metric/cuts definition into registry + durable store; single + bulk (CSV/JSON) modes | `apply_kpi_definition`, `apply_kpi_definitions_from_file`, `apply_accepted_definitions_to_kpis`, `kpi_definition_key` |
| kpi_format_detector.py | 386 | Header+content role detection for KPI sheet columns with confidence/evidence; nesting detection | `detect_kpi_format`, `ColumnRole`, `KpiFormatDetection`, `_content_score`, `_header_score` |
| text_parser.py | 152 | Lexicon-driven metric/cuts inference + SQL-comment KPI extraction; cold-start cuts headers | `extract_kpis_from_sql`, `infer_metric_and_cuts`, `is_template_kpi_row`, `KPI_CUTS_HEADERS` |
| workbook_structure.py | 113 | Deterministic .xlsx reader → (columns, rows, merged_spans) for the detector | `read_workbook_grid`, `read_merged_spans`, `_first_sheet`, `WorkbookGrid` |
| registry_loader.py | 125 | Shared registry/feature-mapping loader + markdown KPI block renderer | `load_kpi_definitions`, `render_kpi_block` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | feature_resolver.py:1219-1224 | Hardcoded healthcare domain words in `_contextual_score`: `feature_norm == "procedure"` with `column_norm in {"description","code"}` adds score. This is exactly the domain-vocabulary leak the module elsewhere documents removing (text_parser docstring, "workspace-agnostic always" memory). On a non-healthcare workspace it is dead weight; on a healthcare-ish one it silently biases column choice. | Remove the procedure-specific branches; rely on the generic name/dictionary/value overlap scoring already present. |
| [BUG] | intent_contract.py:113-122,569,576 | `_FOR_GROUP_RE` and `_MISMATCH_GRAIN_SIGNATURE_RE` hardcode the literal `percentage` but `_SHARE_PATTERN_RE` also accepts `share`/`percent`. A metric written `share(...) / ... for region` is detected as share by `_SHARE_PATTERN_RE` but `for_group_token` is never captured, so the alternative shown to the user falls back to `within_<group>` instead of the real group. Inconsistent share-pattern vocabulary across the three regexes. | Make all three share regexes use the same `(?:percent(?:age)?|share)` alternation. |
| [BUG] | intent_contract.py:359-375 | `_facet_metric` ratio branch only matches when BOTH halves contain an agg fn. A common share metric `count(distinct X) / total` (bare denominator) and any ratio whose denominator is a plain column produce `confidence:"low"` with the raw text echoed as `value`, which then routes a `metric`-low (not `metric`-none) blocker. Acceptable but the metric value passed downstream is unparsed text. | Handle single-sided ratios or explicitly mark denominator-missing ratios; do not emit raw text as `value`. |
| [BUG] | metric_derivation.py:514-530 | `share` intent path computes `_best_measure_column(..., require_numeric=False)` then always returns `value=""`. The chosen `col` is discarded except as evidence — share numerator/denominator are never proposed even when the profile makes them obvious, so every share question becomes a manual blocker. Documented as intentional, but combined with intent_contract's share handling this means share KPIs have NO automated path. | Acceptable if intentional, but consider proposing numerator column as an alternative facet for the panel. |
| [NOT-PROD] | intent_contract.py:84-86 | Module docstring states "Standalone module + CLI. NOT wired into flow.py or the blocker panel." This is STALE: `intent_facet_panel_questions` IS imported and called in both `flow.py:496` and `blocker_question_panel.py:119`. Misleading to a maintainer reasoning about wiring/order. | Update the docstring to reflect that routing is wired. |
| [BUG] | intent_coverage.py:42 | `_AGG_RE` deliberately matches a misspelling `disitnct` as the distinct group but `declared_metric_aggs` then sets `distinct=bool(group2)` — a metric literally containing `disitnct` would be parsed but its column inner text is fine. Minor: the typo-tolerance is asymmetric (only this one misspelling, not e.g. `disctinct`) and undocumented why. | Drop the typo alternative or document the specific upstream source that emits it. |
| [BUG] | kpi_format_detector.py:74 | `_COMPARISON` filter signature includes bare word `\bstatus\b`, a content word that frequently appears in legitimate metric/description prose ("status of claims"). A `description` column full of sentences containing "status" can score as `filters`. The other tokens (`only/exclud/where/in(`) are structural; `status` is the odd content word out. | Remove `\bstatus\b` from the comparison signature (keep operators + structural keywords). |
| [BUG] | metric_derivation.py:574 | `_resolve_time_grain` returns `"month"` as the default grain for any "over time"/"trend" phrase. A silent month default can mis-bucket a question whose natural grain is day or year; it is surfaced as a low-confidence cut only when no date column exists, otherwise it is emitted with `t_conf=0.8`. | Lower confidence for the inferred default grain so the gate confirms, or attach it as an alternative. |
| [INTEGRATION] | intent_contract.py:1127-1150 | `record_intent_answer` mirrors only `denominator_scope`, `grain_bucketing`, `base_source` into `pipeline_decisions.json`. Other routed facets (`metric`, `grain`, `temporal_anchor`, `output_shape`, `filters`) are written to `kpi_intent_answers.json` only — the SQL generator/result-view builder must actively read that store or the answer is cosmetic. Confirm consumer reads `kpi_intent_answers.json`. | Verify generator consumes intent answers for metric/grain/temporal/shape; otherwise those panel answers do nothing. |
| [BUG] | feature_resolver.py:1185-1193 | `_contextual_score` PK-alignment branch can return early with score 24 when `dataset_norm.rstrip("s") == feature_norm.rstrip("s")` and the column is `id`/`code`. Combined with the `auto_proven` threshold (>=14, gap>=4) at line 1113, a feature whose name merely echoes a table name auto-proves to that table's id column with no other evidence. For an ambiguous entity this is a silent false-positive direct mapping. | Require corroborating dictionary/value evidence before auto-proving a table-name→id alignment, or cap such matches below the auto-proven threshold. |
| [BUG] | intent_coverage.py:752-768 | `prose_filter_findings` age-threshold check accepts the bare integer `value in sql_lower` anywhere in the SQL. A threshold like `5` (top 5, limit 5, a column suffix `q5`) would spuriously satisfy "above 5 years", masking a genuinely-missing age filter. Substring (not word-boundary) match on a 1-2 digit number is fragile. | Use word-boundary / numeric-context matching for the age threshold, mirroring `_token_present`. |
| [DUP] | intent_contract.py:186-228 vs intent_coverage.py:133-177 | `_feature_column_lookup` and `_resolve_cut_column` are near-identical copies in both modules (the coverage copy adds a `derived_formula` special-case). Two independent copies of cut-resolution logic risk silent divergence — ironic given the module's stated independence rationale is about the *generator's* parser, not each other. | Extract the shared cut-resolution helper into one module (keeping the derived_formula branch as a parameter) to prevent drift. |
| [BUG] | workbook_structure.py:100 | `columns = matrix[header_rows - 1]` with `header_rows>=1`; if `header_rows` exceeds the matrix length this raises `IndexError` (only `matrix` emptiness is guarded). A multi-header-row workbook smaller than `header_rows` crashes the reader instead of degrading. | Bounds-check `header_rows` against `len(matrix)` and fall back to row 0 / empty grid. |
| [MISSING] | metric_derivation.py:858 / feature_resolver.py | `derive_metric_and_cuts` is the onboarding entry that fills empty metric/cuts, but it never consults `kpi_definition` accepted-definition store; ordering depends on the caller (onboarding.py). If derivation runs after definition apply on a re-onboard the human value can be overwritten by a guess unless `metric_provenance` gates it. Confirm caller applies accepted definitions AFTER derivation. | Verify onboarding applies `apply_accepted_definitions_to_kpis` after `derive_metric_and_cuts`; add a guard if not. |
| [BUG] | feature_resolver.py:480 | `_derivation_pattern_options` swallows ALL exceptions silently (`except Exception: return []`). A real bug in `detect_derivation_patterns` or a malformed `profile_index.json` is indistinguishable from "no patterns". Marked advisory, but it hides genuine failures. | Narrow the except (FileNotFoundError/JSONDecodeError/ImportError) and let unexpected exceptions surface or log. |
| [DEAD] | feature_resolver.py:1726 / intent_contract.py:1048 / kpi_definition.py:464 | `_rel` is reimplemented identically in at least three files in this slice (plus elsewhere). Not dead per se but a repeated private helper that should live in a shared util. | Consolidate `_rel` into `core/storage/workspace_layout` or a path util. |

## Cross-package coupling
- `feature_resolver` is the hub: imports from `features.blockers`, `features.derived_evidence`,
  `features.expression`, `features.derivation_search`, `features.derivation_patterns`,
  `relationships.schema_alias_matching`, `documents.dictionary_reconciliation`,
  `memory.workspace_definitions`, `memory.user_decisions`, `lexicon`, and calls
  `BlockerQuestionPanelBuilder`. It writes `kpi_feature_mapping.json` (the contract the rest
  of the pipeline keys on) and emits the panel directly — the central blocker-producing step.
- `intent_contract.intent_facet_panel_questions` is wired into `flow.py:496` and
  `blocker_question_panel.py:119` (despite the stale "NOT wired" docstring). Answers persist to
  `kpi_intent_answers.json`; `denominator_scope`/`grain_bucketing`/`base_source` mirror to
  `pipeline_decisions.json` via `PipelineDecisionRecorder`.
- `intent_coverage` is consumed by `execution_harness.py` and `harness/intent_coverage_harness.py`
  and references `result_view_builder` only for the proven-relationship state constants; its
  checks deliberately re-derive intent independently of the generator's parser.
- `metric_derivation.derive_metric_and_cuts` + `columns_from_profile_index` are called by
  `onboarding.py` and `generation_workflow.py`; `feature_resolver` reuses
  `columns_from_profile_index`.
- `kpi_format_detector` + `workbook_structure` feed `kpi_confirmation_panel.py` and `onboarding.py`
  (the sheet-ingestion path). `kpi_intent.parse_intent` is the shared structured layer for the
  Polars/PySpark generators (imports the SQL builder's regexes/helpers to keep engines aligned).
- `registry_loader.load_kpi_definitions` is the shared registry reader used by flow, dashboard,
  and generation_workflow; `render_kpi_block` is shared by the panel and results renderers.

## Verdict
Production-viable and unusually disciplined: feature resolution is conservative (collisions block
with per-candidate evidence, expression-shaped names are kept out of auto-prove, derived/guessed
metrics route to confirmation), metric derivation is genuinely evidence-only with honest ambiguity
gating, and the intent-coverage verifier is correctly independent of the generator's parser. The
main defects are (1) a residual hardcoded healthcare `procedure`/`description`/`code` branch in
`_contextual_score` that violates the workspace-agnostic rule, (2) inconsistent share-pattern
vocabulary across the three intent_contract regexes that can lose the group token, (3) a few
substring/word-boundary fragilities (age-threshold integer match, `status` filter signature,
table-name→id auto-prove), and (4) duplicated cut-resolution/`_rel` helpers that risk drift. None
block the pipeline, but the procedure leak and the share-vocabulary mismatch should be fixed before
calling the slice fully workspace-agnostic. No silent data-loss path found; broad `except Exception`
in `_derivation_pattern_options` is the one over-broad swallow worth narrowing.

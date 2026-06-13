# ob-features — audit

## Purpose
`core/onboarding/features/` synthesizes **derived-feature candidate options** for blocked KPI
features and renders them for human review. It does this two ways:

1. **Library-pattern search** (`derivation_search.py` + `derivation_patterns.json` +
   `derived_evidence.py`): matches an unresolved feature *name* against a curated JSON pattern
   library (age_years, days_in_ar, net_paid_amount, ...), binds profiled columns to required
   inputs, fills per-engine formula templates, and emits a fully evidence-shaped option.
2. **Question-driven detectors** (`derivation_patterns.py`): deterministic regex detectors that
   read the KPI *question text* + profiled columns to synthesize three engine-inexpressible
   patterns — duration bucket (start/stop → threshold), temporal self-join recurrence, and
   mixed-UOM normalization — emitting the same option contract.

`blockers.py` clusters/prioritizes blocked features and classifies risk. `expression.py` tokenizes
metric/cut expressions into identifiers (the unresolved tokens that become features).
`derived_markdown.py` validates the strict option contract and renders the per-KPI Markdown+JSON
review artifacts plus a stale-file sweep. All output is `needs_user_confirmation: True`,
`evidence_state: candidate_*_not_ground_truth` — nothing is treated as proof.

**Expression-eval safety: there is NO `eval`/`exec`/`compile` anywhere in this package.** All
"expression" handling is regex tokenization (`expression.py`) and string-template substitution
(`derived_evidence.fill_formula_template` / `substitute_formula_values`). Formulas are emitted as
SQL *text* for downstream engines, never executed in-process. This is the safe design.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 2 | Package docstring only | — |
| `blockers.py` | 147 | Cluster/prioritize blocked features, risk classes, join-key inference | `prioritize_blockers`, `infer_join_candidates`, `risk_class`, `risk_score`, `question_for_feature`, `normalize` |
| `derivation_patterns.py` | 524 | Question-driven regex detectors → JSON-backed options (duration/recurrence/UOM) | `detect_derivation_patterns`, `detect_duration_bucket`, `detect_recurrence_within_window`, `detect_uom_normalization`, `_option`, `_input_col`, `_temporal_pair`, `_observed_unit_codes` |
| `derivation_search.py` | 129 | Score library patterns vs feature name + available columns | `DerivationPatternSearcher`, `DerivationSearchInput`, `DerivationCandidate`, `_candidate_bindings`, `_safe_partial_feature_match` |
| `derived_evidence.py` | 326 | Turn matched library patterns into full option contract w/ evidence | `derived_feature_options`, `derived_input_column`, `semantic_meaning_sources`, `derivation_reasoning`, `derived_evidence_sources`, `value_profile`, `example_value`, `pattern_example_inputs/output` |
| `derived_markdown.py` | 461 | Strict option-contract validation + Markdown/JSON review render + stale sweep + CLI | `DerivedFeatureMarkdownConverter`, `_validate_option`, `_render_review`, `_render_option`, `_mark_stale_review`, `main` |
| `expression.py` | 150 | Tokenize metric/cut expressions into identifiers + function contexts | `extract_expression`, `strip_literals`, `_function_names`, `_function_contexts`, `ExtractedExpression` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [NOT-PROD] | derivation_patterns.py:509-514 | `detect_derivation_patterns` wraps every detector in a bare `except Exception` that silently swallows ALL errors and returns `None`. A genuine bug in a detector (e.g. malformed profile) is indistinguishable from "no pattern applies"; nothing is logged. Resolver caller (feature_resolver.py:480) ALSO bare-excepts. Double swallow = silent dead detection. | Catch narrowly or at minimum log the exception (debug logger) so detector regressions are observable. |
| [NOT-PROD] | derivation_patterns.py:284, 319, 494; derived_evidence.py:65 | Confidence is hardcoded by pattern (duration=`medium`, recurrence=`low`, uom=`medium`) or a 2-state heuristic (`medium` if inputs bound else `low`). It does NOT reflect evidence strength: a `medium` duration option can be emitted off two columns matched only by generic `_START_HINTS`/`_STOP_HINTS` (e.g. "in"/"out"/"to") with no name overlap to the question. Confidence is not calibrated to binding quality. | Derive confidence from binding evidence (name-match vs positional fallback, sample-value date-shape ratio, prose overlap), not a constant. |
| [BUG] | derivation_patterns.py:248-253 | `_temporal_pair` fallback returns `temporal[0], temporal[1]` (first two temporal cols in spec order) when no start/stop name hint matches. This can pair two unrelated dates (e.g. `created_at`, `updated_at`) and emit an `over_N_unit` duration option with `medium` confidence — a semantic false positive the CLAUDE.md rule explicitly forbids offering. | Require at least one of start/stop to match a hint, or drop confidence to `low` and flag positional pairing in `remaining_risk`. |
| [BUG] | derivation_patterns.py:64 | `_START_HINTS` includes `"in"` and `_STOP_HINTS` includes `"to"`/`"out"`. `_name_has` does a substring match, so any column whose name *contains* "in" (e.g. `claim`, `insured`, `origin`, `destination`) is classified a start; "to"/"out" likewise (`total`, `account`, `routing`). High false-positive rate for start/stop selection. | Use token/affix matching (e.g. word boundary or `_`-delimited segments) instead of raw substring; drop the 2-char hints. |
| [BUG] | derived_evidence.py:265 | `substitute_formula_values` does `re.sub(rf"\b{re.escape(column)}\b", ...)` on the formula text for the *example render only*. If a bound column name is a substring/regex-edge of another token or appears inside a SQL function name, substitution is fragile; also `\b` won't fire around quoted identifiers (`"col"`). Cosmetic (example only) but can render a misleading substituted formula. | Substitute on the templated placeholder set or quote-aware tokens, not free-text regex over emitted SQL. |
| [NOT-PROD] | derivation_search.py:99-101 | `_load_patterns` calls `read_text` + `json.loads` with no error handling. A missing/corrupt `derivation_patterns.json` throws at `DerivationPatternSearcher()` construction time (feature_resolver.py:130, in `__init__`), which would hard-fail resolver setup rather than degrading to "no library patterns". | Wrap load in try/except → empty list with a logged warning; the package elsewhere treats pattern detection as advisory. |
| [NOT-PROD] | derivation_patterns.py:1-13 docstring vs code | Module docstring claims it owns only duration + recurrence ("two patterns"); the file actually ships a third (UOM normalization) and the header is stale. Minor but misleads auditors about scope. | Update the module docstring to list all three detectors. |
| [DUP] | blockers.py:145 / derivation_patterns.py:323 / derivation_search.py:127 / derived_markdown.py:391(`_slug`) | `normalize`/`_norm` (`re.sub(r"[^a-z0-9]+","",lower)`) is reimplemented 3x identically; `_slug` is a near-twin. `derived_evidence.py` imports `normalize` from blockers but the other two redefine it locally. | Consolidate to one `normalize` (blockers already exports it) and import it. |
| [DUP] | derivation_patterns.py:139-166 (`_input_col`) vs derived_evidence.py:76-96 (`derived_input_column`) | Two near-identical builders of the input-column evidence dict with the same required fields, diverging slightly (`_input_col` adds `evidence_state: profile_inferred`, omits nothing; `derived_input_column` omits `evidence_state`). Drift risk against `REQUIRED_INPUT_COLUMN_FIELDS`. | Extract one shared builder so both detector paths and library path stay schema-aligned. |
| [MISSING] | derived_evidence.py:91 / derivation_patterns.py:160 | `observed_values` is populated directly from `sample_values` with no cap/redaction. For PHI/PII columns the raw sample values flow into the review Markdown (`_render_values` shows up to 8). CLAUDE.md has a PHI/secret gate elsewhere but this package does not honor any data policy. | Route observed values through the workspace data-policy/redaction layer before embedding in review artifacts. |
| [INTEGRATION] | derivation_patterns.py:303 | Recurrence formula emits a placeholder `FROM <self>` token that is not valid SQL and relies on a downstream rewriter to fill the self-join target. Nothing in this package documents/guarantees that contract; if the consumer renders the formula verbatim it is broken SQL. | Document the `<self>` placeholder contract and confirm the SQL generator substitutes it (out-of-package check). |
| [NOT-PROD] | derived_markdown.py:106-118 | `markdown_path.write_text` / `json_path.write_text` run with no try/except; one unwritable path aborts the whole review run mid-loop leaving partial output and no stale-sweep. | Wrap per-KPI writes; collect failures into the result rather than aborting. |
| [NOT-PROD] | expression.py:131 | `strip_literals` strips `= <ident>` (RHS of equality) to drop filter values, but only the *immediate* token; `col IN (a,b,c)` list members and `col = func(x)` survive as identifiers and become spurious "features". Comment claims filter values come via `workspace_filter_terms`, but unproven workspaces (None) leak them. | Acknowledge in resolver that pre-vocabulary runs over-extract; or strip IN-lists. Low risk (over-extraction blocks, never fabricates). |
| [DEAD] | derived_evidence.py:292-306 | `column_profile_summary` is defined but not referenced anywhere in the package or repo (grep shows no caller). | Remove or wire it in. |
| [DEAD] | derivation_search.py:34 | `DerivationCandidate.evidence_gap` default is set and serialized but never read by any consumer; harmless metadata. | Confirm intent or drop. |

## Cross-package coupling
- **Consumed by `core/onboarding/kpi/feature_resolver.py`** (the primary integration, CONFIRMED live):
  imports `extract_expression`, `DerivationPatternSearcher`/`DerivationSearchInput`,
  `derived_feature_options`, and `detect_derivation_patterns`. Per-feature it runs the library
  searcher (l.380), builds options via `derived_feature_options` (l.391), and — when the library
  yields nothing — falls back to question-driven `detect_derivation_patterns` filtered to options
  whose `derived_column_name` matches the unresolved token (l.398-411). Result is attached as
  `derived_feature_options` on the feature.
- **`core/onboarding/kpi/blocker_question_panel.py`** reads `feature["derived_feature_options"]`
  (l.1103, 1913, 2632) and renders each as a selectable panel option labelled
  `Accept candidate formula from <source_pattern_id>` (l.1534). This is the panel that CLAUDE.md
  mandates as the only place derived-feature asks may originate — coupling is correct.
- **`core/onboarding/kpi/blocker_workflow.py`** invokes `DerivedFeatureMarkdownConverter` to emit
  the review artifacts; `derived_markdown.main` is also a (soft-deprecated) CLI that redirects to
  `prepare-kpi-blocker-panel`.
- **`blockers.normalize` / `risk_class` / `derived_evidence` symbols** are reused by
  `relationships/schema_alias_matching.py`, `memory/workspace_definitions.py`,
  `kpi/parallel_completion.py`, and the medallion layer — `blockers.py` is a broadly-depended-on
  utility module.
- **Semantic-mismatch filtering**: the question-driven detectors gate on prose
  (`_DURATION_RE`/`_RECURRENCE_RE`/`_WINDOW_RE` + the UOM prose gate at
  derivation_patterns.py:426-434) AND profiled-column shape, which is the right layer. The
  *library* path (derivation_search) gates only on feature-NAME term match (≥5-char partial,
  derivation_search.py:117-124) — looser, but the resolver only attaches it to an exact unresolved
  token, and everything stays `needs_user_confirmation: True`. Net: no auto-accept path exists.

## Verdict
**Conditionally production-ready as an advisory/candidate generator, not yet hardened.** The single
most important property holds: **no `eval`/`exec` — formulas are inert SQL text and example
substitution is cosmetic string work**, so there is no expression-eval injection surface in this
package. The evidence/field contract is strong and centrally validated (`_validate_option` enforces
REQUIRED_* field sets, everything is flagged `needs_user_confirmation` + `candidate_not_ground_truth`),
and integration into the resolver → panel is real and matches the CLAUDE.md governance rule.

The real risks are **false-positive candidates** and **un-calibrated confidence**, which directly
contradict the CLAUDE.md "do not offer semantically mismatched derived-feature candidates" rule:
the `_temporal_pair` positional fallback ([BUG] :248) and the substring `_START/_STOP_HINTS`
("in"/"to"/"out", [BUG] :64) can pair unrelated date columns and still emit a `medium`-confidence
duration option. Confidence is a per-pattern constant rather than evidence-derived. Secondary gaps:
unguarded pattern-library load can hard-fail resolver construction, raw `observed_values` bypass any
PHI/redaction policy, and several bare `except Exception` blocks silently hide detector bugs.
Recommend: fix the start/stop matching + positional-pair gate, calibrate confidence to binding
quality, guard the JSON load and per-KPI writes, route observed values through the data policy, and
dedupe `normalize`/input-column builders before relying on this for unattended panel generation.

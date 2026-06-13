# ob-kpi-B (engines/parity/execution) — audit

## Purpose

This slice is the triple-engine KPI codegen + parity contract. For each fully
resolved KPI it generates DuckDB/Databricks SQL (`sql_generator.py`), a Polars
lazy script (`polars_generator.py`), and a PySpark script (`pyspark_generator.py`)
off a single shared source plan (`plan_required_sources`) and a single
engine-neutral intent (`kpi_intent.parse_intent`). `engine_recommender.py` advises
which engine to run per KPI; `generate_kpi_engines.py` is the CLI that fans out to
the chosen engines and records skips. `execution_harness.py` runs the generated SQL
in an in-memory DuckDB, proves the `*_results` view exists, and runs semantic gates.
`local_warehouse.py` materializes Bronze Delta + fact/dim views into a persistent
`warehouse.duckdb`. `engine_parity.py` is the correctness keystone: it regenerates
and executes the Polars variant and compares its Gold rows against the canonical
DuckDB rows (row-for-row under a cap, aggregate-signature above it). The
"polars vs sql match" badge surfaced in results/dashboard comes from here.

## Files

| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| sql_generator.py | 1050 | Authoritative DuckDB/Databricks SQL from ready feature mappings; staging views, Delta/warehouse rewrite, join chain, derived-formula inlining, masking | `DuckDBKPISQLGenerator`, `plan_required_sources`, `choose_feature_ref`, `_choose_base_source`, `_relationship_join_condition`, `quote_ident` |
| polars_generator.py | 644 | Emits Polars lazy script from shared plan+intent | `PolarsKPIGenerator`, `_emit_script`, `_share_lines`, `_derive_dim_lines`, `_metric_agg_expr` |
| pyspark_generator.py | 626 | Emits PySpark DataFrame script (JDK preflight, Bronze ingest, broadcast joins) | `PySparkKPIGenerator`, `_emit_script`, `_share_lines`, `_derive_dim_lines` |
| generate_kpi_engines.py | 323 | CLI fan-out to engines per `--engine` mode; records skips, writes report | `KPIMultiEngineGenerator`, `expand_engine_mode`, `EngineGenerationOutcome` |
| engine_parity.py | 406 | Polars-vs-SQL parity execution + comparison | `evaluate_parity`, `run_polars_parity`, `_aggregate_signature`, `_normalize_cell`, `_normalize_rows` |
| engine_recommender.py | 231 | Size/complexity-aware engine advice; SQL is always default | `KPIEngineRecommender`, `recommend`, `ComplexitySignals` |
| execution_harness.py | 608 | Runs generated SQL, proves result view, semantic gates | `KPIExecutionHarness`, `_execute_one`, `_semantic_errors`, `sql_defines_result_view` |
| local_warehouse.py | 325 | DuckDB warehouse mirroring UC: Bronze Delta + fact/dim views | `LocalWarehouse`, `warehouse_table_name` |

## Findings

| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | sql_generator.py:118-137 | Sensitive-column masking is applied ONLY in SQL (`hash()`/`sha2()`). Polars and PySpark generators have no masking at all, so their Gold/Silver Delta output writes the raw unmasked sensitive values. This is a real PII/PHI leak in the non-SQL engines, and it also guarantees a parity MISMATCH on every KPI whose metric/grain column is sensitive (SQL hashes, Polars does not). | Move masking into the shared layer (intent or `plan_required_sources`) so all three engines mask identically; or drop sensitive features from non-SQL engines and record a skip. |
| [BUG] | sql_generator.py:588-620, 767-776 | Derived-formula body (`_derived_formula` → `evidence[].detail`/`source_columns[].detail`) is inlined into emitted SQL verbatim via regex substitution. The formula string is never validated/escaped — a workspace-authored derivation rule containing `);DROP …`/subqueries is emitted as-is into executable SQL. Identifiers are quoted but the formula expression is not. SQL-injection via KPI feature definitions. | Parse/whitelist the formula to an allowed expression grammar (functions, operators, known columns) before inlining; reject unknown tokens instead of substituting blindly. |
| [BUG] | polars_generator.py:557-568; pyspark_generator.py:539-550 | Filter values flow into generated PYTHON source. For `__age__` filters the raw `filt.op` and `filt.value` are interpolated unmapped (`f'(pl.col("{age_alias}") {filt.op} {filt.value})'`); for non-literal filters `value = filt.value` is interpolated bare. A crafted intent value/op injects arbitrary code into the generated script, which `run_polars_parity` then executes via `subprocess`. | Map the op through `_PL_OPS`/`_SPARK_OPS` in the age branch too; render non-literal values as validated column refs (`pl.col(...)`) not raw text; reject values that are not literals/known columns. |
| [BUG] | engine_parity.py + flow.py:1382-1434; dashboard renderer.py:1075-1081 | PARITY FALSE-COVERAGE: only Polars is ever executed and compared. PySpark is never run in the live results path (the module docstring defers it to an env-gated CI test). The dashboard "[ok] parity" badge and the result packet's "engine parity (polars vs sql)" line therefore certify nothing about PySpark, yet `recommend()` can route a KPI to `pyspark`/`hybrid` and the recommended engine ships unverified. | Either run a PySpark parity arm when PySpark is the recommended/selected engine, or make the badge explicit that only Polars is cross-checked and never present a pyspark-recommended KPI as parity-verified. |
| [BUG] | engine_parity.py:97-117, 131-183 | Numeric tolerance is asymmetric to data magnitude. `_normalize_cell` rounds every int and float to 2 dp and bools stay bool. A genuine divergence smaller than 0.005 (e.g. SQL `ROUND(x,2)` vs Polars `.round(2)` differing only by banker's-vs-half-up rounding at the 2nd dp) is silently absorbed → can mask a real value drift as `match`. Conversely large integer IDs coerced to float lose precision above 2^53. | Compare with explicit per-type tolerance; keep ints as ints (do not coerce to float) so identity columns compare exactly; document the half-up vs banker's rounding contract and align all three engines. |
| [BUG] | polars_generator.py:419-440 vs pyspark_generator.py:449-463 | The non-distinct `mismatched_grain_percentage` share path diverges between Polars and PySpark. Polars computes numerator over the full grain-cell list and denominator over the partition (or grand total) and selects `[cells…, percentage_share]`; PySpark partitions only by `share.partition` for the numerator and emits extra `share_per_group`/`share_total` columns with a different column set. Different shapes/values; only undetected because PySpark is not parity-checked. | Render both engines from one shared share spec (cells, partition, total scope); align emitted columns; add a PySpark parity arm so this cannot regress silently. |
| [NOT-PROD] | local_warehouse.py:176-197 | `query()` decides read-only vs read-write by regex-scanning for `CREATE|DROP|INSERT|…`. A comment or string literal containing those words flips the mode; multi-statement SQL is naively split on `;` (breaks on `;` inside string literals). Fine for a dev smoke harness, not safe for arbitrary SQL. | Use DuckDB's own statement handling / parameterize; do not infer mutation from a keyword regex. |
| [BUG] | sql_generator.py:126 | Masking trigger compares `column.split('.')[-1].strip('"').lower()` — but `column` here is the already-qualified expr (e.g. `s0."Amount"`). For derived-formula features `column` is a full expression, so the split heuristic mis-detects (either masks a non-sensitive col or misses a sensitive one inside a formula). | Resolve sensitivity from the feature's source column metadata, not by string-splitting the rendered expression. |
| [NOT-PROD] | engine_parity.py:354-374; flow.py:1409 | The Polars parity script is run via `subprocess` with `timeout=180` and only the last 3 stderr lines captured on failure. Because the generated script is regenerated and executed during the RESULTS stage, codegen bugs surface only as a truncated parity "error" string, not a hard stop. Combined with the injection finding above, executing generated Python from intent strings is a real risk surface. | Capture full stderr to evidence; sandbox/validate the generated script before exec; treat persistent parity `error` as a gate, not a soft annotation. |
| [BUG] | polars_generator.py:74-83; pyspark_generator.py:81-88 | Polars/PySpark hard-raise on ANY derived-formula feature, so those KPIs are silently `skipped` in parity. The SQL result then ships with NO cross-engine verification while still showing a parity line of `skipped`. A reviewer skimming the badge may read "parity ran" as "parity passed". | Make the result packet/dashboard distinguish "no cross-engine check available" from "verified"; do not let derived-formula KPIs present as parity-covered. |
| [INTEGRATION] | flow.py:1304-1399 | Parity caching keys only on `sql_sha256`. A cached `match` short-circuits re-run, but the cache cannot see changes to the Polars/PySpark GENERATORS or to profiles/relationships. The comment claims mismatch/skip self-heal, but a cached MATCH will persist even if a generator change would now diverge, until the SQL text itself changes. | Include a generator/version fingerprint (or profiles+relationships hash) in the cache key so engine-side changes invalidate prior matches. |
| [MISSING] | engine_parity.py:199-308 | Aggregate mode never compares actual per-column min/max identity for non-numeric columns beyond a distinct COUNT. Two different sets of grouping values with the same cardinality and same row count pass `distinct_counts` + `content_checksum` only because the checksum covers full rows — but if numeric and grouping columns are independently permuted across rows the checksum still differs, so this is mostly safe; however a column that is all-null on one side and all-same-value on the other is caught only if null_counts differ. Edge coverage is thin. | Add a per-column value-set comparison for grouping columns in aggregate mode, or document the residual blind spot. |
| [DUP] | sql_generator.py:417-471 vs 473-550 vs plan_required_sources:858-906 | `_required_source_columns`, `_kpi_source_from`, and `plan_required_sources` each independently recompute base source + required_refs + derived-formula refs. Three copies of the same plan logic risk drift (the whole point of `plan_required_sources` was to be the single source of truth). | Have `_required_source_columns`/`_kpi_source_from` consume `plan_required_sources` output instead of recomputing. |
| [BUG] | pyspark_generator.py:286 | Bronze ingest uses `inferSchema=True` (acknowledged as bootstrap-only in the comment) but there is no production StructType path; types inferred by Spark CSV reader can differ from DuckDB `read_csv_auto`/pyarrow CSV used by SQL and Polars Bronze ingest, producing genuine cross-engine type divergence (e.g. leading-zero codes as int). | Share one typed schema (from the profile) across all three Bronze ingests so column types match before any KPI math. |

## Cross-package coupling

- `core/onboarding/workspace/flow.py` (line 28-29, 659, 1299-1460) is the primary
  consumer: `KPIExecutionHarness().run()` proves result views, and
  `run_polars_parity` is invoked per KPI during `_write_result_preview`, with the
  verdict cached under `interns/state/evidence/engine_parity/current.json` and
  appended to the result packet. This is the path that produces the
  `runs/<date>/results.md` packet the CLAUDE.md forwarding rule references.
- `core/dashboard/renderer.py` (1054-1081) reads `engine_parity/current.json` to
  render the per-KPI "[ok] parity" badge.
- `core/dev/green_gate.py`, `core/onboarding/harness/project_harness.py`, and
  `.github/workflows/ci.yml` register/exercise the parity module; `tests/
  test_engine_parity.py` and `tests/test_engine_parity_aggregate.py` cover
  `evaluate_parity` (the env-gated PySpark CI parity test referenced in the
  docstring lives outside this slice).
- All three generators delegate to `sql_generator.plan_required_sources` /
  `choose_feature_ref` / `_choose_base_source` and to
  `relationships.base_source_selector` + `relationships.contracts`, so source/join
  selection is genuinely shared. `result_view_builder` (sibling-owned) builds the
  SQL result view and the `_band_width_from_decision` shared by all engines.
- No [DEAD] code found: every module has a live CLI `main()` and a flow/CI/test
  consumer.

## Verdict

Architecturally sound and unusually disciplined about cross-engine parity: a
single source plan and a single intent feed all three engines, banding/UTC/age
anchoring are deliberately aligned, and the parity comparator (`evaluate_parity`)
is a genuine row-and-aggregate comparison, not a rubber stamp — it will catch
column-set, row-count, null, numeric-sum, distinct, and checksum drift. It does
NOT false-pass in the trivial sense.

However it is NOT production-ready as the correctness guarantee it advertises,
for four reasons: (1) PII/PHI masking exists only in SQL, leaking raw sensitive
values through the Polars/PySpark Gold tables and forcing spurious parity
mismatches; (2) derived-formula bodies and filter values are inlined into
generated SQL/Python without sanitization — injection through workspace-authored
KPI definitions, then executed via subprocess in the results stage; (3) the
"engine parity" badge certifies ONLY Polars — PySpark, which the recommender can
select, is never executed in the live path, so a pyspark-recommended KPI ships
"verified" with zero cross-engine evidence (the clearest false-pass-of-trust
risk); (4) the 2-dp blanket rounding in `_normalize_cell` can absorb genuine
sub-0.005 value drift and coerces large integer IDs through float. Recommend
gating: shared masking, formula/value allow-listing, a PySpark parity arm (or an
honest badge), and exact (non-float) comparison for identity/grouping columns.

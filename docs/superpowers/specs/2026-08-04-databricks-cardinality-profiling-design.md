# Cardinality/value_pattern/profile_tier for the Databricks/Unity Catalog profiler

## Context

`docs/superpowers/plans/2026-08-03-kpi-column-concept-mapping.md` (Tasks 3/4/4b/4c/5) added
`cardinality_ratio`, `value_pattern`, and `profile_tier` to `ColumnProfile`, computed only in
`core/profiling/data_model_profiler.py`'s local DuckDB CSV pushdown path, and wired them into
`_contextual_score` as new KPI-feature-resolution scoring signals. That plan's own final whole-branch
review flagged the local path's `COUNT(DISTINCT)` computation as an unconditional, TB-scale cost risk.
A fix was attempted (`approx_count_distinct`) and correctly rejected: measured empirically against
DuckDB 1.5.2 on a genuinely all-unique 2M-row column, the approximate ratio swung 0.86-1.17 against a
true 1.0000 -- wide enough to make the `>=0.98` near-unique-identifier threshold a coin flip decided
by hash luck, silently, per column per file. That finding was parked rather than shipped broken.

This design corrects the scope of that follow-up. `core/profiling/databricks_table_profiler.py`
(`profile_uc_table`) -- which profiles a Unity Catalog table via the Databricks SQL warehouse -- is
the platform's actual TB-scale profiling path (a local DuckDB CSV scan is inherently bounded by local
disk and never realistically approaches TB scale in practice). Today `profile_uc_table` computes
`cardinality_ratio`, `value_pattern`, and `profile_tier` for **none** of its columns -- these fields
sit at their dataclass defaults (`None`, `None`, `"raw"`-implicit-default) for every Databricks-backed
profile. This is a bigger gap than "unreliable at scale" -- it's total absence on the path that
matters most.

**Who calls this, and why it changes the design:** `profile_uc_table` is invoked from
`core.onboarding.workspace.onboarding.WorkspaceOnboarder.profile_databricks_tables`, whose table list
comes from `_databricks_source_tables()` -- which reads `workspace_settings.json`'s
`databricks_source: {catalog, schema}`. That is the **customer's own pre-existing source
catalog/schema** in their Databricks workspace: bronze-equivalent tables the customer already had
before this platform touched anything. The platform does not create these tables, does not own them,
and has no write access to run `ANALYZE TABLE` against them. This rules out any design that depends
on the platform being able to guarantee or refresh statistics freshness on the tables it profiles --
that lever does not exist for this use case. (It could exist for a *different*, not-yet-built use
case -- re-profiling this platform's own medallion-built gold/silver output, which it does create and
write via `core/medallion/delta_emitter.py` / `merge_emitter.py` -- but that is out of scope here; see
Out of Scope.)

## Research findings (grounding the design)

- **Spark/Databricks' `approx_count_distinct` (HyperLogLog++) has a tunable `relativeSD` parameter**
  (default 5%), unlike DuckDB's fixed-precision version -- but Spark's own documentation states that
  below `relativeSD < 0.01` it is more efficient to just use exact `count_distinct()`. Approximation
  is not designed to be pushed tight enough to trust a `>=0.98` boundary; this option is not viable at
  any tuning.
- **Unity Catalog managed tables get automatic background column statistics** ("predictive
  optimization", on by default since mid-2025) via an implicit `ANALYZE TABLE`, including
  `distinct_count` per column, readable through `DESCRIBE TABLE EXTENDED table_name column_name` --
  a metastore/catalog read, not a data scan. Confirmed example output:
  `col_name name, data_type string, num_nulls 0, distinct_count 2, avg_col_len 4, max_col_len 4`.
- **External/unmanaged tables do not get predictive optimization.** Statistics are only present if
  something (the customer, or a job outside this platform's control) explicitly ran `ANALYZE TABLE`.
  Since the tables this profiler reads are customer-owned and of unknown disposition (managed,
  external, or a raw volume), this is expected to be a real, common case -- not a rare edge case.
- **Whether `DESCRIBE TABLE EXTENDED table_name` (no column) returns every column's stats in one
  query, or requires one query per column, is not conclusively answered by available documentation.**
  The only concrete, confirmed-working example names a specific column
  (`DESC EXTENDED students name;`). This is flagged as an implementation-time verification item, not
  assumed.

## Design

### Architecture & components

No new files. Extends `core/profiling/databricks_table_profiler.py` only, following the shape of the
existing `_aggregate_column_stats` helper (same file):

- **New helper `_read_cardinality_stats(client, fqn, schema_map) -> dict[str, int | None]`** — issues
  `DESCRIBE TABLE EXTENDED {fqn} {column}` per column (the confirmed-working syntax), parses
  `distinct_count` out of the row output, returns `{column_name: distinct_count_or_None}`.
  **Implementation-time check (do this first, before writing the rest):** verify against a real
  warehouse connection whether `DESCRIBE TABLE EXTENDED {fqn}` with no column name returns per-column
  `distinct_count` for every column in one query. If it does, that is a strictly better version of
  this same design -- swap the per-column loop for one query; nothing else in this design changes.
  If it does not (only table-level metadata comes back), the per-column loop is required as designed.
- **`profile_uc_table`** calls this alongside its existing schema/row-count/aggregate/sample queries,
  and computes `cardinality_ratio = distinct_count / row_count` per column when available.
- **`value_pattern`** requires no new cloud-specific logic. `_infer_value_pattern` already exists in
  `core/profiling/data_model_profiler.py` and operates purely on `sample_values`, which
  `profile_uc_table` already collects via its existing bounded sample query. Import and reuse it
  directly -- do not reimplement.
- **`profile_tier`** is the constant `"raw"` for every column, matching the local profiler's own
  documented rationale (profiling runs pre-medallion, against bronze-shaped source data).

### Data flow

1. Existing, unchanged: `DESCRIBE TABLE` (schema) -> `SELECT count(*)` (row_count) ->
   `_aggregate_column_stats` (exact null_count/min/max, one query) -> sample `SELECT *`
   (sample_values).
2. New: for each column, `DESCRIBE TABLE EXTENDED {fqn} {column}` (or one combined query, pending the
   implementation-time check above), parse `distinct_count` from the row output.
3. `cardinality_ratio = distinct_count / row_count` if both are present and `row_count > 0`; `None`
   otherwise.
4. `value_pattern = _infer_value_pattern(sample_values)`.
5. `profile_tier = "raw"`.

### Error handling

Matches this file's existing, established convention exactly: `_aggregate_column_stats`'s own
docstring says it "returns `{}` (not raised) on any query failure so a warehouse hiccup degrades to
the pre-existing sample-based columns rather than failing profiling outright; the failure is still
recorded in warnings." `_read_cardinality_stats` follows the identical pattern: any
`DESCRIBE TABLE EXTENDED` failure (permissions, unsupported table type, stats never computed, network
hiccup) -- whether for one column or the whole batch -- degrades that column's `cardinality_ratio` to
`None`, never raises, and records one `warnings` entry. Profiling as a whole never fails because a
stats read failed; this mirrors the resolver's own consumer-side contract (`None` means "signal
absent," never "zero" or "false" -- already established by the local-profiler plan).

**Never triggers `ANALYZE TABLE`.** This function is strictly read-only. See Context for why: the
platform does not own the tables it profiles here, so it has no basis for deciding when writing
fresh statistics to someone else's table would be appropriate, and doing so silently would be a
surprising, unrequested side effect with real cost and privilege requirements.

### Testing

Follows the existing `tests/test_databricks_table_profiler.py` `FakeClient` pattern exactly
(substring-matched query responses; no real Databricks connectivity anywhere in tests). New cases:

- `DESCRIBE TABLE EXTENDED` response present with a `distinct_count` row -> `cardinality_ratio`
  computed correctly against the fixture's `row_count`.
- Response absent, or `execute_query` raises for that query -> `cardinality_ratio is None` for the
  affected column(s), a `warnings` entry is recorded, and the rest of the profile (schema, row_count,
  null_count, sample_values) is unaffected.
- A real `value_pattern` case (e.g. a currency-shaped or ISO-date-shaped column) proving the shared
  `_infer_value_pattern` import is wired correctly against warehouse-sourced `sample_values`.
- Every column reports `profile_tier == "raw"`.

## Out of Scope

- **Modifying `core/profiling/data_model_profiler.py`'s local DuckDB path.** Its existing exact
  `COUNT(DISTINCT)` is correct and safe; the TB-scale concern that originally flagged it does not
  apply in practice to a local file scan. Left untouched.
- **Adding an `ANALYZE TABLE` step to this platform's own medallion write pipeline**
  (`delta_emitter.py` / `merge_emitter.py`) to keep *its own* gold/silver tables' statistics fresh for
  a future re-profiling use case. This is a real, legitimate idea, but it is a different subsystem
  (the write pipeline, not the profiler) serving a use case that does not exist in this codebase
  today (nothing currently re-profiles this platform's own medallion output via
  `profile_uc_table`). Worth its own design if and when that use case appears.
- **Any change to `_contextual_score`'s consumption of these signals** (Tasks 4/4b/4c/5's scoring
  logic). This design only makes the signals available on the Databricks path; the consumer side is
  already correct and unchanged.

## Verification

- Unit tests (above) pass under `unittest`.
- `.venv\Scripts\green-gate.exe` shows no new failures beyond this repo's existing 2 known
  pre-existing, unrelated baseline failures.
- The implementation-time check (single-query vs per-column `DESCRIBE TABLE EXTENDED`) is resolved
  and documented in the implementation's own commit/report, not left as an open assumption.

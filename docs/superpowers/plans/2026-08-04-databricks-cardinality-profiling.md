# Databricks/Unity Catalog Cardinality Profiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `core/profiling/databricks_table_profiler.py::profile_uc_table` (the platform's real
TB-scale profiling path) the `cardinality_ratio`, `value_pattern`, and `profile_tier` signals the
local DuckDB path already has, by reading Unity Catalog's own cached column statistics instead of
computing anything — no scan, no approximation, no write access needed to tables this platform
doesn't own.

**Architecture:** One new helper (`_read_cardinality_stats`, mirrors the existing
`_aggregate_column_stats` in the same file) reads `distinct_count` per column via
`DESCRIBE TABLE EXTENDED`, degrading to `None` on any failure — never raising, never triggering
`ANALYZE TABLE`. `value_pattern` reuses `_infer_value_pattern` from `data_model_profiler.py` directly
against already-collected sample values; `profile_tier` is a constant.

**Tech Stack:** Python 3.11, `unittest` (never pytest), Databricks SQL (via the existing
`DatabricksClient` seam, fully mocked in tests).

## Global Constraints

- **Test runner is `unittest`, never `pytest`.** A `PreToolUse` hook blocks `uv run pytest`. Use
  `.venv\Scripts\python.exe -m unittest <module>` or the portable gate `green-gate`.
- **The full gate's failure count must not increase.** This repo's current baseline (2026-08-04) has
  2 pre-existing, unrelated failures (`tests.regressions.test_json_nested_leaf_profiling`,
  `tests.regressions.test_profiler_tb_scale_csv_nullcount...test_null_count_is_correct_even_when_null_is_outside_the_sample_window`)
  that predate this plan entirely and are out of scope. Done correctly means the gate shows exactly
  these same 2 failures and no others — verify by running the real gate, not by assuming.
- **No emojis in any output, report, or generated text.** Use ASCII markers `[ok]` / `[~]` / `[x]`.
- **Workspace-agnostic:** never hardcode against a workspace name, domain, or column vocabulary.
- **No new production files** — this extends `core/profiling/databricks_table_profiler.py` only.
- **Never trigger `ANALYZE TABLE` or any write against a profiled table.** This function reads
  customer-owned source tables the platform has no write access to and no basis for refreshing —
  see `docs/superpowers/specs/2026-08-04-databricks-cardinality-profiling-design.md` (Context) for
  why. Strictly read-only.
- **This environment has no live Databricks warehouse connection.** Every test in this plan uses the
  existing `FakeClient` mock pattern in `tests/test_databricks_table_profiler.py` — there is no way
  to verify the exact row-shape `DESCRIBE TABLE EXTENDED table_name column_name` returns against a
  real warehouse from this sandbox. The parsing logic below is written defensively (looks up a named
  attribute by value regardless of exact column count/order) against the one confirmed example from
  Databricks' own documentation (`col_name name, data_type string, num_nulls 0, distinct_count 2,
  avg_col_len 4, max_col_len 4` — read as attribute/value row pairs). Flag this explicitly in the
  task's commit message as an assumption a real warehouse run should confirm; do not treat it as
  silently verified.

---

### Task 1: Add cardinality_ratio, value_pattern, profile_tier to profile_uc_table

**Files:**
- Modify: `core/profiling/databricks_table_profiler.py` (imports at top; new helper
  `_read_cardinality_stats` near `_aggregate_column_stats`, currently lines 131-178; the
  `ColumnProfile(...)` construction inside `profile_uc_table`, currently lines 98-111)
- Test: `tests/test_databricks_table_profiler.py` (extend the existing `FakeClient`-based test class)

**Interfaces:**
- Consumes: `core.profiling.data_model_profiler._infer_value_pattern(sample_values: list[Any]) -> str | None`
  (already exists, unchanged — read `core/profiling/data_model_profiler.py:560-575` if you want to
  see it, but you do not need to modify it).
- Produces: `profile_uc_table(...)`'s returned `DatasetProfile.columns` now carries
  `cardinality_ratio: float | None`, `value_pattern: str | None`, `profile_tier: str` (always
  `"raw"`) on every `ColumnProfile` — no signature change to `profile_uc_table` itself.

- [ ] **Step 1: Read the current file in full around the areas you'll touch**

Read `core/profiling/databricks_table_profiler.py` lines 1-179 in full before editing anything. Confirm the
line numbers below still match — this file may have shifted since this plan was written.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_databricks_table_profiler.py` (add these methods inside the existing
`ProfileUcTableTests` class — do not create a new test file):

```python
    def test_cardinality_ratio_computed_from_cached_distinct_count(self):
        client = FakeClient(
            {
                # More specific than "DESCRIBE TABLE" below -- must be checked
                # first, since "DESCRIBE TABLE EXTENDED ..." contains the
                # substring "DESCRIBE TABLE" too and FakeClient matches by
                # first-inserted-key-wins. Insertion order in this dict is
                # the match order.
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `DeptID`": (
                    ["info_name", "info_value"],
                    [
                        ["col_name", "DeptID"],
                        ["data_type", "string"],
                        ["num_nulls", "0"],
                        ["distinct_count", "20"],
                    ],
                ),
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_departments` `Name`": (
                    ["info_name", "info_value"],
                    [
                        ["col_name", "Name"],
                        ["data_type", "string"],
                        ["num_nulls", "0"],
                        ["distinct_count", "5"],
                    ],
                ),
                "DESCRIBE TABLE": (
                    ["col_name", "data_type", "comment"],
                    [
                        ["DeptID", "string", ""],
                        ["Name", "string", ""],
                        ["# Detailed Table Information", "", ""],
                        ["Catalog", "healthcare_rcm", ""],
                    ],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                "count(*) - count(": (["c0", "c1"], [["1", "0"]]),
                "SELECT * FROM": (
                    ["DeptID", "Name"],
                    [["1", "Cardiology"], ["2", "Radiology"], ["3", None]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")

        by_name = {c.name: c for c in profile.columns}
        # row_count is 20 (from "SELECT count(*) FROM" above).
        self.assertAlmostEqual(by_name["DeptID"].cardinality_ratio, 20 / 20)
        self.assertAlmostEqual(by_name["Name"].cardinality_ratio, 5 / 20)
        self.assertEqual(profile.warnings, [])

    def test_cardinality_ratio_is_none_when_stats_are_absent(self):
        # No "EXTENDED ..." key registered at all -- FakeClient raises
        # AssertionError("unexpected query") for that call, which the
        # helper must catch and degrade to None + a warning, not propagate.
        client = FakeClient(
            {
                "DESCRIBE TABLE": (
                    ["col_name", "data_type", "comment"],
                    [
                        ["DeptID", "string", ""],
                        ["Name", "string", ""],
                    ],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["20"]]),
                "count(*) - count(": (["c0", "c1"], [["1", "0"]]),
                "SELECT * FROM": (
                    ["DeptID", "Name"],
                    [["1", "Cardiology"], ["2", "Radiology"], ["3", None]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_departments")

        by_name = {c.name: c for c in profile.columns}
        self.assertIsNone(by_name["DeptID"].cardinality_ratio)
        self.assertIsNone(by_name["Name"].cardinality_ratio)
        self.assertTrue(
            any("cardinality_stats_failed" in w for w in profile.warnings),
            f"expected a cardinality_stats_failed warning, got: {profile.warnings}",
        )
        # A stats-read failure must not affect anything else already working.
        self.assertEqual(by_name["DeptID"].null_count, 1)
        self.assertIn("Cardiology", by_name["Name"].sample_values)

    def test_value_pattern_and_profile_tier_are_populated(self):
        client = FakeClient(
            {
                "EXTENDED `healthcare_rcm`.`bronze`.`hospital_a_claims` `ChargeAmount`": (
                    ["info_name", "info_value"],
                    [["distinct_count", "3"]],
                ),
                "DESCRIBE TABLE": (
                    ["col_name", "data_type", "comment"],
                    [["ChargeAmount", "double", ""]],
                ),
                "SELECT count(*) FROM": (["count(1)"], [["3"]]),
                "count(*) - count(": (["c0", "c1", "c2"], [["0", "100.50", "102.50"]]),
                "SELECT * FROM": (
                    ["ChargeAmount"],
                    [["100.50"], ["101.50"], ["102.50"]],
                ),
            }
        )

        profile = profile_uc_table(client, "healthcare_rcm", "bronze", "hospital_a_claims")

        by_name = {c.name: c for c in profile.columns}
        self.assertEqual(by_name["ChargeAmount"].value_pattern, "currency_2dp")
        self.assertEqual(by_name["ChargeAmount"].profile_tier, "raw")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m unittest tests.test_databricks_table_profiler -v`
Expected: the 3 new tests FAIL — `AttributeError: 'ColumnProfile' object has no attribute
'cardinality_ratio'` is wrong (that field already exists with a default of `None`, from the earlier
KPI plan) — the real expected failure is an assertion mismatch: `cardinality_ratio` will be `None` for
every column (nothing computes it yet), so `assertAlmostEqual(by_name["DeptID"].cardinality_ratio, 1.0)`
fails with `None` not being almost-equal to `1.0`. Confirm this is the actual failure reason before
proceeding, not an import error or something unrelated.

- [ ] **Step 4: Add the import**

In `core/profiling/databricks_table_profiler.py`, change (currently lines 22-27):

```python
from core.profiling.data_model_profiler import (
    ColumnProfile,
    DatasetProfile,
    _is_numeric_dtype,
    _is_temporal_dtype,
)
```

to:

```python
from core.profiling.data_model_profiler import (
    ColumnProfile,
    DatasetProfile,
    _infer_value_pattern,
    _is_numeric_dtype,
    _is_temporal_dtype,
)
```

- [ ] **Step 5: Add the `_read_cardinality_stats` helper**

Add this function immediately after `_aggregate_column_stats` (currently ends at line 178):

```python
def _read_cardinality_stats(
    client: "DatabricksClient",
    fqn: str,
    schema_map: dict[str, str],
    warnings: list[str],
) -> dict[str, int | None]:
    """Read each column's ``distinct_count`` from Unity Catalog's own cached
    column statistics (``ANALYZE TABLE ... COMPUTE STATISTICS``, run
    automatically by Databricks predictive optimization on managed tables,
    or manually on others) via ``DESCRIBE TABLE EXTENDED``. A metastore
    read, not a data scan.

    Deliberately NOT an approximate or freshly-computed count: this platform
    profiles customer-owned source tables it cannot write to, so there is no
    lever to guarantee or refresh statistics freshness here (see
    docs/superpowers/specs/2026-08-04-databricks-cardinality-profiling-design.md).
    Per-column failure (stats never computed, unsupported table type,
    permissions, a table with no ANALYZE history) degrades that column to
    ``None`` and is recorded in ``warnings`` -- never raised. ``None`` here
    means "signal absent," the same contract the local DuckDB profiler's
    missing-signal paths already use; never treat it as zero.
    """
    stats: dict[str, int | None] = {}
    for name in schema_map:
        quoted_col = quote_ident_backtick(assert_safe_identifier(name, context="uc column"))
        stats[name] = None
        try:
            _, rows = client.execute_query(f"DESCRIBE TABLE EXTENDED {fqn} {quoted_col}")
        except Exception as exc:  # pragma: no cover - warehouse/network dependent
            warnings.append(f"cardinality_stats_failed:{name}:{type(exc).__name__}:{exc}")
            continue
        for row in rows:
            if len(row) >= 2 and str(row[0]).strip().lower() == "distinct_count":
                try:
                    stats[name] = int(row[1])
                except (TypeError, ValueError):
                    warnings.append(f"cardinality_stats_unparseable:{name}:{row[1]!r}")
                break
    return stats
```

- [ ] **Step 6: Wire it into `profile_uc_table`**

In `profile_uc_table` (currently lines 41-128), find this line (currently line 83):

```python
    agg_stats = _aggregate_column_stats(client, fqn, schema_map, row_count, warnings)
```

Add immediately after it:

```python
    cardinality_stats = _read_cardinality_stats(client, fqn, schema_map, warnings)
```

Then find the `ColumnProfile(...)` construction inside the `for name, dtype in schema_map.items():`
loop (currently lines 98-111):

```python
        stats = agg_stats.get(name)
        columns.append(
            ColumnProfile(
                name=name,
                dtype=dtype,
                nullable=None,
                sample_values=distinct_sorted,
                sample_min=distinct_sorted[0] if distinct_sorted else None,
                sample_max=distinct_sorted[-1] if distinct_sorted else None,
                exact_min=stats["min"] if stats else None,
                exact_max=stats["max"] if stats else None,
                null_count=stats["null_count"] if stats else None,
                source="exact_scan" if stats else "sample_profile",
            )
        )
```

Change it to (adding the three new kwargs, changing nothing else about the existing ones):

```python
        stats = agg_stats.get(name)
        distinct_count = cardinality_stats.get(name)
        cardinality_ratio = (
            (distinct_count / row_count) if distinct_count is not None and row_count else None
        )
        columns.append(
            ColumnProfile(
                name=name,
                dtype=dtype,
                nullable=None,
                sample_values=distinct_sorted,
                sample_min=distinct_sorted[0] if distinct_sorted else None,
                sample_max=distinct_sorted[-1] if distinct_sorted else None,
                exact_min=stats["min"] if stats else None,
                exact_max=stats["max"] if stats else None,
                null_count=stats["null_count"] if stats else None,
                source="exact_scan" if stats else "sample_profile",
                cardinality_ratio=cardinality_ratio,
                value_pattern=_infer_value_pattern(distinct_sorted),
                profile_tier="raw",
            )
        )
```

(Read the surrounding loop in full first, since `distinct_sorted` is defined a few lines above this
block — do not redefine it, reuse the existing variable exactly as the local DuckDB profiler reuses
its own `sample_values` for the same `_infer_value_pattern` call.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m unittest tests.test_databricks_table_profiler -v`
Expected: PASS, all tests in the file (the 3 new ones plus every pre-existing one — confirm none of
the existing tests broke; if `test_profiles_table_via_warehouse_queries_only` or any other
pre-existing test now fails, that means the new `DESCRIBE TABLE EXTENDED` query is being issued
somewhere the existing `FakeClient` fixtures didn't anticipate — check whether that test's
`FakeClient` needs an `"EXTENDED"`-prefixed key added too, since every test that calls
`profile_uc_table` now triggers one `DESCRIBE TABLE EXTENDED` query per column).

- [ ] **Step 8: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: exactly the 2 pre-existing, unrelated failures named in Global Constraints and no others.

- [ ] **Step 9: Commit**

```bash
git add core/profiling/databricks_table_profiler.py tests/test_databricks_table_profiler.py
git commit -m "feat(profiling): read cached cardinality stats for the Databricks/UC profiler

profile_uc_table had none of the cardinality_ratio/value_pattern/
profile_tier signals the local DuckDB CSV path already computes -- the
real TB-scale profiling path had zero benefit from that whole plan.
Reads Unity Catalog's own cached ANALYZE TABLE distinct_count via
DESCRIBE TABLE EXTENDED (a metastore read, not a scan) instead of
computing anything -- this platform profiles customer-owned source
tables it has no write access to, so there is no lever to guarantee or
refresh statistics freshness; None on any failure, matching the local
profiler's own missing-signal contract. Never triggers ANALYZE TABLE.

Row-shape assumption for DESCRIBE TABLE EXTENDED (attribute/value row
pairs) is based on Databricks' own documented example, not verified
against a live warehouse -- this sandbox has no Databricks connection.
Flagging for confirmation whenever this runs against a real workspace."
```

---

## Final Verification

- [ ] **Full gate:** `.venv\Scripts\green-gate.exe` -> exactly the 2 known pre-existing failures, no
  others, test count increased by the 3 new tests.
- [ ] **Re-read the row-shape assumption note** in this plan's Global Constraints and Task 1 Step 5's
  docstring one more time before considering this done — it is a real, disclosed, unverified
  assumption, not a solved problem. The next person with real Databricks warehouse access should
  run `DESCRIBE TABLE EXTENDED <a real table> <a real column>` once and confirm the row shape
  matches what `_read_cardinality_stats` expects (attribute-name/value pairs, `distinct_count` found
  by scanning for that label in column 0). If it doesn't match, only `_read_cardinality_stats`'s
  parsing loop needs to change -- nothing else in this design depends on the exact row shape.

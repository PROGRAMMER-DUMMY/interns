"""Profiler TB-scale fix P1: core/profiling/databricks_table_profiler.py's
profile_uc_table() computed null_count/min/max client-side from a plain
`SELECT * ... LIMIT 1000` sample -- at a billion-row table, 1000 rows in
whatever physical order the warehouse returns first, not random. exact_min/
exact_max were never populated at all. Fixed by adding one real server-side
aggregate query over the FULL table for null_count and (for numeric/temporal
columns) exact min/max. See ~/.claude/plans/dynamic-cooking-firefly.md P1.
"""
from __future__ import annotations

import unittest

from core.profiling.databricks_table_profiler import profile_uc_table


class FakeClient:
    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.queries: list[str] = []

    def execute_query(self, sql: str, **kwargs):
        self.queries.append(sql)
        for key, result in self._responses.items():
            if key in sql:
                return result
        raise AssertionError(f"unexpected query: {sql}")


class RealAggregateNotBiasedBySampleTests(unittest.TestCase):
    def test_null_count_reflects_full_table_not_the_first_sample_rows(self):
        """The classic bias scenario: the first LIMIT-1000 rows happen to
        have zero nulls (e.g. an early-loaded, clean partition), but the
        real table has nulls concentrated elsewhere. Before the fix,
        null_count would have been silently 0. After the fix, it must come
        from the real aggregate, not the sample."""
        client = FakeClient(
            {
                "DESCRIBE TABLE": (["col_name", "data_type"], [["amount", "double"]]),
                "SELECT count(*) FROM": (["count(1)"], [["5000000000"]]),  # 5 billion rows
                # Real aggregate: 1.2 billion nulls, real min/max across the
                # whole table -- deliberately different from what the clean
                # sample below would suggest.
                "count(*) - count(": (["nulls", "mn", "mx"], [["1200000000", "-500.0", "999999.99"]]),
                # The sample (first 1000 rows) is misleadingly clean: zero
                # nulls, a narrow range -- if the code still trusted this for
                # the statistic, the bug would still be present.
                "SELECT * FROM": (["amount"], [[str(float(i))] for i in range(1000)]),
            }
        )

        profile = profile_uc_table(client, "c", "s", "big_table")
        col = profile.columns[0]

        # Must come from the real aggregate, not "0 nulls in the sample".
        self.assertEqual(col.null_count, 1_200_000_000)
        # exact_min/exact_max must be populated at all (previously always
        # None) -- and must be the real values, not the sample's narrow range.
        self.assertEqual(col.exact_min, "-500.0")
        self.assertEqual(col.exact_max, "999999.99")
        self.assertEqual(col.source, "exact_scan")
        self.assertIn("sql_warehouse_aggregate", profile.sources_used)

    def test_non_numeric_non_temporal_column_gets_null_count_but_no_min_max(self):
        """A string column still gets a real null_count from the aggregate
        (count(*) - count(col) works for any type), but no min/max request
        -- matching the local DuckDB profiler's same numeric/temporal gate."""
        client = FakeClient(
            {
                "DESCRIBE TABLE": (["col_name", "data_type"], [["notes", "string"]]),
                "SELECT count(*) FROM": (["count(1)"], [["1000"]]),
                "count(*) - count(": (["nulls"], [["37"]]),
                "SELECT * FROM": (["notes"], [["x"]]),
            }
        )
        profile = profile_uc_table(client, "c", "s", "t")
        col = profile.columns[0]
        self.assertEqual(col.null_count, 37)
        self.assertIsNone(col.exact_min)
        self.assertIsNone(col.exact_max)

    def test_aggregate_query_failure_degrades_to_sample_based_columns_not_a_crash(self):
        """A warehouse hiccup on the new aggregate query must not break
        profiling outright -- it should degrade to the pre-fix sample-based
        behavior and record a warning, not raise."""

        class FlakyClient(FakeClient):
            def execute_query(self, sql: str, **kwargs):
                if "count(*) - count(" in sql:
                    raise RuntimeError("warehouse timeout")
                return super().execute_query(sql, **kwargs)

        client = FlakyClient(
            {
                "DESCRIBE TABLE": (["col_name", "data_type"], [["amount", "double"]]),
                "SELECT count(*) FROM": (["count(1)"], [["1000"]]),
                "SELECT * FROM": (["amount"], [["1.5"], ["2.5"], [None]]),
            }
        )
        profile = profile_uc_table(client, "c", "s", "t")
        self.assertTrue(any("aggregate_stats_failed" in w for w in profile.warnings))
        col = profile.columns[0]
        # Falls back to sample-derived values, source label reflects that.
        self.assertEqual(col.source, "sample_profile")
        self.assertNotIn("sql_warehouse_aggregate", profile.sources_used)

    def test_zero_row_table_skips_aggregate_query_entirely(self):
        client = FakeClient(
            {
                "DESCRIBE TABLE": (["col_name", "data_type"], [["a", "int"]]),
                "SELECT count(*) FROM": (["count(1)"], [["0"]]),
                "SELECT * FROM": (["a"], []),
            }
        )
        # Must not raise "unexpected query" for the aggregate -- it should
        # never be issued when row_count is 0.
        profile = profile_uc_table(client, "c", "s", "t")
        self.assertEqual(profile.row_count, 0)


if __name__ == "__main__":
    unittest.main()

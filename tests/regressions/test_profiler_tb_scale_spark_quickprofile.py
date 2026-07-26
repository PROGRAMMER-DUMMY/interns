"""Profiler TB-scale fix P3: tools/profiler.py's SparkEngine.quick_profile
returned hardcoded mock stats ({"fill_pct": 100.0, "n_unique": 2,
"unique_ratio": 0.01}) for EVERY column, literally commented "Mocked for
quick strat selection". This fed run_pipeline's auto-stratification-column
selector, whose filter (n_unique in [2,250], unique_ratio<=0.05) was
trivially satisfied by every column since the mock always emitted those
exact values -- auto-stratification silently picked an arbitrary column, on
exactly the datasets big enough to have routed to Spark in the first place.
Fixed: a real single-aggregate-pass computation using
approx_count_distinct (HyperLogLog-based, appropriate for a "quick"
pre-check). Spark isn't runnable on this dev box (same constraint as every
other Spark test in this repo) -- verified with a mocked DataFrame + a
mocked `F` (pyspark.sql.functions), never a live JVM.
See ~/.claude/plans/dynamic-cooking-firefly.md P3.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import tools.profiler as profiler_module
from tools.profiler import SparkEngine


class _FakeRow:
    def __init__(self, data: dict):
        self._data = data

    def asDict(self):
        return self._data


class _FakeAgg:
    def __init__(self, row: dict):
        self._row = row

    def collect(self):
        return [_FakeRow(self._row)]


class _FakeSparkDF:
    """Minimal stand-in for a pyspark DataFrame -- only the methods
    quick_profile actually calls."""

    def __init__(self, dtypes: list, total: int, agg_row: dict):
        self._dtypes = dtypes
        self._total = total
        self._agg_row = agg_row
        self.agg_calls: list = []

    @property
    def dtypes(self):
        return self._dtypes

    def limit(self, n):
        return self

    def count(self):
        return self._total

    def agg(self, *exprs):
        self.agg_calls.append(exprs)
        return _FakeAgg(self._agg_row)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class SparkQuickProfileRealValuesTests(unittest.TestCase):
    def _make_engine(self, df) -> SparkEngine:
        engine = SparkEngine.__new__(SparkEngine)
        engine.df = df
        return engine

    def test_returns_real_differentiated_values_not_the_old_mock_constants(self):
        # low_card: 3 distinct values out of 1000 -- a genuine stratification
        # candidate. high_card: an id-like column, ~1000 distinct -- must NOT
        # look like a stratification candidate.
        df = _FakeSparkDF(
            dtypes=[("low_card", "string"), ("high_card", "int")],
            total=1000,
            agg_row={
                "low_card__nonnull": 1000, "low_card__nunique": 3,
                "high_card__nonnull": 1000, "high_card__nunique": 998,
            },
        )
        engine = self._make_engine(df)
        # Plain auto-mocks: F.col(...)/.count(...)/.approx_count_distinct(...)
        # each return a MagicMock, and .alias(...) on a MagicMock returns
        # another MagicMock automatically -- the expression CONTENT is never
        # inspected by _FakeSparkDF.agg, only that a real per-column
        # aggregate pass happens and the canned agg_row comes back.
        fake_F = mock.MagicMock()

        with mock.patch.object(profiler_module, "F", fake_F):
            result = _run(engine.quick_profile(sample_rows=1000))

        records = result.to_dicts() if hasattr(result, "to_dicts") else result
        by_col = {r["column"]: r for r in records}

        # Neither value is the old hardcoded mock (fill_pct=100.0 always
        # happened to coincide here, but n_unique/unique_ratio must not be
        # the literal old constants 2 / 0.01 for both columns).
        self.assertEqual(by_col["low_card"]["n_unique"], 3)
        self.assertEqual(by_col["low_card"]["unique_ratio"], 0.003)
        self.assertEqual(by_col["high_card"]["n_unique"], 998)
        self.assertEqual(by_col["high_card"]["unique_ratio"], 0.998)
        # The two columns must be genuinely distinguishable -- the whole
        # point of the bug being fixed.
        self.assertNotEqual(
            by_col["low_card"]["unique_ratio"], by_col["high_card"]["unique_ratio"]
        )

    def test_stratification_selector_now_correctly_excludes_high_cardinality_columns(self):
        """Reproduces run_pipeline's actual auto-stratification filter
        (n_unique in [2,250], unique_ratio<=0.05) against the fixed output --
        before the fix, EVERY column satisfied this filter because the mock
        was identical for all of them."""
        df = _FakeSparkDF(
            dtypes=[("region", "string"), ("customer_id", "int")],
            total=100_000,
            agg_row={
                "region__nonnull": 100_000, "region__nunique": 4,
                "customer_id__nonnull": 100_000, "customer_id__nunique": 99_500,
            },
        )
        engine = self._make_engine(df)
        # Plain auto-mocks: F.col(...)/.count(...)/.approx_count_distinct(...)
        # each return a MagicMock, and .alias(...) on a MagicMock returns
        # another MagicMock automatically -- the expression CONTENT is never
        # inspected by _FakeSparkDF.agg, only that a real per-column
        # aggregate pass happens and the canned agg_row comes back.
        fake_F = mock.MagicMock()

        with mock.patch.object(profiler_module, "F", fake_F):
            result = _run(engine.quick_profile(sample_rows=100_000))
        records = result.to_dicts() if hasattr(result, "to_dicts") else result

        def eligible(r):
            return 2 <= r["n_unique"] <= 250 and r["unique_ratio"] <= 0.05

        eligible_cols = {r["column"] for r in records if eligible(r)}
        self.assertEqual(eligible_cols, {"region"})
        self.assertNotIn("customer_id", eligible_cols)

    def test_empty_dataframe_does_not_divide_by_zero(self):
        df = _FakeSparkDF(dtypes=[("a", "string")], total=0, agg_row={})
        engine = self._make_engine(df)
        with mock.patch.object(profiler_module, "F", mock.MagicMock()):
            result = _run(engine.quick_profile(sample_rows=10))
        records = result.to_dicts() if hasattr(result, "to_dicts") else result
        self.assertEqual(records[0]["fill_pct"], 0.0)
        self.assertEqual(records[0]["n_unique"], 0)


if __name__ == "__main__":
    unittest.main()

"""Regression: the profiler must emit cardinality_ratio, value_pattern, and
profile_tier -- signals the KPI resolver needs and that did not exist
anywhere in the evidence chain before this fix (confirmed by reading
value_profile()/column_profile_summary() in derived_evidence.py, neither
of which carried them).
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from core.profiling.data_model_profiler import (
    ColumnProfile,
    DataModelProfiler,
    _infer_value_pattern,
    _merge_columns,
)


class ProfilerNewSignalsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        path = Path(self.tmpdir.name) / "sample.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ClaimID", "PayorType", "ChargeAmount"])
            for i in range(50):
                writer.writerow([f"CLAIM{i:06d}", "Commercial" if i % 2 else "Medicare", f"{100 + i}.50"])
        self.path = path

    def test_near_unique_column_reports_high_cardinality(self):
        profile = DataModelProfiler().profile_path(self.path)
        by_name = {col.name: col for col in profile.columns}
        self.assertGreater(by_name["ClaimID"].cardinality_ratio, 0.95)

    def test_low_cardinality_column_reports_low_cardinality(self):
        profile = DataModelProfiler().profile_path(self.path)
        by_name = {col.name: col for col in profile.columns}
        self.assertLess(by_name["PayorType"].cardinality_ratio, 0.2)

    def test_every_column_stamped_raw_tier(self):
        profile = DataModelProfiler().profile_path(self.path)
        for col in profile.columns:
            self.assertEqual(col.profile_tier, "raw")

    def test_currency_value_pattern_inferred(self):
        # Pre-formatted currency strings: some sources deliver these, so the
        # regex fallback must keep working.
        pattern = _infer_value_pattern(["100.50", "101.50", "102.50"])
        self.assertEqual(pattern, "currency_2dp")

    def test_no_pattern_below_threshold(self):
        pattern = _infer_value_pattern(["100.50", "abc", "2024-01-01"])
        self.assertIsNone(pattern)

    def test_float_typed_money_column_reports_currency_through_the_profiler(self):
        # The bug this test exists for: `ChargeAmount` is inferred Float64, so
        # its samples reach _infer_value_pattern as Python floats, and
        # str(100.50) == "100.5" -- the repr drops the trailing zero, so a
        # regex demanding exactly two decimal digits could never match real
        # profiled currency data. The old test only fed hand-typed STRINGS
        # straight to _infer_value_pattern, which is why it never caught this.
        profile = DataModelProfiler().profile_path(self.path)
        by_name = {col.name: col for col in profile.columns}
        self.assertEqual(by_name["ChargeAmount"].dtype, "Float64")
        self.assertEqual(by_name["ChargeAmount"].value_pattern, "currency_2dp")

    def test_higher_precision_float_column_is_not_currency(self):
        path = Path(self.tmpdir.name) / "ratios.csv"
        path.write_text(
            "Ratio\n3.14159\n2.71828\n100.567\n1.41421\n", encoding="utf-8"
        )
        profile = DataModelProfiler().profile_path(path)
        by_name = {col.name: col for col in profile.columns}
        self.assertIsNone(by_name["Ratio"].value_pattern)

    def test_integer_column_is_not_currency(self):
        # Integers round to 2dp losslessly but are not 2-decimal-place money;
        # a count/ID column must not be mistaken for one.
        path = Path(self.tmpdir.name) / "counts.csv"
        path.write_text("VisitCount\n1\n2\n3\n4\n", encoding="utf-8")
        profile = DataModelProfiler().profile_path(path)
        by_name = {col.name: col for col in profile.columns}
        self.assertIsNone(by_name["VisitCount"].value_pattern)


class MergeColumnsCarriesNewSignalsTests(unittest.TestCase):
    """_merge_columns rebuilds a ColumnProfile field by field, so any field it
    forgets is silently dropped. The DuckDB pushdown path bypasses it today,
    but the documented follow-up is extending the polars/parquet paths, which
    do go through it -- and would then lose exactly the three signals this
    plan added.
    """

    def test_merge_keeps_the_incoming_cardinality_pattern_and_tier(self):
        old = ColumnProfile(
            name="ChargeAmount",
            dtype="Float64",
            cardinality_ratio=0.10,
            value_pattern="iso_date",
            profile_tier="raw",
        )
        new = ColumnProfile(
            name="ChargeAmount",
            dtype="Float64",
            cardinality_ratio=0.99,
            value_pattern="currency_2dp",
            profile_tier="exact",
        )
        merged = _merge_columns({"ChargeAmount": old}, {"ChargeAmount": new})["ChargeAmount"]
        self.assertEqual(merged.cardinality_ratio, 0.99)
        self.assertEqual(merged.value_pattern, "currency_2dp")
        self.assertEqual(merged.profile_tier, "exact")

    def test_merge_falls_back_to_the_existing_values(self):
        old = ColumnProfile(
            name="PayorType",
            dtype="Utf8",
            cardinality_ratio=0.02,
            value_pattern="fixed_length_alnum",
            profile_tier="exact",
        )
        new = ColumnProfile(name="PayorType", dtype="Utf8", profile_tier="")
        merged = _merge_columns({"PayorType": old}, {"PayorType": new})["PayorType"]
        self.assertEqual(merged.cardinality_ratio, 0.02)
        self.assertEqual(merged.value_pattern, "fixed_length_alnum")
        # profile_tier is provenance, like `source`: the incoming profile wins
        # unless it says nothing at all. It is not nullable (it defaults to
        # "raw"), so a merge cannot tell "unset" from an explicit "raw".
        self.assertEqual(merged.profile_tier, "exact")

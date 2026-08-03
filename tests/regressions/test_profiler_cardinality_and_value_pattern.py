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

from core.profiling.data_model_profiler import DataModelProfiler, _infer_value_pattern


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
        pattern = _infer_value_pattern(["100.50", "101.50", "102.50"])
        self.assertEqual(pattern, "currency_2dp")

    def test_no_pattern_below_threshold(self):
        pattern = _infer_value_pattern(["100.50", "abc", "2024-01-01"])
        self.assertIsNone(pattern)

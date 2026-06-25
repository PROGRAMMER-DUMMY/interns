"""A KPI question can ask for two measures ("top 10 procedures AND the average
base cost", "...AND the number of times performed") while the registry's single
`metric` field keeps only one. parse_kpi recovers the dropped second measure
from the prose -- but only when its column is a real resolved feature (never a
phantom column that would emit invalid SQL).
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.result_view_builder import parse_kpi


def _kpi(metric: str, name: str, columns: list[str]) -> dict:
    # `features` drive the column lookup parse_kpi resolves measures against.
    return {
        "metric": metric,
        "cuts": "DESCRIPTION",
        "name": name,
        "features": [
            {"feature": c, "state": "proven_direct",
             "source_columns": [{"dataset": "procedures.csv", "column": c}]}
            for c in columns
        ],
    }


class SecondaryMeasureTests(unittest.TestCase):
    def _funcs(self, kpi):
        return [a.fn for a in parse_kpi(kpi).aggregations]

    def test_count_plus_average_emits_both(self):
        kpi = _kpi("avg(BASE_COST)",
                   "top 10 procedures with the highest average base cost and the "
                   "number of times they were performed",
                   ["DESCRIPTION", "BASE_COST"])
        funcs = self._funcs(kpi)
        self.assertIn("avg", funcs)
        self.assertIn("count", funcs)

    def test_average_secondary_needs_a_real_column(self):
        # count(*) primary + "average base cost" prose, but BASE_COST is NOT a
        # resolved feature -> the avg is skipped (no phantom-column SQL).
        kpi = _kpi("count(*)",
                   "top 10 most frequent procedures and the average base cost for each",
                   ["DESCRIPTION"])
        funcs = self._funcs(kpi)
        self.assertEqual(funcs.count("avg"), 0)
        self.assertIn("count", funcs)

    def test_average_secondary_emitted_when_column_present(self):
        kpi = _kpi("count(*)",
                   "top 10 most frequent procedures and the average base cost for each",
                   ["DESCRIPTION", "BASE_COST"])
        funcs = self._funcs(kpi)
        self.assertIn("avg", funcs)
        self.assertIn("count", funcs)

    def test_single_measure_kpi_unchanged(self):
        kpi = _kpi("count(distinct Id)", "How many total encounters occurred each year?",
                   ["Id"])
        self.assertEqual(len(parse_kpi(kpi).aggregations), 1)


if __name__ == "__main__":
    unittest.main()

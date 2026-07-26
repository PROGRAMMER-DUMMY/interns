"""Regression: a share metric must never be aggregated by sum.

Origin (2026-07-26 audit): the generated spec for a "percentage share" KPI
carried `y_format: percent` with `agg: sum` -- summing percentages. CLAUDE.md
names this a deterministic screener defect.

The interesting part is WHERE the bug was. `cuts.headline_agg()` has always
returned "max" for a share and was correct all along. The spec builder in
`inference.py` never called it: it hardcoded `"agg": "sum"` in every branch and
separately set `y_format: "percent"` when the metric was a share. The rule
existed and was not wired -- the same failure mode as the intent-coverage guard
that never ran and the regression tree that was never gated.

So these tests assert the SPEC BUILDER, not just the helper.
"""
from __future__ import annotations

import unittest

from core.dashboard.inference import infer_chart
from core.dashboard.model.cuts import headline_agg, measure_fmt

# The real KPI text from the audited workspace, typo included.
REAL_SHARE_METRIC = (
    "percentage of sum(distinct PatientID)     /   sum(disitnct PatientID) for departement"
)

SHARE_METRICS = [
    REAL_SHARE_METRIC,
    "percent share of lives by gender",
    "share of revenue by channel",
    "proportion of orders fulfilled",
]


class ShareHelperTests(unittest.TestCase):
    """The helper was already right; lock it so it stays right."""

    def test_share_metrics_aggregate_by_max_not_sum(self):
        for metric in SHARE_METRICS:
            with self.subTest(metric=metric):
                self.assertEqual(headline_agg(metric, "", ""), "max")

    def test_share_metrics_format_as_percent(self):
        for metric in SHARE_METRICS:
            with self.subTest(metric=metric):
                self.assertEqual(measure_fmt(metric, "", ""), "percent")

    def test_money_sum_still_sums(self):
        self.assertEqual(headline_agg("sum(PaidAmount)", "", ""), "sum")

    def test_count_still_sums(self):
        self.assertEqual(headline_agg("count(distinct order_id)", "", ""), "sum")


class ShareSpecBuilderTests(unittest.TestCase):
    """The actual defect: the builder emitted percent format with sum agg."""

    def _spec(self, metric: str, cuts: str, columns: list[str]) -> dict:
        return infer_chart(
            definition={"metric": metric, "cuts": cuts, "business_question": metric},
            result_columns=columns,
        )

    def test_share_spec_never_pairs_percent_format_with_sum(self):
        spec = self._spec(
            REAL_SHARE_METRIC,
            "Department Name, VisitType, Gender",
            ["department_name", "visittype", "gender", "percentage_share"],
        )
        if str(spec.get("y_format")) == "percent":
            self.assertNotEqual(
                spec.get("agg"), "sum",
                "a percent-formatted measure must not be summed: shares add to 100%",
            )

    def test_share_spec_with_a_date_cut_also_avoids_sum(self):
        # The date-cut branch builds a separate spec dict and had the same bug.
        spec = self._spec(
            "percent share of revenue by channel",
            "Month (order_date), Channel",
            ["month", "channel", "percentage_share"],
        )
        if str(spec.get("y_format")) == "percent":
            self.assertNotEqual(spec.get("agg"), "sum")

    def test_a_money_sum_kpi_still_sums(self):
        spec = self._spec(
            "sum(PaidAmount)",
            "LineOfBusiness, PayorID",
            ["lineofbusiness", "payorid", "sum_paidamount"],
        )
        self.assertNotEqual(str(spec.get("y_format")), "percent")
        self.assertEqual(spec.get("agg"), "sum")


if __name__ == "__main__":
    unittest.main()

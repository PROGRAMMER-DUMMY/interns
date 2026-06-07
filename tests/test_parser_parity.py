"""Parser parity lock: the SQL parser (result_view_builder.parse_kpi) and the
imperative-engine parser (kpi_intent.parse_intent) must agree on the *semantics*
of a KPI — metric, top-N, filters, and window/ratio classification.

The two intentionally differ in *representation* (SQL emits text expressions;
kpi_intent emits structured fields), but they must never diverge on what a KPI
*means*. This test fails if a future change makes one parser disagree with the
other — i.e. it keeps them a single source of truth in practice.
"""
import unittest

from core.onboarding.kpi.kpi_intent import parse_intent
from core.onboarding.kpi.result_view_builder import _detect_window_intent, parse_kpi


def _ds(col):
    return {"feature": col, "source_columns": [{"column": col}]}


CORPUS = [
    {"name": "total paid by month and lob", "metric": "sum(PaidAmount)",
     "cuts": "Month (ServiceDate), LineOfBusiness",
     "features": [_ds("PaidAmount"), _ds("ServiceDate"), _ds("LineOfBusiness")]},
    {"name": "top 10 payers in Commercial LOB", "metric": "sum(PaidAmount)",
     "cuts": "LineOfBusiness, PayorID",
     "features": [_ds("PaidAmount"), _ds("LineOfBusiness"), _ds("PayorID")]},
    {"name": "distinct patients by gender", "metric": "count(distinct PatientID)",
     "cuts": "Gender", "features": [_ds("PatientID"), _ds("Gender")]},
    {"name": "average paid by gender", "metric": "avg(PaidAmount)",
     "cuts": "Gender", "features": [_ds("PaidAmount"), _ds("Gender")]},
    {"name": "percentage share of lives for department",
     "metric": "percentage of sum(distinct PatientID) / sum(distinct PatientID) for department",
     "cuts": "department", "features": [{"feature": "department", "source_columns": [{"column": "Name"}]}]},
    {"name": "conversion rate", "metric": "sum(orders) / sum(sessions)",
     "cuts": "channel", "features": [_ds("orders"), _ds("sessions"), _ds("channel")]},
    {"name": "running total of revenue by month", "metric": "sum(revenue)",
     "cuts": "Month (event_date)", "features": [_ds("revenue"), _ds("event_date")]},
]


def _sql_plain_metric(kpi):
    parsed = parse_kpi(kpi)
    plain = [a for a in parsed.aggregations if a.window is None]
    if plain:
        return plain[0]
    return parsed.aggregations[0] if parsed.aggregations else None


class ParserParityTests(unittest.TestCase):
    def test_metric_fn_and_column_agree(self):
        for kpi in CORPUS:
            intent = parse_intent(kpi)
            if intent.share or intent.ratio or intent.unsupported_window:
                continue  # composite metrics compared in the window/ratio test
            sqlm = _sql_plain_metric(kpi)
            self.assertIsNotNone(sqlm, kpi["name"])
            self.assertIsNotNone(intent.metric, kpi["name"])
            self.assertEqual(intent.metric.fn, sqlm.fn, kpi["name"])
            self.assertEqual(intent.metric.column or "*", sqlm.column or "*", kpi["name"])
            self.assertEqual(intent.metric.distinct, sqlm.distinct, kpi["name"])

    def test_top_n_agrees(self):
        for kpi in CORPUS:
            self.assertEqual(parse_intent(kpi).top_n, parse_kpi(kpi).limit, kpi["name"])

    def test_window_and_ratio_classification_agrees(self):
        for kpi in CORPUS:
            intent = parse_intent(kpi)
            kind = _detect_window_intent(str(kpi.get("metric", "")), str(kpi.get("name", ""))).get("kind", "")
            if kind in {"percent_of_total", "percent_of_group", "mismatched_grain_percentage"}:
                self.assertIsNotNone(intent.share, kpi["name"])
                self.assertEqual(intent.share.kind, kind, kpi["name"])
            elif kind in {"running_total", "moving_average", "rank_within"}:
                self.assertEqual(intent.unsupported_window, kind, kpi["name"])
            elif "/" in str(kpi.get("metric", "")):
                self.assertIsNotNone(intent.ratio, kpi["name"])

    def test_literal_filter_values_agree(self):
        for kpi in CORPUS:
            intent = parse_intent(kpi)
            if intent.unsupported_window:
                continue
            sql_vals = {f.value.strip("'\"").lower() for f in parse_kpi(kpi).filters if f.is_literal}
            intent_vals = {
                f.value.strip("'\"").lower()
                for f in intent.filters if f.is_literal and f.target != "__age__"
            }
            self.assertEqual(intent_vals, sql_vals, f"{kpi['name']}: sql={sql_vals} intent={intent_vals}")


if __name__ == "__main__":
    unittest.main()

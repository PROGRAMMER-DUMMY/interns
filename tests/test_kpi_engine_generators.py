import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.kpi_intent import parse_intent
from core.onboarding.kpi.polars_generator import PolarsKPIGenerator
from core.onboarding.kpi.pyspark_generator import PySparkKPIGenerator


class KPIIntentTests(unittest.TestCase):
    def test_parses_metric_time_age_and_filters(self):
        kpi = {
            "kpi_id": "kpi_001",
            "name": "amount paid for Medicare LOB for patients above 50 years",
            "metric": "sum(PaidAmount)",
            "cuts": "Month (ServiceDate), LineOfBusiness, Age(DOB)",
            "features": [
                {"feature": "LineOfBusiness", "source_columns": [{"column": "LineOfBusiness"}]},
            ],
        }
        intent = parse_intent(kpi)
        self.assertIsNotNone(intent.metric)
        self.assertEqual(intent.metric.fn, "sum")
        self.assertEqual(intent.metric.column, "PaidAmount")
        kinds = {d.kind for d in intent.dims}
        self.assertIn("time", kinds)
        self.assertIn("age", kinds)
        self.assertIn("column", kinds)
        time_dim = next(d for d in intent.dims if d.kind == "time")
        self.assertEqual(time_dim.column, "ServiceDate")  # not the literal "Month"
        self.assertEqual(time_dim.unit, "month")
        age_dim = next(d for d in intent.dims if d.kind == "age")
        self.assertEqual(age_dim.column, "DOB")            # not the literal "Age"
        # name-derived filters: categorical LOB + age threshold
        self.assertTrue(any(f.target == "LineOfBusiness" and f.value.lower() == "medicare"
                            for f in intent.filters))
        self.assertTrue(any(f.target == "__age__" and f.value == "50" for f in intent.filters))

    def test_top_n_detected(self):
        intent = parse_intent({"name": "top 10 payers", "metric": "sum(amt)", "cuts": "payer"})
        self.assertEqual(intent.top_n, 10)

    def test_mismatched_grain_percentage_share(self):
        kpi = {
            "name": "percentage share of lives by department",
            "metric": "percentage of sum(distinct PatientID) / sum(distinct PatientID) for department",
            "cuts": "department",
            "features": [{"feature": "department", "source_columns": [{"column": "Name"}]}],
        }
        intent = parse_intent(kpi)
        self.assertIsNotNone(intent.share)
        self.assertEqual(intent.share.kind, "mismatched_grain_percentage")
        self.assertEqual(intent.share.partition, "Name")   # resolved via feature lookup
        self.assertTrue(intent.share.metric.distinct)

    def test_ratio_metric(self):
        intent = parse_intent({"name": "conversion", "metric": "sum(orders) / sum(sessions)", "cuts": "channel"})
        self.assertIsNotNone(intent.ratio)
        self.assertEqual(intent.ratio[0].column, "orders")
        self.assertEqual(intent.ratio[1].column, "sessions")

    def test_unsupported_window_flagged(self):
        intent = parse_intent({"name": "running total of revenue by month", "metric": "sum(revenue)", "cuts": "month"})
        self.assertEqual(intent.unsupported_window, "running_total")


class EngineGeneratorTests(unittest.TestCase):
    def _workspace(self, tmp: Path, kpi: dict) -> Path:
        ws = tmp / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        profiles = ws / "interns" / "generated" / "profiles"
        contracts.mkdir(parents=True, exist_ok=True)
        profiles.mkdir(parents=True, exist_ok=True)
        (contracts / "kpi_feature_mapping.json").write_text(json.dumps({"kpis": [kpi]}), encoding="utf-8")
        (profiles / "profile_index.json").write_text(
            json.dumps({"profiles": [{
                "path": "workspaces/demo/datasets/sales.csv",
                "format": "csv",
                "schema": {"region": {}, "amount": {}, "order_date": {}},
            }]}),
            encoding="utf-8",
        )
        (ws / "datasets").mkdir(parents=True, exist_ok=True)
        (ws / "datasets" / "sales.csv").write_text(
            "region,amount,order_date\nNA,10,2024-01-05\nNA,5,2024-02-10\nEU,20,2024-01-20\n",
            encoding="utf-8",
        )
        return tmp

    def _single_source_kpi(self) -> dict:
        ds = "workspaces/demo/datasets/sales.csv"
        return {
            "kpi_id": "kpi_001",
            "name": "total amount by region and month",
            "metric": "sum(amount)",
            "cuts": "region, Month(order_date)",
            "features": [
                {"feature": "amount", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "amount"}]},
                {"feature": "region", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "region"}]},
                {"feature": "order_date", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "order_date"}]},
            ],
        }

    def test_polars_script_is_valid_and_bugfree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._workspace(tmp, self._single_source_kpi())
            result = PolarsKPIGenerator(tmp, "workspaces/demo").generate("kpi_001")
            src = (tmp / result.path).read_text(encoding="utf-8")
            compile(src, result.path, "exec")          # must be valid Python
            self.assertNotIn("pl.today(", src)          # the old fatal bug
            self.assertNotIn('pl.col("Month")', src)    # phantom time column bug
            self.assertIn("dt.truncate", src)           # real time bucket
            self.assertIn("order_date", src)            # real date column
            self.assertIn("parents[5]", src)            # portable anchor, not abs path
            self.assertNotIn(":\\", src)                # no Windows absolute paths baked in

    def test_pyspark_script_is_valid_and_applies_ops_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._workspace(tmp, self._single_source_kpi())
            result = PySparkKPIGenerator(tmp, "workspaces/demo").generate("kpi_001")
            src = (tmp / result.path).read_text(encoding="utf-8")
            compile(src, result.path, "exec")
            self.assertIn('spark.read.format("delta")', src)   # typed read, no infer on hot path
            self.assertIn("date_trunc", src)
            self.assertIn("parents[5]", src)

    def test_derived_formula_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            kpi = self._single_source_kpi()
            kpi["features"].append(
                {"feature": "margin", "state": "proven_direct", "resolution_type": "derived_formula",
                 "source_columns": [{"dataset": "workspaces/demo/datasets/sales.csv", "column": "amount"}]}
            )
            self._workspace(tmp, kpi)
            with self.assertRaises(ValueError):
                PolarsKPIGenerator(tmp, "workspaces/demo").generate("kpi_001")

    def test_unsupported_window_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            kpi = self._single_source_kpi()
            kpi["name"] = "running total of amount by region"
            self._workspace(tmp, kpi)
            with self.assertRaises(ValueError):
                PolarsKPIGenerator(tmp, "workspaces/demo").generate("kpi_001")
            with self.assertRaises(ValueError):
                PySparkKPIGenerator(tmp, "workspaces/demo").generate("kpi_001")

    def test_distinct_share_renders_single_attribution_in_both_engines(self):
        # A DISTINCT-entity share mirrors the SQL single-attribution semantics
        # in every engine: one row (= one grain cell) per entity, then a plain
        # group count — never the overlapping per-cell window count (which made
        # shares total >100%).
        ds = "workspaces/demo/datasets/sales.csv"
        kpi = {
            "kpi_id": "kpi_001",
            "name": "percentage share of amount by region",
            "metric": "percentage of sum(distinct amount) / sum(distinct amount) for region",
            "cuts": "region",
            "features": [
                {"feature": "amount", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "amount"}]},
                {"feature": "region", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "region"}]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._workspace(tmp, kpi)
            pol = PolarsKPIGenerator(tmp, "workspaces/demo").generate("kpi_001")
            psrc = (tmp / pol.path).read_text(encoding="utf-8")
            compile(psrc, pol.path, "exec")
            self.assertIn("percentage_share", psrc)
            self.assertIn('unique(subset=["amount"], keep="first"', psrc)
            self.assertNotIn("n_unique().over(", psrc)
            spk = PySparkKPIGenerator(tmp, "workspaces/demo").generate("kpi_001")
            ssrc = (tmp / spk.path).read_text(encoding="utf-8")
            compile(ssrc, spk.path, "exec")
            self.assertIn("percentage_share", ssrc)
            self.assertIn('Window.partitionBy("amount")', ssrc)
            self.assertIn("F.row_number()", ssrc)
            self.assertNotIn("F.collect_set", ssrc)


if __name__ == "__main__":
    unittest.main()

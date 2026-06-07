import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.generate_kpi_engines import (
    KPIMultiEngineGenerator,
    expand_engine_mode,
)


class ExpandEngineModeTests(unittest.TestCase):
    def test_sql_default(self):
        self.assertEqual(expand_engine_mode("sql", "polars"), ["sql"])

    def test_all_expands_to_three(self):
        self.assertEqual(expand_engine_mode("all", "sql"), ["sql", "polars", "pyspark"])

    def test_comma_list_exact(self):
        self.assertEqual(expand_engine_mode("sql,polars", "pyspark"), ["sql", "polars"])

    def test_recommended_adds_recommended_to_sql(self):
        self.assertEqual(expand_engine_mode("recommended", "pyspark"), ["sql", "pyspark"])

    def test_recommended_sql_stays_sql_only(self):
        self.assertEqual(expand_engine_mode("recommended", "sql"), ["sql"])

    def test_hybrid_recommended_expands_to_sql_polars(self):
        self.assertEqual(expand_engine_mode("recommended", "hybrid"), ["sql", "polars"])

    def test_unknown_label_raises(self):
        with self.assertRaises(ValueError):
            expand_engine_mode("nonsense", "sql")


class MultiEngineGeneratorTests(unittest.TestCase):
    def _workspace(self, tmp: Path, kpis: list[dict]) -> Path:
        ws = tmp / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        profiles = ws / "interns" / "generated" / "profiles"
        contracts.mkdir(parents=True, exist_ok=True)
        profiles.mkdir(parents=True, exist_ok=True)
        (contracts / "kpi_feature_mapping.json").write_text(
            json.dumps({"kpis": kpis}), encoding="utf-8"
        )
        (profiles / "profile_index.json").write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "path": "workspaces/demo/datasets/sales.csv",
                            "format": "csv",
                            "schema": {"region": {}, "amount": {}, "order_date": {}},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (ws / "datasets").mkdir(parents=True, exist_ok=True)
        (ws / "datasets" / "sales.csv").write_text(
            "region,amount,order_date\nNA,10,2024-01-05\nNA,5,2024-02-10\nEU,20,2024-01-20\n",
            encoding="utf-8",
        )
        return tmp

    def _kpi(self, kpi_id: str = "kpi_001") -> dict:
        ds = "workspaces/demo/datasets/sales.csv"
        return {
            "kpi_id": kpi_id,
            "name": "total amount by region and month",
            "metric": "sum(amount)",
            "cuts": "region, Month(order_date)",
            "features": [
                {
                    "feature": "amount",
                    "state": "proven_direct",
                    "source_columns": [{"dataset": ds, "column": "amount"}],
                },
                {
                    "feature": "region",
                    "state": "proven_direct",
                    "source_columns": [{"dataset": ds, "column": "region"}],
                },
                {
                    "feature": "order_date",
                    "state": "proven_direct",
                    "source_columns": [{"dataset": ds, "column": "order_date"}],
                },
            ],
        }

    def _solutions_dir(self, tmp: Path) -> Path:
        return tmp / "workspaces" / "demo" / "interns" / "generated" / "solutions"

    def _report(self, tmp: Path) -> dict:
        report = (
            tmp
            / "workspaces"
            / "demo"
            / "interns"
            / "reports"
            / "engine_generation"
            / "current.json"
        )
        self.assertTrue(report.exists(), "summary report JSON must be written")
        return json.loads(report.read_text(encoding="utf-8"))

    def test_sql_writes_only_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._workspace(tmp, [self._kpi()])
            KPIMultiEngineGenerator(tmp, "workspaces/demo").write(engine="sql")
            sols = self._solutions_dir(tmp)
            self.assertTrue((sols / "kpi_001.sql").exists())
            self.assertFalse((sols / "kpi_001_polars.py").exists())
            self.assertFalse((sols / "kpi_001_pyspark.py").exists())

    def test_all_writes_sql_polars_pyspark(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._workspace(tmp, [self._kpi()])
            KPIMultiEngineGenerator(tmp, "workspaces/demo").write(engine="all")
            sols = self._solutions_dir(tmp)
            # Files are generated; we never EXECUTE the PySpark (no JDK/Spark here).
            self.assertTrue((sols / "kpi_001.sql").exists())
            self.assertTrue((sols / "kpi_001_polars.py").exists())
            self.assertTrue((sols / "kpi_001_pyspark.py").exists())

    def test_recommended_writes_at_least_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._workspace(tmp, [self._kpi()])
            KPIMultiEngineGenerator(tmp, "workspaces/demo").write(engine="recommended")
            self.assertTrue((self._solutions_dir(tmp) / "kpi_001.sql").exists())

    def test_report_lists_engines_and_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._workspace(tmp, [self._kpi()])
            KPIMultiEngineGenerator(tmp, "workspaces/demo").write(engine="all")
            payload = self._report(tmp)
            self.assertEqual(payload["engine_mode"], "all")
            self.assertEqual(payload["default_engine"], "sql")
            self.assertEqual(len(payload["kpis"]), 1)
            entry = payload["kpis"][0]
            self.assertEqual(entry["kpi_id"], "kpi_001")
            self.assertIn("sql", entry["engines_generated"])
            self.assertIn("recommended_engine", entry)
            self.assertIn("recommendation_reasons", entry)
            self.assertTrue(entry["recommendation_reasons"])  # recommender carries a reason
            self.assertTrue(entry["files"])  # generated file paths recorded
            # markdown report also written
            md = (
                tmp
                / "workspaces"
                / "demo"
                / "interns"
                / "reports"
                / "engine_generation"
                / "current.md"
            )
            self.assertTrue(md.exists())

    def test_skip_is_recorded_not_crashed(self):
        # A running-total KPI is unsupported by Polars/PySpark generators, but SQL
        # still renders. The run must record the skips with reasons, not crash.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            kpi = self._kpi()
            kpi["name"] = "running total of amount by region"
            self._workspace(tmp, [kpi])
            KPIMultiEngineGenerator(tmp, "workspaces/demo").write(engine="all")
            payload = self._report(tmp)
            entry = payload["kpis"][0]
            skipped = [o for o in entry["outcomes"] if o["status"] == "skipped"]
            self.assertTrue(skipped, "unsupported engines should be skipped, not crash")
            for outcome in skipped:
                self.assertTrue(outcome["reason"])


if __name__ == "__main__":
    unittest.main()

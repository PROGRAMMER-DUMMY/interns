"""End-to-end PySpark parity — runs ONLY in CI (env CI_RUN_SPARK=1) on a box with
JDK 17 + a Spark-3.5/Delta-3.2 (Scala 2.12) toolchain. Locally it is skipped, since
this developer box and most dev machines lack a working Spark runtime.

It builds a tiny single-source workspace, generates SQL + Polars + PySpark for one
KPI, and asserts the cross-engine gate confirms SQL == Polars == PySpark exactly.
This is the CI pin for PySpark parity.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("CI_RUN_SPARK") == "1", "Spark parity runs only in CI (CI_RUN_SPARK=1)")
class CIPySparkParityTests(unittest.TestCase):
    def _workspace(self, root: Path) -> None:
        ws = root / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        profiles = ws / "interns" / "generated" / "profiles"
        contracts.mkdir(parents=True, exist_ok=True)
        profiles.mkdir(parents=True, exist_ok=True)
        (ws / "datasets").mkdir(parents=True, exist_ok=True)
        (ws / "datasets" / "sales.csv").write_text(
            "region,amount\nNA,10\nNA,5\nEU,20\nEU,7\nAPAC,3\n", encoding="utf-8"
        )
        ds = "workspaces/demo/datasets/sales.csv"
        (profiles / "profile_index.json").write_text(json.dumps({"profiles": [{
            "path": ds, "format": "csv", "row_count": 5,
            "schema": {"region": {}, "amount": {}},
        }]}), encoding="utf-8")
        (contracts / "kpi_feature_mapping.json").write_text(json.dumps({"kpis": [{
            "kpi_id": "kpi_001", "name": "total amount by region",
            "metric": "sum(amount)", "cuts": "region",
            "features": [
                {"feature": "amount", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "amount"}]},
                {"feature": "region", "state": "proven_direct",
                 "source_columns": [{"dataset": ds, "column": "region"}]},
            ],
        }]}), encoding="utf-8")

    def test_sql_polars_pyspark_agree(self):
        from core.onboarding.kpi.polars_generator import PolarsKPIGenerator
        from core.onboarding.kpi.pyspark_generator import PySparkKPIGenerator
        from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator
        from core.onboarding.kpi.verify_kpi_output import KPIOutputVerifier

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            DuckDBKPISQLGenerator(root, "workspaces/demo").generate("kpi_001")
            PolarsKPIGenerator(root, "workspaces/demo").generate("kpi_001")
            PySparkKPIGenerator(root, "workspaces/demo").generate("kpi_001")

            result = KPIOutputVerifier(root, "workspaces/demo", cross_engine=True).verify(kpi_id="kpi_001")
            record = result.records[0]
            checks = " ".join(record.intent_checks)
            self.assertTrue(record.ok, f"verify failed: {record.errors}")
            self.assertIn("SQL == Polars", checks)
            self.assertIn("SQL == PySpark", checks)


if __name__ == "__main__":
    unittest.main()

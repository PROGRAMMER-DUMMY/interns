import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.verify_kpi_output import KPIOutputVerifier


def _has_duckdb() -> bool:
    try:
        import duckdb  # noqa: F401
        return True
    except ImportError:
        return False


class VerifyKPIOutputTests(unittest.TestCase):
    def _workspace(self, tmp: Path, kpi: dict) -> KPIOutputVerifier:
        ws = tmp / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        contracts.mkdir(parents=True, exist_ok=True)
        (contracts / "kpi_feature_mapping.json").write_text(
            json.dumps({"kpis": [kpi]}), encoding="utf-8"
        )
        # a tiny source CSV the generated SQL can read
        (tmp / "sales.csv").write_text(
            "region,amount\nNA,10\nNA,5\nEU,20\n", encoding="utf-8"
        )
        return KPIOutputVerifier(tmp, "workspaces/demo")

    def test_passing_kpi_executes_and_aligns(self):
        if not _has_duckdb():
            self.skipTest("duckdb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            verifier = self._workspace(
                tmp,
                {"kpi_id": "kpi_001", "name": "total amount by region",
                 "metric": "sum(amount)", "cuts": "region"},
            )
            sql = (
                'CREATE OR REPLACE VIEW "kpi_001_features" AS '
                "SELECT region, amount FROM read_csv_auto('sales.csv');\n"
                'CREATE OR REPLACE VIEW "kpi_001_results" AS '
                'SELECT region, SUM(amount) AS sum_amount FROM "kpi_001_features" '
                "GROUP BY region;"
            )
            record = verifier.verify_sql_text("kpi_001", sql)
            self.assertTrue(record.ok, record.errors)
            self.assertTrue(record.executed)
            self.assertEqual(record.row_count, 2)

    def test_garbage_filter_literal_is_blocked(self):
        if not _has_duckdb():
            self.skipTest("duckdb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            verifier = self._workspace(
                tmp,
                {"kpi_id": "kpi_001", "name": "total amount by region for NA",
                 "metric": "sum(amount)", "cuts": "region"},
            )
            # filter literal is a fragment of the KPI prose, not a real value
            sql = (
                'CREATE OR REPLACE VIEW "kpi_001_features" AS '
                "SELECT region, amount FROM read_csv_auto('sales.csv');\n"
                'CREATE OR REPLACE VIEW "kpi_001_results" AS '
                'SELECT region, SUM(amount) AS sum_amount FROM "kpi_001_features" '
                "WHERE region = 'total amount by region for' GROUP BY region;"
            )
            record = verifier.verify_sql_text("kpi_001", sql)
            self.assertFalse(record.ok)
            self.assertTrue(
                any("suspicious filter literal" in e for e in record.errors),
                record.errors,
            )

    def test_unresolved_cut_is_blocked(self):
        if not _has_duckdb():
            self.skipTest("duckdb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # cut "Region Name" does not resolve to any column present in SQL
            verifier = self._workspace(
                tmp,
                {"kpi_id": "kpi_001", "name": "total amount by region",
                 "metric": "sum(amount)", "cuts": "Region Name"},
            )
            sql = (
                'CREATE OR REPLACE VIEW "kpi_001_features" AS '
                "SELECT region, amount FROM read_csv_auto('sales.csv');\n"
                'CREATE OR REPLACE VIEW "kpi_001_results" AS '
                'SELECT SUM(amount) AS sum_amount FROM "kpi_001_features";'
            )
            record = verifier.verify_sql_text("kpi_001", sql)
            self.assertFalse(record.ok)
            self.assertTrue(any("did not resolve" in e for e in record.errors), record.errors)

    def test_missing_metric_is_blocked(self):
        if not _has_duckdb():
            self.skipTest("duckdb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            verifier = self._workspace(
                tmp,
                {"kpi_id": "kpi_001", "name": "total amount by region",
                 "metric": "sum(amount)", "cuts": "region"},
            )
            # result view sums nothing — metric not implemented
            sql = (
                'CREATE OR REPLACE VIEW "kpi_001_features" AS '
                "SELECT region, amount FROM read_csv_auto('sales.csv');\n"
                'CREATE OR REPLACE VIEW "kpi_001_results" AS '
                'SELECT region FROM "kpi_001_features" GROUP BY region;'
            )
            record = verifier.verify_sql_text("kpi_001", sql)
            self.assertFalse(record.ok)
            self.assertTrue(any("not implemented" in e for e in record.errors), record.errors)


if __name__ == "__main__":
    unittest.main()

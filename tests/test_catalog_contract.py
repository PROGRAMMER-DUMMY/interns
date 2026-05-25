from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from core.onboarding.catalog_contract import CatalogContractBuilder
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class CatalogContractTests(unittest.TestCase):
    def _create_workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        (workspace / "datasets").mkdir(parents=True)
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets" / "transactions.csv").write_text(
            "ClaimID,PaidAmount,LineOfBusiness\n"
            "C1,10.50,Commercial\n"
            "C2,20.25,Medicare\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),Confirm paid amount source\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "data_model.md").write_text(
            "# Data Model\n\ntransactions has ClaimID, PaidAmount, LineOfBusiness.\n",
            encoding="utf-8",
        )
        return workspace

    def test_builds_catalog_contract_from_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            result = CatalogContractBuilder(root, "workspaces/demo").build()

            self.assertEqual(result.object_count, 1)
            contract = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            obj = contract["objects"][0]
            self.assertEqual(obj["logical_name"], "raw.transactions")
            self.assertEqual(obj["layer"], "raw")
            self.assertEqual(obj["dataset"], "workspaces/demo/datasets/transactions.csv")
            self.assertIn("profile_path", obj)
            binding_types = {binding["binding_type"] for binding in obj["physical_bindings"]}
            self.assertEqual(binding_types, {"local_file", "duckdb_view"})

    def test_duckdb_sql_uses_catalog_bootstrap_before_business_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            result = DuckDBKPISQLGenerator(root, "workspaces/demo").generate("kpi_001")

            sql = (root / result.path).read_text(encoding="utf-8")
            self.assertIn("-- BEGIN CATALOG BOOTSTRAP", sql)
            self.assertIn('"catalog_raw_transactions"', sql)
            self.assertIn("read_csv_auto('workspaces/demo/datasets/transactions.csv'", sql)
            business_sql = re.sub(
                r"--\s*BEGIN CATALOG BOOTSTRAP\b.*?--\s*END CATALOG BOOTSTRAP\b",
                "",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            self.assertNotIn("read_csv_auto", business_sql)
            self.assertIn('FROM "catalog_raw_transactions"', business_sql)


if __name__ == "__main__":
    unittest.main()

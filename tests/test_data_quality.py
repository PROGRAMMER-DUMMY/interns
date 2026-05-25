from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.catalog_contract import CatalogContractBuilder
from core.onboarding.data_quality import DataQualityHarness, DuplicateDecisionRecorder, DuplicateReviewPanel
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class DataQualityDuplicateReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed")

    def _workspace_with_duplicates(self, root: Path) -> None:
        workspace = root / "workspaces" / "demo"
        (workspace / "datasets").mkdir(parents=True)
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets" / "transactions.csv").write_text(
            "TransactionID,PatientID,ServiceDate,PayorID,DeptID,PaidAmount,ClaimID,ModifiedDate\n"
            "T1,P1,2024-01-01,PAY1,D1,10.00,C1,2024-01-03\n"
            "T1,P1,2024-01-01,PAY1,D1,10.00,C1,2024-01-04\n"
            "T2,P2,2024-01-02,PAY2,D2,20.00,C2,2024-01-05\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "kpi_registry.csv").write_text(
            "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
            "What is paid amount?,Baseline KPI,PayorID,sum(PaidAmount),Confirm source\n",
            encoding="utf-8",
        )
        (workspace / "docs" / "data_model.md").write_text(
            "# Data Model\n\ntransactions has TransactionID, PatientID, ServiceDate, PayorID, DeptID, PaidAmount, ClaimID.\n",
            encoding="utf-8",
        )

    def _prepare(self, root: Path) -> None:
        self._workspace_with_duplicates(root)
        WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
        CatalogContractBuilder(root, "workspaces/demo").build()

    def test_data_quality_harness_detects_duplicate_and_redacts_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare(root)

            result = DataQualityHarness(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertEqual(result.status, "blocked")
            self.assertGreaterEqual(result.finding_count, 1)
            report = json.loads((root / result.evidence_path).read_text(encoding="utf-8"))
            self.assertEqual(report["artifact_type"], "data_quality_harness/current.json")
            first = report["findings"][0]
            self.assertEqual(first["status"], "unresolved")
            self.assertIn("SELECT", first["query"])
            self.assertIn("<redacted>", first["sample_output_table"])
            self.assertNotIn("T1", first["sample_output_table"])
            self.assertNotIn("P1", first["sample_output_table"])
            contract = json.loads((root / result.contract_path).read_text(encoding="utf-8"))
            self.assertEqual(contract["artifact_type"], "data_quality_contract.json")
            self.assertFalse(contract["policy"]["duplicate_policy"]["sql_mutation_in_milestone_1"])

    def test_duplicate_review_panel_shows_query_result_and_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare(root)
            DataQualityHarness(root, "workspaces/demo").run()

            result = DuplicateReviewPanel(root, "workspaces/demo").prepare()

            self.assertEqual(result.status, "needs_user_answer")
            panel = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["artifact_type"], "duplicate_review/current.json")
            self.assertIn("query", panel)
            self.assertIn("sample_output_table", panel)
            self.assertEqual([option["option_id"] for option in panel["options"]], ["option_a", "option_b", "option_c"])
            markdown = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertIn("## Detection Query", markdown)
            self.assertIn("## Redacted Result Sample", markdown)

    def test_apply_duplicate_review_answer_records_decision_and_unblocks_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare(root)
            DataQualityHarness(root, "workspaces/demo").run()
            DuplicateReviewPanel(root, "workspaces/demo").prepare()

            result = DuplicateDecisionRecorder(root, "workspaces/demo").apply("option_a")

            decision_path = root / result["decision_path"]
            decisions = json.loads(decision_path.read_text(encoding="utf-8"))
            self.assertEqual(decisions["artifact_type"], "duplicate_decisions.json")
            self.assertEqual(decisions["decisions"][0]["action"], "preserve")
            self.assertFalse(decisions["decisions"][0]["approved_for_sql_mutation"])
            self.assertEqual(result["data_quality"]["finding_count"], result["next_panel"]["finding_count"])
            self.assertLess(
                result["data_quality"]["unresolved_finding_count"],
                result["data_quality"]["finding_count"],
            )


if __name__ == "__main__":
    unittest.main()

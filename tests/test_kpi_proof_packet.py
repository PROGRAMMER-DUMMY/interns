from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.proof_packet import KPIProofPacketBuilder


class KPIProofPacketTests(unittest.TestCase):
    def test_recommend_packet_writes_markdown_and_json_without_applying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "Demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            evidence = workspace / "interns" / "generated" / "evidence"
            contracts.mkdir(parents=True)
            evidence.mkdir(parents=True)
            (contracts / "kpi_registry.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "kpi_registry.json",
                        "kpis": [
                            {
                                "name": "What is paid amount by month?",
                                "description": "Paid amount trend.",
                                "metric": "sum(PaidAmount)",
                                "cuts": "Month(ServiceDate)",
                                "source": "demo.xlsx",
                                "status": "needs_mapping",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "kpi_feature_mapping.json",
                        "version": 2,
                        "workspace": "workspaces/Demo",
                        "summary": {
                            "kpi_count": 1,
                            "ready_kpi_count": 1,
                            "blocked_kpi_count": 0,
                            "unresolved_feature_count": 0,
                        },
                        "blocker_clusters": [],
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "name": "What is paid amount by month?",
                                "metric": "sum(PaidAmount)",
                                "cuts": "Month(ServiceDate)",
                                "status": "ready_for_sql",
                                "features": [
                                    {
                                        "feature": "PaidAmount",
                                        "state": "proven_direct",
                                        "resolution_type": "direct_column",
                                        "source_columns": [
                                            {
                                                "dataset": str(workspace / "transactions.csv"),
                                                "column": "PaidAmount",
                                                "profile_path": "workspaces/Demo/interns/generated/profiles/transactions.csv.profile.json",
                                                "observed_values": [100, 125],
                                            }
                                        ],
                                    }
                                ],
                                "open_questions": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = KPIProofPacketBuilder(root, "workspaces/Demo", domain="healthcare").run()

            self.assertEqual(result.mode, "recommend")
            self.assertEqual(result.kpi_count, 1)
            report = root / result.report_path
            payload = json.loads((root / result.report_json_path).read_text(encoding="utf-8"))
            text = report.read_text(encoding="utf-8")
            self.assertIn("KPI: kpi_001", text)
            self.assertIn("Source Row Traceability", text)
            self.assertIn("Final Column Mapping / Recommendations", text)
            self.assertIn("Reliability Gates", text)
            self.assertEqual(payload["mode"], "recommend")
            self.assertEqual(payload["kpis"][0]["recommendation_class"], "auto-acceptable")

    def test_cli_rejects_non_recommend_modes_in_first_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "Demo").mkdir(parents=True)
            with self.assertRaises(ValueError):
                KPIProofPacketBuilder(root, "workspaces/Demo", mode="execute")


if __name__ == "__main__":
    unittest.main()

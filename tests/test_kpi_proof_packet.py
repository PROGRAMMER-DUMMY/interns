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

    def test_packet_includes_catalog_route_pipeline_and_layered_harness_evidence_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "Demo"
            contracts = workspace / "interns" / "generated" / "contracts"
            evidence = workspace / "interns" / "generated" / "evidence"
            pipeline_execution = evidence / "pipeline_execution_harness"
            data_quality = evidence / "data_quality_harness"
            duplicate_review = workspace / "interns" / "reports" / "duplicate_review"
            layered_report = workspace / "interns" / "reports" / "layered_pipeline_harness"
            contracts.mkdir(parents=True)
            evidence.mkdir(parents=True)
            pipeline_execution.mkdir(parents=True)
            data_quality.mkdir(parents=True)
            duplicate_review.mkdir(parents=True)
            layered_report.mkdir(parents=True)
            (contracts / "kpi_registry.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "kpi_registry.json",
                        "kpis": [{"name": "What is paid amount?", "metric": "sum(PaidAmount)"}],
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "kpi_feature_mapping.json",
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "name": "What is paid amount?",
                                "metric": "sum(PaidAmount)",
                                "features": [
                                    {
                                        "feature": "PaidAmount",
                                        "state": "proven_direct",
                                        "resolution_type": "direct_column",
                                        "source_columns": [
                                            {"dataset": str(workspace / "transactions.csv"), "column": "PaidAmount"}
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "catalog_contract.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "catalog_contract.json",
                        "workspace": "workspaces/Demo",
                        "policy": {},
                        "objects": [{"logical_name": "raw.transactions"}],
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "data_engineering_route.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "data_engineering_route.json",
                        "workspace": "workspaces/Demo",
                        "selected_track": "medallion",
                        "catalog_object_count": 1,
                        "status": "ready_for_pipeline_plan",
                    }
                ),
                encoding="utf-8",
            )
            (contracts / "pipeline_plan.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_plan.json",
                        "workspace": "workspaces/Demo",
                        "selected_track": "medallion",
                        "target_engine": "sql",
                        "table_format": "local_parquet",
                        "policy": {},
                        "layers": [],
                        "quality_gates": [],
                        "blockers": [
                            {
                                "type": "percentage_denominator_scope_unresolved",
                                "message": "Denominator scope is unresolved.",
                            }
                        ],
                        "status": "blocked",
                    }
                ),
                encoding="utf-8",
            )
            (layered_report / "current.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "layered_pipeline_harness/current.json",
                        "ok": False,
                        "status": "failed",
                        "summary": {"catalog_objects": 1, "track": "medallion"},
                        "findings": [
                            {
                                "severity": "error",
                                "code": "pipeline_plan_blocked",
                                "message": "Pipeline plan status is blocked.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pipeline_execution / "current.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "pipeline_execution_harness/current.json",
                        "ok": False,
                        "status": "failed",
                        "summary": {"passed_count": 2, "failed_count": 1},
                    }
                ),
                encoding="utf-8",
            )
            (data_quality / "current.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "data_quality_harness/current.json",
                        "ok": False,
                        "status": "failed",
                        "summary": {"passed_count": 3, "failed_count": 1},
                    }
                ),
                encoding="utf-8",
            )
            (duplicate_review / "current.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "duplicate_review/current.json",
                        "summary": {"duplicate_finding_count": 2, "unresolved_finding_count": 1},
                    }
                ),
                encoding="utf-8",
            )

            result = KPIProofPacketBuilder(root, "workspaces/Demo", domain="healthcare").run()

            payload = json.loads((root / result.report_json_path).read_text(encoding="utf-8"))
            evidence_payload = payload["data_engineering_evidence"]
            self.assertEqual(evidence_payload["catalog_object_count"], 1)
            self.assertEqual(evidence_payload["selected_track"], "medallion")
            self.assertEqual(evidence_payload["table_format"], "local_parquet")
            self.assertEqual(evidence_payload["pipeline_status"], "blocked")
            self.assertEqual(evidence_payload["layered_harness_status"], "failed")
            self.assertEqual(evidence_payload["pipeline_execution_harness"]["status"], "failed")
            self.assertEqual(evidence_payload["pipeline_execution_harness"]["passed"], 2)
            self.assertEqual(evidence_payload["pipeline_execution_harness"]["failed"], 1)
            self.assertEqual(evidence_payload["data_quality_harness"]["status"], "failed")
            self.assertEqual(evidence_payload["data_quality_harness"]["duplicate_finding_count"], 2)
            self.assertEqual(evidence_payload["data_quality_harness"]["unresolved_finding_count"], 1)
            self.assertTrue(payload["artifacts"]["pipeline_execution_harness"]["exists"])
            self.assertTrue(payload["artifacts"]["data_quality_harness"]["exists"])
            self.assertTrue(payload["artifacts"]["duplicate_review"]["exists"])
            self.assertIn(
                "percentage_denominator_scope_unresolved: Denominator scope is unresolved.",
                evidence_payload["blockers"],
            )
            self.assertIn(
                "pipeline_plan_blocked: Pipeline plan status is blocked.",
                evidence_payload["blockers"],
            )
            text = (root / result.report_path).read_text(encoding="utf-8")
            self.assertIn("Data Engineering Evidence", text)
            self.assertIn("local_parquet", text)
            self.assertIn("pipeline_plan_blocked", text)
            self.assertIn("2 passed / 1 failed", text)
            self.assertIn("2 duplicate / 1 unresolved", text)


if __name__ == "__main__":
    unittest.main()

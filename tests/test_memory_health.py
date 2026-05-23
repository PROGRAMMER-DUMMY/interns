from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.memory.health import MemoryHealthValidator, main


class MemoryHealthTests(unittest.TestCase):
    def test_writes_health_report_for_workspace_and_team_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            memory_dir = workspace / "interns" / "generated" / "memory"
            team_dir = root / "state" / "team_memory"
            memory_dir.mkdir(parents=True)
            team_dir.mkdir(parents=True)

            (memory_dir / "workspace_memory.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "workspace_memory",
                        "definitions": [
                            {
                                "memory_id": "feature__paid_amount",
                                "feature": "PaidAmount",
                                "status": "user_confirmed",
                                "confidence": 0.93,
                                "last_verified": "2026-05-01T00:00:00+00:00",
                                "expires_if": "payer contract changes",
                                "evidence": [
                                    {
                                        "path": "workspaces/demo/interns/generated/contracts/kpi_feature_mapping.json"
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (team_dir / "team_memory.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "team_memory",
                        "definitions": [
                            {
                                "memory_id": "kpi__denial_rate",
                                "canonical_name": "Denial Rate",
                                "approval_state": "approved",
                                "confidence": 88,
                                "sources": [{"path": "workspaces/other/docs/kpi_registry.json"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = MemoryHealthValidator(root, "workspaces/demo").run()

            self.assertTrue(result.ok)
            self.assertEqual(result.status, "healthy")
            self.assertEqual(result.entry_count, 2)
            self.assertEqual(result.average_confidence, 0.905)
            report_path = root / result.current_json_path
            markdown_path = root / result.current_markdown_path
            evidence_path = root / result.evidence_path
            self.assertTrue(report_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertTrue(evidence_path.exists())

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["source_count"], 2)
            self.assertEqual(report["summary"]["critical_count"], 0)
            self.assertEqual(
                sorted(entry["scope"] for entry in report["entries"]),
                ["team", "workspace"],
            )
            paid = next(entry for entry in report["entries"] if entry["label"] == "PaidAmount")
            self.assertEqual(paid["status"], "user_confirmed")
            self.assertEqual(paid["confidence_band"], "high")
            self.assertEqual(paid["evidence_count"], 1)

    def test_invalid_json_and_low_confidence_are_reported_as_critical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            memory_dir = workspace / "interns" / "generated" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "bad.json").write_text("{bad json", encoding="utf-8")
            (memory_dir / "blocked.json").write_text(
                json.dumps(
                    {
                        "definitions": [
                            {
                                "feature": "FacilityID",
                                "state": "blocked_missing_evidence",
                                "confidence": 0.2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = MemoryHealthValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertEqual(result.status, "critical")
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            codes = {finding["code"] for finding in report["findings"]}
            self.assertIn("invalid_json", codes)
            self.assertIn("unhealthy_memory_entry", codes)

    def test_cli_returns_nonzero_for_critical_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            memory_dir = workspace / "interns" / "generated" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "memory.json").write_text(
                json.dumps({"definitions": [{"feature": "Age", "status": "rejected"}]}),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "--repo-root",
                    str(root),
                    "--workspace",
                    "workspaces/demo",
                    "--no-team-memory",
                ]
            )

            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()

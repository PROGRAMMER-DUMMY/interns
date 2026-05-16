from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.user_decisions import apply_decision_to_feature, apply_user_decision
from core.storage.workspace_layout import WorkspaceLayout


class UserDecisionTests(unittest.TestCase):
    def test_apply_decision_to_feature_records_evidence_and_clears_confirmed_question(self):
        feature = {
            "feature": "DeniedAmount",
            "state": "blocked_missing_evidence",
            "question": "question",
            "evidence": [],
            "decision_history": [],
            "source_columns": [],
        }

        apply_decision_to_feature(
            feature,
            state="user_confirmed",
            resolution_type="user_confirmed",
            evidence_note="Accepted by BA.",
            source_columns=["DeniedAmount"],
        )

        self.assertEqual(feature["state"], "user_confirmed")
        self.assertIsNone(feature["question"])
        self.assertEqual(feature["source_columns"][0]["column"], "DeniedAmount")
        self.assertEqual(feature["evidence"][0]["detail"], "Accepted by BA.")
        self.assertEqual(feature["decision_history"][0]["resolution_type"], "user_confirmed")

    def test_apply_user_decision_persists_mapping_memory_and_requirements(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            mapping_path = layout.contracts_dir / "kpi_feature_mapping.json"
            mapping_path.write_text(
                json.dumps(
                    {
                        "workspace": "workspaces/demo",
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "status": "blocked_questions_pending",
                                "features": [
                                    {
                                        "feature": "DeniedAmount",
                                        "state": "blocked_missing_evidence",
                                        "question": "question",
                                        "source_columns": [],
                                        "evidence": [],
                                        "decision_history": [],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = apply_user_decision(
                root,
                "workspaces/demo",
                kpi_id="kpi_001",
                feature="DeniedAmount",
                state="user_confirmed",
                resolution_type="user_confirmed",
                evidence_note="Accepted by BA.",
                source_columns=["DeniedAmount"],
            )

            self.assertEqual(summary["ready_kpi_count"], 1)
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            self.assertEqual(mapping["kpis"][0]["features"][0]["state"], "user_confirmed")
            self.assertTrue((layout.memory_dir / "decision_history.md").exists())
            requirements = json.loads((layout.requirements_dir / "requirements.json").read_text())
            self.assertEqual(requirements["user_confirmed_feature_decisions"][0]["feature"], "DeniedAmount")


if __name__ == "__main__":
    unittest.main()

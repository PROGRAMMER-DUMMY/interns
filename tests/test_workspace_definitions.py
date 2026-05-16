from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace_definitions import (
    apply_workspace_definition,
    apply_workspace_definition_to_mapping,
    load_workspace_definitions,
    upsert_workspace_definition,
)
from core.storage.workspace_layout import WorkspaceLayout


class WorkspaceDefinitionTests(unittest.TestCase):
    def test_upsert_workspace_definition_replaces_by_normalized_feature(self):
        definitions = {
            "version": 1,
            "definitions": [
                {"feature": "Denied Amount", "definition": "old"},
                {"feature": "Age", "definition": "age"},
            ],
        }

        upsert_workspace_definition(definitions, {"feature": "DeniedAmount", "definition": "new"})

        by_feature = {item["feature"]: item for item in definitions["definitions"]}
        self.assertNotIn("Denied Amount", by_feature)
        self.assertEqual(by_feature["DeniedAmount"]["definition"], "new")
        self.assertEqual([item["feature"] for item in definitions["definitions"]], ["Age", "DeniedAmount"])

    def test_apply_workspace_definition_to_mapping_updates_matching_features_only(self):
        mapping = {
            "kpis": [
                {
                    "kpi_id": "kpi_001",
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
                },
                {
                    "kpi_id": "kpi_002",
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
                },
            ]
        }
        definition = {
            "feature": "DeniedAmount",
            "state": "user_confirmed",
            "resolution_type": "workspace_definition",
            "definition": "Denied amount means denied claim line amount.",
            "source_columns": [{"dataset": "", "column": "DeniedAmount"}],
            "applies_to_kpis": ["kpi_001"],
        }

        updated = apply_workspace_definition_to_mapping(mapping, definition)

        self.assertEqual(updated, 1)
        self.assertEqual(mapping["kpis"][0]["features"][0]["state"], "user_confirmed")
        self.assertIsNone(mapping["kpis"][0]["features"][0]["question"])
        self.assertEqual(mapping["kpis"][1]["features"][0]["state"], "blocked_missing_evidence")

    def test_apply_workspace_definition_persists_contract_and_requirements(self):
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

            summary = apply_workspace_definition(
                root,
                "workspaces/demo",
                feature="DeniedAmount",
                state="user_confirmed",
                resolution_type="workspace_definition",
                evidence_note="Accepted by BA.",
                definition="Denied amount definition.",
                source_columns=["DeniedAmount"],
            )

            self.assertEqual(summary["ready_kpi_count"], 1)
            definitions = load_workspace_definitions(layout)
            self.assertEqual(definitions["definitions"][0]["feature"], "DeniedAmount")
            requirements = json.loads((layout.requirements_dir / "requirements.json").read_text())
            self.assertEqual(requirements["workspace_feature_definitions"][0]["feature"], "DeniedAmount")


if __name__ == "__main__":
    unittest.main()

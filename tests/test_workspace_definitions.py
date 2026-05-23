from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.memory.workspace_definitions import (
    apply_workspace_definition,
    apply_workspace_definitions_to_mapping,
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

    def test_scoped_workspace_definitions_override_broad_definitions(self):
        mapping = {
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "features": [
                        {
                            "feature": "cost",
                            "state": "blocked_missing_evidence",
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
                            "feature": "cost",
                            "state": "blocked_missing_evidence",
                            "source_columns": [],
                            "evidence": [],
                            "decision_history": [],
                        }
                    ],
                },
            ]
        }
        definitions = {
            "definitions": [
                {
                    "feature": "cost",
                    "state": "user_confirmed",
                    "resolution_type": "workspace_definition",
                    "definition": "generic",
                    "source_columns": [{"dataset": "", "column": "generic.COST"}],
                    "applies_to_kpis": [],
                },
                {
                    "feature": "cost",
                    "state": "user_confirmed",
                    "resolution_type": "workspace_definition",
                    "definition": "specific",
                    "source_columns": [{"dataset": "procedures.csv", "column": "BASE_COST"}],
                    "applies_to_kpis": ["kpi_001"],
                },
            ]
        }

        updated = apply_workspace_definitions_to_mapping(mapping, definitions)

        self.assertEqual(updated, 3)
        by_kpi = {kpi["kpi_id"]: kpi["features"][0] for kpi in mapping["kpis"]}
        self.assertEqual(by_kpi["kpi_001"]["source_columns"][0]["column"], "BASE_COST")
        self.assertEqual(by_kpi["kpi_002"]["source_columns"][0]["column"], "generic.COST")

    def test_scoped_workspace_definition_can_override_proven_direct_mapping(self):
        mapping = {
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "features": [
                        {
                            "feature": "encounter",
                            "state": "proven_direct",
                            "source_columns": [{"dataset": "procedures.csv", "column": "ENCOUNTER"}],
                            "evidence": [],
                            "decision_history": [],
                        }
                    ],
                }
            ]
        }
        definition = {
            "feature": "encounter",
            "state": "user_confirmed",
            "resolution_type": "workspace_definition",
            "definition": "encounters.Id",
            "source_columns": [{"dataset": "encounters.csv", "column": "Id"}],
            "applies_to_kpis": ["kpi_001"],
        }

        updated = apply_workspace_definition_to_mapping(mapping, definition)

        self.assertEqual(updated, 1)
        feature = mapping["kpis"][0]["features"][0]
        self.assertEqual(feature["state"], "user_confirmed")
        self.assertEqual(feature["source_columns"][0]["dataset"], "encounters.csv")
        self.assertEqual(feature["source_columns"][0]["column"], "Id")

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

    def test_apply_workspace_definition_normalizes_file_qualified_source_column(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "encounters.csv").write_text("START\n2024-01-01\n", encoding="utf-8")
            (layout.contracts_dir / "kpi_feature_mapping.json").write_text(
                json.dumps(
                    {
                        "workspace": "workspaces/demo",
                        "kpis": [
                            {
                                "kpi_id": "kpi_001",
                                "status": "blocked_questions_pending",
                                "features": [
                                    {
                                        "feature": "Year",
                                        "state": "blocked_missing_evidence",
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

            apply_workspace_definition(
                root,
                "workspaces/demo",
                feature="Year",
                state="user_confirmed",
                resolution_type="workspace_definition",
                evidence_note="Accepted temporal anchor.",
                definition="Year from encounters.START.",
                source_columns=["encounters.csv.START"],
            )

            definitions = load_workspace_definitions(layout)
            source = definitions["definitions"][0]["source_columns"][0]
            self.assertEqual(source["dataset"], "workspaces/demo/encounters.csv")
            self.assertEqual(source["column"], "START")


if __name__ == "__main__":
    unittest.main()

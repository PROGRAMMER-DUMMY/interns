from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.relationships.contracts import RelationshipContractBuilder
from core.onboarding.relationships.source_to_target_planner import _source_group
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.storage.workspace_layout import WorkspaceLayout


class RelationshipContractTests(unittest.TestCase):
    def test_source_group_keeps_root_workspace_csvs_distinct(self):
        self.assertNotEqual(
            _source_group("workspaces/demo/orders.csv"),
            _source_group("workspaces/demo/order_item_refunds.csv"),
        )

    def test_builder_reads_root_data_dictionary_foreign_keys(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "data_dictionary.csv").write_text(
                "table,column,description\n"
                "encounters,Id,Primary Key. Unique Identifier of the encounter.\n"
                "procedures,Encounter,Foreign key to the Encounter where the procedure was performed.\n",
                encoding="utf-8",
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "encounters.csv"),
                        "schema": {"Id": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/encounters.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "procedures.csv"),
                        "schema": {"ENCOUNTER": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/procedures.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index),
                encoding="utf-8",
            )
            (layout.contracts_dir / "domain_model.json").write_text(
                json.dumps({"data_models": ["workspaces/demo/data_dictionary.csv"]}),
                encoding="utf-8",
            )

            result = RelationshipContractBuilder(root, "workspaces/demo").build()

            self.assertEqual(result.executable_relationship_count, 1)
            contract = json.loads((layout.contracts_dir / "relationship_contracts.json").read_text())
            relationship = contract["relationships"][0]
            self.assertEqual(relationship["left_dataset"], "workspaces/demo/procedures.csv")
            self.assertEqual(relationship["left_column"], "ENCOUNTER")
            self.assertEqual(relationship["right_dataset"], "workspaces/demo/encounters.csv")
            self.assertEqual(relationship["right_column"], "Id")
            self.assertEqual(relationship["state"], "proven_data_model")
            self.assertTrue(relationship["executable_usage_policy"]["allowed_in_sql_generation"])

    def test_builder_infers_fk_suffix_relationships_from_dictionary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "data_dictionary.csv").write_text(
                "table,column,description\n"
                "orders,order_id,Unique identifier for each order (PK)\n"
                "order_items,order_id,Unique identifier for the order the item belongs to (FK)\n",
                encoding="utf-8",
            )
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "path": "workspaces/demo/orders.csv",
                                "schema": {"order_id": "Int64"},
                                "profile_path": "workspaces/demo/interns/generated/profiles/orders.csv.profile.json",
                            },
                            {
                                "path": "workspaces/demo/order_items.csv",
                                "schema": {"order_id": "Int64"},
                                "profile_path": "workspaces/demo/interns/generated/profiles/order_items.csv.profile.json",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (layout.contracts_dir / "domain_model.json").write_text(
                json.dumps({"data_models": ["workspaces/demo/data_dictionary.csv"]}),
                encoding="utf-8",
            )

            result = RelationshipContractBuilder(root, "workspaces/demo").build()

            self.assertEqual(result.executable_relationship_count, 1)
            contract = json.loads((layout.contracts_dir / "relationship_contracts.json").read_text())
            relationship = contract["relationships"][0]
            self.assertEqual(relationship["left_dataset"], "workspaces/demo/order_items.csv")
            self.assertEqual(relationship["left_column"], "order_id")
            self.assertEqual(relationship["right_dataset"], "workspaces/demo/orders.csv")
            self.assertEqual(relationship["right_column"], "order_id")

    def test_builder_drops_candidate_pair_when_proven_pair_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "relationship_model.md").write_text(
                "orders joins website_sessions on website_session_id.\n",
                encoding="utf-8",
            )
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "path": "workspaces/demo/orders.csv",
                                "schema": {"website_session_id": "Int64", "user_id": "Int64"},
                                "profile_path": "workspaces/demo/interns/generated/profiles/orders.csv.profile.json",
                            },
                            {
                                "path": "workspaces/demo/website_sessions.csv",
                                "schema": {"website_session_id": "Int64", "user_id": "Int64"},
                                "profile_path": "workspaces/demo/interns/generated/profiles/website_sessions.csv.profile.json",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (layout.contracts_dir / "domain_model.json").write_text(
                json.dumps({"data_models": ["workspaces/demo/relationship_model.md"]}),
                encoding="utf-8",
            )

            result = RelationshipContractBuilder(root, "workspaces/demo").build()

            self.assertEqual(result.relationship_count, 1)
            self.assertEqual(result.executable_relationship_count, 1)
            self.assertEqual(result.candidate_relationship_count, 0)

    def test_validator_rejects_stale_relationship_summary_after_manual_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (layout.contracts_dir / "relationship_contracts.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "relationship_contracts.json",
                        "version": 1,
                        "generated_by": "build-relationship-contracts",
                        "relationships": [
                            {
                                "relationship_id": "patients__patientid__transactions__patientid",
                                "approval": "approved",
                                "executable_usage_policy": {
                                    "allowed_in_sql_generation": True,
                                },
                            }
                        ],
                        "summary": {
                            "relationship_count": 1,
                            "executable_relationship_count": 0,
                            "candidate_relationship_count": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = WorkspaceArtifactValidator(root, "workspaces/demo").run()

            self.assertFalse(result.ok)
            self.assertTrue(
                any("executable_relationship_count" in error for error in result.errors),
                result.errors,
            )


if __name__ == "__main__":
    unittest.main()

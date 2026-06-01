from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.relationships.contracts import (
    RelationshipContractBuilder,
    apply_relationship_answer,
)
from core.onboarding.relationships.contracts import _norm as _norm_col
from core.onboarding.relationships.source_to_target_planner import _source_group
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.storage.workspace_layout import WorkspaceLayout


class RelationshipApprovalProvenanceTests(unittest.TestCase):
    """BUG-014: relationship approvals must record agent-vs-human provenance."""

    def _seed(self, root: Path) -> tuple[str, str, Path]:
        workspace = root / "workspaces" / "demo"
        layout = WorkspaceLayout(project_root=workspace)
        layout.ensure_runtime_dirs()
        contracts_path = layout.contracts_dir / "relationship_contracts.json"
        contracts_path.parent.mkdir(parents=True, exist_ok=True)
        contracts_path.write_text(
            json.dumps(
                {
                    "relationships": [
                        {
                            "relationship_id": "a__k__b__k",
                            "left_dataset": "a.csv",
                            "right_dataset": "b.csv",
                            "state": "profile_validated",
                            "executable_usage_policy": {"allowed_in_sql_generation": False},
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
        return "workspaces/demo", str(root), contracts_path

    def test_agent_asserted_approval_records_agent_source(self):
        with tempfile.TemporaryDirectory() as td:
            ws, repo, path = self._seed(Path(td))
            result = apply_relationship_answer(
                repo, ws, relationship_id="a__k__b__k", answer="approve"
            )
            self.assertEqual(result.source, "agent")
            self.assertEqual(result.confirmed_by, "")
            saved = json.loads(path.read_text(encoding="utf-8"))["relationships"][0]
            self.assertEqual(saved["approval"]["source"], "agent")

    def test_human_confirmed_approval_records_human_source(self):
        with tempfile.TemporaryDirectory() as td:
            ws, repo, path = self._seed(Path(td))
            result = apply_relationship_answer(
                repo, ws, relationship_id="a__k__b__k", answer="approve", confirmed_by="shubham"
            )
            self.assertEqual(result.source, "human")
            self.assertEqual(result.confirmed_by, "shubham")
            saved = json.loads(path.read_text(encoding="utf-8"))["relationships"][0]
            self.assertEqual(saved["approval"]["source"], "human")
            self.assertEqual(saved["approval"]["confirmed_by"], "shubham")


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
                                "state": "user_confirmed",
                                "approval": {"state": "approved"},
                                "decision_history": [
                                    {"state": "profile_validated", "note": "Generated.", "timestamp": "now"}
                                ],
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

    def test_multi_shared_column_pair_prefers_dimension_unique_key(self):
        """BUG-003: two tables share two key columns; one is unique on the
        dimension side and one is not. The inferred executable-capable edge must
        use the UNIQUE column, and the non-unique-key edge must not be silently
        marked executable.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            # providers: ProviderID is a PK (unique), DeptID is NOT unique.
            (workspace / "providers.csv").write_text(
                "ProviderID,DeptID\n"
                "PROV01,DEPT01\n"
                "PROV02,DEPT01\n"
                "PROV03,DEPT02\n"
                "PROV04,DEPT02\n",
                encoding="utf-8",
            )
            # transactions: neither column is unique (fact rows).
            (workspace / "transactions.csv").write_text(
                "ProviderID,DeptID\n"
                "PROV01,DEPT01\n"
                "PROV01,DEPT01\n"
                "PROV02,DEPT01\n"
                "PROV03,DEPT02\n"
                "PROV04,DEPT02\n",
                encoding="utf-8",
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "providers.csv"),
                        "schema": {"ProviderID": "String", "DeptID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/providers.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "transactions.csv"),
                        "schema": {"ProviderID": "String", "DeptID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index), encoding="utf-8"
            )

            RelationshipContractBuilder(root, "workspaces/demo").build()
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            relationships = contract["relationships"]

            # An edge on the UNIQUE dimension key (ProviderID) must exist, with
            # the unique side as the right (dimension) dataset.
            provider_edges = [
                r for r in relationships if _norm_col(r["right_column"]) == "providerid"
            ]
            self.assertTrue(provider_edges, relationships)
            provider_edge = provider_edges[0]
            self.assertEqual(provider_edge["right_dataset"], "workspaces/demo/providers.csv")
            self.assertTrue(provider_edge["uniqueness_checks"]["right_key_unique"])
            self.assertFalse(provider_edge["grain_impact"].get("fan_out_risk"))

            # No emitted edge may carry the non-unique DeptID dimension key while
            # silently marked executable.
            for r in relationships:
                if _norm_col(r["right_column"]) == "deptid":
                    self.assertFalse(
                        r["executable_usage_policy"]["allowed_in_sql_generation"],
                        r,
                    )
                    self.assertFalse(r["uniqueness_checks"]["right_key_unique"], r)
                    self.assertTrue(r["grain_impact"]["fan_out_risk"], r)

    def test_non_unique_dimension_key_not_executable_after_doc_promotion(self):
        """A non-unique-key profile edge must NOT be promoted to executable even
        when a data-model doc references both entities and the key.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            # Both tables share only DeptID, and it is non-unique on both sides.
            (workspace / "providers.csv").write_text(
                "DeptID\nDEPT01\nDEPT01\nDEPT02\nDEPT02\n", encoding="utf-8"
            )
            (workspace / "shifts.csv").write_text(
                "DeptID\nDEPT01\nDEPT01\nDEPT02\n", encoding="utf-8"
            )
            (workspace / "data_model.md").write_text(
                "Star schema. providers and shifts relationships: foreign key on DeptID.\n",
                encoding="utf-8",
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "providers.csv"),
                        "schema": {"DeptID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/providers.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "shifts.csv"),
                        "schema": {"DeptID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/shifts.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index), encoding="utf-8"
            )
            (layout.contracts_dir / "domain_model.json").write_text(
                json.dumps({"data_models": ["workspaces/demo/data_model.md"]}),
                encoding="utf-8",
            )

            RelationshipContractBuilder(root, "workspaces/demo").build()
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            self.assertEqual(contract["summary"]["executable_relationship_count"], 0, contract)
            for r in contract["relationships"]:
                self.assertFalse(
                    r["executable_usage_policy"]["allowed_in_sql_generation"], r
                )

    def test_single_shared_column_relationship_still_produced(self):
        """Guardrail: single-shared-column relationships must still be produced
        (e.g. patients<->transactions on PatientID).
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "patients.csv").write_text(
                "PatientID\nP01\nP02\nP03\n", encoding="utf-8"
            )
            (workspace / "transactions.csv").write_text(
                "PatientID\nP01\nP01\nP02\nP03\n", encoding="utf-8"
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "patients.csv"),
                        "schema": {"PatientID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/patients.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "transactions.csv"),
                        "schema": {"PatientID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index), encoding="utf-8"
            )

            result = RelationshipContractBuilder(root, "workspaces/demo").build()
            self.assertEqual(result.relationship_count, 1)
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            edge = contract["relationships"][0]
            # patients.PatientID is the unique dimension side.
            self.assertEqual(edge["right_dataset"], "workspaces/demo/patients.csv")
            self.assertTrue(edge["uniqueness_checks"]["right_key_unique"])

    def test_zero_resolution_edge_not_executable_with_ri_block_reason(self):
        """BUG-004(b): a fact->dim FK whose left keys resolve 0% to the right
        side (different key namespace) must NOT be executable and must carry a
        referential-integrity block reason / RI risk flag.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            # providers.ProviderID is a unique PK (dimension side), but uses a
            # different namespace ("H1-...") than transactions.ProviderID
            # ("PROV..."), so 0% of fact keys resolve.
            (workspace / "providers.csv").write_text(
                "ProviderID\nH1-PROV01\nH1-PROV02\nH1-PROV03\nH1-PROV04\n",
                encoding="utf-8",
            )
            (workspace / "transactions.csv").write_text(
                "ProviderID\nPROV01\nPROV01\nPROV02\nPROV03\n",
                encoding="utf-8",
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "providers.csv"),
                        "schema": {"ProviderID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/providers.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "transactions.csv"),
                        "schema": {"ProviderID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index), encoding="utf-8"
            )

            result = RelationshipContractBuilder(root, "workspaces/demo").build()

            self.assertEqual(result.executable_relationship_count, 0)
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            self.assertEqual(len(contract["relationships"]), 1, contract)
            edge = contract["relationships"][0]
            # ProviderID is unique on providers, so providers is the dimension
            # (right) side; but RI failure overrides and blocks execution.
            self.assertEqual(edge["right_dataset"], "workspaces/demo/providers.csv")
            self.assertFalse(edge["executable_usage_policy"]["allowed_in_sql_generation"], edge)
            self.assertIn(
                "referential_integrity_failed",
                edge["executable_usage_policy"]["block_reason"],
            )
            self.assertTrue(edge["grain_impact"]["referential_integrity_risk"], edge)
            self.assertEqual(
                edge["referential_integrity_checks"]["left_key_resolution_ratio"], 0.0
            )
            self.assertEqual(
                edge["referential_integrity_checks"]["status"], "referential_integrity_failed"
            )

    def test_full_resolution_edge_passes_ri_gate(self):
        """Guardrail: an edge whose left keys fully resolve to the unique right
        key stays executable-capable (RI does not over-block valid joins).
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "patients.csv").write_text(
                "PatientID\nP01\nP02\nP03\n", encoding="utf-8"
            )
            (workspace / "transactions.csv").write_text(
                "PatientID\nP01\nP01\nP02\nP03\n", encoding="utf-8"
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "patients.csv"),
                        "schema": {"PatientID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/patients.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "transactions.csv"),
                        "schema": {"PatientID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index), encoding="utf-8"
            )

            RelationshipContractBuilder(root, "workspaces/demo").build()
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            edge = contract["relationships"][0]
            self.assertEqual(
                edge["referential_integrity_checks"]["left_key_resolution_ratio"], 1.0
            )
            self.assertFalse(edge["grain_impact"]["referential_integrity_risk"], edge)
            self.assertEqual(
                edge["referential_integrity_checks"]["status"], "left_keys_resolve"
            )

    def test_diagram_declared_fk_is_consumed_and_ranked_above_column_overlap(self):
        """BUG-004(a): a fact->dim FK declared in the parsed DataModel diagram
        sidecar is consumed as relationship evidence and ranked above raw
        profile column-name overlap (proven_data_model with a diagram source).
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "transactions.csv").write_text(
                "PatientID\nP01\nP01\nP02\nP03\n", encoding="utf-8"
            )
            (workspace / "patients.csv").write_text(
                "PatientID\nP01\nP02\nP03\n", encoding="utf-8"
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "patients.csv"),
                        "schema": {"PatientID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/patients.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "transactions.csv"),
                        "schema": {"PatientID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index), encoding="utf-8"
            )
            # Emulate the image_parser sidecar: a diagram-declared
            # Fact_Transactions -> Dim_Patient FK whose endpoints matched the
            # transactions/patients profiles on PatientID.
            sidecar_dir = layout.generated_dir / "data_model_images"
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            (sidecar_dir / "datamodel.model.json").write_text(
                json.dumps(
                    {
                        "source_image": "workspaces/demo/docs/DataModel.png",
                        "relationships": [
                            {
                                "relationship_id": "fact__patientid__dim_patient__patientid",
                                "from_table": "Fact_Transactions",
                                "from_column": "PatientID",
                                "to_table": "Dim_Patient",
                                "to_column": "PatientID",
                                "confidence": 0.86,
                            }
                        ],
                        "profile_matching": {
                            "relationship_matches": [
                                {
                                    "relationship_id": "fact__patientid__dim_patient__patientid",
                                    "state": "profile_matched",
                                    "confidence": 0.9,
                                    "from_match": {
                                        "table_name": "Fact_Transactions",
                                        "column_name": "PatientID",
                                        "dataset": "workspaces/demo/transactions.csv",
                                        "profile_column": "PatientID",
                                    },
                                    "to_match": {
                                        "table_name": "Dim_Patient",
                                        "column_name": "PatientID",
                                        "dataset": "workspaces/demo/patients.csv",
                                        "profile_column": "PatientID",
                                    },
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            RelationshipContractBuilder(root, "workspaces/demo").build()
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            self.assertEqual(len(contract["relationships"]), 1, contract)
            edge = contract["relationships"][0]
            # Diagram evidence ranked above raw column overlap -> proven_data_model.
            self.assertEqual(edge["state"], "proven_data_model")
            self.assertTrue(
                any(
                    src.get("type") == "data_model_image_diagram"
                    for src in edge.get("evidence_sources", [])
                ),
                edge["evidence_sources"],
            )
            # patients is the unique dimension side and keys fully resolve, so the
            # diagram FK is executable-capable.
            self.assertEqual(edge["right_dataset"], "workspaces/demo/patients.csv")
            self.assertTrue(edge["executable_usage_policy"]["allowed_in_sql_generation"], edge)

    def test_diagram_declared_fk_stays_blocked_on_ri_failure(self):
        """A diagram-declared FK that does not resolve in the data (key-namespace
        mismatch) must stay non-executable despite diagram evidence.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            (workspace / "transactions.csv").write_text(
                "ProviderID\nPROV01\nPROV02\nPROV03\n", encoding="utf-8"
            )
            (workspace / "providers.csv").write_text(
                "ProviderID\nH1-PROV01\nH1-PROV02\nH1-PROV03\n", encoding="utf-8"
            )
            profile_index = {
                "profiles": [
                    {
                        "path": str(workspace / "providers.csv"),
                        "schema": {"ProviderID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/providers.csv.profile.json",
                    },
                    {
                        "path": str(workspace / "transactions.csv"),
                        "schema": {"ProviderID": "String"},
                        "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                    },
                ]
            }
            (layout.profiles_dir / "profile_index.json").write_text(
                json.dumps(profile_index), encoding="utf-8"
            )
            sidecar_dir = layout.generated_dir / "data_model_images"
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            (sidecar_dir / "datamodel.model.json").write_text(
                json.dumps(
                    {
                        "source_image": "workspaces/demo/docs/DataModel.png",
                        "relationships": [
                            {
                                "relationship_id": "fact__providerid__dim_provider__providerid",
                                "from_table": "Fact_Transactions",
                                "to_table": "Dim_Provider",
                                "confidence": 0.86,
                            }
                        ],
                        "profile_matching": {
                            "relationship_matches": [
                                {
                                    "relationship_id": "fact__providerid__dim_provider__providerid",
                                    "state": "profile_matched",
                                    "confidence": 0.9,
                                    "from_match": {
                                        "dataset": "workspaces/demo/transactions.csv",
                                        "profile_column": "ProviderID",
                                    },
                                    "to_match": {
                                        "dataset": "workspaces/demo/providers.csv",
                                        "profile_column": "ProviderID",
                                    },
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            RelationshipContractBuilder(root, "workspaces/demo").build()
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            edge = contract["relationships"][0]
            self.assertTrue(
                any(
                    src.get("type") == "data_model_image_diagram"
                    for src in edge.get("evidence_sources", [])
                ),
                edge["evidence_sources"],
            )
            self.assertFalse(edge["executable_usage_policy"]["allowed_in_sql_generation"], edge)
            self.assertIn(
                "referential_integrity_failed",
                edge["executable_usage_policy"]["block_reason"],
            )

    def test_apply_relationship_answer_approves_and_recomputes_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            relationship_id = "patients__patientid__transactions__patientid"
            (layout.contracts_dir / "relationship_contracts.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "relationship_contracts.json",
                        "version": 1,
                        "generated_by": "build-relationship-contracts",
                        "relationships": [
                            {
                                "relationship_id": relationship_id,
                                "left_dataset": "workspaces/demo/patients.csv",
                                "left_column": "PatientID",
                                "right_dataset": "workspaces/demo/transactions.csv",
                                "right_column": "PatientID",
                                "state": "profile_validated",
                                "approval": {"state": "needs_review"},
                                "executable_usage_policy": {
                                    "allowed_in_sql_generation": False,
                                    "allowed_in_polars_generation": False,
                                    "allowed_in_pyspark_generation": False,
                                    "allowed_in_medallion_generation": False,
                                    "block_reason": "candidate relationship requires data-model proof or user confirmation",
                                },
                                "decision_history": [
                                    {"state": "profile_validated", "note": "Generated.", "timestamp": "now"}
                                ],
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

            result = apply_relationship_answer(
                root,
                "workspaces/demo",
                relationship_id=relationship_id,
                answer="approve",
                evidence_note="User approved PatientID join for Hospital A.",
            )

            self.assertEqual(result.executable_relationship_count, 1)
            contract = json.loads((layout.contracts_dir / "relationship_contracts.json").read_text())
            relationship = contract["relationships"][0]
            self.assertEqual(relationship["state"], "user_confirmed")
            self.assertTrue(relationship["executable_usage_policy"]["allowed_in_sql_generation"])
            self.assertTrue(
                any(item.get("state") == "user_confirmed" for item in relationship["decision_history"])
            )
            validation = WorkspaceArtifactValidator(root, "workspaces/demo").run()
            self.assertTrue(validation.ok, validation.errors)


if __name__ == "__main__":
    unittest.main()

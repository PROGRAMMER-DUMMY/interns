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


class DiagramAbstractNameResolverTests(unittest.TestCase):
    """BUG-023 (real fix): abstract star-schema diagram names (Fact/Dim_X/X_ID)
    must resolve to physical profile datasets/columns so that
    _relationships_from_diagram_sidecars can promote executable edges without
    any apply-relationship-answer call.

    Concrete scenario mirrors the Healthcare-RCM-Data-Platform (Hospital A):
      Diagram edge                          Physical join
      Fact.Patient_ID -> Dim_Patient.Patient_ID  transactions.PatientID -> patients.PatientID
      Fact.DeptID     -> Dim_Diagnosis.Dept_ID   transactions.DeptID    -> departments.DeptID
      Fact.Provider_ID -> Dim_Provider.Provider_ID transactions.ProviderID -> providers.ProviderID
      Fact.ICD_Code   -> Dim_Diagnosis.ICD_Code  (no physical counterpart -> skipped)

    3 of 4 edges must resolve and pass the RI/uniqueness gate; the ICD_Code edge
    must be silently dropped (never fabricated).
    """

    def _build_workspace(self, root: Path) -> WorkspaceLayout:
        workspace = root / "workspaces" / "demo"
        layout = WorkspaceLayout(project_root=workspace)
        layout.ensure_runtime_dirs()
        # Physical CSVs: patients/providers/departments are dimension tables
        # (unique PKs); transactions is the fact table.
        (workspace / "patients.csv").write_text(
            "PatientID\nP01\nP02\nP03\n", encoding="utf-8"
        )
        (workspace / "providers.csv").write_text(
            "ProviderID\nPROV01\nPROV02\nPROV03\n", encoding="utf-8"
        )
        (workspace / "departments.csv").write_text(
            "DeptID\nDEPT01\nDEPT02\n", encoding="utf-8"
        )
        (workspace / "transactions.csv").write_text(
            "PatientID,ProviderID,DeptID\n"
            "P01,PROV01,DEPT01\n"
            "P01,PROV02,DEPT01\n"
            "P02,PROV01,DEPT02\n"
            "P03,PROV03,DEPT02\n",
            encoding="utf-8",
        )
        profile_index = {
            "profiles": [
                {
                    "path": str(workspace / "patients.csv"),
                    "schema": {"PatientID": "String"},
                    "profile_path": "workspaces/demo/interns/generated/profiles/patients.csv.profile.json",
                },
                {
                    "path": str(workspace / "providers.csv"),
                    "schema": {"ProviderID": "String"},
                    "profile_path": "workspaces/demo/interns/generated/profiles/providers.csv.profile.json",
                },
                {
                    "path": str(workspace / "departments.csv"),
                    "schema": {"DeptID": "String"},
                    "profile_path": "workspaces/demo/interns/generated/profiles/departments.csv.profile.json",
                },
                {
                    "path": str(workspace / "transactions.csv"),
                    "schema": {"PatientID": "String", "ProviderID": "String", "DeptID": "String"},
                    "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                },
            ]
        }
        (layout.profiles_dir / "profile_index.json").write_text(
            json.dumps(profile_index), encoding="utf-8"
        )
        return layout

    def _write_abstract_sidecar(self, layout: WorkspaceLayout, root: Path) -> None:
        """Write a diagram sidecar that uses abstract star-schema names.

        The sidecar has NOT pre-resolved anything in profile_matching; it only
        carries the raw OCR-inferred relationships with abstract table/column names.
        _relationships_from_diagram_sidecars must call _diagram_resolved_edges which
        reads profile_matching.relationship_matches — so we also write those as they
        would be produced after image_parser runs the profile matching.  The whole
        point of the resolver is that those matches now use physical dataset paths
        and physical column names.
        """
        sidecar_dir = layout.generated_dir / "data_model_images"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        workspace_path = str(layout.project_root).replace("\\", "/")
        # Simulate the profile_matching output produced by the fixed image_parser:
        # abstract diagram names -> resolved physical datasets + columns.
        sidecar = {
            "source_image": "workspaces/demo/docs/DataModel.png",
            "relationships": [
                {
                    "relationship_id": "fact__patient_id__dim_patient__patient_id",
                    "from_table": "Fact",
                    "from_column": "Patient_ID",
                    "to_table": "Dim_Patient",
                    "to_column": "Patient_ID",
                    "confidence": 0.85,
                },
                {
                    "relationship_id": "fact__deptid__dim_diagnosis__dept_id",
                    "from_table": "Fact",
                    "from_column": "DeptID",
                    "to_table": "Dim_Diagnosis",
                    "to_column": "Dept_ID",
                    "confidence": 0.85,
                },
                {
                    "relationship_id": "fact__provider_id__dim_provider__provider_id",
                    "from_table": "Fact",
                    "from_column": "Provider_ID",
                    "to_table": "Dim_Provider",
                    "to_column": "Provider_ID",
                    "confidence": 0.85,
                },
                {
                    "relationship_id": "fact__icd_code__dim_diagnosis__icd_code",
                    "from_table": "Fact",
                    "from_column": "ICD_Code",
                    "to_table": "Dim_Diagnosis",
                    "to_column": "ICD_Code",
                    "confidence": 0.75,
                },
            ],
            "profile_matching": {
                "relationship_matches": [
                    # Patient edge: Fact.Patient_ID -> transactions.PatientID;
                    # Dim_Patient.Patient_ID -> patients.PatientID
                    {
                        "relationship_id": "fact__patient_id__dim_patient__patient_id",
                        "state": "profile_matched",
                        "confidence": 0.85,
                        "from_match": {
                            "table_name": "Fact",
                            "column_name": "Patient_ID",
                            "dataset": str(workspace_path) + "/transactions.csv",
                            "profile_column": "PatientID",
                        },
                        "to_match": {
                            "table_name": "Dim_Patient",
                            "column_name": "Patient_ID",
                            "dataset": str(workspace_path) + "/patients.csv",
                            "profile_column": "PatientID",
                        },
                    },
                    # DeptID edge: Fact.DeptID -> transactions.DeptID;
                    # Dim_Diagnosis.Dept_ID -> departments.DeptID
                    {
                        "relationship_id": "fact__deptid__dim_diagnosis__dept_id",
                        "state": "profile_matched",
                        "confidence": 0.85,
                        "from_match": {
                            "table_name": "Fact",
                            "column_name": "DeptID",
                            "dataset": str(workspace_path) + "/transactions.csv",
                            "profile_column": "DeptID",
                        },
                        "to_match": {
                            "table_name": "Dim_Diagnosis",
                            "column_name": "Dept_ID",
                            "dataset": str(workspace_path) + "/departments.csv",
                            "profile_column": "DeptID",
                        },
                    },
                    # Provider edge: Fact.Provider_ID -> transactions.ProviderID;
                    # Dim_Provider.Provider_ID -> providers.ProviderID
                    {
                        "relationship_id": "fact__provider_id__dim_provider__provider_id",
                        "state": "profile_matched",
                        "confidence": 0.85,
                        "from_match": {
                            "table_name": "Fact",
                            "column_name": "Provider_ID",
                            "dataset": str(workspace_path) + "/transactions.csv",
                            "profile_column": "ProviderID",
                        },
                        "to_match": {
                            "table_name": "Dim_Provider",
                            "column_name": "Provider_ID",
                            "dataset": str(workspace_path) + "/providers.csv",
                            "profile_column": "ProviderID",
                        },
                    },
                    # ICD_Code edge: no physical counterpart -> unmatched, skipped.
                    {
                        "relationship_id": "fact__icd_code__dim_diagnosis__icd_code",
                        "state": "unmatched",
                        "confidence": 0.0,
                        "from_match": {},
                        "to_match": {},
                    },
                ]
            },
        }
        (sidecar_dir / "datamodel.model.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )

    def test_abstract_diagram_names_resolve_and_promote_executable_edges(self):
        """3 of 4 diagram edges must resolve to physical datasets/columns and
        become executable proven_data_model edges via the RI/uniqueness gate.
        The ICD_Code edge (no physical counterpart) must be silently skipped.
        No apply-relationship-answer call may be used.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            layout = self._build_workspace(root)
            self._write_abstract_sidecar(layout, root)

            result = RelationshipContractBuilder(root, "workspaces/demo").build()

            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            relationships = contract["relationships"]

            # All diagram-sourced edges must use the data_model_image_diagram type.
            diagram_edges = [
                r for r in relationships
                if any(
                    src.get("type") == "data_model_image_diagram"
                    for src in r.get("evidence_sources", [])
                )
            ]
            # 3 edges resolve (Patient, Dept, Provider); ICD_Code is skipped.
            self.assertEqual(
                len(diagram_edges), 3,
                f"Expected 3 resolved diagram edges, got {len(diagram_edges)}: "
                + str([(r['left_dataset'], r['left_column'], r['right_dataset'], r['right_column']) for r in diagram_edges])
            )

            # Each of the 3 resolved edges must be executable (RI + uniqueness pass).
            executable_diagram_edges = [
                r for r in diagram_edges
                if r.get("executable_usage_policy", {}).get("allowed_in_sql_generation")
            ]
            self.assertEqual(
                len(executable_diagram_edges), 3,
                f"Expected all 3 diagram edges executable, got {len(executable_diagram_edges)}: "
                + str([(r['state'], r['executable_usage_policy']) for r in diagram_edges])
            )

            # All 3 must be proven_data_model (diagram evidence ranks above profile overlap).
            for edge in diagram_edges:
                self.assertEqual(
                    edge["state"], "proven_data_model",
                    f"Diagram edge must be proven_data_model: {edge}"
                )

            # The ICD_Code edge must NOT appear in any form (not even non-executable).
            icd_edges = [
                r for r in relationships
                if _norm_col(r.get("left_column", "")) == "icdcode"
                or _norm_col(r.get("right_column", "")) == "icdcode"
            ]
            self.assertEqual(
                len(icd_edges), 0,
                f"ICD_Code edge (no physical counterpart) must be silently skipped, got: {icd_edges}"
            )

            # The transactions dataset must appear as the left (fact/"many") side
            # in at least 2 of the 3 resolved edges (PatientID and ProviderID).
            transactions_left = [
                r for r in diagram_edges
                if "transactions" in r.get("left_dataset", "").replace("\\", "/")
            ]
            self.assertGreaterEqual(
                len(transactions_left), 2,
                f"transactions.csv must be left dataset for at least 2 edges: {[(r['left_dataset'],r['left_column']) for r in diagram_edges]}"
            )

            # Summary counts must match.
            summary = contract["summary"]
            self.assertEqual(
                summary["executable_relationship_count"],
                result.executable_relationship_count,
            )
            self.assertGreaterEqual(result.executable_relationship_count, 3)

    def test_unresolvable_diagram_edge_silently_skipped_not_fabricated(self):
        """An edge whose endpoints have no physical counterpart (ICD_Code) must
        be silently dropped by _diagram_resolved_edges — never fabricated or
        added as a non-executable candidate.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            # Only transactions and patients; NO ICD/diagnosis/departments datasets.
            (workspace / "patients.csv").write_text(
                "PatientID\nP01\nP02\n", encoding="utf-8"
            )
            (workspace / "transactions.csv").write_text(
                "PatientID\nP01\nP01\nP02\n", encoding="utf-8"
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
            sidecar_dir = layout.generated_dir / "data_model_images"
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            # Sidecar with only an unmatched ICD_Code edge.
            sidecar = {
                "source_image": "workspaces/demo/docs/DataModel.png",
                "relationships": [
                    {
                        "relationship_id": "fact__icd_code__dim_diagnosis__icd_code",
                        "from_table": "Fact",
                        "from_column": "ICD_Code",
                        "to_table": "Dim_Diagnosis",
                        "to_column": "ICD_Code",
                        "confidence": 0.75,
                    }
                ],
                "profile_matching": {
                    "relationship_matches": [
                        {
                            "relationship_id": "fact__icd_code__dim_diagnosis__icd_code",
                            "state": "unmatched",
                            "confidence": 0.0,
                            "from_match": {},
                            "to_match": {},
                        }
                    ]
                },
            }
            (sidecar_dir / "datamodel.model.json").write_text(
                json.dumps(sidecar), encoding="utf-8"
            )

            RelationshipContractBuilder(root, "workspaces/demo").build()
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            icd_edges = [
                r for r in contract["relationships"]
                if _norm_col(r.get("left_column", "")) == "icdcode"
                or _norm_col(r.get("right_column", "")) == "icdcode"
            ]
            self.assertEqual(
                len(icd_edges), 0,
                f"Unresolvable ICD_Code edge must not appear in contracts: {icd_edges}"
            )


class RootFactDatasetSelectionTests(unittest.TestCase):
    """BUG-023 (root-fact fix): when the diagram's abstract Fact table can map
    to more than one physical dataset (e.g. both encounters and transactions carry
    PatientID), the resolver must promote the ROOT FACT — the dataset whose unique
    key is NOT referenced by any other dataset (in-degree 0 in the join graph).

    Concrete scenario: encounters has EncounterID (unique PK) and PatientID (FK);
    transactions has TransactionID (unique PK) and EncounterID (FK) + PatientID (FK).
    transactions references encounters (EncounterID), so encounters has in-degree 1
    and transactions has in-degree 0.  The diagram edge Fact.PatientID -> patients
    must promote transactions->patients, NOT encounters->patients.
    """

    def _build_workspace(self, root: Path) -> WorkspaceLayout:
        workspace = root / "workspaces" / "demo"
        layout = WorkspaceLayout(project_root=workspace)
        layout.ensure_runtime_dirs()
        # patients: unique PK (PatientID)
        (workspace / "patients.csv").write_text(
            "PatientID\nP01\nP02\nP03\n", encoding="utf-8"
        )
        # encounters: unique PK (EncounterID), FK PatientID -- referenced by transactions
        (workspace / "encounters.csv").write_text(
            "EncounterID,PatientID\n"
            "ENC01,P01\n"
            "ENC02,P01\n"
            "ENC03,P02\n"
            "ENC04,P03\n",
            encoding="utf-8",
        )
        # transactions: unique PK (TransactionID), FK EncounterID + FK PatientID
        # transactions references encounters, so encounters has in-degree 1;
        # nothing references transactions (in-degree 0 = root fact)
        (workspace / "transactions.csv").write_text(
            "TransactionID,EncounterID,PatientID\n"
            "TRANS01,ENC01,P01\n"
            "TRANS02,ENC01,P01\n"
            "TRANS03,ENC02,P01\n"
            "TRANS04,ENC03,P02\n"
            "TRANS05,ENC04,P03\n",
            encoding="utf-8",
        )
        profile_index = {
            "profiles": [
                {
                    "path": str(workspace / "patients.csv"),
                    "schema": {"PatientID": "String"},
                    "profile_path": "workspaces/demo/interns/generated/profiles/patients.csv.profile.json",
                },
                {
                    "path": str(workspace / "encounters.csv"),
                    "schema": {"EncounterID": "String", "PatientID": "String"},
                    "profile_path": "workspaces/demo/interns/generated/profiles/encounters.csv.profile.json",
                },
                {
                    "path": str(workspace / "transactions.csv"),
                    "schema": {"TransactionID": "String", "EncounterID": "String", "PatientID": "String"},
                    "profile_path": "workspaces/demo/interns/generated/profiles/transactions.csv.profile.json",
                },
            ]
        }
        (layout.profiles_dir / "profile_index.json").write_text(
            json.dumps(profile_index), encoding="utf-8"
        )
        return layout

    def test_diagram_edge_with_ambiguous_fact_promotes_root_fact_not_referenced_table(self):
        """The diagram edge Fact.PatientID -> Dim_Patient.PatientID must resolve
        to transactions->patients (root fact, in-degree 0), NOT encounters->patients
        (encounters is referenced by transactions so has in-degree 1).
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            layout = self._build_workspace(root)
            workspace_path = str(layout.project_root).replace("\\", "/")
            sidecar_dir = layout.generated_dir / "data_model_images"
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            # Sidecar as produced by image_parser: it mistakenly matched Fact to
            # encounters.csv (wrong choice; the fix must override it to transactions).
            (sidecar_dir / "datamodel.model.json").write_text(
                json.dumps({
                    "source_image": "workspaces/demo/docs/DataModel.png",
                    "relationships": [
                        {
                            "relationship_id": "fact__patientid__dim_patient__patientid",
                            "from_table": "Fact",
                            "from_column": "Patient_ID",
                            "to_table": "Dim_Patient",
                            "to_column": "Patient_ID",
                            "confidence": 0.85,
                        }
                    ],
                    "profile_matching": {
                        "relationship_matches": [
                            {
                                "relationship_id": "fact__patientid__dim_patient__patientid",
                                "state": "profile_matched",
                                "confidence": 0.85,
                                # image_parser picked encounters (wrong)
                                "from_match": {
                                    "table_name": "Fact",
                                    "column_name": "Patient_ID",
                                    "dataset": workspace_path + "/encounters.csv",
                                    "profile_column": "PatientID",
                                },
                                "to_match": {
                                    "table_name": "Dim_Patient",
                                    "column_name": "Patient_ID",
                                    "dataset": workspace_path + "/patients.csv",
                                    "profile_column": "PatientID",
                                },
                            }
                        ]
                    },
                }),
                encoding="utf-8",
            )

            RelationshipContractBuilder(root, "workspaces/demo").build()
            contract = json.loads(
                (layout.contracts_dir / "relationship_contracts.json").read_text()
            )
            diagram_edges = [
                r for r in contract["relationships"]
                if any(
                    src.get("type") == "data_model_image_diagram"
                    for src in r.get("evidence_sources", [])
                )
            ]
            self.assertEqual(len(diagram_edges), 1, contract["relationships"])
            edge = diagram_edges[0]
            # Must be transactions (root fact, in-degree 0), NOT encounters
            self.assertIn(
                "transactions", edge["left_dataset"].replace("\\", "/"),
                f"Root-fact selection must pick transactions (in-degree 0), got: {edge['left_dataset']}",
            )
            self.assertNotIn(
                "encounters", edge["left_dataset"].replace("\\", "/"),
                f"encounters has in-degree 1 (referenced by transactions) and must NOT be chosen: {edge['left_dataset']}",
            )
            # Edge must be executable (RI passes since transactions.PatientID -> patients.PatientID resolves)
            self.assertTrue(
                edge["executable_usage_policy"]["allowed_in_sql_generation"],
                f"transactions->patients edge must be executable; got: {edge['executable_usage_policy']}",
            )
            self.assertEqual(edge["state"], "proven_data_model")


if __name__ == "__main__":
    unittest.main()

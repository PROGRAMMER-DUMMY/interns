"""blocker_question_panel.py's human-facing labels for a UC-sourced
(Unity Catalog `catalog`.`schema`.`table` fqn) candidate must show the real
table name, not Path(fqn).stem's mangled "catalog.schema" (everything after
the LAST dot stripped). Found live: a real end-to-end walkthrough against
the healthcare_rcm.bronze catalog showed three distinct candidate tables
all rendered as the identical, useless `healthcare_rcm`.`bronze`.PaidAmount
label in the blocker panel a human reads to pick between them.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.blocker_question_panel import (
    _answer_demo,
    _physical_option_proof,
    _sql_table_label,
    _where_it_lands,
)
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.storage.workspace_layout import WorkspaceLayout

UC_FQN_A = "`healthcare_rcm`.`bronze`.`cptcodes`"
UC_FQN_B = "`healthcare_rcm`.`bronze`.`claims_hospital1`"


class SqlTableLabelUcSourcedTests(unittest.TestCase):
    def test_uc_fqn_resolves_to_real_table_name(self):
        self.assertEqual(_sql_table_label(UC_FQN_A), "cptcodes")

    def test_two_uc_tables_in_the_same_catalog_schema_are_distinguishable(self):
        label_a = _sql_table_label(UC_FQN_A)
        label_b = _sql_table_label(UC_FQN_B)
        self.assertNotEqual(label_a, label_b)
        self.assertEqual(label_a, "cptcodes")
        self.assertEqual(label_b, "claims_hospital1")

    def test_local_csv_path_unchanged(self):
        self.assertEqual(_sql_table_label("workspaces/demo/datasets/transactions.csv"), "transactions")


class WhereItLandsUcSourcedTests(unittest.TestCase):
    def test_proven_join_shows_real_table_name(self):
        feature = {
            "state": "proven_join",
            "source_columns": [
                {"dataset": UC_FQN_A, "column": "Code"},
                {"dataset": UC_FQN_A, "column": "CptCodeID"},
            ],
        }
        self.assertEqual(_where_it_lands(feature), "cptcodes.Code via CptCodeID")

    def test_user_confirmed_shows_real_table_name(self):
        feature = {
            "state": "user_confirmed",
            "source_columns": [{"dataset": UC_FQN_B, "column": "ClaimID"}],
        }
        self.assertEqual(_where_it_lands(feature), "claims_hospital1.ClaimID")


class PhysicalOptionProofUcSourcedTests(unittest.TestCase):
    def test_option_proof_query_references_real_table(self):
        option = {"column": "Code", "dataset": UC_FQN_A}
        proof = _physical_option_proof(option, [])
        self.assertIn("cptcodes", proof["query"])
        self.assertNotIn("healthcare_rcm.bronze", proof["query"])


class AnswerDemoUcSourcedTests(unittest.TestCase):
    def test_answer_demo_table_is_real_table_name(self):
        demo = _answer_demo(
            {"metric": "sum(Code)", "name": "test kpi"},
            {"feature": "code_total"},
            {"dataset": UC_FQN_A, "column": "Code"},
        )
        self.assertIn("cptcodes", demo.get("query", ""))


class WorkspaceFeatureDefinitionUcSourcedValidationTests(unittest.TestCase):
    """A saved workspace-level answer for a UC-sourced feature (apply-kpi-
    panel-answer picking a `catalog`.`schema`.`table` candidate) was rejected
    by validate-workspace-artifacts outright: `workspace definition source
    column dataset must be repo-relative` -- the validator assumed every
    dataset value is a workspaces/... file path, hard-blocking the exact
    apply-kpi-panel-answer flow that accepts an exclusive-mode Databricks
    workspace's answer. Found live in the same walkthrough as the label bug.
    """

    def test_uc_fqn_source_column_dataset_passes_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            path = layout.contracts_dir / "workspace_feature_definitions.json"
            path.write_text(
                json.dumps(
                    {
                        "artifact_type": "workspace_feature_definitions.json",
                        "version": 1,
                        "generated_by": "apply-kpi-panel-answer",
                        "definitions": [
                            {
                                "feature": "LineOfBusiness",
                                "definition": "physical column",
                                "resolution_type": "physical_column",
                                "source_columns": [
                                    {"dataset": UC_FQN_A, "column": "LineOfBusiness"}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            validator = WorkspaceArtifactValidator(Path(tmp), "workspaces/demo")
            validator._validate_workspace_definitions()
            self.assertEqual(validator.result.errors, [])

    def test_local_repo_relative_dataset_still_required_for_non_uc_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspaces" / "demo"
            layout = WorkspaceLayout(project_root=workspace)
            layout.ensure_runtime_dirs()
            path = layout.contracts_dir / "workspace_feature_definitions.json"
            path.write_text(
                json.dumps(
                    {
                        "artifact_type": "workspace_feature_definitions.json",
                        "version": 1,
                        "generated_by": "apply-kpi-panel-answer",
                        "definitions": [
                            {
                                "feature": "Amount",
                                "definition": "physical column",
                                "resolution_type": "physical_column",
                                "source_columns": [
                                    {"dataset": "not_repo_relative.csv", "column": "Amount"}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            validator = WorkspaceArtifactValidator(Path(tmp), "workspaces/demo")
            validator._validate_workspace_definitions()
            self.assertTrue(validator.result.errors)


if __name__ == "__main__":
    unittest.main()

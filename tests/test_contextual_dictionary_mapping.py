import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.blocker_question_panel import BlockerQuestionPanelBuilder
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class ContextualDictionaryMappingTests(unittest.TestCase):
    def test_resolver_uses_dictionary_context_for_generic_cost_terms(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "hospital"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "procedures.csv").write_text(
                "Id,BASE_COST,DESCRIPTION\nP1,10,Appendectomy\n",
                encoding="utf-8",
            )
            (workspace / "datasets" / "encounters.csv").write_text(
                "Id,BASE_ENCOUNTER_COST,TOTAL_CLAIM_COST,PAYER\nE1,50.0,75.0,Aetna\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "What is the average base cost for each procedure?,Cost KPI,Procedure,average base cost,\n"
                "What is the average total claim cost by payer?,Cost KPI,Payer,average total claim cost,\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_dictionary.csv").write_text(
                "Table,Field,Description\n"
                "procedures,Base_Cost,The line item cost of the procedure.\n"
                "procedures,Description,Description of the procedure.\n"
                "encounters,Base_Encounter_Cost,The base cost of the encounter, not including line items.\n"
                "encounters,Total_Claim_Cost,The total cost of the encounter, including all line items.\n"
                "encounters,Payer,Foreign key to the payer.\n",
                encoding="utf-8",
            )

            WorkspaceOnboarder(root, "workspaces/hospital", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/hospital", domain="healthcare").run()

            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            by_kpi = {item["kpi_id"]: item for item in mapping["kpis"]}

            base_cost = next(feature for feature in by_kpi["kpi_001"]["features"] if feature["feature"] == "cost")
            total_cost = next(feature for feature in by_kpi["kpi_002"]["features"] if feature["feature"] == "cost")

            self.assertEqual(base_cost["state"], "proven_alias")
            self.assertEqual(base_cost["resolution_type"], "contextual_dictionary_column")
            self.assertEqual(base_cost["source_columns"][0]["column"], "BASE_COST")
            proof = base_cost["source_columns"][0]["mapping_proof"]
            self.assertEqual(proof["proof_state"], "dictionary_and_profile_backed")
            self.assertTrue(any(file.endswith("docs/data_dictionary.csv") for file in proof["source_files"]))
            self.assertIn("profile_path", proof["profile_evidence"])
            self.assertEqual(proof["dictionary_evidence"]["field"], "Base_Cost")
            self.assertEqual(proof["sample_query"], 'SELECT "BASE_COST" AS "basecost" FROM "procedures" LIMIT 5;')
            self.assertEqual(proof["sample_output"][0], {"basecost": 10})
            self.assertTrue(
                any(
                    source.get("evidence_state") == "data_dictionary"
                    for source in base_cost["source_columns"][0]["semantic_meaning_sources"]
                )
            )
            self.assertEqual(total_cost["state"], "proven_alias")
            self.assertEqual(total_cost["source_columns"][0]["column"], "TOTAL_CLAIM_COST")

    def test_resolver_creates_json_backed_time_derivation_options(self):
        try:
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("polars is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "hospital"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "encounters.csv").write_text(
                "Id,START,STOP\nE1,2024-01-01T00:00Z,2024-01-02T02:00Z\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
                "How many encounters occurred each year?,Temporal KPI,Year,encounter count,\n"
                "What percentage of encounters were over 24 hours versus under 24 hours?,Temporal KPI,EncounterDurationBucket,percentage,\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "data_model.md").write_text("# Data Model\n", encoding="utf-8")

            WorkspaceOnboarder(root, "workspaces/hospital", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/hospital", domain="healthcare", include_candidates=True).run()

            mapping = json.loads(
                (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json")
                .read_text(encoding="utf-8")
            )
            features = {
                feature["feature"]: feature
                for kpi in mapping["kpis"]
                for feature in kpi["features"]
            }

            self.assertIn("extract(year from cast(START as timestamp))", features["Year"]["derived_feature_options"][0]["formula"])
            duration_option = features["EncounterDurationBucket"]["derived_feature_options"][0]
            self.assertIn("date_diff('hour'", duration_option["formula"])
            self.assertEqual(duration_option["evidence_state"], "candidate_derivation_not_ground_truth")

    def test_blocker_panel_carries_physical_mapping_proof_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "hospital"
            contracts = workspace / "interns" / "generated" / "contracts"
            contracts.mkdir(parents=True)
            mapping = {
                "artifact_type": "kpi_feature_mapping.json",
                "version": 2,
                "generated_by": "resolve-kpi-features",
                "workspace": "workspaces/hospital",
                "kpis": [
                    {
                        "kpi_id": "kpi_001",
                        "name": "What is average cost?",
                        "source": "workspaces/hospital/docs/kpi_registry.csv",
                        "features": [
                            {
                                "feature": "cost",
                                "state": "candidate_unconfirmed",
                                "resolution_type": "contextual_column_candidate",
                                "source_columns": [
                                    {
                                        "dataset": "workspaces/hospital/datasets/procedures.csv",
                                        "column": "BASE_COST",
                                        "profile_path": (
                                            "workspaces/hospital/interns/generated/profiles/"
                                            "procedures.csv.profile.json"
                                        ),
                                        "observed_values": [10, 20],
                                        "score": 12,
                                        "semantic_meaning_sources": [
                                            {
                                                "file": "workspaces/hospital/docs/data_dictionary.csv",
                                                "evidence_state": "data_dictionary",
                                            }
                                        ],
                                        "mapping_proof": {
                                            "proof_state": "dictionary_and_profile_backed",
                                            "source_files": [
                                                "workspaces/hospital/interns/generated/profiles/"
                                                "procedures.csv.profile.json",
                                                "workspaces/hospital/docs/data_dictionary.csv",
                                            ],
                                            "sample_output": [{"basecost": 10}, {"basecost": 20}],
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            (contracts / "kpi_feature_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

            panel_result = BlockerQuestionPanelBuilder(root, "workspaces/hospital").run()
            panel = json.loads((root / panel_result.current_json).read_text(encoding="utf-8"))

            self.assertTrue(any(file.endswith("docs/data_dictionary.csv") for file in panel["evidence_files"]))
            self.assertTrue(any(file.endswith("procedures.csv.profile.json") for file in panel["evidence_files"]))
            physical_option = next(option for option in panel["options"] if option.get("physical_column_option"))
            self.assertEqual(
                physical_option["physical_column_option"]["mapping_proof"]["proof_state"],
                "dictionary_and_profile_backed",
            )
            demo = physical_option["physical_column_option"]["answer_demo"]
            self.assertIn("SELECT", demo["query"])
            self.assertIn("sample_output", demo)
            self.assertIn("|", demo["sample_output_table"])
            self.assertIn("source_column_samples", demo)
            self.assertIn("|", demo["source_column_samples"][0]["sample_output_table"])


if __name__ == "__main__":
    unittest.main()

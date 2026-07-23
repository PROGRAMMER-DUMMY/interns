import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.blocker_question_panel import BlockerQuestionPanelBuilder
from core.onboarding.kpi.feature_resolver import (
    KPIFeatureResolver,
    _dedupe_features_by_physical_column,
)
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

            ef_paths = [e["file"] if isinstance(e, dict) else e for e in panel["evidence_files"]]
            self.assertTrue(any(f.endswith("docs/data_dictionary.csv") for f in ef_paths))
            self.assertTrue(any(f.endswith("procedures.csv.profile.json") for f in ef_paths))
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


class PhysicalColumnDedupTests(unittest.TestCase):
    """BUG-001: multiple features for one KPI that resolve to the SAME physical
    column must collapse to one, and a candidate duplicate must not raise a
    phantom blocker when a sibling already proves the column."""

    def _feature(self, name, state, dataset, column):
        return {
            "feature": name,
            "state": state,
            "resolution_type": "direct_column",
            "source_columns": [{"dataset": dataset, "column": column}],
            "question": None
            if state in {"proven_direct", "proven_alias"}
            else f"Which physical column should define `{name}`?",
        }

    def test_collapses_same_physical_column_and_drops_phantom_blocker(self):
        # The "Department Name" dimension split into three tokens, all of which
        # resolve to departments.Name. One is an unconfirmed misspelling.
        features = [
            self._feature("PatientID", "proven_direct", "datasets/patients.csv", "PatientID"),
            self._feature("departement", "candidate_unconfirmed", "datasets/departments.csv", "Name"),
            self._feature("Department", "proven_alias", "datasets/departments.csv", "Name"),
            self._feature("Name", "proven_direct", "datasets/departments.csv", "Name"),
            self._feature("VisitType", "proven_direct", "datasets/visits.csv", "VisitType"),
        ]

        deduped = _dedupe_features_by_physical_column(features)

        # departments.Name collapsed from 3 features to 1; distinct columns kept.
        dept_features = [
            f for f in deduped
            if f["source_columns"][0]["column"] == "Name"
            and "departments" in f["source_columns"][0]["dataset"]
        ]
        self.assertEqual(len(dept_features), 1)

        # The phantom candidate_unconfirmed for the misspelling is gone; the
        # surviving feature for that column is proven.
        self.assertNotIn(
            "candidate_unconfirmed",
            [f["state"] for f in deduped],
        )
        self.assertIn(dept_features[0]["state"], {"proven_direct", "proven_alias"})

        # Features resolving to genuinely different columns are untouched.
        kept_columns = {f["source_columns"][0]["column"] for f in deduped}
        self.assertEqual(kept_columns, {"PatientID", "Name", "VisitType"})

    def _contextual_candidate_feature(self, name, ranked_pairs):
        """An unresolved contextual feature carrying a ranked candidate list.

        Mirrors the real resolver shape: ``source_columns`` and ``candidates``
        hold every scored candidate in descending-score order, so index 0 is
        the top-ranked target the feature would resolve to.
        """
        return {
            "feature": name,
            "state": "candidate_unconfirmed",
            "resolution_type": "contextual_column_candidate",
            "source_columns": [
                {"dataset": dataset, "column": column} for dataset, column in ranked_pairs
            ],
            "candidates": [
                {"state": "candidate_unconfirmed", "source": dataset, "column": column}
                for dataset, column in ranked_pairs
            ],
            "question": f"Should `{name}` use context/dictionary candidate(s)?",
        }

    def test_multi_candidate_unconfirmed_collapses_into_proven_sibling(self):
        # The real BUG-001 shape: a candidate_unconfirmed feature whose
        # top-ranked CANDIDATE column equals a proven sibling's RESOLVED
        # column, while carrying many lower-ranked candidates as well. The
        # frozenset of all its source_columns never equals the proven sibling's
        # single-column key, so the same-key collapse cannot catch it — the
        # candidate-vs-proven pass must.
        features = [
            self._feature("PatientID", "proven_direct", "datasets/patients.csv", "PatientID"),
            self._contextual_candidate_feature(
                "departement",
                [
                    ("datasets/departments.csv", "Name"),  # top-ranked target
                    ("datasets/patients.csv", "DOB"),
                    ("datasets/patients.csv", "Gender"),
                    ("datasets/transactions.csv", "VisitType"),
                ],
            ),
            self._feature("Name", "proven_direct", "datasets/departments.csv", "Name"),
            self._feature("VisitType", "proven_direct", "datasets/transactions.csv", "VisitType"),
        ]

        deduped = _dedupe_features_by_physical_column(features)

        # The phantom candidate_unconfirmed for departments.Name is dropped.
        self.assertNotIn("candidate_unconfirmed", [f["state"] for f in deduped])
        self.assertNotIn("departement", [f["feature"] for f in deduped])
        # Every proven feature survives untouched.
        self.assertEqual(
            [(f["feature"], f["state"]) for f in deduped],
            [
                ("PatientID", "proven_direct"),
                ("Name", "proven_direct"),
                ("VisitType", "proven_direct"),
            ],
        )

    def test_multi_candidate_unconfirmed_kept_when_no_proven_sibling_matches(self):
        # A genuine blocker: the top-ranked candidate column is NOT proven by
        # any sibling, so the feature must remain a blocker.
        features = [
            self._feature("PatientID", "proven_direct", "datasets/patients.csv", "PatientID"),
            self._contextual_candidate_feature(
                "mystery",
                [
                    ("datasets/lookup.csv", "Code"),  # top-ranked, not proven anywhere
                    ("datasets/patients.csv", "Gender"),
                ],
            ),
            self._feature("Name", "proven_direct", "datasets/departments.csv", "Name"),
        ]

        deduped = _dedupe_features_by_physical_column(features)

        self.assertIn("mystery", [f["feature"] for f in deduped])
        self.assertIn("candidate_unconfirmed", [f["state"] for f in deduped])
        self.assertEqual(len(deduped), 3)

    def test_two_unrelated_unconfirmed_features_sharing_the_same_candidate_set_both_survive(self):
        # Found live: two GENUINELY DIFFERENT derived features ("churned",
        # "active_last_q") both got the identical generic, low-confidence
        # candidate list (four unrelated "ts" columns matched via broad
        # contextual scoring, not real relevance to either concept) -- with
        # NO proven sibling anywhere. Sharing the same candidate SET is not
        # the same thing as "resolving to the same physical column"; it's
        # two independently-unresolved concepts landing on the same generic
        # guess. One was silently dropped, permanently losing a real
        # blocker question -- neither ever reached the panel.
        features = [
            self._contextual_candidate_feature(
                "churned",
                [
                    ("datasets/audit_log.csv", "ts"),
                    ("datasets/gps_pings.csv", "ts"),
                    ("datasets/edi_messages.csv", "ts"),
                    ("datasets/temperature_logs.csv", "ts"),
                ],
            ),
            self._contextual_candidate_feature(
                "active_last_q",
                [
                    ("datasets/audit_log.csv", "ts"),
                    ("datasets/gps_pings.csv", "ts"),
                    ("datasets/edi_messages.csv", "ts"),
                    ("datasets/temperature_logs.csv", "ts"),
                ],
            ),
        ]

        deduped = _dedupe_features_by_physical_column(features)

        self.assertEqual(
            sorted(f["feature"] for f in deduped),
            ["active_last_q", "churned"],
        )

    def test_distinct_columns_are_never_merged(self):
        features = [
            self._feature("a", "proven_direct", "datasets/t.csv", "ColA"),
            self._feature("b", "candidate_unconfirmed", "datasets/t.csv", "ColB"),
        ]
        deduped = _dedupe_features_by_physical_column(features)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(
            [f["state"] for f in deduped],
            ["proven_direct", "candidate_unconfirmed"],
        )

    def test_unresolved_features_without_columns_are_preserved(self):
        features = [
            self._feature("a", "proven_direct", "datasets/t.csv", "ColA"),
            {
                "feature": "mystery",
                "state": "blocked_missing_evidence",
                "source_columns": [],
                "question": "What defines `mystery`?",
            },
            {
                "feature": "enigma",
                "state": "blocked_missing_evidence",
                "source_columns": [],
                "question": "What defines `enigma`?",
            },
        ]
        deduped = _dedupe_features_by_physical_column(features)
        # No physical column => never collapsed together.
        self.assertEqual(len(deduped), 3)


if __name__ == "__main__":
    unittest.main()

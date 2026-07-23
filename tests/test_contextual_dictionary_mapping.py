import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.blocker_question_panel import BlockerQuestionPanelBuilder
from core.onboarding.kpi.feature_resolver import (
    KPIFeatureResolver,
    _dedupe_features_by_physical_column,
    _redupe_all_kpis_after_definitions,
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

    def test_redupe_after_definitions_collapses_a_definition_reintroduced_duplicate(self):
        # Found live (Healthcare-RCM-Data-Platform, kpi_002): _resolve_kpi
        # dedupes each KPI's OWN features by physical column internally
        # (its own _dedupe_features_by_physical_column call), but
        # apply_workspace_definitions_to_mapping runs AFTER every KPI is
        # resolved, updating an already-existing feature entry in place from
        # a reusable workspace-level definition -- with no re-dedup
        # afterward. "Department" auto-proved via the alias/dictionary path
        # inside _resolve_kpi (resolving to departments.Name); a SEPARATE
        # sibling feature "Name", still unresolved at the time _resolve_kpi's
        # own dedup ran, later got confirmed via a reusable workspace
        # definition pointing to the SAME physical column -- and nothing
        # re-collapsed the two afterward. The generated SQL emitted two
        # separate columns for the same value, and the SQL generator's
        # cuts-resolution and the harness's grain-coverage check picked
        # DIFFERENT ones of the two as canonical, so the harness failed
        # claiming a dimension was "absent" that was actually present under
        # a sibling feature's name.
        dept_dataset = "workspaces/rcm/datasets/departments.csv"
        mapping = {
            "kpis": [
                {
                    "kpi_id": "kpi_002",
                    "features": [
                        {
                            "feature": "Department",
                            "state": "proven_alias",
                            "resolution_type": "contextual_dictionary_column",
                            "source_columns": [{"dataset": dept_dataset, "column": "Name"}],
                        },
                        {
                            "feature": "Name",
                            "state": "candidate_unconfirmed",
                            "resolution_type": "contextual_column_candidate",
                            "source_columns": [{"dataset": dept_dataset, "column": "Name"}],
                            "candidates": [
                                {"source": dept_dataset, "column": "Name", "name_matched": True}
                            ],
                        },
                        {
                            "feature": "VisitType",
                            "state": "proven_direct",
                            "resolution_type": "direct_column",
                            "source_columns": [
                                {"dataset": "workspaces/rcm/datasets/transactions.csv", "column": "VisitType"}
                            ],
                        },
                    ],
                }
            ]
        }

        # Simulates apply_workspace_definitions_to_mapping confirming "Name"
        # via a reusable workspace-level definition, straight after
        # _resolve_kpi's own per-KPI dedup already ran and left it
        # unresolved (and therefore un-grouped with "Department").
        name_feature = next(f for f in mapping["kpis"][0]["features"] if f["feature"] == "Name")
        name_feature["state"] = "user_confirmed"
        name_feature["resolution_type"] = "physical_column"

        _redupe_all_kpis_after_definitions(mapping)

        features = mapping["kpis"][0]["features"]
        feature_names = [f["feature"] for f in features]
        same_column_features = [
            f for f in features
            if f.get("source_columns") and f["source_columns"][0].get("column") == "Name"
        ]
        self.assertEqual(
            len(same_column_features), 1,
            f"expected departments.Name to collapse to one feature, got: {feature_names}",
        )
        # VisitType (a genuinely different column) must survive untouched.
        self.assertIn("VisitType", feature_names)

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
        the top-ranked target the feature would resolve to. Each pair is
        either ``(dataset, column)`` (generic-context match, the common case)
        or ``(dataset, column, name_matched)`` to mark a candidate as
        genuinely reflecting the feature's OWN name (see
        ``_contextual_score``'s ``name_matched``), e.g. a misspelling.
        """
        def _unpack(pair):
            if len(pair) == 3:
                return pair
            dataset, column = pair
            return dataset, column, False

        return {
            "feature": name,
            "state": "candidate_unconfirmed",
            "resolution_type": "contextual_column_candidate",
            "source_columns": [
                {"dataset": dataset, "column": column}
                for dataset, column, _name_matched in (_unpack(pair) for pair in ranked_pairs)
            ],
            "candidates": [
                {
                    "state": "candidate_unconfirmed",
                    "source": dataset,
                    "column": column,
                    "name_matched": name_matched,
                }
                for dataset, column, name_matched in (_unpack(pair) for pair in ranked_pairs)
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
                    ("datasets/departments.csv", "Name", True),  # top-ranked, genuine name match
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

    def test_generic_top_candidate_matching_a_proven_sibling_is_not_a_phantom_duplicate(self):
        # Found live (kpi_001, Hostile_Synthetic): "on_time" has zero real
        # lexical relationship to "carrier_cd", but in a sparse-context KPI
        # both scored the SAME weak, generic top candidate
        # (shipments.carrier_cd) purely from KPI-text overlap -- not from
        # "on_time" itself matching that column. Because "carrier_cd" was
        # already proven to shipments.carrier_cd, the old second pass treated
        # "on_time"'s generic top candidate as a phantom duplicate of the
        # proven column and silently dropped it, permanently losing the
        # blocker question for a feature that had never actually resolved to
        # anything. A candidate with no genuine name-based relationship to
        # the feature (name_matched=False, the default) must never trigger
        # this drop, even when it happens to be the top of the list.
        features = [
            self._feature("carrier_cd", "proven_direct", "datasets/shipments.csv", "carrier_cd"),
            self._contextual_candidate_feature(
                "on_time",
                [
                    ("datasets/shipments.csv", "carrier_cd"),  # generic top match, no name relation
                    ("datasets/shipments.csv", "note"),
                    ("datasets/shipments.csv", "ts"),
                ],
            ),
        ]

        deduped = _dedupe_features_by_physical_column(features)

        self.assertIn("on_time", [f["feature"] for f in deduped])
        self.assertIn("candidate_unconfirmed", [f["state"] for f in deduped])
        self.assertEqual(len(deduped), 2)

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

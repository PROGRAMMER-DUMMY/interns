from __future__ import annotations

import unittest

from core.onboarding.features.blockers import infer_join_candidates, prioritize_blockers, risk_class


class FeatureBlockerTests(unittest.TestCase):
    def test_prioritize_blockers_ranks_financial_reuse_first(self):
        mapping = {
            "kpis": [
                {
                    "kpi_id": "KPI_001",
                    "features": [
                        {"feature": "PaidAmount", "state": "blocked_no_source"},
                        {"feature": "DepartmentID", "state": "blocked_no_source"},
                    ],
                },
                {
                    "kpi_id": "KPI_002",
                    "features": [
                        {"feature": "PaidAmount", "state": "candidate_unconfirmed"},
                        {"feature": "ServiceDate", "state": "blocked_no_source"},
                    ],
                },
            ]
        }

        clusters = prioritize_blockers(mapping)

        self.assertEqual(clusters[0]["feature"], "PaidAmount")
        self.assertEqual(clusters[0]["count"], 2)
        self.assertEqual(clusters[0]["risk"], "financial_correctness")
        self.assertEqual(clusters[0]["example_kpis"], ["KPI_001", "KPI_002"])

    def test_infer_join_candidates_requires_same_key_across_datasets(self):
        joins = infer_join_candidates(
            [
                {
                    "source_columns": [
                        {"dataset": "claims", "column": "PatientID"},
                        {"dataset": "patients", "column": "PatientID"},
                    ]
                },
                {"source_columns": [{"dataset": "claims", "column": "PaidAmount"}]},
            ]
        )

        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0]["key"], "patientid")
        self.assertEqual(joins[0]["datasets"], ["claims", "patients"])

    def test_risk_classifies_temporal_and_structural_features(self):
        self.assertEqual(risk_class("ServiceDate"), "temporal_correctness")
        self.assertEqual(risk_class("ProviderID"), "structural")


if __name__ == "__main__":
    unittest.main()

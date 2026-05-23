from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.onboarding.kpi.generation_quality import (
    column_like_token_overlap,
    looks_like_business_question,
    missing_discussion_points,
    score_kpis,
)


class KPIGenerationQualityTests(unittest.TestCase):
    def test_score_kpis_rewards_dataset_and_data_model_evidence(self):
        kpi = SimpleNamespace(
            name="What is paid amount trend?",
            description="Owner uses this to decide payer follow-up.",
            cuts="ServiceDate, Payer",
            metric="sum(PaidAmount)",
            refinement_required="Include acceptance tests and filters.",
        )
        score = score_kpis(
            [kpi],
            {
                "files": ["workspaces/demo/datasets/paid_amount.csv"],
                "possible_kpi_files": ["docs/kpis.csv"],
                "possible_data_model_files": ["docs/model.md"],
            },
            ["docs/context.md"],
        )

        self.assertEqual(score["kpi_count"], 1)
        self.assertEqual(score["coverage"]["dataset_file_count"], 1)
        self.assertGreater(score["implementation_readiness"], 80)

    def test_missing_discussion_points_identifies_context_gap(self):
        kpi = SimpleNamespace(
            name="Paid amount",
            description="",
            cuts="",
            metric="sum(PaidAmount)",
            refinement_required="",
        )

        missing = missing_discussion_points(kpi, has_context=False)

        self.assertIn("stakeholder_context", missing)
        self.assertIn("grain_or_dimensions", missing)

    def test_business_question_and_column_overlap_helpers(self):
        self.assertTrue(looks_like_business_question("What is denial rate?"))
        self.assertTrue(column_like_token_overlap("paid amount", ["datasets/payment.csv"]))


if __name__ == "__main__":
    unittest.main()

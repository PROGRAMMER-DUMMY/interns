from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.onboarding.kpi.generation_quality import (
    column_like_token_overlap,
    looks_like_business_question,
    missing_discussion_points,
    result_format_candidates,
    score_kpis,
    understanding_score,
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
        self.assertTrue(column_like_token_overlap("paid amount", ["datasets/paid_amount.csv"]))


class UnderstandingScoreTests(unittest.TestCase):
    def test_score_bounded_and_structured(self):
        kpi = SimpleNamespace(
            name="What is the trend of total amount by month and region",
            description="monthly revenue", cuts="Month, Region",
            metric="sum(amount)", refinement_required=False,
        )
        u = understanding_score(kpi, has_context=False, data_files=["sales.csv"], has_data_model=True)
        self.assertTrue(0 <= u["score"] <= 100)
        for key in ("dimensions", "lowest_dimension", "next_question"):
            self.assertIn(key, u)

    def test_well_specified_scores_higher_than_vague(self):
        strong = understanding_score(
            SimpleNamespace(name="Trend of total amount by month and region", description="rev",
                            cuts="Month, Region", metric="sum(amount)", refinement_required=False),
            has_context=True, data_files=["sales.csv"], has_data_model=True,
        )["score"]
        vague = understanding_score(
            SimpleNamespace(name="stuff", description="", cuts="", metric="", refinement_required=False),
            has_context=False, data_files=[], has_data_model=False,
        )["score"]
        self.assertGreater(strong, vague)


class ResultFormatCandidatesTests(unittest.TestCase):
    def test_offers_multiple_example_layouts_with_tables(self):
        kpi = {"name": "top 10 regions by amount over time", "metric": "sum(amount)", "cuts": "Month, Region"}
        candidates = result_format_candidates(kpi)
        self.assertGreaterEqual(len(candidates), 2)
        for candidate in candidates:
            blob = " ".join(str(v) for v in candidate.values())
            self.assertIn("|", blob, f"layout {candidate.get('layout_id')} lacks an example table")


class SkillRoutingTests(unittest.TestCase):
    def test_grill_active_with_score_driven_mode(self):
        # Phase 1 consolidation: self-grill + clarify-ambiguity merged into
        # grill-requirements. One entry, always active; the WHY switches from
        # full interview (low score) to clarify-ambiguity mode (high score).
        from core.onboarding.kpi.generation_workflow import _conversation_skills

        low = {s["name"]: s for s in _conversation_skills({"quality_score": {"understanding_score": 30}})}
        high = {s["name"]: s for s in _conversation_skills({"quality_score": {"understanding_score": 90}})}
        self.assertTrue(low["grill-requirements"]["active"])
        self.assertTrue(high["grill-requirements"]["active"])
        self.assertNotIn("Clarify-ambiguity mode", low["grill-requirements"]["why"])
        self.assertIn("Clarify-ambiguity mode", high["grill-requirements"]["why"])
        self.assertTrue(high["to-solution-brief"]["active"])


if __name__ == "__main__":
    unittest.main()

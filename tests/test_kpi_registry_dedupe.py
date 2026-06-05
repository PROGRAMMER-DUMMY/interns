"""KPI ingestion dedupe across registries.

Onboarding can discover both a finalized generation registry and the raw source
file it was generated from; ingesting both double-counts every KPI (the 20-vs-10
bug). Dedupe collapses by normalized business question, keeping the richest
entry. Workspace-agnostic — keyed on question text only, never a source path.
"""
from __future__ import annotations

import unittest

from core.onboarding.workspace.onboarding import (
    KpiDefinition,
    _dedupe_kpis_by_name,
    _kpi_name_key,
)


class KpiDedupeTests(unittest.TestCase):
    def test_same_question_from_two_registries_collapses_to_one(self) -> None:
        kpis = [
            KpiDefinition(
                name="How many orders occurred each year?",
                description="Orders overview",
                refinement_required="Discuss owner, tests",
                source="docs/kpi_registry.generated.json",
            ),
            KpiDefinition(
                name="How many orders occurred each year?",
                source="docs/orders_questions.sql",
            ),
        ]
        deduped, warnings = _dedupe_kpis_by_name(kpis)
        self.assertEqual(len(deduped), 1)
        # The richer entry (has description + refinement) survives.
        self.assertEqual(deduped[0].source, "docs/kpi_registry.generated.json")
        self.assertTrue(any("kpi_dedupe" in w for w in warnings))

    def test_richer_entry_wins_regardless_of_order(self) -> None:
        bare = KpiDefinition(name="Average order value by region?", source="a.sql")
        rich = KpiDefinition(
            name="Average order value by region?",
            metric="avg(order_value)",
            cuts="region",
            source="b.json",
        )
        deduped, _ = _dedupe_kpis_by_name([bare, rich])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].metric, "avg(order_value)")

    def test_distinct_questions_are_preserved_in_order(self) -> None:
        kpis = [
            KpiDefinition(name="Q one?"),
            KpiDefinition(name="Q two?"),
            KpiDefinition(name="Q one?"),  # duplicate of the first
        ]
        deduped, warnings = _dedupe_kpis_by_name(kpis)
        self.assertEqual([k.name for k in deduped], ["Q one?", "Q two?"])
        self.assertTrue(any("kpi_dedupe:1" in w for w in warnings))

    def test_name_key_normalizes_punctuation_and_whitespace(self) -> None:
        self.assertEqual(
            _kpi_name_key("How  many ORDERS, each year?"),
            _kpi_name_key("how many orders each year"),
        )

    def test_empty_names_are_never_collapsed(self) -> None:
        kpis = [KpiDefinition(name=""), KpiDefinition(name="")]
        deduped, warnings = _dedupe_kpis_by_name(kpis)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()

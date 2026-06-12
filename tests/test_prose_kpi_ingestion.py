"""Prose markdown KPI ingestion (hostile-workspace finding F2).

A natural-language KPI document ("### KPI 1 - On-time delivery rate" followed
by stakeholder prose) must yield KPI rows whose full prose body survives into
``description``. Heading-only ingestion starved feature extraction and produced
blocked-with-no-question dead ends.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.onboarding import _read_markdown_kpis


def _parse(markdown: str) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "kpi_wishlist.md"
        path.write_text(markdown, encoding="utf-8")
        return _read_markdown_kpis(path, root)


class ProseSectionIngestionTests(unittest.TestCase):
    def test_heading_plus_prose_body_becomes_description(self) -> None:
        kpis = _parse(
            "# Notes from the review\n"
            "\n"
            "Intro paragraph that belongs to no KPI.\n"
            "\n"
            "### KPI 1 - On-time delivery rate\n"
            "\n"
            'Marco: "Of everything we delivered, what share got there on time."\n'
            "\n"
            "(Note: unclear if counted from booking or pickup.)\n"
            "\n"
            "### KPI 2 - Monthly revenue per active account\n"
            "\n"
            "Take what we actually billed, by month.\n"
        )
        self.assertEqual(len(kpis), 2)
        self.assertEqual(kpis[0].name, "KPI 1 - On-time delivery rate")
        self.assertIn("what share got there on time", kpis[0].description)
        self.assertIn("booking or pickup", kpis[0].description)
        self.assertEqual(kpis[1].name, "KPI 2 - Monthly revenue per active account")
        self.assertIn("actually billed", kpis[1].description)

    def test_prose_body_stops_at_same_level_non_kpi_heading(self) -> None:
        kpis = _parse(
            "### KPI 1 - Damage claim rate\n"
            "Share of delivered jobs with a damage claim.\n"
            "### Appendix\n"
            "This appendix text belongs to no KPI.\n"
        )
        self.assertEqual(len(kpis), 1)
        self.assertIn("damage claim", kpis[0].description)
        self.assertNotIn("appendix text", kpis[0].description)

    def test_prose_body_stops_at_thematic_break(self) -> None:
        kpis = _parse(
            "### KPI 10 - Perfect shipment rate\n"
            "Delivered on time, no claim, no dispute.\n"
            "---\n"
            "Shared trailer: the data dictionary may be stale.\n"
        )
        self.assertEqual(len(kpis), 1)
        self.assertIn("no claim, no dispute", kpis[0].description)
        self.assertNotIn("Shared trailer", kpis[0].description)

    def test_deeper_subheading_stays_in_body(self) -> None:
        kpis = _parse(
            "## KPI 3 - Average depot dwell hours\n"
            "How long freight sits before it moves out.\n"
            "### Carlisle detail\n"
            "The Carlisle lot swear it is under four hours.\n"
        )
        self.assertEqual(len(kpis), 1)
        self.assertIn("Carlisle detail", kpis[0].description)
        self.assertIn("under four hours", kpis[0].description)

    def test_hostile_workspace_wishlist_keeps_all_prose(self) -> None:
        doc = Path("workspaces/Hostile_Synthetic/docs/kpi_wishlist_ops_review.md")
        if not doc.exists():  # pragma: no cover - optional fixture
            self.skipTest("hostile workspace fixture not present")
        kpis = _read_markdown_kpis(doc.resolve(), Path.cwd().resolve())
        self.assertEqual(len(kpis), 10)
        self.assertTrue(all(kpi.description.strip() for kpi in kpis))
        by_name = {kpi.name: kpi for kpi in kpis}
        on_time = by_name["KPI 1 - On-time delivery rate"]
        self.assertIn("service promise", on_time.description)
        # Sandra's parenthetical doubt is evidence and must survive.
        self.assertIn("booked", on_time.description)


class TableIngestionRegressionTests(unittest.TestCase):
    def test_plain_table_rows_still_parse(self) -> None:
        kpis = _parse(
            "| KPI | Description |\n"
            "|---|---|\n"
            "| Net revenue | Total billed minus credits |\n"
            "| Claim rate | Claims over delivered |\n"
        )
        self.assertEqual([kpi.name for kpi in kpis], ["Net revenue", "Claim rate"])
        self.assertEqual(kpis[0].description, "Total billed minus credits")

    def test_table_under_kpi_container_heading_yields_row_kpis(self) -> None:
        kpis = _parse(
            "## KPIs\n"
            "\n"
            "| KPI | Description |\n"
            "|---|---|\n"
            "| Net revenue | Total billed minus credits |\n"
        )
        self.assertEqual([kpi.name for kpi in kpis], ["Net revenue"])

    def test_kpi_heading_with_prose_and_table_keeps_prose_description(self) -> None:
        kpis = _parse(
            "### KPI 7 - Invoice dispute rate\n"
            "Share of invoices disputed, monthly.\n"
            "| charge_type | meaning |\n"
            "|---|---|\n"
            "| BASE | base freight |\n"
        )
        self.assertEqual(len(kpis), 1)
        self.assertEqual(kpis[0].name, "KPI 7 - Invoice dispute rate")
        self.assertIn("Share of invoices disputed", kpis[0].description)


class WikiGovernanceNoteExclusionTests(unittest.TestCase):
    """Platform-generated wiki notes are decision records, not KPI inputs.

    Re-ingesting them creates phantom KPIs on re-onboarding (an RCM wiki
    feature note resurfaced as a 4th 'ready' KPI)."""

    def test_wiki_files_are_not_kpi_or_model_inputs(self) -> None:
        from core.onboarding.workspace.onboarding import WorkspaceOnboarder

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "wiki" / "features").mkdir(parents=True)
            (workspace / "docs" / "kpi_wishlist.md").write_text(
                "### KPI 1 - Revenue\nBilled revenue by month.\n", encoding="utf-8"
            )
            (workspace / "datasets" / "orders.csv").write_text(
                "Id,Amount\nO1,10\n", encoding="utf-8"
            )
            (workspace / "wiki" / "features" / "intent_kpi_001_grain.md").write_text(
                "# Feature: intent::kpi_001::grain\n\n- Accepted option_a\n",
                encoding="utf-8",
            )
            inputs = WorkspaceOnboarder(root, "workspaces/demo").discover_inputs()
            self.assertEqual(
                inputs.kpi_registries, ["workspaces/demo/docs/kpi_wishlist.md"]
            )
            self.assertFalse(
                any(
                    "wiki" in path
                    for path in inputs.kpi_registries + inputs.data_models
                ),
                f"wiki note leaked into inputs: {inputs}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

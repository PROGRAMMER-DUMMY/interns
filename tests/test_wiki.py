"""Unit tests for the per-workspace LLM wiki module."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.wiki import (
    WikiLayout,
    build_feature_scaffold,
    read_feature_note,
    upsert_feature_note,
)
from core.wiki.template import WHY_TODO_MARKER


def _panel(feature: str = "revenue_amt") -> dict:
    return {
        "feature": feature,
        "applies_to_kpis": ["KPI-001", "KPI-002"],
        "evidence_files": ["workspaces/x/datasets/claims/claims.csv"],
        "options": [
            {
                "option_id": "option_a",
                "label": "claims.revenue_amt",
                "json_backed": True,
                "business_summary": "Use revenue_amt as the canonical mapping.",
                "physical_column_option": {
                    "dataset": "claims",
                    "column": "revenue_amt",
                    "dtype": "double",
                    "row_count": 12345,
                },
            },
            {
                "option_id": "option_b",
                "label": "claims.claim_amt",
                "json_backed": True,
                "business_summary": "Different grain — per-claim, not per-encounter.",
                "physical_column_option": {
                    "dataset": "claims",
                    "column": "claim_amt",
                    "dtype": "double",
                },
            },
        ],
    }


class WikiWriterTests(unittest.TestCase):
    def test_first_apply_scaffolds_machine_and_human_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WikiLayout(project_root=Path(tmp))
            panel = _panel()
            scaffold = build_feature_scaffold(
                feature="revenue_amt",
                option=panel["options"][0],
                panel=panel,
                evidence_note="picked the canonical column",
                contract_ref="workspaces/x/.../workspace_feature_definitions.json#revenue_amt",
            )
            path = upsert_feature_note(layout, "revenue_amt", scaffold)

            self.assertTrue(path.exists())
            note = read_feature_note(layout, "revenue_amt")
            self.assertIsNotNone(note)
            self.assertEqual(note.frontmatter["entity_type"], "feature")
            self.assertEqual(note.frontmatter["entity_id"], "revenue_amt")
            self.assertEqual(note.frontmatter["applies_to_kpis"], ["KPI-001", "KPI-002"])
            self.assertIn("Current state", note.sections)
            self.assertIn("Decision history", note.sections)
            self.assertIn("claims.revenue_amt", note.sections["Current state"])
            self.assertEqual(len(note.sections["Decision history"].splitlines()), 1)
            # Human sections seeded with TODO markers, not user prose
            self.assertIn(WHY_TODO_MARKER, note.sections["Why (user)"])
            self.assertFalse(note.has_user_why)
            # Rejected option_b appears
            self.assertIn("option_b", note.sections["Rejected options"])
            # Evidence rendered
            self.assertIn("claims.csv", note.sections["Evidence"])

    def test_reapply_appends_history_and_preserves_user_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WikiLayout(project_root=Path(tmp))
            panel = _panel()
            scaffold1 = build_feature_scaffold(
                feature="revenue_amt",
                option=panel["options"][0],
                panel=panel,
                contract_ref="x",
                timestamp="2026-05-16T10:00:00+00:00",
            )
            upsert_feature_note(layout, "revenue_amt", scaffold1)

            # User edits the human-owned `Why` section
            path = layout.feature_note_path("revenue_amt")
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                WHY_TODO_MARKER,
                "Finance uses revenue_amt as the canonical net-of-adjustments amount.",
            )
            path.write_text(text, encoding="utf-8")

            # Re-apply with a different option (simulating a re-decision)
            scaffold2 = build_feature_scaffold(
                feature="revenue_amt",
                option=panel["options"][1],
                panel=panel,
                contract_ref="x",
                timestamp="2026-05-17T11:00:00+00:00",
            )
            upsert_feature_note(layout, "revenue_amt", scaffold2)

            note = read_feature_note(layout, "revenue_amt")
            self.assertIsNotNone(note)
            # History appended (2 entries, newest at bottom)
            history_lines = note.sections["Decision history"].splitlines()
            self.assertEqual(len(history_lines), 2)
            self.assertIn("2026-05-16T10:00:00", history_lines[0])
            self.assertIn("2026-05-17T11:00:00", history_lines[1])
            # User prose preserved verbatim
            self.assertTrue(note.has_user_why)
            self.assertIn("canonical net-of-adjustments", note.why)
            # Current state reflects latest decision
            self.assertIn("claim_amt", note.sections["Current state"])

    def test_custom_definition_records_user_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WikiLayout(project_root=Path(tmp))
            panel = _panel(feature="encounter_date")
            panel["options"] = [
                {
                    "option_id": "custom",
                    "label": "Custom rule",
                    "json_backed": False,
                }
            ]
            scaffold = build_feature_scaffold(
                feature="encounter_date",
                option=panel["options"][0],
                panel=panel,
                custom_definition="MIN(service_dt, admit_dt) per encounter",
                evidence_note="confirmed with finance",
                contract_ref="x",
            )
            upsert_feature_note(layout, "encounter_date", scaffold)
            note = read_feature_note(layout, "encounter_date")
            self.assertIsNotNone(note)
            self.assertIn("MIN(service_dt, admit_dt)", note.sections["Current state"])
            self.assertIn("custom", note.sections["Decision history"])

    def test_slugified_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = WikiLayout(project_root=Path(tmp))
            panel = _panel(feature="Net Patient Revenue ($)")
            scaffold = build_feature_scaffold(
                feature="Net Patient Revenue ($)",
                option=panel["options"][0],
                panel=panel,
                contract_ref="x",
            )
            path = upsert_feature_note(layout, "Net Patient Revenue ($)", scaffold)
            self.assertTrue(path.name.endswith(".md"))
            # Special chars collapsed
            self.assertNotIn("$", path.name)
            self.assertNotIn(" ", path.name)


if __name__ == "__main__":
    unittest.main()

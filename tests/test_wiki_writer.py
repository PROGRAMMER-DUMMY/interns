"""Regression tests for the KPI wiki writer (W1 SQL-trim + W2 lineage/links).

Asserts, on a fully synthetic (workspace-agnostic) fixture:
- W1: the full generated SQL is NOT inlined; a ``SQL:`` link plus the
  result-shaping body and a preview table are present.
- W2: Lineage, Decision history, and ``[[name]]`` cross-links render, with
  relationship provenance (source / confirmed_by) carried through.
- Human-authored sections survive a re-write verbatim.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.wiki import (
    WikiLayout,
    build_kpi_completion_scaffold,
    upsert_kpi_note,
)
from core.wiki.reader import read_note


KPI_ID = "kpi_007"

# A deliberately multi-statement script: bootstrap + features view + results
# view. Only the results body should surface on the wiki page.
FULL_SQL = """\
-- Authoritative KPI SQL.
INSTALL delta;
LOAD delta;

CREATE OR REPLACE VIEW "kpi_007_features" AS
SELECT s0."amount" AS "amount", s1."region" AS "region"
FROM "orders" AS s0
LEFT JOIN "customers" AS s1 ON s0."cust_id" = s1."cust_id";

CREATE OR REPLACE VIEW "kpi_007_results" AS
SELECT "region" AS region, ROUND(SUM("amount"), 2) AS total_amount
FROM "kpi_007_features"
WHERE "region" = 'NORTH'
GROUP BY "region"
ORDER BY "region";
"""

PREVIEW_MD = """\
| region | total_amount |
| ------ | ------------ |
| NORTH  | 1234.56      |
"""


def _write_contracts(project_root: Path) -> str:
    """Lay down minimal source-to-target / relationship / decision contracts."""
    contracts = project_root / "interns" / "generated" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    solutions = project_root / "interns" / "generated" / "solutions"
    solutions.mkdir(parents=True, exist_ok=True)

    ds_orders = "workspaces/demo/datasets/orders.csv"
    ds_customers = "workspaces/demo/datasets/customers.csv"
    rel_id = "orders__cust_id__customers__cust_id"

    plan = {
        "kpis": [
            {
                "kpi_id": KPI_ID,
                "dimensions_and_filters": "region; region = NORTH",
                "selected_source_datasets": [ds_orders, ds_customers],
                "join_plan": {
                    "executable_relationships": [
                        {
                            "relationship_id": rel_id,
                            "left_dataset": ds_orders,
                            "left_column": "cust_id",
                            "right_dataset": ds_customers,
                            "right_column": "cust_id",
                            "state": "user_confirmed",
                        }
                    ]
                },
                "grain": {
                    "description": "region",
                    "dimensions": ["region"],
                },
                "temporal_anchor": {"features": ["order_date"]},
            }
        ]
    }
    (contracts / "source_to_target_plan.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )

    rels = {
        "relationships": [
            {
                "relationship_id": rel_id,
                "left_dataset": ds_orders,
                "left_column": "cust_id",
                "right_dataset": ds_customers,
                "right_column": "cust_id",
                "state": "user_confirmed",
                "approval": {
                    "state": "approved",
                    "source": "human",
                    "confirmed_by": "casey",
                    "approved_at": "2026-06-09T10:00:00+00:00",
                },
            }
        ]
    }
    (contracts / "relationship_contracts.json").write_text(
        json.dumps(rels), encoding="utf-8"
    )

    decisions = {
        "grain_bucketing_decisions": {KPI_ID: "band_continuous_cuts"},
        "grain_bucketing_reasons": {KPI_ID: "intent-contract answer (human)"},
        "percentage_denominator_scopes": {KPI_ID: "all_rows"},
    }
    (contracts / "pipeline_decisions.json").write_text(
        json.dumps(decisions), encoding="utf-8"
    )

    sql_path_abs = solutions / f"{KPI_ID}.sql"
    sql_path_abs.write_text(FULL_SQL, encoding="utf-8")
    # Path stored on the entry is repo-relative, as flow.py records it.
    return "interns/generated/solutions/" + f"{KPI_ID}.sql"


def _entry(sql_rel_path: str) -> dict:
    return {
        "kpi_id": KPI_ID,
        "sql_path": sql_rel_path,
        "sql_text": FULL_SQL,
        "definition": {
            "business_question": "What is total amount by region for the north region",
            "metric": "sum(amount)",
            "cuts": "region",
            "feature_mappings": [{"feature": "amount"}, {"feature": "region"}],
        },
        "status": "ok",
        "preview_markdown": PREVIEW_MD,
        "result_view": f"{KPI_ID}_results",
    }


class KpiWikiWriterTests(unittest.TestCase):
    def _build(self, project_root: Path, sql_rel: str):
        return build_kpi_completion_scaffold(
            kpi_id=KPI_ID,
            entry=_entry(sql_rel),
            timestamp="2026-06-10T00:00:00+00:00",
            project_root=project_root,
        )

    def test_w1_sql_not_inlined_but_linked_with_shaping_and_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            sql_rel = _write_contracts(project_root)
            layout = WikiLayout(project_root=project_root)
            scaffold = self._build(project_root, sql_rel)
            path = upsert_kpi_note(layout, KPI_ID, scaffold)
            text = path.read_text(encoding="utf-8")

            # Full SQL is NOT inlined: bootstrap + the features-view DEFINITION
            # are absent (the results view legitimately reads FROM that view).
            self.assertNotIn("INSTALL delta", text)
            self.assertNotIn('CREATE OR REPLACE VIEW "kpi_007_features"', text)
            self.assertNotIn("LEFT JOIN", text)
            # SQL link IS present (both Definition and Current state reference it).
            self.assertIn(f"]({sql_rel})", text)
            self.assertIn("Full generated SQL", text)
            # Result-shaping body IS present (the results view only).
            self.assertIn("result-shaping view: kpi_007_results", text)
            self.assertIn("GROUP BY", text)
            self.assertIn("WHERE", text)
            # Preview table IS present.
            self.assertIn("| total_amount |", text)
            self.assertIn("1234.56", text)

    def test_w2_lineage_crosslinks_and_decision_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            sql_rel = _write_contracts(project_root)
            layout = WikiLayout(project_root=project_root)
            scaffold = self._build(project_root, sql_rel)
            path = upsert_kpi_note(layout, KPI_ID, scaffold)
            note = read_note(path)
            self.assertIsNotNone(note)

            # Lineage section: datasets -> joins -> grain -> filters.
            lineage = note.sections["Lineage"]
            self.assertIn("Datasets", lineage)
            self.assertIn("[[orders]]", lineage)
            self.assertIn("[[customers]]", lineage)
            self.assertIn("Joins", lineage)
            self.assertIn("[[orders__cust_id__customers__cust_id]]", lineage)
            self.assertIn("Grain", lineage)
            self.assertIn("region", lineage)
            self.assertIn("Filters", lineage)

            # Decision history: grain bucketing + denominator + join provenance.
            decisions = note.sections["Decision history"]
            self.assertIn("band_continuous_cuts", decisions)
            self.assertIn("intent-contract answer (human)", decisions)
            self.assertIn("Denominator scope", decisions)
            self.assertIn("all_rows", decisions)
            self.assertIn("source `human`", decisions)
            self.assertIn("confirmed_by `casey`", decisions)

            # Cross-links surfaced in frontmatter + Related notes (feature links too).
            self.assertIn("[[orders]]", note.frontmatter.get("related", []))
            self.assertIn("[[amount]]", note.frontmatter.get("related", []))
            self.assertIn("[[orders]]", note.sections["Related notes"])

    def test_human_sections_preserved_across_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            sql_rel = _write_contracts(project_root)
            layout = WikiLayout(project_root=project_root)
            upsert_kpi_note(layout, KPI_ID, self._build(project_root, sql_rel))

            path = layout.kpi_note_path(KPI_ID)
            text = path.read_text(encoding="utf-8")
            marker = "<!-- TODO: explain why this option was the right business choice -->"
            self.assertIn(marker, text)
            text = text.replace(
                marker,
                "Finance signed off on NORTH-only scope for the regional rollup.",
            )
            # Also edit the (auto-seeded) Related notes to prove it persists.
            text = text.replace(
                "## Related notes\n",
                "## Related notes\n\nSee also the quarterly board deck.\n",
                1,
            )
            path.write_text(text, encoding="utf-8")

            # Re-write (e.g. a re-run). Machine sections refresh; human stays.
            upsert_kpi_note(layout, KPI_ID, self._build(project_root, sql_rel))
            note = read_note(path)
            self.assertIsNotNone(note)
            self.assertIn(
                "Finance signed off on NORTH-only scope", note.sections["Why (user)"]
            )
            self.assertIn(
                "quarterly board deck", note.sections["Related notes"]
            )
            # Machine sections still correct after the rewrite.
            self.assertIn("result-shaping view: kpi_007_results", note.sections["Current state"])
            self.assertIn("band_continuous_cuts", note.sections["Decision history"])

    def test_degrades_without_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            layout = WikiLayout(project_root=project_root)
            # No contracts written; sql_path points nowhere resolvable.
            scaffold = build_kpi_completion_scaffold(
                kpi_id=KPI_ID,
                entry=_entry("interns/generated/solutions/kpi_007.sql"),
                timestamp="2026-06-10T00:00:00+00:00",
                project_root=project_root,
            )
            path = upsert_kpi_note(layout, KPI_ID, scaffold)
            note = read_note(path)
            self.assertIsNotNone(note)
            # Still trims SQL and shows shaping (shaping comes from sql_text).
            self.assertIn("result-shaping view: kpi_007_results", note.sections["Current state"])
            self.assertNotIn("INSTALL delta", path.read_text(encoding="utf-8"))
            # Lineage/decisions degrade gracefully, not crash.
            self.assertIn("No lineage recorded", note.sections["Lineage"])
            self.assertIn("No machine decisions recorded", note.sections["Decision history"])


if __name__ == "__main__":
    unittest.main()

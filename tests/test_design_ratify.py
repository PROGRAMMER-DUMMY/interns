"""Minimal medallion design-panel ratification (G2 unblock slice of 3.6).

Contract under test:
- Ratifying a fact/dimension/relationship flips needs_user_confirmation to
  False and records confirmed_by/confirmed_at/confirmation_reasoning on the
  item itself, in star_schema.json.
- Ratifying regenerates the design panel so the item drops off open_count.
- A blank --reasoning is refused outright -- nothing is written. This is the
  whole point of the tool: it must not collapse into a name-only rubber
  stamp indistinguishable from a real review in the audit trail.
- Agent-asserted confirmed_by is refused outright -- nothing is written.
- An unknown item id is refused outright -- nothing is written.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.medallion.design import _write_design_panel
from core.medallion.design_ratify import ratify_design_panel_item
from core.medallion.silver_contract import SilverContract
from core.medallion.star_schema import DimensionTable, FactTable, Relationship, StarSchema
from core.storage.workspace_layout import WorkspaceLayout


def _fixture_workspace(tmp: str) -> tuple[Path, Path]:
    repo = Path(tmp)
    ws = repo / "workspaces" / "demo"
    layout = WorkspaceLayout(project_root=ws)
    medallion_dir = layout.generated_dir / "medallion"
    medallion_dir.mkdir(parents=True, exist_ok=True)

    schema = StarSchema(
        workspace="demo",
        facts=[FactTable(name="accessorial", grain="one row per accessorial charge")],
        dimensions=[DimensionTable(name="provider")],
        relationships=[
            Relationship(
                from_table="silver.accessorial", from_column="provider_id",
                to_table="silver.provider", to_column="provider_id",
            )
        ],
    )
    (medallion_dir / "star_schema.json").write_text(json.dumps(schema.to_dict(), indent=2), encoding="utf-8")

    silver_contract = SilverContract(workspace="demo")
    (medallion_dir / "silver_contract.json").write_text(
        json.dumps(silver_contract.to_dict(), indent=2), encoding="utf-8"
    )
    _write_design_panel(layout, schema, silver_contract)
    return repo, ws


class RatifyDesignPanelTests(unittest.TestCase):
    def test_ratify_fact_flips_flag_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            result = ratify_design_panel_item(
                repo, ws, item="fact:accessorial", answer="ratify",
                confirmed_by="shubham", reasoning="pipeline-mechanics proof only, no KPI depends on this table",
            )
            self.assertEqual(result["source"], "human")
            self.assertEqual(result["remaining_open_count"], 2)

            layout = WorkspaceLayout(project_root=ws)
            data = json.loads((layout.generated_dir / "medallion" / "star_schema.json").read_text(encoding="utf-8"))
            fact = next(f for f in data["facts"] if f["name"] == "accessorial")
            self.assertFalse(fact["needs_user_confirmation"])
            self.assertEqual(fact["confirmed_by"], "shubham")
            self.assertEqual(
                fact["confirmation_reasoning"],
                "pipeline-mechanics proof only, no KPI depends on this table",
            )
            self.assertTrue(fact["confirmed_at"])

    def test_ratify_dimension_and_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            ratify_design_panel_item(
                repo, ws, item="dim:provider", answer="ratify",
                confirmed_by="shubham", reasoning="no history requirement identified for this fixture",
            )
            result = ratify_design_panel_item(
                repo, ws, item="rel:silver.accessorial.provider_id->silver.provider.provider_id",
                answer="ratify", confirmed_by="shubham", reasoning="natural FK, unambiguous",
            )
            self.assertEqual(result["remaining_open_count"], 1)

    def test_regenerated_panel_drops_ratified_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            ratify_design_panel_item(
                repo, ws, item="fact:accessorial", answer="ratify",
                confirmed_by="shubham", reasoning="pipeline-mechanics proof only",
            )
            layout = WorkspaceLayout(project_root=ws)
            panel = json.loads(
                (layout.reports_dir / "medallion_design_panel" / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(panel["open_count"], 2)
            ids = {i["id"] for i in panel["items"]}
            self.assertNotIn("fact:accessorial", ids)

    def test_blank_reasoning_is_refused_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            with self.assertRaises(ValueError):
                ratify_design_panel_item(
                    repo, ws, item="fact:accessorial", answer="ratify",
                    confirmed_by="shubham", reasoning="   ",
                )
            layout = WorkspaceLayout(project_root=ws)
            data = json.loads((layout.generated_dir / "medallion" / "star_schema.json").read_text(encoding="utf-8"))
            fact = next(f for f in data["facts"] if f["name"] == "accessorial")
            self.assertTrue(fact["needs_user_confirmation"])

    def test_agent_asserted_confirmer_is_refused_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            with self.assertRaises(ValueError):
                ratify_design_panel_item(
                    repo, ws, item="fact:accessorial", answer="ratify",
                    confirmed_by="claude", reasoning="looks fine",
                )
            with self.assertRaises(ValueError):
                ratify_design_panel_item(
                    repo, ws, item="fact:accessorial", answer="ratify",
                    confirmed_by="", reasoning="looks fine",
                )
            layout = WorkspaceLayout(project_root=ws)
            data = json.loads((layout.generated_dir / "medallion" / "star_schema.json").read_text(encoding="utf-8"))
            fact = next(f for f in data["facts"] if f["name"] == "accessorial")
            self.assertTrue(fact["needs_user_confirmation"])

    def test_unknown_item_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = _fixture_workspace(tmp)
            with self.assertRaises(ValueError):
                ratify_design_panel_item(
                    repo, ws, item="fact:does_not_exist", answer="ratify",
                    confirmed_by="shubham", reasoning="n/a",
                )


if __name__ == "__main__":
    unittest.main()

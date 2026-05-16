"""End-to-end integration: apply_kpi_panel_answer writes a wiki note, the next
panel build reads it back and surfaces a `prior_decision_wiki` block.

The real apply path touches validation, the resolver, and a re-prepare step.
Here we stub those out and exercise just the wiki splice — the value is proving
the wiring (apply → upsert → read → panel) actually fires through the public
entry points, not the resolver internals (which have their own coverage).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.onboarding import blocker_question_panel as panel_mod
from core.onboarding import kpi_blocker_workflow as workflow_mod
from core.wiki import WikiLayout, read_feature_note


def _make_workspace(tmp: Path) -> Path:
    workspace = tmp / "workspaces" / "demo"
    contracts = workspace / "interns" / "generated" / "contracts"
    reports = workspace / "interns" / "reports" / "blocker_question_panel"
    contracts.mkdir(parents=True)
    reports.mkdir(parents=True)
    panel = {
        "version": 1,
        "feature": "revenue_amt",
        "applies_to_kpis": ["KPI-001"],
        "evidence_files": ["workspaces/demo/datasets/claims.csv"],
        "options": [
            {
                "option_id": "option_a",
                "label": "claims.revenue_amt",
                "json_backed": True,
                "business_summary": "Canonical revenue mapping.",
                "physical_column_option": {
                    "dataset": "claims",
                    "column": "revenue_amt",
                    "dtype": "double",
                    "row_count": 1000,
                },
            },
            {
                "option_id": "custom",
                "label": "Enter a custom definition",
                "json_backed": False,
            },
        ],
    }
    (reports / "current.json").write_text(json.dumps(panel), encoding="utf-8")
    return workspace


class WikiIntegrationTests(unittest.TestCase):
    def test_apply_writes_wiki_note_via_real_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _make_workspace(root)

            class _Validation:
                ok = True
                errors: list[str] = []

                def summary(self):
                    return {"ok": True}

            class _Prepared:
                def summary(self):
                    return {"prepared": True}

            with patch.object(
                workflow_mod, "WorkspaceArtifactValidator"
            ) as mock_validator, patch.object(
                workflow_mod, "apply_workspace_definition", return_value={"summary": "ok"}
            ) as mock_apply, patch.object(
                workflow_mod, "prepare_kpi_blocker_panel", return_value=_Prepared()
            ):
                mock_validator.return_value.run.return_value = _Validation()
                result = workflow_mod.apply_kpi_panel_answer(
                    root,
                    "workspaces/demo",
                    answer="option_a",
                )

            self.assertEqual(result.accepted_option_id, "option_a")
            mock_apply.assert_called_once()

            note = read_feature_note(
                WikiLayout(project_root=workspace), "revenue_amt"
            )
            self.assertIsNotNone(note)
            self.assertEqual(note.frontmatter["entity_id"], "revenue_amt")
            self.assertEqual(note.frontmatter["applies_to_kpis"], ["KPI-001"])
            self.assertIn("claims.revenue_amt", note.sections["Current state"])

    def test_panel_builder_reads_prior_wiki_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _make_workspace(root)

            # First, simulate apply by writing the note via the real upsert path
            from core.wiki import build_feature_scaffold, upsert_feature_note

            layout = WikiLayout(project_root=workspace)
            panel = json.loads(
                (workspace / "interns" / "reports" / "blocker_question_panel" / "current.json")
                .read_text(encoding="utf-8")
            )
            scaffold = build_feature_scaffold(
                feature="revenue_amt",
                option=panel["options"][0],
                panel=panel,
                evidence_note="canonical pick",
                contract_ref="demo#revenue_amt",
            )
            upsert_feature_note(layout, "revenue_amt", scaffold)

            # User fills the why
            note_path = layout.feature_note_path("revenue_amt")
            text = note_path.read_text(encoding="utf-8")
            text = text.replace(
                "<!-- TODO: explain why this option was the right business choice -->",
                "Finance uses revenue_amt as the canonical net-of-adjustments amount.",
            )
            note_path.write_text(text, encoding="utf-8")

            # Now build a synthetic mapping with the same unresolved feature and let
            # the panel builder read the prior wiki decision back.
            mapping = {
                "workspace": "workspaces/demo",
                "kpis": [
                    {
                        "kpi_id": "KPI-001",
                        "source": "workspaces/demo/datasets/claims.csv",
                        "features": [
                            {
                                "feature": "revenue_amt",
                                "state": "blocked_ambiguous",
                                "source_columns": [
                                    {
                                        "dataset": "claims",
                                        "column": "revenue_amt",
                                        "dtype": "double",
                                        "row_count": 1000,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            (workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json").write_text(
                json.dumps(mapping), encoding="utf-8"
            )

            builder = panel_mod.BlockerQuestionPanelBuilder(root, "workspaces/demo")
            result = builder.run()

            # The current panel JSON should carry the wiki block
            current = json.loads(Path(root / result.current_json).read_text(encoding="utf-8"))
            self.assertIn("prior_decision_wiki", current)
            self.assertTrue(current["prior_decision_wiki"]["has_user_why"])
            self.assertIn(
                "net-of-adjustments", current["prior_decision_wiki"]["user_why"]
            )

            # The markdown render also surfaces it
            md = Path(root / result.current_markdown).read_text(encoding="utf-8")
            self.assertIn("Prior Decision (from wiki)", md)
            self.assertIn("net-of-adjustments", md)


if __name__ == "__main__":
    unittest.main()

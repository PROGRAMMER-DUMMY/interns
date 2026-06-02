"""Routing of low-confidence KPI intent-contract facets into the blocker panel,
the answer store, denominator-scope mirroring, and gate-provenance surfacing.

Closes the handoff's "intent-contract routing not wired" gap.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.intent_contract import (
    answer_key,
    intent_facet_panel_questions,
    load_intent_answers,
    record_intent_answer,
    write_intent_contract,
)
from core.onboarding.workspace.flow import _collect_gate_provenance
from core.storage.workspace_layout import WorkspaceLayout


_SHARE_KPI = {
    "kpi_id": "kpi_002",
    "name": "Percentage share of lives for department",
    "metric": "percentage of count(distinct member_id) / count(distinct member_id) for department",
    "cuts": "department",
    "features": [
        {"feature": "member_id", "source_columns": [{"column": "member_id", "dataset": "d.csv"}]},
        {"feature": "department", "source_columns": [{"column": "DeptName", "dataset": "d.csv"}]},
    ],
}


def _make_ws(tmp: Path, kpis: list[dict]) -> tuple[Path, str, WorkspaceLayout]:
    workspace_rel = "workspaces/demo"
    contracts = tmp / workspace_rel / "interns" / "generated" / "contracts"
    contracts.mkdir(parents=True)
    # Real onboarding shape: {"kpis": [...]}
    (contracts / "kpi_registry.json").write_text(
        json.dumps({"artifact_type": "kpi_registry.json", "kpis": kpis}), encoding="utf-8"
    )
    layout = WorkspaceLayout(project_root=(tmp / workspace_rel).resolve())
    return tmp, workspace_rel, layout


class PanelQuestionRoutingTests(unittest.TestCase):
    def test_share_kpi_produces_denominator_question(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _, ws, _ = _make_ws(tmp, [_SHARE_KPI])
            questions = intent_facet_panel_questions(tmp, ws)
            facets = {q["facet"] for q in questions}
            self.assertIn("denominator_scope", facets)
            denom = next(q for q in questions if q["facet"] == "denominator_scope")
            self.assertEqual(denom["answer_type"], "intent_facet_clarification")
            # options carry the intent_facet payload the apply step consumes
            self.assertTrue(any(o.get("intent_facet") for o in denom["options"]))
            self.assertTrue(denom["feature"].startswith("intent::kpi_002::"))

    def test_real_dict_registry_shape_is_read(self):
        # regression: registry written as {"kpis": [...]} must be consumed
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _, ws, _ = _make_ws(tmp, [_SHARE_KPI])
            self.assertGreater(len(intent_facet_panel_questions(tmp, ws)), 0)


class RecordAnswerTests(unittest.TestCase):
    def test_denominator_answer_mirrors_to_pipeline_decisions(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _, ws, layout = _make_ws(tmp, [_SHARE_KPI])
            record_intent_answer(
                tmp, ws,
                kpi_id="kpi_002", facet="denominator_scope",
                value="within_department", source="human", confirmed_by="shubham",
            )
            answers = load_intent_answers(layout)
            self.assertIn(answer_key("kpi_002", "denominator_scope"), answers)
            # mirrored into pipeline_decisions.json so SQL actually applies it
            pd = json.loads((layout.contracts_dir / "pipeline_decisions.json").read_text())
            self.assertEqual(pd["percentage_denominator_scopes"]["kpi_002"], "within_department")

    def test_answered_facet_skipped_in_panel(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _, ws, _ = _make_ws(tmp, [_SHARE_KPI])
            record_intent_answer(
                tmp, ws,
                kpi_id="kpi_002", facet="denominator_scope",
                value="within_department", source="human", confirmed_by="shubham",
            )
            questions = intent_facet_panel_questions(tmp, ws)
            self.assertNotIn("denominator_scope", {q["facet"] for q in questions})


class GateProvenanceTests(unittest.TestCase):
    def test_unanswered_intent_facet_is_agent_gate(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _, ws, layout = _make_ws(tmp, [_SHARE_KPI])
            write_intent_contract(tmp, ws)  # emits kpi_intent_contract.json
            gates = _collect_gate_provenance({}, layout)
            intent_gates = [g for g in gates if g["gate"].startswith("intent:")]
            self.assertTrue(intent_gates)
            self.assertTrue(all(g["source"] == "agent" for g in intent_gates))

    def test_answered_intent_facet_is_human_gate(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _, ws, layout = _make_ws(tmp, [_SHARE_KPI])
            write_intent_contract(tmp, ws)
            record_intent_answer(
                tmp, ws,
                kpi_id="kpi_002", facet="denominator_scope",
                value="within_department", source="human", confirmed_by="shubham",
            )
            gates = _collect_gate_provenance({}, layout)
            denom = next(
                (g for g in gates if g["gate"] == "intent:kpi_002:denominator_scope"), None
            )
            self.assertIsNotNone(denom)
            self.assertEqual(denom["source"], "human")
            self.assertEqual(denom["confirmed_by"], "shubham")

    def test_no_intent_contract_means_no_intent_gates(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _, ws, layout = _make_ws(tmp, [_SHARE_KPI])
            gates = _collect_gate_provenance({}, layout)
            self.assertEqual([g for g in gates if g["gate"].startswith("intent:")], [])


if __name__ == "__main__":
    unittest.main()

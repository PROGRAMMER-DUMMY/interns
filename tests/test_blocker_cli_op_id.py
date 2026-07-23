"""apply-kpi-panel-answer idempotency must include WHICH blocker was
answered, not just the literal --answer text.

Found live: answering `option_a` for `cargo_claims`, then `option_a` for a
DIFFERENT blocker (`shipments`), collided on the same op_id -- the second
call was silently skipped as an idempotent replay of the first, so the
`shipments` answer was never actually applied.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.blocker_cli import _current_panel_question_id
from core.onboarding.workspace.idempotency import compute_op_id


def _write_panel(ws: Path, *, question_id: str = "", feature: str = "") -> None:
    panel_dir = ws / "interns" / "reports" / "blocker_question_panel"
    panel_dir.mkdir(parents=True, exist_ok=True)
    payload = {}
    if question_id:
        payload["question_id"] = question_id
    if feature:
        payload["feature"] = feature
    (panel_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")


class CurrentPanelQuestionIdTests(unittest.TestCase):
    def test_reads_question_id_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            _write_panel(ws, question_id="cargo_claims")
            self.assertEqual(_current_panel_question_id(ws), "cargo_claims")

    def test_falls_back_to_feature_when_question_id_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            _write_panel(ws, feature="shipments")
            self.assertEqual(_current_panel_question_id(ws), "shipments")

    def test_returns_empty_string_when_panel_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            self.assertEqual(_current_panel_question_id(ws), "")

    def test_same_answer_text_for_different_blockers_produces_different_op_ids(self) -> None:
        # This is the actual bug: two DIFFERENT blockers both answered
        # `option_a` must not collide on op_id just because the answer text
        # is identical.
        op_id_cargo_claims = compute_op_id(
            "apply-kpi-panel-answer", workspace="ws", domain="logistics",
            answer="option_a", custom_definition="", evidence_note="",
            via_cli_agent=False, question_id="cargo_claims",
        )
        op_id_shipments = compute_op_id(
            "apply-kpi-panel-answer", workspace="ws", domain="logistics",
            answer="option_a", custom_definition="", evidence_note="",
            via_cli_agent=False, question_id="shipments",
        )
        self.assertNotEqual(op_id_cargo_claims, op_id_shipments)


if __name__ == "__main__":
    unittest.main()

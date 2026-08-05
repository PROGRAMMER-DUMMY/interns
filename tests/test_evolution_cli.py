"""prepare-drift-panel / apply-drift-answer CLI envelope behaviour."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from core.evolution.cli import apply_drift_answer_main, prepare_drift_panel_main
from core.storage.workspace_layout import WorkspaceLayout


def _discovery(tables: list[dict]) -> dict:
    return {"artifact_type": "intake/discovery.json", "status": "ok", "tables": tables}


def _table(name: str, columns: list[dict] | None) -> dict:
    return {
        "name": name,
        "path": f"/data/{name}",
        "format": "parquet",
        "size_bytes": 1,
        "row_estimate": None,
        "is_streaming": False,
        "columns": columns,
    }


class DriftCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.workspace_rel = "workspaces/sample_ws"
        self.workspace = self.repo_root / self.workspace_rel
        self.workspace.mkdir(parents=True)
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def write_discovery(self, payload: dict) -> None:
        path = self.layout.generated_dir / "intake" / "discovery.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def run_cli(self, main, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv + ["--workspace", self.workspace_rel, "--repo-root", str(self.repo_root)])
        return code, buffer.getvalue()

    def prepare(self) -> tuple[int, str]:
        return self.run_cli(prepare_drift_panel_main, [])

    def open_panel(self) -> str:
        self.write_discovery(
            _discovery([_table("orders", [{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}])])
        )
        self.prepare()
        self.write_discovery(_discovery([_table("orders", [{"name": "id", "type": "bigint"}])]))
        self.prepare()
        panel = json.loads(
            (self.layout.reports_dir / "schema_drift_panel" / "current.json").read_text(encoding="utf-8")
        )
        return str(panel["findings"][0]["finding_id"])

    def test_entry_points_are_cost_anchored(self):
        self.assertTrue(getattr(prepare_drift_panel_main, "__anchored__", False))
        self.assertTrue(getattr(apply_drift_answer_main, "__anchored__", False))
        self.assertEqual(
            getattr(prepare_drift_panel_main, "__anchored_command__", ""), "prepare-drift-panel"
        )
        self.assertEqual(
            getattr(apply_drift_answer_main, "__anchored_command__", ""), "apply-drift-answer"
        )

    def test_prepare_snapshots_and_reports_baseline(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "id", "type": "bigint"}])]))
        code, output = self.prepare()
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "baseline")
        history = self.layout.generated_dir / "intake" / "discovery_history"
        self.assertEqual(len(list(history.glob("*.json"))), 1)

    def test_prepare_opens_a_panel_on_action_needed_drift(self):
        self.open_panel()
        panel_path = self.layout.reports_dir / "schema_drift_panel" / "current.json"
        self.assertTrue(panel_path.exists())

    def test_apply_quarantine_with_a_human_writes_the_exclusions_contract(self):
        finding_id = self.open_panel()
        code, output = self.run_cli(
            apply_drift_answer_main,
            ["--finding", finding_id, "--answer", "quarantine_column", "--confirmed-by", "Dana Rivera"],
        )
        self.assertEqual(code, 0)
        self.assertIn("schema_exclusions.json", output)
        contract = json.loads(
            (self.layout.contracts_dir / "schema_exclusions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract, {
            "orders": {
                "excluded_columns": ["status"],
                "decided_by": "Dana Rivera",
                "at": contract["orders"]["at"],
            }
        })

    def test_replayed_apply_is_idempotent(self):
        finding_id = self.open_panel()
        argv = ["--finding", finding_id, "--answer", "quarantine_column", "--confirmed-by", "Dana Rivera"]
        first_code, _ = self.run_cli(apply_drift_answer_main, argv)
        second_code, second_output = self.run_cli(apply_drift_answer_main, argv)
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertIn("idempotent_replay", second_output)
        contract = json.loads(
            (self.layout.contracts_dir / "schema_exclusions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["orders"]["excluded_columns"], ["status"])

    def test_agent_asserted_quarantine_exits_non_zero_and_writes_nothing(self):
        finding_id = self.open_panel()
        code, output = self.run_cli(
            apply_drift_answer_main, ["--finding", finding_id, "--answer", "quarantine_column"]
        )
        self.assertEqual(code, 1)
        self.assertIn("refused", output)
        self.assertFalse((self.layout.contracts_dir / "schema_exclusions.json").exists())

    def test_unknown_finding_exits_non_zero(self):
        self.open_panel()
        code, output = self.run_cli(
            apply_drift_answer_main,
            ["--finding", "nope", "--answer", "propagate", "--confirmed-by", "Dana Rivera"],
        )
        self.assertEqual(code, 1)
        self.assertIn("refused", output)


if __name__ == "__main__":
    unittest.main()

"""The schema-drift panel: rendered only when a finding needs a human decision."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.evolution.panel import (
    FINDING_OPTION_IDS,
    apply_drift_answer,
    prepare_drift_panel,
)
from core.onboarding.workspace.delegation import routing_for
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


class _PanelCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.workspace_rel = "workspaces/sample_ws"
        self.workspace = self.repo_root / self.workspace_rel
        self.workspace.mkdir(parents=True)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.panel_dir = self.layout.reports_dir / "schema_drift_panel"

    def write_discovery(self, payload: dict) -> None:
        path = self.layout.generated_dir / "intake" / "discovery.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def prepare(self):
        return prepare_drift_panel(self.repo_root, self.workspace_rel)

    def panel_json(self) -> dict:
        return json.loads((self.panel_dir / "current.json").read_text(encoding="utf-8"))

    def panel_md(self) -> str:
        return (self.panel_dir / "current.md").read_text(encoding="utf-8")


class PanelRenderingTests(_PanelCase):
    def test_no_discovery_reports_it_and_writes_no_panel(self):
        result = self.prepare()
        self.assertEqual(result.status, "no_discovery")
        self.assertFalse((self.panel_dir / "current.json").exists())

    def test_first_discovery_is_a_baseline_not_drift(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "id", "type": "bigint"}])]))
        result = self.prepare()
        self.assertEqual(result.status, "baseline")
        self.assertTrue(result.snapshot_created)
        self.assertFalse((self.panel_dir / "current.json").exists())

    def test_additive_drift_does_not_open_a_panel(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "id", "type": "bigint"}])]))
        self.prepare()
        self.write_discovery(
            _discovery(
                [_table("orders", [{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}])]
            )
        )
        result = self.prepare()
        self.assertEqual(result.status, "info_only")
        self.assertEqual(result.action_needed_count, 0)
        self.assertEqual(result.info_count, 1)
        self.assertFalse((self.panel_dir / "current.json").exists())

    def test_type_change_opens_a_panel_with_json_backed_evidence(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "int"}])]))
        self.prepare()
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "bigint"}])]))
        result = self.prepare()

        self.assertEqual(result.status, "needs_user_answer")
        self.assertEqual(result.action_needed_count, 1)
        panel = self.panel_json()
        self.assertEqual(panel["artifact_type"], "schema_drift_panel/current.json")
        self.assertEqual(panel["generated_by"], "prepare-drift-panel")
        self.assertEqual(panel["workspace"], self.workspace_rel)
        finding = panel["findings"][0]
        self.assertEqual(finding["kind"], "type_change")
        self.assertEqual(finding["severity"], "action_needed")
        evidence = finding["evidence"]
        self.assertEqual(evidence["table"], "orders")
        self.assertEqual(evidence["column"], "amount")
        self.assertEqual(evidence["old_type"], "int")
        self.assertEqual(evidence["new_type"], "bigint")
        self.assertTrue(evidence["first_seen_snapshot"])
        self.assertEqual(evidence["first_seen_snapshot"], panel["current_snapshot"])
        self.assertEqual(evidence["compared_to_snapshot"], panel["previous_snapshot"])

    def test_panel_options_carry_ids_and_no_recommendation_for_a_loss(self):
        self.write_discovery(
            _discovery([_table("orders", [{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}])])
        )
        self.prepare()
        self.write_discovery(_discovery([_table("orders", [{"name": "id", "type": "bigint"}])]))
        self.prepare()

        finding = self.panel_json()["findings"][0]
        self.assertEqual(finding["kind"], "removed_column")
        self.assertEqual([option["option_id"] for option in finding["options"]], list(FINDING_OPTION_IDS))
        # Nothing is safe to recommend when a column disappears -- stay honest.
        self.assertEqual(finding["recommended_option_id"], "")

    def test_table_scoped_findings_never_offer_a_column_option(self):
        self.write_discovery(_discovery([_table("orders", None), _table("legacy", None)]))
        self.prepare()
        self.write_discovery(_discovery([_table("orders", None)]))
        self.prepare()

        finding = self.panel_json()["findings"][0]
        self.assertEqual(finding["kind"], "removed_table")
        self.assertNotIn("quarantine_column", [option["option_id"] for option in finding["options"]])

    def test_markdown_is_ascii_and_uses_ascii_markers(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "int"}])]))
        self.prepare()
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "bigint"}])]))
        self.prepare()

        markdown = self.panel_md()
        markdown.encode("ascii")
        self.assertIn("[x]", markdown)
        self.assertIn("apply-drift-answer", markdown)
        self.assertIn("orders", markdown)

    def test_stale_panel_is_cleared_when_drift_no_longer_needs_action(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "int"}])]))
        self.prepare()
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "bigint"}])]))
        self.prepare()
        self.assertTrue((self.panel_dir / "current.json").exists())

        # A third discovery that only adds a column: the earlier panel is history.
        self.write_discovery(
            _discovery([_table("orders", [{"name": "amount", "type": "bigint"}, {"name": "note", "type": "string"}])])
        )
        result = self.prepare()
        self.assertEqual(result.status, "info_only")
        self.assertFalse((self.panel_dir / "current.json").exists())
        self.assertFalse((self.panel_dir / "current.md").exists())

    def test_columns_unknown_note_reaches_the_panel_result(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "id", "type": "bigint"}])]))
        self.prepare()
        self.write_discovery(_discovery([_table("orders", None), _table("legacy_gone", None)]))
        result = self.prepare()
        self.assertTrue(any("columns_unknown" in note for note in result.notes))


class ApplyAnswerTests(_PanelCase):
    def open_removed_column_panel(self) -> str:
        self.write_discovery(
            _discovery([_table("orders", [{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}])])
        )
        self.prepare()
        self.write_discovery(_discovery([_table("orders", [{"name": "id", "type": "bigint"}])]))
        self.prepare()
        return self.panel_json()["findings"][0]["finding_id"]

    def exclusions(self) -> dict:
        return json.loads(
            (self.layout.contracts_dir / "schema_exclusions.json").read_text(encoding="utf-8")
        )

    def decisions(self) -> dict:
        return json.loads(
            (self.layout.contracts_dir / "schema_drift_decisions.json").read_text(encoding="utf-8")
        )

    def test_quarantine_writes_the_exact_exclusions_contract(self):
        finding_id = self.open_removed_column_panel()
        result = apply_drift_answer(
            self.repo_root,
            self.workspace_rel,
            finding_id=finding_id,
            answer="quarantine_column",
            confirmed_by="Dana Rivera",
        )
        self.assertTrue(result["ok"])
        contract = self.exclusions()
        self.assertEqual(list(contract), ["orders"])
        entry = contract["orders"]
        self.assertEqual(sorted(entry), ["at", "decided_by", "excluded_columns"])
        self.assertEqual(entry["excluded_columns"], ["status"])
        self.assertEqual(entry["decided_by"], "Dana Rivera")
        self.assertTrue(entry["at"])

    def test_replaying_the_same_quarantine_changes_nothing(self):
        finding_id = self.open_removed_column_panel()
        for _ in range(2):
            apply_drift_answer(
                self.repo_root,
                self.workspace_rel,
                finding_id=finding_id,
                answer="quarantine_column",
                confirmed_by="Dana Rivera",
            )
        first = (self.layout.contracts_dir / "schema_exclusions.json").read_text(encoding="utf-8")
        apply_drift_answer(
            self.repo_root,
            self.workspace_rel,
            finding_id=finding_id,
            answer="quarantine_column",
            confirmed_by="Dana Rivera",
        )
        self.assertEqual(self.exclusions()["orders"]["excluded_columns"], ["status"])
        self.assertEqual(
            (self.layout.contracts_dir / "schema_exclusions.json").read_text(encoding="utf-8"), first
        )

    def test_quarantine_merges_columns_and_keeps_a_stable_order(self):
        finding_id = self.open_removed_column_panel()
        apply_drift_answer(
            self.repo_root,
            self.workspace_rel,
            finding_id=finding_id,
            answer="quarantine_column",
            confirmed_by="Dana Rivera",
        )
        path = self.layout.contracts_dir / "schema_exclusions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["orders"]["excluded_columns"] = ["zip_code", "status"]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        apply_drift_answer(
            self.repo_root,
            self.workspace_rel,
            finding_id=finding_id,
            answer="quarantine_column",
            confirmed_by="Dana Rivera",
        )
        self.assertEqual(self.exclusions()["orders"]["excluded_columns"], ["status", "zip_code"])

    def test_agent_asserted_quarantine_is_refused(self):
        finding_id = self.open_removed_column_panel()
        for confirmer in ("", "claude"):
            with self.subTest(confirmed_by=confirmer):
                with self.assertRaises(PermissionError):
                    apply_drift_answer(
                        self.repo_root,
                        self.workspace_rel,
                        finding_id=finding_id,
                        answer="quarantine_column",
                        confirmed_by=confirmer,
                    )
        self.assertFalse((self.layout.contracts_dir / "schema_exclusions.json").exists())

    def test_agent_asserted_block_pipeline_is_refused(self):
        finding_id = self.open_removed_column_panel()
        with self.assertRaises(PermissionError):
            apply_drift_answer(
                self.repo_root,
                self.workspace_rel,
                finding_id=finding_id,
                answer="block_pipeline",
                confirmed_by="",
            )

    def test_propagate_records_provenance_without_writing_exclusions(self):
        finding_id = self.open_removed_column_panel()
        result = apply_drift_answer(
            self.repo_root,
            self.workspace_rel,
            finding_id=finding_id,
            answer="propagate",
            confirmed_by="",
        )
        self.assertTrue(result["ok"])
        decision = self.decisions()[finding_id]
        self.assertEqual(decision["answer"], "propagate")
        self.assertEqual(decision["source"], "agent")
        self.assertFalse((self.layout.contracts_dir / "schema_exclusions.json").exists())

    def test_human_confirmation_is_recorded_as_human(self):
        finding_id = self.open_removed_column_panel()
        apply_drift_answer(
            self.repo_root,
            self.workspace_rel,
            finding_id=finding_id,
            answer="block_pipeline",
            confirmed_by="Dana Rivera",
        )
        decision = self.decisions()[finding_id]
        self.assertEqual(decision["source"], "human")
        self.assertEqual(decision["decided_by"], "Dana Rivera")
        self.assertEqual(decision["table"], "orders")

    def test_unknown_finding_and_unknown_answer_are_rejected(self):
        finding_id = self.open_removed_column_panel()
        with self.assertRaises(ValueError):
            apply_drift_answer(
                self.repo_root,
                self.workspace_rel,
                finding_id="not_a_finding",
                answer="propagate",
                confirmed_by="Dana Rivera",
            )
        with self.assertRaises(ValueError):
            apply_drift_answer(
                self.repo_root,
                self.workspace_rel,
                finding_id=finding_id,
                answer="ignore_it",
                confirmed_by="Dana Rivera",
            )

    def test_quarantine_is_rejected_for_a_table_scoped_finding(self):
        self.write_discovery(_discovery([_table("orders", None), _table("legacy", None)]))
        self.prepare()
        self.write_discovery(_discovery([_table("orders", None)]))
        self.prepare()
        finding_id = self.panel_json()["findings"][0]["finding_id"]
        with self.assertRaises(ValueError):
            apply_drift_answer(
                self.repo_root,
                self.workspace_rel,
                finding_id=finding_id,
                answer="quarantine_column",
                confirmed_by="Dana Rivera",
            )

    def test_apply_without_a_panel_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_drift_answer(
                self.repo_root,
                self.workspace_rel,
                finding_id="anything",
                answer="propagate",
                confirmed_by="Dana Rivera",
            )


class StageRoutingTests(_PanelCase):
    def test_panel_and_result_carry_the_schema_drift_roster(self):
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "int"}])]))
        self.prepare()
        self.write_discovery(_discovery([_table("orders", [{"name": "amount", "type": "bigint"}])]))
        result = self.prepare()
        self.assertEqual(result.status, "needs_user_answer")

        roster = routing_for("schema_drift_review")
        self.assertTrue(roster["agents"], "schema_drift_review routes no agent")
        for label, payload in (("panel", self.panel_json()), ("result", result.summary())):
            with self.subTest(payload=label):
                self.assertEqual(payload["stage"], "schema_drift_review")
                self.assertEqual(payload["required_specialists"], roster["agents"])
                self.assertEqual(payload["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()

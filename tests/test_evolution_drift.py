"""Schema-evolution snapshots and drift detection (core/evolution)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.evolution.drift import (
    SEVERITY_ACTION,
    SEVERITY_INFO,
    detect_drift,
)
from core.evolution.snapshot import (
    MAX_SNAPSHOTS,
    list_snapshots,
    load_snapshot,
    snapshot_discovery,
)
from core.storage.workspace_layout import WorkspaceLayout


def _discovery(tables: list[dict]) -> dict:
    return {
        "artifact_type": "intake/discovery.json",
        "status": "ok",
        "connector": "local_files",
        "tables": tables,
    }


def _table(name: str, columns: list[dict] | None, **extra) -> dict:
    payload = {
        "name": name,
        "path": f"/data/{name}",
        "format": "parquet",
        "size_bytes": 10,
        "row_estimate": None,
        "is_streaming": False,
        "columns": columns,
    }
    payload.update(extra)
    return payload


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.workspace = self.repo_root / "workspaces" / "sample_ws"
        self.workspace.mkdir(parents=True)
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def write_discovery(self, payload: dict) -> None:
        path = self.layout.generated_dir / "intake" / "discovery.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def test_missing_discovery_is_a_reason_not_a_crash(self):
        result = snapshot_discovery(self.layout, repo_root=self.repo_root)
        self.assertFalse(result.created)
        self.assertEqual(result.reason, "no_discovery")
        self.assertEqual(result.snapshot_path, "")
        self.assertEqual(list_snapshots(self.layout), [])

    def test_first_snapshot_is_created_and_readable(self):
        payload = _discovery([_table("orders", [{"name": "id", "type": "bigint"}])])
        self.write_discovery(payload)
        result = snapshot_discovery(self.layout, repo_root=self.repo_root)
        self.assertTrue(result.created)
        self.assertEqual(result.reason, "created")
        self.assertEqual(result.snapshot_count, 1)
        snapshots = list_snapshots(self.layout)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(load_snapshot(snapshots[0]), payload)
        self.assertTrue(result.snapshot_path.startswith("workspaces/sample_ws/"))

    def test_identical_discovery_is_not_snapshotted_twice(self):
        self.write_discovery(_discovery([_table("orders", None)]))
        snapshot_discovery(self.layout, repo_root=self.repo_root)
        again = snapshot_discovery(self.layout, repo_root=self.repo_root)
        self.assertFalse(again.created)
        self.assertEqual(again.reason, "identical_to_latest")
        self.assertEqual(len(list_snapshots(self.layout)), 1)

    def test_changed_discovery_adds_a_snapshot(self):
        self.write_discovery(_discovery([_table("orders", None)]))
        snapshot_discovery(self.layout, repo_root=self.repo_root)
        self.write_discovery(_discovery([_table("orders", None), _table("items", None)]))
        second = snapshot_discovery(self.layout, repo_root=self.repo_root)
        self.assertTrue(second.created)
        self.assertEqual(len(list_snapshots(self.layout)), 2)

    def test_rotation_keeps_only_the_newest_snapshots(self):
        for index in range(MAX_SNAPSHOTS + 5):
            self.write_discovery(_discovery([_table(f"orders_{index}", None)]))
            snapshot_discovery(self.layout, repo_root=self.repo_root)
        snapshots = list_snapshots(self.layout)
        self.assertEqual(len(snapshots), MAX_SNAPSHOTS)
        newest = load_snapshot(snapshots[-1])
        self.assertEqual(newest["tables"][0]["name"], f"orders_{MAX_SNAPSHOTS + 4}")
        oldest = load_snapshot(snapshots[0])
        self.assertEqual(oldest["tables"][0]["name"], "orders_5")

    def test_snapshots_are_ordered_oldest_to_newest(self):
        for index in range(3):
            self.write_discovery(_discovery([_table(f"t{index}", None)]))
            snapshot_discovery(self.layout, repo_root=self.repo_root)
        names = [path.name for path in list_snapshots(self.layout)]
        self.assertEqual(names, sorted(names))


class DriftDetectionTests(unittest.TestCase):
    def test_no_change_is_no_drift(self):
        payload = _discovery([_table("orders", [{"name": "id", "type": "bigint"}])])
        report = detect_drift(payload, payload)
        self.assertFalse(report.has_drift)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.action_needed, [])

    def test_added_column_is_informational(self):
        prev = _discovery([_table("orders", [{"name": "id", "type": "bigint"}])])
        curr = _discovery(
            [_table("orders", [{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}])]
        )
        report = detect_drift(prev, curr)
        self.assertEqual([f.column for f in report.added_columns], ["status"])
        self.assertEqual(report.added_columns[0].new_type, "string")
        self.assertEqual(report.added_columns[0].severity, SEVERITY_INFO)
        self.assertEqual(report.action_needed, [])
        self.assertTrue(report.has_drift)

    def test_removed_column_needs_action(self):
        prev = _discovery(
            [_table("orders", [{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}])]
        )
        curr = _discovery([_table("orders", [{"name": "id", "type": "bigint"}])])
        report = detect_drift(prev, curr)
        self.assertEqual([f.column for f in report.removed_columns], ["status"])
        self.assertEqual(report.removed_columns[0].old_type, "string")
        self.assertEqual(report.removed_columns[0].severity, SEVERITY_ACTION)
        self.assertEqual(len(report.action_needed), 1)

    def test_type_change_needs_action_and_reports_both_types(self):
        prev = _discovery([_table("orders", [{"name": "amount", "type": "int"}])])
        curr = _discovery([_table("orders", [{"name": "amount", "type": "decimal(18,2)"}])])
        report = detect_drift(prev, curr)
        self.assertEqual(len(report.type_changes), 1)
        change = report.type_changes[0]
        self.assertEqual((change.table, change.column), ("orders", "amount"))
        self.assertEqual(change.old_type, "int")
        self.assertEqual(change.new_type, "decimal(18,2)")
        self.assertEqual(change.severity, SEVERITY_ACTION)

    def test_added_table_is_informational_and_removed_table_needs_action(self):
        prev = _discovery([_table("orders", None), _table("legacy", None)])
        curr = _discovery([_table("orders", None), _table("items", None)])
        report = detect_drift(prev, curr)
        self.assertEqual(report.added_tables, ["items"])
        self.assertEqual(report.removed_tables, ["legacy"])
        kinds = {(f.kind, f.severity) for f in report.findings}
        self.assertIn(("added_table", SEVERITY_INFO), kinds)
        self.assertIn(("removed_table", SEVERITY_ACTION), kinds)
        self.assertEqual([f.kind for f in report.action_needed], ["removed_table"])

    def test_null_columns_on_either_side_never_fabricate_drift(self):
        with_columns = _discovery([_table("orders", [{"name": "id", "type": "bigint"}])])
        without_columns = _discovery([_table("orders", None)])
        for prev, curr in ((with_columns, without_columns), (without_columns, with_columns)):
            with self.subTest(prev_has_columns=prev is with_columns):
                report = detect_drift(prev, curr)
                self.assertEqual(report.added_columns, [])
                self.assertEqual(report.removed_columns, [])
                self.assertEqual(report.type_changes, [])
                self.assertTrue(any("columns_unknown" in note for note in report.notes))
                self.assertIn("orders", " ".join(report.notes))

    def test_both_sides_known_empty_columns_is_not_unknown(self):
        prev = _discovery([_table("orders", [])])
        curr = _discovery([_table("orders", [])])
        report = detect_drift(prev, curr)
        self.assertEqual(report.notes, [])
        self.assertFalse(report.has_drift)

    def test_alternate_column_key_names_are_read(self):
        prev = _discovery([_table("orders", [{"column_name": "amount", "data_type": "int"}])])
        curr = _discovery([_table("orders", [{"column_name": "amount", "data_type": "bigint"}])])
        report = detect_drift(prev, curr)
        self.assertEqual(len(report.type_changes), 1)
        self.assertEqual(report.type_changes[0].old_type, "int")

    def test_missing_type_is_empty_string_never_invented(self):
        prev = _discovery([_table("orders", [{"name": "id"}])])
        curr = _discovery([_table("orders", [{"name": "id"}, {"name": "status"}])])
        report = detect_drift(prev, curr)
        self.assertEqual(report.added_columns[0].new_type, "")

    def test_summary_is_json_serialisable(self):
        prev = _discovery([_table("orders", [{"name": "id", "type": "bigint"}])])
        curr = _discovery([_table("items", [{"name": "id", "type": "bigint"}])])
        summary = detect_drift(prev, curr).summary()
        json.dumps(summary)
        self.assertEqual(summary["added_tables"], ["items"])
        self.assertEqual(summary["removed_tables"], ["orders"])
        self.assertEqual(summary["action_needed_count"], 1)


if __name__ == "__main__":
    unittest.main()

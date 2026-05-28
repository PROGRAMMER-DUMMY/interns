"""Tests for the panel preview executor."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.panel_preview_executor import (
    PreviewResult,
    execute_preview,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_CSV = (
    "workspaces/Healthcare-RCM-Data-Platform/datasets/EMR/"
    "trendytech-hospital-a/patients.csv"
)


class ExecutePreviewBasicTests(unittest.TestCase):
    def test_ok_status_for_inline_select(self) -> None:
        result = execute_preview(
            sql="SELECT 1 AS x, 2 AS y UNION ALL SELECT 3, 4",
            repo_root=REPO_ROOT,
            dataset_paths=[],
        )
        self.assertIsInstance(result, PreviewResult)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.columns, ["x", "y"])
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[0], {"x": 1, "y": 2})
        self.assertEqual(result.rows[1], {"x": 3, "y": 4})
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_empty_status_for_zero_row_result(self) -> None:
        result = execute_preview(
            sql="SELECT 1 AS x WHERE 1=0",
            repo_root=REPO_ROOT,
            dataset_paths=[],
        )
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.rows, [])
        self.assertEqual(result.columns, ["x"])
        self.assertEqual(result.error, "Query returned 0 rows")

    def test_error_status_for_invalid_sql(self) -> None:
        result = execute_preview(
            sql="SELECT FROM WHERE not-sql",
            repo_root=REPO_ROOT,
            dataset_paths=[],
        )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.rows, [])
        self.assertEqual(result.columns, [])
        self.assertIsNotNone(result.error)
        assert result.error is not None  # for type-checkers
        self.assertTrue(len(result.error) > 0)
        self.assertLessEqual(len(result.error), 200)


class ExecutePreviewDatasetPathTests(unittest.TestCase):
    def test_dataset_path_outside_repo_root_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "rogue.csv"
            outside.write_text("a,b\n1,2\n", encoding="utf-8")
            result = execute_preview(
                sql="SELECT 1",
                repo_root=REPO_ROOT,
                dataset_paths=[outside],
            )
        self.assertEqual(result.status, "error")
        assert result.error is not None
        self.assertIn("not under repo_root", result.error)

    def test_dataset_path_missing_is_error(self) -> None:
        result = execute_preview(
            sql="SELECT 1",
            repo_root=REPO_ROOT,
            dataset_paths=[Path("workspaces/__does_not_exist__/nope.csv")],
        )
        self.assertEqual(result.status, "error")
        assert result.error is not None
        self.assertIn("does not exist", result.error)


class ExecutePreviewSerializationTests(unittest.TestCase):
    def test_summary_is_json_serializable(self) -> None:
        result = execute_preview(
            sql=(
                "SELECT CAST('1.23' AS DECIMAL(5,2)) AS amount, "
                "DATE '2024-01-02' AS d, "
                "TIMESTAMP '2024-01-02 03:04:05' AS ts, "
                "NULL AS n"
            ),
            repo_root=REPO_ROOT,
            dataset_paths=[],
        )
        self.assertEqual(result.status, "ok")
        # Must not raise and must not need a default= encoder.
        payload = json.dumps(result.summary())
        self.assertIn('"status": "ok"', payload)
        row = result.rows[0]
        self.assertIsInstance(row["amount"], str)
        self.assertIsInstance(row["d"], str)
        self.assertIsInstance(row["ts"], str)
        self.assertIsNone(row["n"])


class ExecutePreviewCsvTests(unittest.TestCase):
    def test_reads_workspace_csv(self) -> None:
        csv_abs = REPO_ROOT / WORKSPACE_CSV
        if not csv_abs.exists():
            self.skipTest(f"workspace CSV missing: {WORKSPACE_CSV}")
        sql = (
            f"SELECT * FROM read_csv_auto('{WORKSPACE_CSV}') LIMIT 3"
        )
        result = execute_preview(
            sql=sql,
            repo_root=REPO_ROOT,
            dataset_paths=[Path(WORKSPACE_CSV)],
        )
        self.assertEqual(result.status, "ok", msg=result.error)
        self.assertGreater(len(result.columns), 0)
        self.assertGreaterEqual(len(result.rows), 1)
        self.assertLessEqual(len(result.rows), 3)
        # Each row should be a dict keyed by the columns.
        for row in result.rows:
            self.assertEqual(set(row.keys()), set(result.columns))
        # And the whole payload must round-trip through JSON.
        json.dumps(result.summary())


class ExecutePreviewTimeoutTests(unittest.TestCase):
    def test_timeout_status_for_long_query(self) -> None:
        result = execute_preview(
            sql="SELECT count(*) FROM range(100000000000)",
            repo_root=REPO_ROOT,
            dataset_paths=[],
            timeout_seconds=0.1,
        )
        self.assertEqual(result.status, "timeout", msg=result.error)
        assert result.error is not None
        self.assertIn("0.1", result.error)
        self.assertIn("budget", result.error)


class ExecutePreviewTruncationTests(unittest.TestCase):
    def test_truncation_still_reports_ok(self) -> None:
        # range(10) yields 10 rows; max_rows=5 should truncate but stay "ok".
        result = execute_preview(
            sql="SELECT * FROM range(10) AS t(n)",
            repo_root=REPO_ROOT,
            dataset_paths=[],
            max_rows=5,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.rows), 5)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()

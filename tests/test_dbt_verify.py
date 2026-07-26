"""core.orchestration.dbt_verify: `dbt show` verification of a generated
project. `subprocess.run` is mocked -- this suite covers the gate, the
zero-rows verdict and the report, not a live warehouse round trip (same split
test_dbt_backfill.py uses).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.orchestration.dbt_verify import DbtShowVerifier, _preview_row_count
from core.storage.workspace_layout import WorkspaceLayout
from tests.test_dbt_backfill import _build_real_dbt_project

_AUTHORIZED_ENV = {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}


def _proc(returncode: int = 0, rows: int | None = 1, stderr: str = "") -> MagicMock:
    if rows is None:
        stdout = "12:00:00  Running with dbt=1.11.0\n"
    else:
        stdout = (
            "12:00:00  Running with dbt=1.11.0\n"
            + json.dumps({"node": "fct_kpi_001", "show": [{"c": i} for i in range(rows)]})
            + "\n"
        )
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class PreviewParsingTests(unittest.TestCase):
    def test_counts_rows_from_the_json_payload(self):
        self.assertEqual(_preview_row_count(_proc(rows=3).stdout), 3)

    def test_empty_preview_is_zero_not_none(self):
        self.assertEqual(_preview_row_count(_proc(rows=0).stdout), 0)

    def test_unparseable_output_is_none_not_a_guess(self):
        self.assertIsNone(_preview_row_count("Completed successfully\n"))


class ProjectDiscoveryTests(unittest.TestCase):
    def test_missing_dbt_project_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            WorkspaceLayout(project_root=root / "workspaces" / "demo").ensure_runtime_dirs()
            with self.assertRaises(FileNotFoundError):
                DbtShowVerifier(root, "workspaces/demo").run()


class RemoteGateTests(unittest.TestCase):
    def test_without_remote_approval_nothing_runs_and_nothing_is_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch.dict(os.environ, {}, clear=False), patch(
                "core.orchestration.dbt_verify.subprocess.run"
            ) as mock_run:
                os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                with self.assertRaises(PermissionError):
                    DbtShowVerifier(root, "workspaces/demo").run()
                mock_run.assert_not_called()
            report = json.loads(
                (root / "workspaces" / "demo" / "interns" / "reports" / "dbt_verify"
                 / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "refused_no_remote_approval")
            self.assertEqual(report["checks"], [])
            self.assertTrue(report["models"])


class VerdictTests(unittest.TestCase):
    def _run(self, root: Path, proc: MagicMock):
        with patch.dict(os.environ, _AUTHORIZED_ENV), patch(
            "core.orchestration.dbt_verify.subprocess.run", return_value=proc
        ):
            return DbtShowVerifier(root, "workspaces/demo").run()

    def test_rows_returned_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            result = self._run(root, _proc(rows=5))
            self.assertEqual(result.status, "verified")
            self.assertGreaterEqual(result.models_checked, 1)

    def test_zero_rows_fails_rather_than_passing_a_blank_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with self.assertRaises(RuntimeError):
                self._run(root, _proc(rows=0))
            report = json.loads(
                (root / "workspaces" / "demo" / "interns" / "reports" / "dbt_verify"
                 / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["checks"][0]["ok"])
            self.assertEqual(report["checks"][0]["rows"], 0)

    def test_dbt_error_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with self.assertRaises(RuntimeError):
                self._run(root, _proc(returncode=1, rows=None, stderr="Compilation Error"))

    def test_unreadable_preview_is_not_reported_as_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with self.assertRaises(RuntimeError):
                self._run(root, _proc(rows=None))
            report = json.loads(
                (root / "workspaces" / "demo" / "interns" / "reports" / "dbt_verify"
                 / "current.json").read_text(encoding="utf-8")
            )
            self.assertTrue(report["checks"][0]["unverified"])


if __name__ == "__main__":
    unittest.main()

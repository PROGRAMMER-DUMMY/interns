"""core.orchestration.dbt_backfill: bounded, dry-run-capable dbt microbatch
backfill. `subprocess.run` is mocked for the "executed" path -- this suite
covers the bound/refusal/report logic, not a live `dbt build` (that's a
separate real-infrastructure verification step, same split
test_dbt_project_generator.py already uses -- it never calls a live
`dbt build` inside its unit tests either).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.onboarding.kpi.dbt_project_generator import DbtProjectGenerator
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.orchestration.dbt_backfill import DbtBackfillRunner
from core.storage.workspace_layout import WorkspaceLayout


def _build_real_dbt_project(root: Path) -> Path:
    workspace = root / "workspaces" / "demo"
    (workspace / "datasets").mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    (workspace / "datasets" / "transactions.csv").write_text(
        "ClaimID,PaidAmount,LineOfBusiness\nC1,10.50,Commercial\nC2,20.25,Medicare\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "kpi_registry.csv").write_text(
        "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
        "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),\n",
        encoding="utf-8",
    )
    WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
    KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
    DbtProjectGenerator(root, "workspaces/demo", catalog="main", schema="rcm").generate()
    return workspace


class NoDbtProjectTests(unittest.TestCase):
    def test_missing_dbt_project_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            WorkspaceLayout(project_root=workspace).ensure_runtime_dirs()
            with self.assertRaises(FileNotFoundError):
                DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="2026-01-01", event_time_end="2026-01-05",
                )


class DateValidationTests(unittest.TestCase):
    def test_bad_date_format_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with self.assertRaises(ValueError):
                DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="01/01/2026", event_time_end="2026-01-05",
                )

    def test_end_before_start_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with self.assertRaises(ValueError):
                DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="2026-01-10", event_time_end="2026-01-05",
                )


class DryRunTests(unittest.TestCase):
    def test_within_bound_dry_run_never_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            result = DbtBackfillRunner(root, "workspaces/demo").run(
                event_time_start="2026-01-01", event_time_end="2026-01-05", dry_run=True,
            )
            self.assertEqual(result.status, "dry_run")
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertFalse(report["over_bound"])
            self.assertFalse(report["would_refuse"])
            self.assertIn("--event-time-start", report["command"])

    def test_over_bound_dry_run_reports_would_refuse_but_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            result = DbtBackfillRunner(root, "workspaces/demo").run(
                event_time_start="2026-01-01", event_time_end="2026-06-01",
                dry_run=True, max_span_days=31,
            )
            self.assertEqual(result.status, "dry_run")
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertTrue(report["over_bound"])
            self.assertTrue(report["would_refuse"])


class ExecutionGateTests(unittest.TestCase):
    def test_over_bound_execution_without_confirmation_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch("core.orchestration.dbt_backfill.subprocess.run") as mock_run:
                with self.assertRaises(PermissionError):
                    DbtBackfillRunner(root, "workspaces/demo").run(
                        event_time_start="2026-01-01", event_time_end="2026-06-01",
                        max_span_days=31,
                    )
                mock_run.assert_not_called()

    def test_over_bound_execution_with_human_confirmation_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            mock_proc = MagicMock(returncode=0, stdout="Completed successfully", stderr="")
            with patch(
                "core.orchestration.dbt_backfill.subprocess.run", return_value=mock_proc
            ) as mock_run:
                result = DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="2026-01-01", event_time_end="2026-06-01",
                    max_span_days=31, confirmed_by="Shubham",
                )
            self.assertEqual(result.status, "executed")
            mock_run.assert_called_once()

    def test_within_bound_execution_needs_no_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            mock_proc = MagicMock(returncode=0, stdout="Completed successfully", stderr="")
            with patch("core.orchestration.dbt_backfill.subprocess.run", return_value=mock_proc):
                result = DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="2026-01-01", event_time_end="2026-01-05",
                )
            self.assertEqual(result.status, "executed")

    def test_dbt_build_failure_surfaces_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            mock_proc = MagicMock(returncode=1, stdout="", stderr="Compilation Error")
            with patch("core.orchestration.dbt_backfill.subprocess.run", return_value=mock_proc):
                with self.assertRaises(RuntimeError):
                    DbtBackfillRunner(root, "workspaces/demo").run(
                        event_time_start="2026-01-01", event_time_end="2026-01-05",
                    )


class ProvenanceTests(unittest.TestCase):
    def test_agent_style_confirmed_by_is_not_treated_as_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with self.assertRaises(PermissionError):
                DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="2026-01-01", event_time_end="2026-06-01",
                    max_span_days=31, confirmed_by="claude",
                )


if __name__ == "__main__":
    unittest.main()

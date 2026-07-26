"""core.orchestration.dbt_backfill: bounded, dry-run-capable dbt microbatch
backfill. `subprocess.run` is mocked for the "executed" path -- this suite
covers the bound/refusal/report logic, not a live `dbt build` (that's a
separate real-infrastructure verification step, same split
test_dbt_project_generator.py already uses -- it never calls a live
`dbt build` inside its unit tests either).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.onboarding.kpi.dbt_project_generator import DbtProjectGenerator
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.orchestration.dbt_backfill import DbtBackfillRunner
from core.storage.workspace_layout import WorkspaceLayout

# run() now checks the remote-execution gate before actually calling
# subprocess.run (Security S1) -- tests below that exercise OTHER behavior
# (execution succeeding/failing, span-bound confirmation) authorize remote
# execution explicitly so they keep testing what they always tested, isolated
# from the new gate's own state.
_AUTHORIZED_ENV = {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}


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
            with patch.dict(os.environ, _AUTHORIZED_ENV), patch(
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
            with patch.dict(os.environ, _AUTHORIZED_ENV), patch(
                "core.orchestration.dbt_backfill.subprocess.run", return_value=mock_proc
            ):
                result = DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="2026-01-01", event_time_end="2026-01-05",
                )
            self.assertEqual(result.status, "executed")

    def test_dbt_build_failure_surfaces_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            mock_proc = MagicMock(returncode=1, stdout="", stderr="Compilation Error")
            with patch.dict(os.environ, _AUTHORIZED_ENV), patch(
                "core.orchestration.dbt_backfill.subprocess.run", return_value=mock_proc
            ):
                with self.assertRaises(RuntimeError):
                    DbtBackfillRunner(root, "workspaces/demo").run(
                        event_time_start="2026-01-01", event_time_end="2026-01-05",
                    )


class DeferTests(unittest.TestCase):
    """`--defer` + `state:modified+` against DBT_STATE_PATH production artifacts."""

    def _run(self, root: Path, **kwargs):
        return DbtBackfillRunner(root, "workspaces/demo").run(
            event_time_start="2026-01-01", event_time_end="2026-01-05",
            dry_run=True, defer=True, **kwargs,
        )

    def test_defer_without_state_path_is_refused_not_silently_full_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DBT_STATE_PATH", None)
                with self.assertRaises(ValueError):
                    self._run(root)

    def test_a_state_path_inside_the_repo_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            from core.paths import PROJECT_ROOT

            committed = PROJECT_ROOT / "target"
            with patch.dict(os.environ, {"DBT_STATE_PATH": str(committed)}):
                with self.assertRaises(ValueError):
                    self._run(root)

    def test_a_local_state_path_without_a_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch.dict(os.environ, {"DBT_STATE_PATH": state}):
                with self.assertRaises(FileNotFoundError):
                    self._run(root)

    def test_defer_selects_state_modified_and_passes_the_state_path(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state:
            root = Path(tmp)
            _build_real_dbt_project(root)
            (Path(state) / "manifest.json").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"DBT_STATE_PATH": state}):
                result = self._run(root)
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertTrue(report["defer"])
            self.assertEqual(report["select"], "state:modified+")
            self.assertIn("--defer", report["command"])
            self.assertIn("--state", report["command"])
            self.assertIn("state:modified+", report["command"])

    def test_a_remote_state_uri_is_taken_on_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch.dict(os.environ, {"DBT_STATE_PATH": "s3://bucket/dbt-state/prod"}):
                result = self._run(root)
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(report["state_path"], "s3://bucket/dbt-state/prod")

    def test_an_explicit_select_wins_over_state_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch.dict(os.environ, {"DBT_STATE_PATH": "s3://bucket/prod"}):
                result = self._run(root, select="fct_kpi_001")
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(report["select"], "fct_kpi_001")

    def test_without_defer_nothing_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            result = DbtBackfillRunner(root, "workspaces/demo").run(
                event_time_start="2026-01-01", event_time_end="2026-01-05", dry_run=True,
            )
            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertFalse(report["defer"])
            self.assertNotIn("--defer", report["command"])
            self.assertNotIn("--select", report["command"])


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

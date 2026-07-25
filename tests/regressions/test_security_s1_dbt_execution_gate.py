"""Security S1: the dbt/Airflow production path had NO equivalent to the
medallion deploy path's G5 remote-execution gate -- a within-span-bound
`run-dbt-backfill`, a Cosmos-wired `build_dbt_tasks`, and the shared
`DBT_BUILD_STAGE` shell command (used by plain `pipeline-run`, Dagster, AND
the Airflow BashOperator fallback) could all execute real `dbt build` with
zero human authorization. Fixed by reusing `deploy_gates.check_remote_approval`
(the exact G5 check) across all three surfaces. See
~/.claude/plans/dynamic-cooking-firefly.md S1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

from core.onboarding.databricks import deploy_gates
from core.onboarding.kpi.dbt_project_generator import DbtProjectGenerator
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.orchestration import cosmos_dag, dbt_backfill, pipeline_stages
from core.storage.workspace_layout import WorkspaceLayout

_UNAUTHORIZED_ENV = {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": ""}
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


class DeployGatesCliTests(unittest.TestCase):
    def test_refuses_when_env_unset(self):
        with patch.dict(os.environ, _UNAUTHORIZED_ENV):
            self.assertEqual(deploy_gates.main([]), 1)

    def test_ok_when_env_set(self):
        with patch.dict(os.environ, _AUTHORIZED_ENV):
            self.assertEqual(deploy_gates.main([]), 0)

    def test_registered_as_a_real_console_script(self):
        scripts = deploy_gates.entry_point_scripts if hasattr(deploy_gates, "entry_point_scripts") else None
        # entry_point_scripts lives in cost_ledger.py; import it directly.
        from core.observability.cost_ledger import entry_point_scripts

        scripts = entry_point_scripts()
        self.assertEqual(scripts.get("check-remote-execution-gate"), "core.onboarding.databricks.deploy_gates:main")

    def test_exempt_from_anchoring_with_a_real_reason(self):
        from core.observability.cost_ledger import EXEMPTIONS

        self.assertIn("check-remote-execution-gate", EXEMPTIONS)
        self.assertTrue(EXEMPTIONS["check-remote-execution-gate"].strip())


class DbtBackfillGateTests(unittest.TestCase):
    def test_within_bound_backfill_now_refuses_without_remote_approval(self):
        """Before this fix: a within-span-bound backfill executed `dbt build`
        with zero human confirmation of any kind. Proves that's closed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch.dict(os.environ, _UNAUTHORIZED_ENV), patch(
                "core.orchestration.dbt_backfill.subprocess.run"
            ) as mock_run:
                with self.assertRaises(PermissionError) as ctx:
                    dbt_backfill.DbtBackfillRunner(root, "workspaces/demo").run(
                        event_time_start="2026-01-01", event_time_end="2026-01-05",
                    )
                mock_run.assert_not_called()
            self.assertIn("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", str(ctx.exception))

    def test_within_bound_backfill_proceeds_once_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            mock_proc = MagicMock(returncode=0, stdout="ok", stderr="")
            with patch.dict(os.environ, _AUTHORIZED_ENV), patch(
                "core.orchestration.dbt_backfill.subprocess.run", return_value=mock_proc
            ) as mock_run:
                result = dbt_backfill.DbtBackfillRunner(root, "workspaces/demo").run(
                    event_time_start="2026-01-01", event_time_end="2026-01-05",
                )
            self.assertEqual(result.status, "executed")
            mock_run.assert_called_once()

    def test_span_bound_refusal_still_takes_precedence(self):
        """The new gate is additive -- an over-bound unconfirmed span still
        refuses on ITS OWN reason, not masked by the remote-execution one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_real_dbt_project(root)
            with patch.dict(os.environ, _UNAUTHORIZED_ENV), patch(
                "core.orchestration.dbt_backfill.subprocess.run"
            ) as mock_run:
                with self.assertRaises(PermissionError) as ctx:
                    dbt_backfill.DbtBackfillRunner(root, "workspaces/demo").run(
                        event_time_start="2026-01-01", event_time_end="2026-06-01",
                        max_span_days=31,
                    )
                mock_run.assert_not_called()
            self.assertIn("exceeds the bound", str(ctx.exception))


class CosmosGateTests(unittest.TestCase):
    def test_refuses_before_even_attempting_the_cosmos_import(self):
        """The gate is checked first specifically so it's provable regardless
        of whether astronomer-cosmos happens to be installed here."""
        with patch.dict(os.environ, _UNAUTHORIZED_ENV):
            with self.assertRaises(SystemExit) as ctx:
                cosmos_dag.build_dbt_tasks(workspace="workspaces/demo", repo_root=".")
        self.assertIn("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", str(ctx.exception))
        self.assertNotIn("astronomer-cosmos is not installed", str(ctx.exception))

    def test_authorized_env_reaches_the_next_check(self):
        """With the gate satisfied, the function proceeds to its normal next
        precondition (cosmos import / workspace validation) -- proves the gate
        doesn't accidentally block the authorized case too."""
        with patch.dict(os.environ, _AUTHORIZED_ENV):
            with self.assertRaises((SystemExit, ValueError)) as ctx:
                cosmos_dag.build_dbt_tasks(workspace="workspaces/demo", repo_root=".")
        # Must NOT be the remote-execution refusal this time.
        self.assertNotIn("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", str(ctx.exception.args))


class PipelineStageWiringTests(unittest.TestCase):
    def test_dbt_build_stage_command_includes_the_gate(self):
        self.assertIn("check-remote-execution-gate", pipeline_stages.DBT_BUILD_STAGE.command)
        # Must run AFTER project generation, BEFORE the actual dbt invocation.
        cmd = pipeline_stages.DBT_BUILD_STAGE.command
        gen_idx = cmd.index("generate-dbt-project")
        gate_idx = cmd.index("check-remote-execution-gate")
        build_idx = cmd.index("dbt build")
        self.assertLess(gen_idx, gate_idx)
        self.assertLess(gate_idx, build_idx)


class EndToEndCliTests(unittest.TestCase):
    """The actual registered console script, via a real subprocess -- proves
    the wiring survives packaging, not just direct-import calls."""

    def test_cli_refuses_when_unset(self):
        env = {**os.environ, "AUTORESEARCH_ALLOW_REMOTE_EXECUTION": ""}
        proc = subprocess.run(
            [sys.executable, "-m", "core.onboarding.databricks.deploy_gates"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", proc.stdout)

    def test_cli_ok_when_set(self):
        env = {**os.environ, "AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}
        proc = subprocess.run(
            [sys.executable, "-m", "core.onboarding.databricks.deploy_gates"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()

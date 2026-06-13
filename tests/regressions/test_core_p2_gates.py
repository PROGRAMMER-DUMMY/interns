"""Regression tests for core remediation P2 — gate / approval / SSRF bypass.

Themes T7 (gate/approval bypass) + T8 (external-root / SSRF). Workspace-agnostic.
"""
from __future__ import annotations

import inspect
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


# ── P2.1 Genie-lane gate: apply refuses without G3 human provenance ──────────
class GenieLaneGateTests(unittest.TestCase):
    def test_run_deployment_accepts_confirmed_by(self) -> None:
        from core.onboarding.databricks.workspace_deployer import run_deployment

        self.assertIn("confirmed_by", inspect.signature(run_deployment).parameters)

    def _fake_planner(self, root: Path):
        planner = mock.MagicMock()
        planner.repo_root = root
        planner.layout.project_root = root / "workspaces" / "demo"
        (root / "workspaces" / "demo").mkdir(parents=True, exist_ok=True)
        planner.build_plan.return_value = {"summary": {"operation_count": 0}}
        return planner

    def test_genie_apply_refuses_agent_asserted(self) -> None:
        from core.failures import WorkflowBlockedError
        from core.onboarding.databricks import workspace_deployer

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = self._fake_planner(root)
            old = os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION")
            os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = "1"
            try:
                with mock.patch.object(
                    workspace_deployer,
                    "DatabricksWorkspaceDeploymentPlanner",
                    return_value=planner,
                ):
                    # confirm + env set, but NO human name -> G3 must block
                    # BEFORE any remote client/config is touched.
                    with self.assertRaises(WorkflowBlockedError) as ctx:
                        workspace_deployer.run_deployment(
                            root, "workspaces/demo",
                            apply=True, confirm_remote_mutation=True, confirmed_by="",
                        )
                    blob = (str(ctx.exception) + " " + repr(getattr(ctx.exception, "failure", ""))).lower()
                    self.assertIn("provenance", blob)
            finally:
                if old is None:
                    os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                else:
                    os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = old

    def test_genie_apply_passes_g3_with_human_name(self) -> None:
        # With a human name, G3 passes and execution proceeds PAST the gate to
        # config/client setup (which then fails for an unrelated reason) — i.e.
        # the refusal is specifically about human-provenance, not present here.
        from core.failures import WorkflowBlockedError
        from core.onboarding.databricks import workspace_deployer

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = self._fake_planner(root)
            old = os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION")
            os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = "1"
            try:
                with mock.patch.object(
                    workspace_deployer,
                    "DatabricksWorkspaceDeploymentPlanner",
                    return_value=planner,
                ):
                    try:
                        workspace_deployer.run_deployment(
                            root, "workspaces/demo",
                            apply=True, confirm_remote_mutation=True, confirmed_by="Reviewer",
                        )
                    except WorkflowBlockedError as exc:
                        # If it blocks, it must NOT be for human-provenance.
                        self.assertNotIn("human_provenance", str(getattr(exc, "failure", "")))
                    except Exception:
                        pass  # any non-block failure (no real Databricks) is fine
            finally:
                if old is None:
                    os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                else:
                    os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = old


if __name__ == "__main__":
    unittest.main()

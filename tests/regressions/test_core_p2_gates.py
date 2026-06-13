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


# ── P2.2 Remote-target gate (local allow-list, fail closed) ──────────────────
class RemoteTargetGateTests(unittest.TestCase):
    def _build(self, root: Path, target: str, mode: str):
        from core.onboarding.pipeline_deployment_plan import PipelineDeploymentPlanner

        (root / "workspaces" / "demo").mkdir(parents=True, exist_ok=True)
        return PipelineDeploymentPlanner(root, "workspaces/demo", target=target, mode=mode).build()

    def test_databricks_apply_refused_without_env(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
            try:
                # 'databricks' is non-local -> must be gated (the old deny-list missed it).
                with self.assertRaises(PermissionError):
                    self._build(root, "databricks", "apply")
                # An unknown target must also fail closed.
                with self.assertRaises(PermissionError):
                    self._build(root, "some_new_remote", "apply")
            finally:
                if old is not None:
                    os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = old

    def test_local_apply_allowed_without_env(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
            try:
                result = self._build(root, "local", "apply")  # exempt
                self.assertEqual(result.status, "planned_apply")
            finally:
                if old is not None:
                    os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = old


# ── P2.3 Bootstrap does no network on the local-safe path ────────────────────
class BootstrapNoNetworkTests(unittest.TestCase):
    def test_health_check_skipped_without_remote_env(self) -> None:
        from unittest.mock import patch

        from core.onboarding.workspace.bootstrap import AutoBootstrap

        old = os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "workspaces" / "demo").mkdir(parents=True, exist_ok=True)
                boot = AutoBootstrap.__new__(AutoBootstrap)
                # Minimal duck-typed config: databricks enabled + active.
                db = type("DB", (), {"enabled": True, "execution": "jobs",
                                     "is_active": lambda self: True})()
                boot.cfg = type("Cfg", (), {"databricks": db})()
                with patch(
                    "core.execution.databricks_client.DatabricksClient"
                ) as client:
                    readiness = boot.check_databricks_readiness()
                    client.assert_not_called()  # NO network when env is unset
                self.assertFalse(readiness.active)
                self.assertIn("skipped", readiness.message)
        finally:
            if old is not None:
                os.environ["AUTORESEARCH_ALLOW_REMOTE_EXECUTION"] = old


# ── P2.4 External-root allowlist ─────────────────────────────────────────────
class ExternalRootAllowlistTests(unittest.TestCase):
    def test_is_within_allowed_roots(self) -> None:
        from core.storage.external_data import ExternalDataPolicy, is_within_allowed_roots

        with TemporaryDirectory() as repo_td, TemporaryDirectory() as ext_td, TemporaryDirectory() as other_td:
            repo, ext, other = Path(repo_td), Path(ext_td), Path(other_td)
            in_repo = repo / "datasets" / "x.csv"
            policy = ExternalDataPolicy(configured_roots=(ext.resolve(),))
            self.assertTrue(is_within_allowed_roots(in_repo, repo, policy))
            self.assertTrue(is_within_allowed_roots(ext / "a.csv", repo, policy))
            self.assertFalse(is_within_allowed_roots(other / "a.csv", repo, policy))
            # No policy -> only in-repo allowed.
            self.assertFalse(is_within_allowed_roots(ext / "a.csv", repo, None))

    def test_discovery_refuses_unlisted_root(self) -> None:
        from core.onboarding.sources.external_discovery import ExternalSourceDiscoverer

        with TemporaryDirectory() as repo_td, TemporaryDirectory() as ext_td:
            repo, ext = Path(repo_td), Path(ext_td)
            (repo / "workspaces" / "w").mkdir(parents=True)
            (ext / "f.csv").write_text("id\n1\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                ExternalSourceDiscoverer(repo, "workspaces/w", ext, max_files=5).run()


# ── P2.5 SSRF egress control ─────────────────────────────────────────────────
class SsrfEgressTests(unittest.TestCase):
    def test_blocks_metadata_ip(self) -> None:
        from core.onboarding.sources.catalog import assert_url_egress_allowed

        for bad in (
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1/admin",
            "http://10.0.0.5/internal",
            "file:///etc/passwd",
        ):
            with self.assertRaises(RuntimeError):
                assert_url_egress_allowed(bad)

    def test_allows_public_host(self) -> None:
        from core.onboarding.sources.catalog import assert_url_egress_allowed

        # 8.8.8.8 is a public IP literal (no DNS needed); must not raise.
        assert_url_egress_allowed("https://8.8.8.8/data.json")


if __name__ == "__main__":
    unittest.main()

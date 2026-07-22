from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.config import Config, DatabricksConfig
from core.execution.backend import DuckDBBackend, StrictWarehouseBackend, build_execution_backend, normalize_command
from core.execution.databricks_client import DatabricksClient
from core.resource.manager import ResourceDecision


class ExecutionBackendTests(unittest.TestCase):
    def test_command_normalization_accepts_legacy_strings(self):
        self.assertEqual(
            normalize_command("uv run python workspaces/demo/interns/evaluation/experiment.py"),
            ["uv", "run", "python", "workspaces/demo/interns/evaluation/experiment.py"],
        )
        self.assertEqual(normalize_command(["uv", "run"]), ["uv", "run"])

    def test_databricks_backend_requires_explicit_remote_approval(self):
        os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
        cfg = Config(
            databricks=DatabricksConfig(
                enabled=True,
                execution="jobs",
                host="https://example.cloud.databricks.com",
                token="token",
            )
        )
        self.assertIsInstance(build_execution_backend(cfg), DuckDBBackend)

    def test_health_check_redacts_secret_shaped_exception_message(self):
        # databricks_client.py has no logger/print of its own -- it returns
        # raw exception text to callers who print/log it. If the SDK ever
        # echoes a token back in an error message, it must be redacted here
        # at the source, not left to whatever the caller does with it.
        client = DatabricksClient(DatabricksConfig(enabled=True, host="https://example", token="token"))

        class _FakeUser:
            def me(self):
                raise RuntimeError(
                    "connection failed using dapi1234567890abcdef1234 as credential"
                )

        class _FakeClient:
            current_user = _FakeUser()

        client.get_client = lambda: _FakeClient()
        ok, msg = client.health_check()
        self.assertFalse(ok)
        self.assertNotIn("dapi1234567890abcdef1234", msg)
        self.assertIn("[REDACTED:SECRET]", msg)

    def test_discover_capabilities_redacts_secret_shaped_errors(self):
        client = DatabricksClient(DatabricksConfig(enabled=True, host="https://example", token="token"))

        class _FakeUser:
            def me(self):
                raise RuntimeError("auth failed with dapi1234567890abcdef1234")

        class _FakeCatalogs:
            def list(self):
                raise RuntimeError("catalogs unavailable, token=abcdef123456")

        class _FakeJobs:
            def list(self, limit=1):
                raise RuntimeError("jobs unavailable")

        class _FakeClient:
            current_user = _FakeUser()
            catalogs = _FakeCatalogs()
            jobs = _FakeJobs()

        client.get_client = lambda: _FakeClient()
        caps = client.discover_capabilities()
        self.assertNotIn("dapi1234567890abcdef1234", caps["current_user_error"])
        self.assertIn("[REDACTED:SECRET]", caps["current_user_error"])
        self.assertNotIn("abcdef123456", caps["catalog_error"])
        self.assertIn("[REDACTED:SECRET]", caps["catalog_error"])

    def test_get_client_uses_explicit_host_token_when_both_set(self):
        client = DatabricksClient(
            DatabricksConfig(enabled=True, host="https://example", token="tok", profile="unused")
        )
        with mock.patch("databricks.sdk.WorkspaceClient") as fake_ws:
            client.get_client()
        fake_ws.assert_called_once_with(host="https://example", token="tok")

    def test_get_client_falls_back_to_databrickscfg_profile_when_host_or_token_unset(self):
        # Neither host nor token set (e.g. a workstation authenticated purely
        # via `databricks auth login` / `databricks configure`, no .env
        # duplication) -- must fall back to profile-based resolution instead
        # of constructing WorkspaceClient with empty host/token.
        client = DatabricksClient(DatabricksConfig(enabled=True, profile="my-profile"))
        with mock.patch("databricks.sdk.WorkspaceClient") as fake_ws:
            client.get_client()
        fake_ws.assert_called_once_with(profile="my-profile")

    def test_is_active_true_via_databrickscfg_profile_when_no_env_token(self):
        cfg = DatabricksConfig(enabled=True, profile="DEFAULT")  # host/token both empty
        with mock.patch("databricks.sdk.core.Config") as fake_cfg_cls:
            fake_cfg_cls.return_value.authenticate.return_value = None
            self.assertTrue(cfg.is_active())
        fake_cfg_cls.assert_called_once_with(profile="DEFAULT")

    def test_is_active_false_when_neither_env_token_nor_profile_work(self):
        cfg = DatabricksConfig(enabled=True, profile="DEFAULT")  # host/token both empty
        with mock.patch("databricks.sdk.core.Config") as fake_cfg_cls:
            fake_cfg_cls.return_value.authenticate.side_effect = RuntimeError("no auth found")
            self.assertFalse(cfg.is_active())

    def test_is_active_disabled_short_circuits_before_any_profile_check(self):
        cfg = DatabricksConfig(enabled=False, profile="DEFAULT")
        with mock.patch("databricks.sdk.core.Config") as fake_cfg_cls:
            self.assertFalse(cfg.is_active())
        fake_cfg_cls.assert_not_called()

    def test_databricks_jobs_reject_local_python_paths_before_submission(self):
        client = DatabricksClient(DatabricksConfig(enabled=True, host="https://example", token="token"))
        with self.assertRaises(ValueError):
            client.submit_job_run(
                {"experiment_cmd": ["uv", "run", "python", "workspaces/demo/interns/evaluation/experiment.py"]},
                30,
            )

    def test_databricks_warehouse_strict_mode_does_not_fallback(self):
        class FailingClient:
            def _extract_warehouse_id(self):
                return "warehouse_1"

            def get_client(self):
                raise RuntimeError("remote unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            sql_path = Path(tmp) / "query.sql"
            log_path = Path(tmp) / "run.log"
            sql_path.write_text("SELECT 1", encoding="utf-8")
            backend = StrictWarehouseBackend(
                FailingClient(),
                DatabricksConfig(enabled=True, execution="warehouse", fallback="fail"),
            )
            result = backend.execute({"sql_file": str(sql_path)}, 10, 10, log_path)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("databricks_backend: warehouse", result.log_content)
            self.assertIn("remote unavailable", result.log_content)

    def test_duckdb_backend_blocks_when_resource_decision_blocks_local_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            decision = ResourceDecision(
                status="blocked",
                mode="local_blocked_remote_recommended",
                recommended_workers=1,
                recommended_api_concurrency=1,
                max_stream_chunk_bytes=1_048_576,
                warnings=[],
                blockers=["workspace_disk_budget_exceeded"],
            )

            result = DuckDBBackend(resource_decision=decision).execute(
                {"experiment_cmd": ["python", "-c", "print('should not run')"]},
                10,
                10,
                log_path,
            )

            self.assertEqual(result.exit_code, 1)
            self.assertIn("Resource preflight blocked", result.log_content)
            self.assertEqual(result.metadata["resource_decision"]["mode"], "local_blocked_remote_recommended")


if __name__ == "__main__":
    unittest.main()

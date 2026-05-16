from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.config import Config, DatabricksConfig
from core.execution.backend import DuckDBBackend, StrictWarehouseBackend, build_execution_backend, normalize_command
from core.execution.databricks_client import DatabricksClient


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


if __name__ == "__main__":
    unittest.main()

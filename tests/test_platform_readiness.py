"""core.platform_readiness: read-only Databricks/dbt/Airflow readiness check.

Reuses existing primitives (resolve_databricks_config, DatabricksClient.
health_check, cosmos_dag.cosmos_available) -- these tests mock at those
seams rather than the real environment, since dbt/Airflow presence varies
by machine and this venv's own dbt install status has already flipped
during this session.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from core.platform_readiness import ReadinessReport, check, main


class DatabricksCheckTests(unittest.TestCase):
    def test_not_enabled_is_not_configured(self):
        mock_cfg = MagicMock(enabled=False)
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg):
            report = check("")
        self.assertEqual(report.databricks["status"], "not_configured")
        self.assertEqual(report.blockers(), [])

    def test_enabled_and_healthy_is_ready(self):
        mock_cfg = MagicMock(enabled=True, catalog="healthcare_rcm", execution="warehouse")
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(True, "Connected as a@b.com"),
        ):
            report = check("")
        self.assertEqual(report.databricks["status"], "ready")
        self.assertEqual(report.blockers(), [])

    def test_enabled_but_unreachable_is_a_real_blocker(self):
        mock_cfg = MagicMock(enabled=True, catalog="healthcare_rcm", execution="warehouse")
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(False, "Authentication failed (HTTP 401)."),
        ):
            report = check("")
        self.assertEqual(report.databricks["status"], "blocked")
        self.assertEqual(len(report.blockers()), 1)
        self.assertIn("401", report.blockers()[0])

    def test_enterprise_id_threads_through_to_config_resolution(self):
        mock_cfg = MagicMock(enabled=False)
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg) as fake_resolve:
            check("acme_corp")
        fake_resolve.assert_called_once_with("acme_corp")


class DbtAndAirflowNeverBlockTests(unittest.TestCase):
    """dbt/Airflow absence is a capability gap for the cloud-native path, not
    a broken state -- the local-DuckDB KPI flow works without either, so
    neither should ever appear in blockers()."""

    def test_dbt_not_installed_is_not_a_blocker(self):
        with patch("importlib.util.find_spec", return_value=None), patch(
            "core.config.resolve_databricks_config", return_value=MagicMock(enabled=False)
        ):
            report = check("")
        self.assertEqual(report.dbt["status"], "not_installed")
        self.assertEqual(report.blockers(), [])

    def test_airflow_not_installed_is_not_a_blocker(self):
        with patch("importlib.util.find_spec", return_value=None), patch(
            "core.config.resolve_databricks_config", return_value=MagicMock(enabled=False)
        ):
            report = check("")
        self.assertEqual(report.airflow["status"], "not_installed")
        self.assertEqual(report.blockers(), [])


class ReportShapeTests(unittest.TestCase):
    def test_summary_is_json_serializable(self):
        report = ReadinessReport(
            databricks={"status": "ready", "detail": "x"},
            dbt={"status": "ready", "detail": "y"},
            airflow={"status": "not_installed", "detail": "z"},
        )
        json.dumps(report.summary())  # must not raise


class MainCliTests(unittest.TestCase):
    def test_json_flag_prints_valid_json_and_exit_reflects_blockers(self):
        import io
        from contextlib import redirect_stdout

        mock_cfg = MagicMock(enabled=True, catalog="c", execution="warehouse")
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(False, "down"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = main(["--json"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["databricks"]["status"], "blocked")
        self.assertEqual(exit_code, 1)

    def test_workspace_flag_resolves_enterprise_id_from_databricks_source(self):
        import io
        import json as json_mod
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspaces" / "demo"
            ws.mkdir(parents=True)
            (ws / "workspace_settings.json").write_text(
                json_mod.dumps({"databricks_source": {"enterprise_id": "acme_corp"}}),
                encoding="utf-8",
            )
            with patch("core.config.resolve_databricks_config", return_value=MagicMock(enabled=False)) as fake_resolve:
                with redirect_stdout(io.StringIO()):
                    main(["--workspace", "workspaces/demo", "--repo-root", str(root), "--json"])
            fake_resolve.assert_called_once_with("acme_corp")


class CostTelemetryCheckTests(unittest.TestCase):
    """3.1: system.billing/system.query are NOT on by default. A warehouse that
    works perfectly and a cost query that returns nothing is the failure mode."""

    def test_not_probed_without_the_remote_gate_and_never_claimed_ready(self):
        import os

        mock_cfg = MagicMock(enabled=True, catalog="c", execution="warehouse")
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(True, "ok"),
        ), patch("core.execution.databricks_client.DatabricksClient.execute_query") as q:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                report = check("")
        q.assert_not_called()
        self.assertEqual(report.cost_telemetry["status"], "unknown")
        self.assertIn("system-schemas enable", report.cost_telemetry["detail"])

    def test_an_unreadable_system_schema_is_blocked_with_the_enable_command(self):
        import os

        mock_cfg = MagicMock(enabled=True, catalog="c", execution="warehouse")
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(True, "ok"),
        ), patch(
            "core.execution.databricks_client.DatabricksClient.execute_query",
            side_effect=RuntimeError("TABLE_OR_VIEW_NOT_FOUND"),
        ), patch.dict(os.environ, {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}):
            report = check("")
        self.assertEqual(report.cost_telemetry["status"], "blocked")
        self.assertIn("system.billing.usage", report.cost_telemetry["detail"])
        self.assertIn("system-schemas enable", report.cost_telemetry["detail"])
        # A capability gap, not a broken platform -- the local path still works.
        self.assertEqual(report.blockers(), [])

    def test_all_three_readable_is_ready(self):
        import os

        mock_cfg = MagicMock(enabled=True, catalog="c", execution="warehouse")
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(True, "ok"),
        ), patch(
            "core.execution.databricks_client.DatabricksClient.execute_query",
            return_value=(["1"], [["1"]]),
        ), patch.dict(os.environ, {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}):
            report = check("")
        self.assertEqual(report.cost_telemetry["status"], "ready")
        self.assertIn("reconcile-warehouse-cost", report.cost_telemetry["detail"])


if __name__ == "__main__":
    unittest.main()

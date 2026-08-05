"""core.platform_readiness: read-only Databricks/dbt/Airflow readiness check.

Reuses existing primitives (resolve_databricks_config, DatabricksClient.
health_check, cosmos_dag.cosmos_available) -- these tests mock at those
seams rather than the real environment, since dbt/Airflow presence varies
by machine and this venv's own dbt install status has already flipped
during this session.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.platform_readiness import (
    ReadinessReport,
    check,
    detect_auth_source,
    find_profile_conflicts,
    main,
    profile_conflict_note,
)

HOST = "https://dbc-a2362023-5116.cloud.databricks.com"


def _write_cfg(tmp: str, body: str) -> str:
    path = Path(tmp) / ".databrickscfg"
    path.write_text(body, encoding="utf-8")
    return str(path)


class ProfileConflictTests(unittest.TestCase):
    """The real state on this machine: DEFAULT (Valid: YES) and
    dbc-a2362023-5116 (Valid: NO) both point at one host, so every
    `databricks bundle` command dies with 'multiple profiles matched' while
    WorkspaceClient(profile=...) works -- readiness said 'ready' anyway."""

    def test_two_profiles_one_host_is_one_conflict_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_cfg(tmp, f"""
[DEFAULT]
host = {HOST}
token = dapi-fake

[dbc-a2362023-5116]
host = {HOST}/
auth_type = databricks-cli
""")
            self.assertEqual(
                find_profile_conflicts(cfg), [["DEFAULT", "dbc-a2362023-5116"]]
            )

    def test_different_hosts_do_not_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_cfg(tmp, f"""
[DEFAULT]
host = {HOST}

[other]
host = https://adb-999.azuredatabricks.net
""")
            self.assertEqual(find_profile_conflicts(cfg), [])

    def test_missing_file_is_empty_not_an_exception(self):
        self.assertEqual(find_profile_conflicts("no/such/.databrickscfg"), [])

    def test_note_names_profiles_and_never_leaks_the_host(self):
        note = profile_conflict_note([["DEFAULT", "dbc-a2362023-5116"]])
        self.assertIn("multiple profiles matched", note)
        self.assertIn("DEFAULT", note)
        self.assertIn("DATABRICKS_CONFIG_PROFILE", note)
        self.assertNotIn(HOST, note)
        self.assertNotIn("cloud.databricks.com", note)
        self.assertNotIn("dapi", note)

    def test_no_conflicts_means_no_note(self):
        self.assertEqual(profile_conflict_note([]), "")


class AuthSourceTests(unittest.TestCase):
    """Report WHICH credential was used, so an operator can tell why the CLI
    and the SDK disagree."""

    def setUp(self):
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE", "DATABRICKS_CONFIG_FILE"):
            os.environ.pop(var, None)

    def test_env_host_wins(self):
        os.environ["DATABRICKS_HOST"] = HOST
        os.environ["DATABRICKS_CONFIG_PROFILE"] = "DEFAULT"
        self.assertEqual(detect_auth_source(), "env:DATABRICKS_HOST")

    def test_pinned_profile_env_beats_the_config_file(self):
        os.environ["DATABRICKS_CONFIG_PROFILE"] = "DEFAULT"
        self.assertEqual(detect_auth_source(), "env:DATABRICKS_CONFIG_PROFILE:DEFAULT")

    def test_single_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_cfg(tmp, f"[DEFAULT]\nhost = {HOST}\n")
            self.assertEqual(detect_auth_source(cfg), "profile:DEFAULT")

    def test_conflicting_profiles_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_cfg(tmp, f"[DEFAULT]\nhost = {HOST}\n\n[dupe]\nhost = {HOST}\n")
            self.assertEqual(detect_auth_source(cfg), "profile:ambiguous")

    def test_no_credentials_anywhere(self):
        self.assertEqual(detect_auth_source("no/such/.databrickscfg"), "none")

    def test_config_file_env_var_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATABRICKS_CONFIG_FILE"] = _write_cfg(
                tmp, f"[DEFAULT]\nhost = {HOST}\n\n[dupe]\nhost = {HOST}\n"
            )
            self.assertEqual(detect_auth_source(), "profile:ambiguous")


class WarehouseStateTests(unittest.TestCase):
    """The account's only warehouse is STOPPED at rest (serverless PRO). A cold
    start is a note, never a failure -- status must stay `ready`."""

    def _report(self, warehouses, conflicts):
        mock_cfg = MagicMock(enabled=True, catalog="c", execution="warehouse")
        client = MagicMock()
        client.warehouses.list.return_value = warehouses
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(True, "Connected as a@b.com"),
        ), patch(
            "core.execution.databricks_client.DatabricksClient.get_client", return_value=client
        ), patch(
            "core.platform_readiness.find_profile_conflicts", return_value=conflicts
        ), patch(
            "core.platform_readiness.detect_auth_source", return_value="profile:ambiguous"
        ):
            return check("")

    def test_stopped_warehouse_and_conflicts_are_notes_not_failures(self):
        stopped = MagicMock(state=MagicMock(value="STOPPED"))
        report = self._report([stopped], [["DEFAULT", "dbc-a2362023-5116"]])
        db = report.databricks
        self.assertEqual(db["status"], "ready")
        self.assertEqual(report.blockers(), [])
        self.assertEqual(db["warehouse_state"], "STOPPED")
        self.assertEqual(db["auth_source"], "profile:ambiguous")
        self.assertEqual(db["profile_conflicts"], [["DEFAULT", "dbc-a2362023-5116"]])
        self.assertIn("[~] warehouse STOPPED: the first query pays a cold start", db["notes"])
        self.assertTrue(any("multiple profiles matched" in n for n in db["notes"]))

    def test_running_warehouse_gets_no_cold_start_note(self):
        running = MagicMock(state=MagicMock(value="RUNNING"))
        db = self._report([running], []).databricks
        self.assertEqual(db["warehouse_state"], "RUNNING")
        self.assertEqual(db["notes"], [])

    def test_unreachable_warehouse_list_is_none_not_a_crash(self):
        mock_cfg = MagicMock(enabled=True, catalog="c", execution="warehouse")
        client = MagicMock()
        client.warehouses.list.side_effect = RuntimeError("PERMISSION_DENIED")
        with patch("core.config.resolve_databricks_config", return_value=mock_cfg), patch(
            "core.execution.databricks_client.DatabricksClient.health_check",
            return_value=(True, "ok"),
        ), patch(
            "core.execution.databricks_client.DatabricksClient.get_client", return_value=client
        ), patch("core.platform_readiness.find_profile_conflicts", return_value=[]):
            db = check("").databricks
        self.assertEqual(db["status"], "ready")
        self.assertIsNone(db["warehouse_state"])


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
            "core.platform_readiness._warehouse_state", return_value=None
        ), patch(
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

    def setUp(self):
        # These all take the authenticated path, which now also lists warehouses;
        # no test may touch the network.
        patcher = patch("core.platform_readiness._warehouse_state", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

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

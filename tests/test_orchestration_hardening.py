"""Orchestration emitter hardening (cloud-first restructure spec section 8).

Airflow/Cosmos are optional deps and are not installed here (same constraint
test_cosmos_dag.py and test_pipeline_orchestration.py already work under), so
these tests cover the pure emitters -- the schedule derivation and the emitted
command text -- which is where every rule in scope actually lives.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.orchestration import airflow_dag, cosmos_dag


class HashedScheduleTests(unittest.TestCase):
    def test_offset_is_stable_for_one_workspace(self):
        first = airflow_dag.hashed_schedule("workspaces/rcm")
        for _ in range(3):
            self.assertEqual(airflow_dag.hashed_schedule("workspaces/rcm"), first)

    def test_offset_differs_across_workspaces(self):
        self.assertNotEqual(
            airflow_dag.hashed_schedule("workspaces/rcm"),
            airflow_dag.hashed_schedule("workspaces/other"),
        )

    def test_is_a_daily_cron_inside_the_nightly_window(self):
        for workspace in ("workspaces/a", "workspaces/b", "workspaces/c", "workspaces/d"):
            minute, hour, rest = airflow_dag.hashed_schedule(workspace).split(" ", 2)
            self.assertEqual(rest, "* * *")
            self.assertIn(int(minute), range(0, 60))
            self.assertIn(int(hour), range(1, 6))

    def test_not_a_fixed_default_time(self):
        # A fleet all landing on the same minute is the thundering herd this
        # exists to prevent -- so a spread across ids is the actual assertion.
        offsets = {airflow_dag.hashed_schedule(f"workspaces/ws{i}") for i in range(20)}
        self.assertGreater(len(offsets), 10)


class BackfillSeamTests(unittest.TestCase):
    def test_backfill_command_is_params_driven(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = cosmos_dag.backfill_command(
                workspace="workspaces/demo", repo_root=tmp
            )
            self.assertIn("{{ params.event_time_start }}", command)
            self.assertIn("{{ params.event_time_end }}", command)
            self.assertIn("run-dbt-backfill", command)
            self.assertIn("--workspace workspaces/demo", command)

    def test_no_declared_event_time_reports_full_refresh_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp) / "workspaces" / "demo" / "dbt" / "models" / "marts"
            models.mkdir(parents=True)
            (models / "fct_x.sql").write_text(
                "{{ config(materialized='table', cluster_by=['a']) }}\nselect 1 as a\n",
                encoding="utf-8",
            )
            command = cosmos_dag.backfill_command(
                workspace="workspaces/demo", repo_root=tmp
            )
            self.assertIn("DEGRADED", command)
            self.assertIn("FULL REFRESH", command)

    def test_declared_event_time_promises_no_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp) / "workspaces" / "demo" / "dbt" / "models" / "marts"
            models.mkdir(parents=True)
            (models / "fct_x.sql").write_text(
                "{{ config(materialized='incremental', incremental_strategy='microbatch', "
                "event_time='paid_at', lookback=2, unique_key='id', "
                "on_schema_change='append_new_columns') }}\nselect 1 as id\n",
                encoding="utf-8",
            )
            command = cosmos_dag.backfill_command(
                workspace="workspaces/demo", repo_root=tmp
            )
            self.assertNotIn("DEGRADED", command)


class PublishGoldSeamTests(unittest.TestCase):
    def test_publish_command_runs_the_generated_swap_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = cosmos_dag.publish_gold_command(
                workspace="workspaces/demo", repo_root=tmp
            )
            self.assertIn("dbt run-operation publish_gold", command)
            self.assertIn("--project-dir", command)
            self.assertIn("--profiles-dir", command)
            # The swap lives in the generated macro, never as inline DDL here.
            self.assertNotIn("CREATE OR REPLACE", command.upper())


class FleetOffsetTests(unittest.TestCase):
    def test_stable_for_one_workspace(self):
        first = airflow_dag._fleet_offset("workspaces/rcm", 7)
        for _ in range(3):
            self.assertEqual(airflow_dag._fleet_offset("workspaces/rcm", 7), first)

    def test_bounded_by_modulus(self):
        for modulus in (7, 28):
            for i in range(10):
                offset = airflow_dag._fleet_offset(f"workspaces/ws{i}", modulus)
                self.assertIn(offset, range(0, modulus))

    def test_spreads_across_a_fleet(self):
        offsets = {airflow_dag._fleet_offset(f"workspaces/ws{i}", 7) for i in range(20)}
        self.assertGreater(len(offsets), 1)


class ReadOwnerTests(unittest.TestCase):
    def test_defaults_to_unassigned_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            self.assertEqual(airflow_dag._read_owner(root, "workspaces/demo"), "unassigned")

    def test_empty_workspace_defaults_to_unassigned(self):
        self.assertEqual(airflow_dag._read_owner(Path("."), ""), "unassigned")

    def test_reads_the_intake_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake_dir = root / "workspaces" / "demo" / "interns" / "generated" / "intake"
            intake_dir.mkdir(parents=True)
            (intake_dir / "intake_answers.json").write_text(
                json.dumps({"owner_and_operations": {"answer": "  data-eng-oncall  "}}),
                encoding="utf-8",
            )
            self.assertEqual(
                airflow_dag._read_owner(root, "workspaces/demo"), "data-eng-oncall"
            )

    def test_blank_answer_defaults_to_unassigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake_dir = root / "workspaces" / "demo" / "interns" / "generated" / "intake"
            intake_dir.mkdir(parents=True)
            (intake_dir / "intake_answers.json").write_text(
                json.dumps({"owner_and_operations": {"answer": "  "}}), encoding="utf-8"
            )
            self.assertEqual(airflow_dag._read_owner(root, "workspaces/demo"), "unassigned")


class AlertingBannerTests(unittest.TestCase):
    def test_banner_when_no_webhook_and_unassigned(self):
        banner = airflow_dag._alerting_banner(owner="unassigned", webhook_configured=False)
        self.assertIn("ALERTING NOT CONFIGURED", banner)
        self.assertTrue(banner.startswith("[x]"))

    def test_no_banner_when_webhook_configured(self):
        self.assertEqual(
            airflow_dag._alerting_banner(owner="unassigned", webhook_configured=True), ""
        )

    def test_no_banner_when_owner_known(self):
        self.assertEqual(
            airflow_dag._alerting_banner(owner="data-eng-oncall", webhook_configured=False), ""
        )


class NotifyFailurePayloadTests(unittest.TestCase):
    def test_payload_carries_owner_severity_workspace_task(self):
        from unittest.mock import MagicMock, patch

        from core.orchestration.airflow_dag import _DEFAULT_WORKSPACE_ENV, _notify_failure

        ti = MagicMock(task_id="dbt_build")
        dag = MagicMock(dag_id="autoresearch_medallion_pipeline")
        dag.default_args = {"owner": "data-eng-oncall"}
        with mock.patch.dict(
            os.environ,
            {"AUTORESEARCH_ALERT_WEBHOOK_URL": "https://hooks.example/x",
             _DEFAULT_WORKSPACE_ENV: "workspaces/demo"},
        ):
            with patch("urllib.request.urlopen") as mock_urlopen:
                _notify_failure({"task_instance": ti, "dag": dag})
                mock_urlopen.assert_called_once()
                request = mock_urlopen.call_args[0][0]
                payload = json.loads(request.data.decode("utf-8"))
                self.assertEqual(payload["owner"], "data-eng-oncall")
                self.assertEqual(payload["severity"], "error")
                self.assertEqual(payload["workspace"], "workspaces/demo")
                self.assertEqual(payload["task"], "dbt_build")

    def test_missing_owner_defaults_to_unassigned(self):
        from unittest.mock import MagicMock, patch

        from core.orchestration.airflow_dag import _notify_failure

        ti = MagicMock(task_id="dbt_build")
        dag = MagicMock(dag_id="autoresearch_medallion_pipeline")
        dag.default_args = {}
        with mock.patch.dict(os.environ, {"AUTORESEARCH_ALERT_WEBHOOK_URL": "https://hooks.example/x"}):
            with patch("urllib.request.urlopen") as mock_urlopen:
                _notify_failure({"task_instance": ti, "dag": dag})
                request = mock_urlopen.call_args[0][0]
                payload = json.loads(request.data.decode("utf-8"))
                self.assertEqual(payload["owner"], "unassigned")


class RetentionHoursTests(unittest.TestCase):
    def test_defaults_when_sla_contract_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                cosmos_dag._retention_hours(Path(tmp), "workspaces/demo", "silver"), 365 * 24
            )
            self.assertEqual(
                cosmos_dag._retention_hours(Path(tmp), "workspaces/demo", "gold"), 730 * 24
            )

    def test_reads_declared_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            medallion_dir = root / "workspaces" / "demo" / "interns" / "generated" / "medallion"
            medallion_dir.mkdir(parents=True)
            (medallion_dir / "sla_contract.json").write_text(
                json.dumps({"retention_policy": {"silver_days": 30, "gold_days": 90}}),
                encoding="utf-8",
            )
            self.assertEqual(
                cosmos_dag._retention_hours(root, "workspaces/demo", "silver"), 30 * 24
            )
            self.assertEqual(
                cosmos_dag._retention_hours(root, "workspaces/demo", "gold"), 90 * 24
            )

    def test_never_zero_hours_on_a_malformed_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            medallion_dir = root / "workspaces" / "demo" / "interns" / "generated" / "medallion"
            medallion_dir.mkdir(parents=True)
            (medallion_dir / "sla_contract.json").write_text(
                json.dumps({"retention_policy": {"silver_days": 0}}), encoding="utf-8"
            )
            hours = cosmos_dag._retention_hours(root, "workspaces/demo", "silver")
            self.assertGreater(hours, 0)


def _write_generated_dbt_project(root: Path, workspace: str) -> Path:
    dbt_dir = root / workspace / "dbt"
    (dbt_dir / "models" / "staging").mkdir(parents=True)
    (dbt_dir / "models" / "intermediate").mkdir(parents=True)
    (dbt_dir / "models" / "marts").mkdir(parents=True)
    (dbt_dir / "models" / "staging" / "stg_x.sql").write_text("select 1", encoding="utf-8")
    (dbt_dir / "models" / "intermediate" / "int_x.sql").write_text("select 1", encoding="utf-8")
    (dbt_dir / "models" / "marts" / "fct_kpi_001.sql").write_text("select 1", encoding="utf-8")
    (dbt_dir / "dbt_project.yml").write_text(
        "\n".join([
            "name: 'demo'",
            "profile: 'demo'",
            "version: '1.0.0'",
            "models:",
            "  demo:",
            "    staging:",
            "      +schema: silver",
            "    intermediate:",
            "      +schema: silver",
            "    marts:",
            "      +schema: gold__staging",
            "vars:",
            "  gold_schema: gold",
            "  gold_staging_schema: gold__staging",
            "  catalog: main",
            "",
        ]),
        encoding="utf-8",
    )
    return dbt_dir


class MaintenanceCommandTests(unittest.TestCase):
    def test_no_project_yet_is_a_harmless_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = cosmos_dag.maintenance_command(
                workspace="workspaces/demo", repo_root=tmp, layer="gold", weekday=2,
            )
            self.assertIn("echo", command)
            self.assertNotIn("OPTIMIZE", command)

    def test_emits_optimize_and_vacuum_per_table_never_retain_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_generated_dbt_project(root, "workspaces/demo")
            command = cosmos_dag.maintenance_command(
                workspace="workspaces/demo", repo_root=tmp, layer="gold", weekday=2,
            )
            self.assertIn("OPTIMIZE", command)
            self.assertIn("fct_kpi_001", command)
            self.assertIn("VACUUM", command)
            self.assertIn("RETAIN", command)
            self.assertNotIn("RETAIN 0 HOURS", command)
            self.assertIn("`main`.`gold`.`fct_kpi_001`", command)

    def test_silver_layer_covers_staging_and_intermediate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_generated_dbt_project(root, "workspaces/demo")
            command = cosmos_dag.maintenance_command(
                workspace="workspaces/demo", repo_root=tmp, layer="silver", weekday=2,
            )
            self.assertIn("stg_x", command)
            self.assertIn("int_x", command)
            self.assertIn("`silver`", command)

    def test_weekday_gate_is_in_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_generated_dbt_project(root, "workspaces/demo")
            command = cosmos_dag.maintenance_command(
                workspace="workspaces/demo", repo_root=tmp, layer="gold", weekday=0,
            )
            self.assertIn("date +%u", command)
            self.assertIn("-eq 1", command)  # weekday 0 (Monday) -> isoweekday 1


class GhostReconcileCommandTests(unittest.TestCase):
    def test_command_is_monthly_gated_and_calls_the_cli(self):
        command = cosmos_dag.ghost_reconcile_command(
            workspace="workspaces/demo", repo_root=".", day_of_month=15, schema="gold",
        )
        self.assertIn("date +%d", command)
        self.assertIn("-eq 15", command)
        self.assertIn("ghost-reconcile", command)
        self.assertIn("--workspace workspaces/demo", command)
        self.assertIn("--schema gold", command)


class PublishDbtStateCommandTests(unittest.TestCase):
    def test_command_shells_to_the_governed_cli_never_inline_databricks_fs_cp(self):
        command = cosmos_dag.publish_dbt_state_command(
            workspace="workspaces/demo", repo_root="."
        )
        self.assertIn("publish-dbt-state", command)
        self.assertIn("--workspace workspaces/demo", command)
        # The swap/publish lives in the governed CLI, never inline here --
        # same "one command, two callers can't diverge" rule as
        # publish_gold_command above.
        self.assertNotIn("databricks fs cp", command)


class BackfillPoolTests(unittest.TestCase):
    def test_backfill_task_construction_carries_a_dedicated_pool(self):
        # Same static-source-inspection bar DbtStateWiringOrderTests below
        # already established for this Airflow-not-installed environment: a
        # dedicated `backfill` pool is what stops a large replay from
        # starving task slots the nightly scheduled run needs (gap research
        # / airflow_cli_reference.md section "Known gaps in our wiring" #1).
        import inspect

        source = inspect.getsource(airflow_dag)
        idx = source.index("cosmos_dag.build_backfill_task")
        call_snippet = source[idx: idx + 200]
        self.assertIn('pool="backfill"', call_snippet)

    def test_bootstrap_command_is_documented_in_the_module_header(self):
        # The pool itself must exist before a DAG run can use it -- that is a
        # one-time, per-deployment `airflow pools set` call, not something a
        # DAG-parse-time import can create. Documented (setup_pools) rather
        # than silently assumed.
        import inspect

        source = inspect.getsource(airflow_dag)
        self.assertIn("setup_pools", source)
        self.assertIn('airflow pools set backfill 2 "bounded replay capacity"', source)


class DbtStateWiringOrderTests(unittest.TestCase):
    def test_publish_dbt_state_is_wired_after_publish_gold(self):
        # Airflow is not installed in this environment (module docstring,
        # top of file) -- a real DAG object can't be built here, so this
        # pins the wiring statically, the same bar
        # test_pipeline_orchestration.py's AirflowWiringTests already
        # established for the sibling days_ago regression.
        import inspect

        source = inspect.getsource(airflow_dag)
        gold_idx = source.index("cosmos_dag.build_publish_gold_task")
        state_idx = source.index("cosmos_dag.build_publish_dbt_state_task")
        self.assertLess(
            gold_idx, state_idx,
            "publish_dbt_state must be constructed after publish_gold",
        )
        self.assertIn("publish_task >> state_task", source)


if __name__ == "__main__":
    unittest.main()

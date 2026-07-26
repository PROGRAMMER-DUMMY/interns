"""The pipeline DAG topology is well-formed and dependency-ordered, and the
Dagster wiring imports gracefully without Dagster installed.
"""
from __future__ import annotations

import os
import unittest

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.onboarding.data_source_panel import DataSourceAnswerRecorder
from core.orchestration.pipeline_stages import (
    DBT_BUILD_STAGE,
    STAGES,
    command_for,
    stage_map,
    stages_for_workspace,
    topological_order,
)


class PipelineTopologyTests(unittest.TestCase):
    def test_every_upstream_is_a_known_stage(self):
        keys = {s.key for s in STAGES}
        for s in STAGES:
            for up in s.upstream:
                self.assertIn(up, keys, f"{s.key} depends on unknown stage {up}")

    def test_topological_order_respects_dependencies(self):
        order = topological_order()
        pos = {k: i for i, k in enumerate(order)}
        self.assertEqual(set(order), {s.key for s in STAGES})
        for s in STAGES:
            for up in s.upstream:
                self.assertLess(pos[up], pos[s.key],
                                f"{up} must run before {s.key}")

    def test_expected_pipeline_shape(self):
        # The canonical flow: build depends on design; kpi depends on build +
        # resolve; dashboard depends on kpi.
        smap = stage_map()
        self.assertIn("medallion_design", smap["medallion_build"].upstream)
        self.assertIn("medallion_build", smap["kpi_results"].upstream)
        self.assertIn("resolve_features", smap["kpi_results"].upstream)
        # The dashboard is gated by the intent-coverage guard, which is itself
        # downstream of generation -- guards are stages, not optional CLIs.
        self.assertIn("kpi_intent_coverage", smap["dashboard"].upstream)
        self.assertIn("kpi_results", smap["kpi_intent_coverage"].upstream)

    def test_command_templating(self):
        stage = stage_map()["medallion_build"]
        cmd = command_for(stage, "workspaces/demo")
        self.assertIn("workspaces/demo", cmd)
        self.assertNotIn("{ws}", cmd)

    def test_dbt_build_stage_retries_via_dbt_retry_before_a_full_rebuild(self):
        # dbt retry (1.6+) resumes from target/run_results.json instead of
        # rebuilding every model from scratch -- only correct here because
        # this BashOperator path runs in the SAME stable {ws}/dbt directory
        # every invocation (unlike the Cosmos path, which temp-clones).
        cmd = command_for(DBT_BUILD_STAGE, "workspaces/demo")
        retry_idx = cmd.index("dbt retry")
        build_idx = cmd.index("dbt build")
        self.assertLess(retry_idx, build_idx, "dbt retry must be attempted before dbt build")
        self.assertIn("||", cmd)


class StagesForWorkspaceTests(unittest.TestCase):
    """stages_for_workspace(): byte-identical to STAGES for every local-only
    workspace; dbt_build supersedes medallion_build+kpi_results only for a
    workspace that actually declared a databricks_source.
    """

    def test_local_only_workspace_gets_the_exact_default_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            result = stages_for_workspace(str(root), "workspaces/demo")
            self.assertEqual(result, STAGES)

    def test_exclusive_mode_workspace_substitutes_dbt_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            DataSourceAnswerRecorder(root, "workspaces/demo").apply(
                "databricks_exclusive", catalog="main", schema="bronze", confirmed_by="shubham"
            )
            result = stages_for_workspace(str(root), "workspaces/demo")
            keys = [s.key for s in result]
            self.assertIn("dbt_build", keys)
            self.assertNotIn("medallion_build", keys)
            self.assertNotIn("kpi_results", keys)
            # dbt_build supersedes kpi_results, and the intent guard repoints onto
            # it -- still after SQL generation, still gating the dashboard.
            guard = next(s for s in result if s.key == "kpi_intent_coverage")
            self.assertEqual(guard.upstream, ("dbt_build",))
            dashboard = next(s for s in result if s.key == "dashboard")
            self.assertEqual(dashboard.upstream, ("kpi_intent_coverage",))
            # Every other stage's identity/command is untouched.
            onboard = next(s for s in result if s.key == "onboard")
            self.assertEqual(onboard, stage_map()["onboard"])

    def test_dbt_path_topology_is_still_a_valid_dag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "demo").mkdir(parents=True)
            DataSourceAnswerRecorder(root, "workspaces/demo").apply(
                "databricks_exclusive", catalog="main", schema="bronze", confirmed_by="shubham"
            )
            stages = stages_for_workspace(str(root), "workspaces/demo")
            order = topological_order(stages)
            pos = {k: i for i, k in enumerate(order)}
            self.assertEqual(set(order), {s.key for s in stages})
            for s in stages:
                for up in s.upstream:
                    self.assertLess(pos[up], pos[s.key])

    def test_dbt_build_stage_is_not_a_member_of_the_default_stages(self):
        # Confirms DBT_BUILD_STAGE is additive-only, not baked into what
        # every existing caller (airflow_dag.py, dagster_defs.py,
        # pipeline-run) already renders unconditionally.
        self.assertNotIn(DBT_BUILD_STAGE.key, {s.key for s in STAGES})


class DagsterWiringTests(unittest.TestCase):
    def test_module_imports_without_dagster(self):
        # The module must import (documenting the shape + exposing run_pipeline)
        # even when dagster is not installed.
        from core.orchestration import dagster_defs
        self.assertTrue(hasattr(dagster_defs, "run_pipeline"))
        self.assertTrue(hasattr(dagster_defs, "build_definitions"))

    def test_build_definitions_requires_dagster_clearly(self):
        from core.orchestration import dagster_defs
        try:
            import dagster  # noqa: F401
        except ImportError:
            with self.assertRaises(SystemExit):
                dagster_defs.build_definitions("workspaces/demo")


class AirflowWiringTests(unittest.TestCase):
    def test_module_imports_without_airflow(self):
        from core.orchestration import airflow_dag
        self.assertTrue(hasattr(airflow_dag, "build_dag"))

    def test_does_not_use_the_removed_days_ago_helper(self):
        # Found live against a real Airflow install: airflow.utils.dates.days_ago
        # no longer exists (removed) -- and was Airflow's own documented
        # anti-pattern anyway (a dynamic start_date shifts on every DAG
        # re-parse, causing duplicate/missing runs). Static source check since
        # this venv doesn't have Airflow installed to catch it at import time.
        import inspect

        from core.orchestration import airflow_dag
        source = inspect.getsource(airflow_dag)
        self.assertNotIn("from airflow.utils.dates import days_ago", source)

    def test_build_dag_requires_airflow_clearly(self):
        from core.orchestration import airflow_dag
        try:
            import airflow  # noqa: F401
        except ImportError:
            with self.assertRaises(SystemExit):
                airflow_dag.build_dag("workspaces/demo")

    def test_build_dag_with_custom_stages_still_fails_gracefully(self):
        # The new `stages` param (for the dbt-path topology) must not bypass
        # the same clear, graceful failure when Airflow isn't installed.
        from core.orchestration import airflow_dag
        try:
            import airflow  # noqa: F401
        except ImportError:
            with self.assertRaises(SystemExit):
                airflow_dag.build_dag("workspaces/demo", stages=(DBT_BUILD_STAGE,))


class NotifyFailureTests(unittest.TestCase):
    """_notify_failure: webhook alert on a real task failure. Airflow's
    on_failure_callback only fires once retries are exhausted, so every
    call here is already "page-worthy" by construction -- no severity
    routing needed at this layer."""

    def test_no_webhook_url_is_a_silent_no_op(self):
        from core.orchestration.airflow_dag import _notify_failure

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTORESEARCH_ALERT_WEBHOOK_URL", None)
            with patch("urllib.request.urlopen") as mock_urlopen:
                _notify_failure({"task_instance": None, "dag": None})
                mock_urlopen.assert_not_called()

    def test_webhook_url_set_posts_a_message(self):
        from core.orchestration.airflow_dag import _notify_failure

        ti = MagicMock(task_id="dbt_build")
        dag = MagicMock(dag_id="autoresearch_medallion_pipeline")
        with patch.dict(os.environ, {"AUTORESEARCH_ALERT_WEBHOOK_URL": "https://hooks.example/x"}):
            with patch("urllib.request.urlopen") as mock_urlopen:
                _notify_failure({"task_instance": ti, "dag": dag})
                mock_urlopen.assert_called_once()
                request = mock_urlopen.call_args[0][0]
                self.assertEqual(request.full_url, "https://hooks.example/x")
                self.assertIn(b"dbt_build", request.data)

    def test_webhook_failure_never_raises(self):
        from core.orchestration.airflow_dag import _notify_failure

        with patch.dict(os.environ, {"AUTORESEARCH_ALERT_WEBHOOK_URL": "https://hooks.example/x"}):
            with patch("urllib.request.urlopen", side_effect=RuntimeError("network down")):
                _notify_failure({"task_instance": None, "dag": None})  # must not raise


class OrchestratorChoiceTests(unittest.TestCase):
    def test_recommends_by_situation(self):
        from core.orchestration.pipeline_stages import recommend_orchestrator
        self.assertEqual(
            recommend_orchestrator()["recommended"], "pipeline-run")
        self.assertEqual(
            recommend_orchestrator(scheduled=True, existing_airflow=True)["recommended"],
            "airflow")
        self.assertEqual(
            recommend_orchestrator(scheduled=True, want_backfills=True)["recommended"],
            "dagster")
        self.assertEqual(
            recommend_orchestrator(scheduled=True)["recommended"], "dagster")


if __name__ == "__main__":
    unittest.main()

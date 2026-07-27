"""Regression: Phase D wiring -- event-driven triggers and the CI producer.

Two loops were left open:

* `Stage.produces` has always been an artifact-glob list described in the code
  as "advisory". Airflow 3 Assets are what make it load-bearing: a task that
  declares its outputs lets a downstream DAG run when DATA ARRIVES instead of on
  a cron guess. `stage_assets()` must degrade to [] on Airflow 2 rather than
  raise, so the same topology still builds a schedule-driven DAG.

* `run-dbt-backfill --defer` reads a production manifest from `$DBT_STATE_PATH`.
  We built that consumer with no producer, so `--defer` refused on a missing
  `manifest.json` -- correctly, but permanently. The `publish-manifest` job in
  `.github/workflows/dbt-ci.yml` is the producer, and this test exists so
  deleting it is a visible failure rather than a silent regression back to
  "defer never works".
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core.orchestration.airflow_dag import stage_assets
from core.orchestration.pipeline_stages import STAGES

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "dbt-ci.yml"
_AIRFLOW_DIR = _REPO / "docker" / "airflow"


class StageAssetTests(unittest.TestCase):
    def test_every_stage_that_produces_artifacts_declares_them(self):
        # If this ever hits zero, `produces` has been emptied and event-driven
        # scheduling silently degrades to cron-only.
        producing = [s for s in STAGES if getattr(s, "produces", ())]
        self.assertGreater(len(producing), 0)

    def test_stage_assets_degrades_to_empty_without_airflow_3(self):
        # Must not raise: the topology has to stay usable on Airflow 2 and with
        # no Airflow installed at all.
        result = stage_assets(STAGES[0], "workspaces/demo")
        self.assertIsInstance(result, list)

    def test_a_stage_with_no_outputs_yields_no_assets(self):
        class Bare:
            produces = ()

        self.assertEqual(stage_assets(Bare(), "workspaces/demo"), [])


class AirflowContainerTests(unittest.TestCase):
    """Airflow cannot run natively on Windows (POSIX pwd/fork), so the
    containerised setup IS the install and must stay coherent."""

    def test_the_compose_and_dockerfile_exist(self):
        self.assertTrue((_AIRFLOW_DIR / "docker-compose.yaml").exists())
        self.assertTrue((_AIRFLOW_DIR / "docker-compose.override.yaml").exists())
        self.assertTrue((_AIRFLOW_DIR / "Dockerfile").exists())

    def test_the_image_is_pinned_to_airflow_3(self):
        # Assets / AssetOrTimeSchedule are Airflow 3 features; pinning to 2.x
        # would make stage_assets() silently a no-op forever.
        text = (_AIRFLOW_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("apache/airflow:3.", text)

    def test_the_dag_stub_calls_the_factory(self):
        # build_dag() returns a DAG object rather than writing a file, so a stub
        # in dags/ is required for Airflow to discover anything at all.
        stub = (_AIRFLOW_DIR / "dags" / "autoresearch_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("build_dag", stub)
        self.assertIn("AUTORESEARCH_PIPELINE_WORKSPACE", stub)

    def test_the_container_never_grants_remote_execution(self):
        # Baking AUTORESEARCH_ALLOW_REMOTE_EXECUTION into an image would make it
        # permanent and defeat the gate. It may be MENTIONED in a comment.
        text = (_AIRFLOW_DIR / "docker-compose.override.yaml").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", stripped)


class CiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

    def test_the_manifest_producer_exists_and_runs_only_on_main(self):
        job = self.workflow["jobs"]["publish-manifest"]
        self.assertIn("refs/heads/main", job["if"])
        body = yaml.dump(job)
        self.assertIn("manifest.json", body)
        self.assertIn("DBT_STATE_PATH", body)

    def test_slim_ci_defers_against_the_published_state(self):
        body = yaml.dump(self.workflow["jobs"]["slim-ci"])
        self.assertIn("state:modified+", body)
        self.assertIn("--defer", body)

    def test_the_gate_runs_before_anything_touches_the_warehouse(self):
        for job in ("slim-ci", "publish-manifest"):
            with self.subTest(job=job):
                self.assertIn("gate", self.workflow["jobs"][job]["needs"])

    def test_blast_radius_needs_no_warehouse(self):
        # dbt-index is offline by design; requiring credentials here would make
        # the most useful review signal the easiest one to switch off.
        body = yaml.dump(self.workflow["jobs"]["blast-radius"])
        self.assertIn("dbt-index", body)
        self.assertNotIn("DATABRICKS_TOKEN", body)

    def test_ci_never_schedules_a_pipeline(self):
        # CI owns change; Airflow owns time. A `schedule:` trigger here would
        # quietly make GitHub Actions the orchestrator.
        self.assertNotIn("schedule", self.workflow.get(True, self.workflow.get("on", {})))


if __name__ == "__main__":
    unittest.main()

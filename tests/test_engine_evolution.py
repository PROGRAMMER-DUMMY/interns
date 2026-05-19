from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.optimization.engine_evolution import EngineEvolutionMemory, EngineEvolutionRecord, main


class EngineEvolutionTests(unittest.TestCase):
    def _workspace(self, repo: Path) -> Path:
        workspace = repo / "workspaces" / "demo"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets").mkdir(parents=True)
        return workspace

    def test_records_engine_lessons_and_writes_evolution_markdown(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            memory = EngineEvolutionMemory(repo, "workspaces/demo")

            artifacts = memory.record(
                EngineEvolutionRecord(
                    stage="gold_kpi",
                    engine="polars",
                    workload_signature="csv_small_groupby",
                    resource_mode="local_streaming",
                    input_rows=100,
                    output_rows=10,
                    input_bytes=1000,
                    elapsed_seconds=0.5,
                    status="success",
                    validation_status="ok",
                    workload_shape={
                        "operation_type": "aggregation",
                        "file_formats": ["csv"],
                        "input_rows": 100,
                        "group_by_count": 1,
                    },
                    decision_analysis={
                        "chosen_reason": "Polars streaming was selected for local CSV aggregation.",
                        "risk": "low",
                    },
                    bottlenecks=[{"type": "csv_scan", "severity": "medium"}],
                    alternatives=[{"engine": "sql", "rejected_reason": "slower prior scan"}],
                    promotion={"promoted": True, "criteria": "validated and fastest"},
                    next_experiment={"goal": "compare_against_duckdb_groupby"},
                )
            )

            self.assertTrue((repo / artifacts["engine_evolution"]).exists())
            self.assertTrue((workspace / "interns/generated/memory/evolution.md").exists())
            recommendation = memory.recommendation_for(
                stage="gold_kpi",
                resource_mode="local_streaming",
                target_engine="hybrid",
            )
            self.assertEqual(recommendation["engine"], "polars")
            self.assertEqual(recommendation["evidence_count"], 1)
            lesson = recommendation["lesson"]
            self.assertEqual(lesson["workload_family"], "aggregation_small_csv_groupby1")
            self.assertEqual(lesson["promotion_state"], "promoted")
            self.assertEqual(lesson["common_bottlenecks"][0]["value"], "csv_scan")

    def test_cli_records_engine_evolution(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)

            rc = main(
                [
                    "--repo-root",
                    str(repo),
                    "--workspace",
                    "workspaces/demo",
                    "--stage",
                    "gold_kpi",
                    "--engine",
                    "sql",
                    "--workload-signature",
                    "small_join",
                    "--resource-mode",
                    "local_standard",
                    "--elapsed-seconds",
                    "0.2",
                    "--workload-shape-json",
                    '{"operation_type":"join","input_rows":1000,"join_count":1}',
                    "--decision-analysis-json",
                    '{"chosen_reason":"SQL join was simplest"}',
                    "--alternatives-json",
                    '[{"engine":"polars","rejected_reason":"not tested"}]',
                    "--json",
                ]
            )

            self.assertEqual(rc, 0)
            payload = json.loads((workspace / "interns/generated/memory/engine_evolution.json").read_text())
            self.assertEqual(payload["lessons"][0]["engine"], "sql")
            self.assertEqual(payload["records"][0]["workload_shape"]["operation_type"], "join")

    def test_engine_evolution_preserves_existing_evolution_sections(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            evolution = workspace / "interns/generated/memory/evolution.md"
            evolution.parent.mkdir(parents=True)
            evolution.write_text("# Existing Evolution\n\nKeep this deployment note.\n", encoding="utf-8")
            memory = EngineEvolutionMemory(repo, "workspaces/demo")

            memory.record(
                EngineEvolutionRecord(
                    stage="gold_kpi",
                    engine="sql",
                    workload_signature="small_join",
                    resource_mode="local_standard",
                    elapsed_seconds=0.1,
                    status="success",
                    validation_status="ok",
                )
            )
            memory.record(
                EngineEvolutionRecord(
                    stage="gold_kpi",
                    engine="polars",
                    workload_signature="small_groupby",
                    resource_mode="local_standard",
                    elapsed_seconds=0.2,
                    status="success",
                    validation_status="ok",
                )
            )

            text = evolution.read_text(encoding="utf-8")
            self.assertIn("Keep this deployment note.", text)
            self.assertEqual(text.count("<!-- engine-evolution:start -->"), 1)
            self.assertIn("Engine Evolution", text)


if __name__ == "__main__":
    unittest.main()

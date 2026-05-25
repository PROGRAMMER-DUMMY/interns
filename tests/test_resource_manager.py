from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.resource.cli import main
from core.resource.manager import HardwareProfile, ResourceBudget, ResourceManager


class ResourceManagerTests(unittest.TestCase):
    def _hardware(self, *, free_disk: int = 10_000_000_000, available_memory: int | None = 4_000_000_000) -> HardwareProfile:
        return HardwareProfile(
            cpu_count=8,
            total_memory_bytes=8_000_000_000,
            available_memory_bytes=available_memory,
            workspace_disk_free_bytes=free_disk,
            workspace_disk_total_bytes=20_000_000_000,
            temp_disk_free_bytes=free_disk,
            temp_disk_total_bytes=20_000_000_000,
            platform="test",
            temp_dir="C:/tmp",
        )

    def test_decide_recommends_workers_and_api_concurrency(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            manager = ResourceManager(workspace, hardware=self._hardware())

            decision = manager.decide(estimated_bytes=100_000_000, workload="api_ingestion")

            self.assertEqual(decision.status, "ok")
            self.assertEqual(decision.recommended_workers, 7)
            self.assertEqual(decision.recommended_api_concurrency, 8)

    def test_decide_blocks_when_disk_budget_would_exhaust_headroom(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            manager = ResourceManager(
                workspace,
                hardware=self._hardware(free_disk=1_000_000_000),
                budget=ResourceBudget(disk_safety_multiplier=3.0, min_free_disk_fraction=0.15),
            )

            decision = manager.decide(estimated_bytes=500_000_000, workload="api_ingestion")

            self.assertEqual(decision.status, "blocked")
            self.assertTrue(any("workspace_disk_budget_exceeded" in item for item in decision.blockers))

    def test_profiling_settings_reduce_rows_and_disable_expensive_checks_under_pressure(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            manager = ResourceManager(
                workspace,
                hardware=self._hardware(available_memory=50_000_000),
                budget=ResourceBudget(max_memory_fraction=0.5),
            )

            settings = manager.profiling_settings(
                requested_sample_rows=100_000,
                requested_exact=True,
                estimated_bytes=100_000_000,
            )

            self.assertEqual(settings.mode, "local_streaming")
            self.assertEqual(settings.sample_rows, 25_000)
            self.assertFalse(settings.exact_profile)
            self.assertFalse(settings.expensive_checks)

    def test_transformation_settings_block_local_when_resource_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            manager = ResourceManager(
                workspace,
                hardware=self._hardware(free_disk=1_000_000_000),
                budget=ResourceBudget(disk_safety_multiplier=3.0, min_free_disk_fraction=0.15),
            )

            settings = manager.transformation_settings(target_engine="sql", estimated_bytes=500_000_000)

            self.assertEqual(settings.mode, "local_blocked_remote_recommended")
            self.assertFalse(settings.local_execution_allowed)
            self.assertEqual(settings.target_engine_recommendation, "databricks")

    def test_cli_writes_resource_preflight_artifacts(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = repo / "workspaces" / "demo"
            workspace.mkdir(parents=True)

            rc = main(["--repo-root", str(repo), "--workspace", "workspaces/demo", "--json"])

            self.assertEqual(rc, 0)
            payload = json.loads((workspace / "interns/generated/evidence/resource_preflight.json").read_text())
            self.assertEqual(payload["artifact_type"], "resource_preflight.json")
            self.assertTrue((workspace / "interns/reports/resource_preflight.md").exists())


if __name__ == "__main__":
    unittest.main()

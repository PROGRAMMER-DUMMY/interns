"""Dependency-aware parallel KPI completion planning.

Proves the plan keeps KPIs that share a blocker on one worker (so a shared
decision is made once) while fanning genuinely-independent KPIs across workers,
with the worker count scaling off the number of independent components.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.parallel_completion import (
    build_completion_graph,
    decide_worker_count,
    plan_parallel_completion,
)


def _ready(feature: str, dataset: str, column: str) -> dict:
    return {
        "feature": feature,
        "state": "proven_direct",
        "source_columns": [{"dataset": dataset, "column": column}],
    }


def _blocked(feature: str) -> dict:
    return {"feature": feature, "state": "blocked_missing_evidence", "source_columns": []}


class WorkerCountTests(unittest.TestCase):
    def test_ladder(self) -> None:
        self.assertEqual(decide_worker_count(3), 1)
        self.assertEqual(decide_worker_count(4), 2)
        self.assertEqual(decide_worker_count(6), 2)
        self.assertEqual(decide_worker_count(7), 4)
        self.assertEqual(decide_worker_count(12), 4)
        self.assertEqual(decide_worker_count(13), 6)
        self.assertEqual(decide_worker_count(50), 6)

    def test_cap_is_respected(self) -> None:
        self.assertEqual(decide_worker_count(50, max_workers=2), 2)


class DependencyGraphTests(unittest.TestCase):
    def test_kpis_sharing_an_unresolved_feature_are_one_component(self) -> None:
        mapping = {
            "kpis": [
                {"kpi_id": "kpi_001", "features": [_blocked("encounter date")]},
                {"kpi_id": "kpi_002", "features": [_blocked("encounter date")]},
                {"kpi_id": "kpi_003", "features": [_ready("region", "orders.csv", "region")]},
            ]
        }
        graph = build_completion_graph(mapping)
        # 001+002 share the blocker -> one component; 003 independent.
        comps = [sorted(c) for c in graph["components"]]
        self.assertIn(["kpi_001", "kpi_002"], comps)
        self.assertIn(["kpi_003"], comps)
        self.assertEqual(len(graph["components"]), 2)
        self.assertIn("encounterdate", graph["shared_blockers"])

    def test_independent_kpis_are_singleton_components(self) -> None:
        mapping = {
            "kpis": [
                {"kpi_id": "kpi_001", "features": [_blocked("alpha")]},
                {"kpi_id": "kpi_002", "features": [_blocked("beta")]},
            ]
        }
        graph = build_completion_graph(mapping)
        self.assertEqual(len(graph["components"]), 2)
        self.assertEqual(graph["shared_blockers"], {})

    def test_shared_join_links_kpis(self) -> None:
        join = {"key": "patient_id", "datasets": ["encounters.csv", "patients.csv"]}
        mapping = {
            "kpis": [
                {"kpi_id": "kpi_001", "features": [], "join_candidates": [join]},
                {"kpi_id": "kpi_002", "features": [], "join_candidates": [join]},
            ]
        }
        graph = build_completion_graph(mapping)
        self.assertEqual(len(graph["components"]), 1)


class PlanArtifactTests(unittest.TestCase):
    def _write_mapping(self, root: Path, kpis: list[dict]) -> Path:
        ws = root / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "kpi_feature_mapping.json").write_text(
            json.dumps({"kpis": kpis, "blocker_clusters": []}), encoding="utf-8"
        )
        return ws

    def test_shared_blocker_stays_on_one_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 8 independent KPIs + a shared-blocker pair -> >6 components -> fan out.
            kpis = [
                {"kpi_id": f"kpi_{i:03d}", "features": [_blocked(f"f{i}")]}
                for i in range(1, 9)
            ]
            kpis += [
                {"kpi_id": "kpi_009", "features": [_blocked("shared")]},
                {"kpi_id": "kpi_010", "features": [_blocked("shared")]},
            ]
            self._write_mapping(root, kpis)
            result = plan_parallel_completion(root, "workspaces/demo")
            self.assertEqual(result.kpi_count, 10)
            self.assertEqual(result.component_count, 9)  # 8 singletons + 1 pair
            self.assertGreaterEqual(result.worker_count, 2)
            self.assertEqual(result.shared_blocker_count, 1)

            plan = json.loads((root / result.plan_path).read_text(encoding="utf-8"))
            # kpi_009 and kpi_010 must land on the SAME worker (shared blocker).
            for worker in plan["execution"]["phase_2_parallel"]["worker_assignments"]:
                ids = set(worker["kpi_ids"])
                if "kpi_009" in ids or "kpi_010" in ids:
                    self.assertIn("kpi_009", ids)
                    self.assertIn("kpi_010", ids)
            # Every KPI is assigned exactly once across workers.
            assigned = [
                kid
                for w in plan["execution"]["phase_2_parallel"]["worker_assignments"]
                for kid in w["kpi_ids"]
            ]
            self.assertEqual(sorted(assigned), [f"kpi_{i:03d}" for i in range(1, 11)])

    def test_small_workload_stays_serial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kpis = [{"kpi_id": "kpi_001", "features": [_blocked("a")]}]
            self._write_mapping(root, kpis)
            result = plan_parallel_completion(root, "workspaces/demo")
            self.assertEqual(result.worker_count, 1)


if __name__ == "__main__":
    unittest.main()

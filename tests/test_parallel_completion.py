"""Dependency-aware parallel KPI completion planning.

Proves the plan keeps KPIs that share a blocker on one worker (so a shared
decision is made once) while fanning genuinely-independent KPIs across workers,
with the worker count scaling off the number of independent components.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.parallel_completion import (
    DEFAULT_PARALLEL_KPI_THRESHOLD,
    PARALLEL_KPI_THRESHOLD_ENV,
    build_completion_graph,
    count_ready_kpis,
    decide_worker_count,
    dispatch_parallel_completion,
    plan_parallel_completion,
    resolve_parallel_threshold,
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


class ReadyKpiCountTests(unittest.TestCase):
    def test_counts_only_fully_resolved_kpis(self) -> None:
        mapping = {
            "kpis": [
                {"kpi_id": "kpi_001", "features": [_ready("region", "orders.csv", "region")]},
                {"kpi_id": "kpi_002", "features": [_blocked("amount")]},
                {
                    "kpi_id": "kpi_003",
                    "features": [
                        _ready("region", "orders.csv", "region"),
                        _blocked("amount"),
                    ],
                },
            ]
        }
        # Only kpi_001 has no unresolved feature/join.
        self.assertEqual(count_ready_kpis(mapping), 1)

    def test_ready_kpi_with_unproven_join_is_not_ready(self) -> None:
        join = {"key": "order_id", "datasets": ["orders.csv", "shipments.csv"]}
        mapping = {
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "features": [_ready("region", "orders.csv", "region")],
                    "join_candidates": [join],
                }
            ]
        }
        self.assertEqual(count_ready_kpis(mapping), 0)


class ThresholdResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop(PARALLEL_KPI_THRESHOLD_ENV, None)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(PARALLEL_KPI_THRESHOLD_ENV, None)
        else:
            os.environ[PARALLEL_KPI_THRESHOLD_ENV] = self._saved

    def test_default_when_unset(self) -> None:
        self.assertEqual(resolve_parallel_threshold(), DEFAULT_PARALLEL_KPI_THRESHOLD)

    def test_explicit_arg_wins_over_env(self) -> None:
        os.environ[PARALLEL_KPI_THRESHOLD_ENV] = "10"
        self.assertEqual(resolve_parallel_threshold(2), 2)

    def test_env_override(self) -> None:
        os.environ[PARALLEL_KPI_THRESHOLD_ENV] = "7"
        self.assertEqual(resolve_parallel_threshold(), 7)

    def test_malformed_env_falls_back_to_default(self) -> None:
        os.environ[PARALLEL_KPI_THRESHOLD_ENV] = "not-a-number"
        self.assertEqual(resolve_parallel_threshold(), DEFAULT_PARALLEL_KPI_THRESHOLD)


class DispatchDecisionTests(unittest.TestCase):
    def _write_mapping(self, root: Path, kpis: list[dict]) -> Path:
        ws = root / "workspaces" / "demo"
        contracts = ws / "interns" / "generated" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "kpi_feature_mapping.json").write_text(
            json.dumps({"kpis": kpis, "blocker_clusters": []}), encoding="utf-8"
        )
        return ws

    def test_below_threshold_stays_sequential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 2 ready, independent KPIs; threshold 3 -> sequential.
            kpis = [
                {"kpi_id": "kpi_001", "features": [_ready("a", "orders.csv", "a")]},
                {"kpi_id": "kpi_002", "features": [_ready("b", "orders.csv", "b")]},
            ]
            self._write_mapping(root, kpis)
            decision = dispatch_parallel_completion(
                root, "workspaces/demo", threshold=3
            )
            self.assertEqual(decision.mode, "sequential")
            self.assertFalse(decision.fan_out)
            self.assertEqual(decision.ready_kpi_count, 2)
            # Plan artifact still written for auditability.
            self.assertTrue((root / decision.plan.plan_path).exists())

    def test_above_threshold_fans_out_via_parallel_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 8 ready, independent KPIs; threshold 3 -> parallel fan-out.
            kpis = [
                {
                    "kpi_id": f"kpi_{i:03d}",
                    "features": [_ready(f"f{i}", "orders.csv", f"c{i}")],
                }
                for i in range(1, 9)
            ]
            self._write_mapping(root, kpis)
            decision = dispatch_parallel_completion(
                root, "workspaces/demo", threshold=3
            )
            self.assertEqual(decision.mode, "parallel")
            self.assertTrue(decision.fan_out)
            self.assertEqual(decision.ready_kpi_count, 8)
            self.assertGreater(decision.plan.worker_count, 1)
            # The parallel_kpi_completion delegation route is carried for the caller.
            self.assertIn("workspace-flow-orchestrator", decision.route["agents"])
            self.assertIn("kpi-analyst", decision.route["agents"])

    def test_single_component_stays_sequential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 5 KPIs all sharing one join -> 1 connected component -> 1 worker ->
            # no independent work to spread, so sequential regardless of count.
            join = {"key": "order_id", "datasets": ["orders.csv", "shipments.csv"]}
            kpis = [
                {
                    "kpi_id": f"kpi_{i:03d}",
                    "features": [_ready(f"f{i}", "orders.csv", f"c{i}")],
                    "join_candidates": [join],
                }
                for i in range(1, 6)
            ]
            self._write_mapping(root, kpis)
            decision = dispatch_parallel_completion(
                root, "workspaces/demo", threshold=3
            )
            self.assertEqual(decision.plan.component_count, 1)
            self.assertEqual(decision.mode, "sequential")


if __name__ == "__main__":
    unittest.main()

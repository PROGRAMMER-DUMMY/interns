"""
Rank next optimization strategies from hotspots, semantic contracts, and memory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.optimization.memory import OptimizationMemory
from core.governance.semantic_contract import SemanticContract


@dataclass(frozen=True)
class OptimizationPlan:
    recommended_strategy: str
    rationale: str
    candidate_strategies: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_prompt_context(self) -> str:
        return json.dumps(
            {
                "recommended_strategy": self.recommended_strategy,
                "rationale": self.rationale,
                "candidate_strategies": self.candidate_strategies,
                "risk_notes": self.risk_notes,
                "evidence": self.evidence,
            },
            indent=2,
        )


class OptimizationPlanner:
    def __init__(self, memory: OptimizationMemory, root: Path):
        self.memory = memory
        self.root = root

    def build_plan(self, task: dict, contract: SemanticContract) -> OptimizationPlan:
        hotspots = self._load_hotspots(task)
        memory_stats = self.memory.pattern_stats()
        strategies = self._strategies_from_hotspots(hotspots)

        if not strategies:
            strategies = self._strategies_from_memory(memory_stats)

        if not strategies:
            strategies = ["predicate_pushdown", "cte_rewrite", "aggregation_rewrite"]

        risk_notes = []
        if contract.rules:
            risk_notes.append(
                f"Correctness is gated by {len(contract.rules)} semantic contract rules."
            )
        if any(s in {"join_rewrite", "case_simplification"} for s in strategies):
            risk_notes.append("Join and null-handling changes require strict output comparison.")

        recommended = strategies[0]
        return OptimizationPlan(
            recommended_strategy=recommended,
            rationale=self._rationale(recommended, hotspots, memory_stats),
            candidate_strategies=strategies[:5],
            risk_notes=risk_notes,
            evidence={
                "hotspots": hotspots,
                "memory_stats": memory_stats[:10],
                "semantic_contract": contract.summary(),
            },
        )

    def _load_hotspots(self, task: dict) -> dict[str, Any]:
        path_value = task.get("hotspots_file") or "workspaces/sql_optimization/hotspots.json"
        path = self.root / path_value
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _strategies_from_hotspots(self, hotspots: dict[str, Any]) -> list[str]:
        strategies: list[str] = []
        for suggestion in hotspots.get("suggestions", []) or []:
            kind = suggestion.get("type", "")
            if kind == "full_table_scan":
                strategies.append("predicate_pushdown")
            elif kind == "repeated_scan":
                strategies.append("cte_rewrite")
            elif kind == "heavy_aggregation":
                strategies.append("aggregation_rewrite")
            elif kind == "cartesian_product":
                strategies.append("join_rewrite")
            elif kind == "operator_dominates":
                strategies.append("operator_focused_rewrite")
        return _unique(strategies)

    def _strategies_from_memory(self, memory_stats: list[dict[str, Any]]) -> list[str]:
        ranked = sorted(
            memory_stats,
            key=lambda row: (row.get("success_rate", 0.0), row.get("avg_metric_delta", 0.0)),
            reverse=True,
        )
        return [row["change_type"] for row in ranked if row.get("attempts", 0) > 0]

    def _rationale(
        self,
        strategy: str,
        hotspots: dict[str, Any],
        memory_stats: list[dict[str, Any]],
    ) -> str:
        if hotspots.get("suggestions"):
            first = hotspots["suggestions"][0]
            return f"Hotspot evidence suggests {strategy}: {first.get('message', '')}"
        for row in memory_stats:
            if row.get("change_type") == strategy:
                return (
                    f"Memory favors {strategy}: success_rate={row.get('success_rate')}, "
                    f"avg_metric_delta={row.get('avg_metric_delta')}"
                )
        return f"Try {strategy} as the safest general data-engineering optimization pattern."


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys([value for value in values if value]))

"""
Structured learning memory for optimization experiments.

This layer records why a change was tried, what happened, and whether that
pattern should be trusted more or less next time.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from core.workspace import Workspace


@dataclass
class OptimizationMemoryRecord:
    run_id: str
    task_id: str
    artifact: str
    change_type: str
    change_types: list[str]
    expected_reason: str
    actual_result: str
    decision: str
    baseline_metric: Optional[float]
    candidate_metric: Optional[float]
    metric_delta: Optional[float]
    direction: str
    correctness_passed: bool
    guardrails_failed: list[str] = field(default_factory=list)
    execution_time_seconds: Optional[float] = None
    matching_score: Optional[float] = None
    confidence: str = "low"
    evidence: dict[str, Any] = field(default_factory=dict)


class OptimizationMemory:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def record(self, record: OptimizationMemoryRecord) -> None:
        self.workspace.log_optimization_memory(asdict(record))

    def pattern_stats(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.workspace.get_optimization_pattern_stats(limit=limit)

    def recent_lessons(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.workspace.get_recent_optimization_memory(limit=limit)

    def as_prompt_context(self, limit: int = 8) -> str:
        lessons = self.recent_lessons(limit=limit)
        if not lessons:
            return "No optimization memory has been recorded yet."
        return json.dumps(lessons, indent=2)


def describe_actual_result(
    baseline_metric: Optional[float],
    candidate_metric: Optional[float],
    direction: str,
    status: str,
) -> tuple[Optional[float], str]:
    if baseline_metric is None or candidate_metric is None:
        return None, f"status={status}; insufficient metric history for delta."

    delta = candidate_metric - baseline_metric
    improved = delta > 0 if direction == "higher" else delta < 0
    outcome = "improved" if improved else "degraded_or_flat"
    return delta, f"{outcome}: candidate={candidate_metric}, baseline={baseline_metric}, delta={delta:.4f}"

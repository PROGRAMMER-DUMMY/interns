"""
core/orchestration/governor.py — Medallion pipeline error routing.

Deterministic error->specialist routing with a per-stage retry cap (circuit
breaker), used by core/medallion/build.py to route Bronze/Silver/Gold stage
failures.

This module previously also carried a KPI/SQL-error routing variant
(`decide_routing`) and a specialist-invocation helper (`run_specialist`) --
both confirmed to have zero callers anywhere in the platform (the medallion
build path only ever calls `decide_medallion_routing`) and removed rather than
left half-wired. If KPI-level error routing is needed later, build it as a
real, wired feature rather than resurrecting dead code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from core.config import Config

@dataclass
class RoutingDecision:
    target_agent: str
    reason: str
    retry_count: int = 0
    is_terminal: bool = False


MEDALLION_ROUTING: Dict[str, tuple[str, int]] = {
    "BRONZE_LOAD_FAIL":         ("data_engineer",       2),
    "SILVER_TRANSFORM_FAIL":    ("sql_specialist",      2),
    "SILVER_ASSERTION_FAILED":  ("medallion_architect", 2),
    "GOLD_DERIVATION_FAIL":     ("medallion_architect", 2),
    "KPI_ROW_EQUALITY_FAIL":    ("medallion_architect", 1),
    "SQL_LINT_FAIL":            ("sql_specialist",      2),
}


class Governor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._retry_map: Dict[str, int] = {}

    def decide_medallion_routing(self, stage_code: str, error_message: str) -> RoutingDecision:
        """Route a medallion pipeline error to the correct specialist with retry cap."""
        specialist, cap = MEDALLION_ROUTING.get(stage_code, ("medallion_architect", 1))
        retries = self._retry_map.get(stage_code, 0)
        if retries >= cap:
            return RoutingDecision(
                target_agent="human",
                reason=f"{stage_code} exceeded retry cap ({cap})",
                retry_count=retries,
                is_terminal=True,
            )
        self._retry_map[stage_code] = retries + 1
        return RoutingDecision(
            target_agent=specialist,
            reason=f"{stage_code}: routing to {specialist}",
            retry_count=retries + 1,
        )

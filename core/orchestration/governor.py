"""
core/orchestration/governor.py — Multi-Agent Governance & Routing.

Orchestrates the lifecycle of specialist agents, performs deterministic error routing,
and enforces retry limits (circuit breaker).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from core.agents.registry import InternRegistry
from core.config import Config

logger = logging.getLogger(__name__)

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
    def __init__(self, cfg: Config, registry: InternRegistry):
        self.cfg = cfg
        self.registry = registry
        self._retry_map: Dict[str, int] = {}
        self.MAX_RETRIES = 3

    def decide_routing(self, kpi_id: str, error_message: str) -> RoutingDecision:
        retries = self._retry_map.get(kpi_id, 0)
        
        if retries >= self.MAX_RETRIES:
            return RoutingDecision(
                target_agent="human",
                reason=f"Exceeded max retries ({self.MAX_RETRIES}) for KPI {kpi_id}.",
                retry_count=retries,
                is_terminal=True
            )

        # Deterministic Routing Logic
        error_lower = error_message.lower()
        
        if "binder error" in error_lower or "syntax error" in error_lower or "column not found" in error_lower:
            target = "sql_specialist"
            reason = "SQL syntax or column reference error detected."
        elif "relationship" in error_lower or "join" in error_lower or "mapping" in error_lower:
            target = "data_engineer"
            reason = "Schema mapping or relationship discovery issue detected."
        else:
            target = "sql_specialist" # Default fallback
            reason = f"Unknown error type: {error_message[:100]}..."

        self._retry_map[kpi_id] = retries + 1
        return RoutingDecision(
            target_agent=target,
            reason=reason,
            retry_count=retries + 1
        )

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

    def run_specialist(self, agent_name: str, request: str, context: Dict[str, Any]) -> str:
        agent = self.registry.get_intern(agent_name)
        logger.info(f"Invoking agent '{agent_name}' with request: {request[:100]}...")
        return agent.run(request, context)

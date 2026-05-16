"""
core/medallion/tier_router.py — Task-class to model-tier routing (P4).

Tiers are derived from the discovered + ranked model set, not from name patterns.
Task classes declare a minimum tier; the router picks the cheapest tier >= minimum.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.medallion.model_classifier import ModelCapability

TIERS = ("heavy", "medium", "light")
_TIER_ORDER = {"light": 0, "medium": 1, "heavy": 2}

TASK_MINIMUMS: dict[str, str] = {
    "bronze_append_sql":      "light",
    "silver_transform_sql":   "light",
    "silver_derived_column":  "light",
    "lineage_explanation_md": "light",
    "silver_contract_rules":  "medium",
    "kpi_sql_regeneration":   "medium",
    "star_schema_design":     "medium",
}


class InsufficientModelCapability(RuntimeError):
    pass


@dataclass
class TierAssignment:
    by_tier: dict[str, list[str]] = field(default_factory=dict)   # tier -> [model_id, ...]
    ranking: list[str] = field(default_factory=list)               # best to least capable


def rank_models(capabilities: list["ModelCapability"]) -> list[str]:
    def score(c: "ModelCapability") -> tuple:
        return (
            c.benchmark_composite or 0.0,
            c.parameter_count or 0.0,
            c.context_window or 0,
            1 if c.vision_capable else 0,
        )
    return [c.model_id for c in sorted(capabilities, key=score, reverse=True)]


def assign_tiers(ranking: list[str]) -> TierAssignment:
    n = len(ranking)
    if n == 0:
        return TierAssignment()
    if n == 1:
        return TierAssignment(
            by_tier={"heavy": ranking, "medium": ranking, "light": ranking},
            ranking=ranking,
        )
    if n == 2:
        return TierAssignment(
            by_tier={"heavy": [ranking[0]], "medium": [ranking[0]], "light": [ranking[1]]},
            ranking=ranking,
        )
    if n == 3:
        return TierAssignment(
            by_tier={"heavy": [ranking[0]], "medium": [ranking[1]], "light": [ranking[2]]},
            ranking=ranking,
        )
    third = max(n // 3, 1)
    return TierAssignment(
        by_tier={
            "heavy":  ranking[:third],
            "medium": ranking[third:2*third],
            "light":  ranking[2*third:],
        },
        ranking=ranking,
    )


def pick_model(task_class: str, assignment: TierAssignment) -> tuple[str, str]:
    """Return (tier, model_id). Cheapest tier >= minimum for task_class."""
    minimum = TASK_MINIMUMS.get(task_class, "light")
    for tier in ("light", "medium", "heavy"):
        if _tier_at_or_above(tier, minimum) and assignment.by_tier.get(tier):
            return (tier, assignment.by_tier[tier][0])
    raise InsufficientModelCapability(
        f"No discovered tier meets minimum `{minimum}` for task_class `{task_class}`"
    )


def should_escalate(
    consecutive_failures: int,
    current_tier: str,
    requires_judgment: bool,
    cache_miss: bool,
) -> bool:
    if consecutive_failures >= 2:
        return True
    if requires_judgment and cache_miss:
        return True
    return False


def should_deescalate(
    budget_burn_rate: float,
    progress_fraction: float,
    cache_near_hit: bool,
    cheap_mode: bool,
) -> bool:
    if cheap_mode:
        return True
    if cache_near_hit:
        return True
    if budget_burn_rate > 0.6 and progress_fraction < 0.6:
        return True
    return False


def _tier_at_or_above(tier: str, minimum: str) -> bool:
    return _TIER_ORDER.get(tier, 0) >= _TIER_ORDER.get(minimum, 0)

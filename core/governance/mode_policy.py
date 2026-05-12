"""
Mode planning for scoreable artifact optimization.

Production mode is intentionally bounded. Global exploration can discover ideas,
but it cannot promote changes unless policy explicitly allows it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.governance.contracts import EXPLORATION_MODES, OptimizationPolicy


@dataclass(frozen=True)
class ModePlan:
    active_mode: str
    candidate_modes: list[str] = field(default_factory=list)
    promotion_allowed: bool = True
    rationale: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_mode": self.active_mode,
            "candidate_modes": self.candidate_modes,
            "promotion_allowed": self.promotion_allowed,
            "rationale": self.rationale,
            "warnings": self.warnings,
        }


class ModePlanner:
    def __init__(self, policy: OptimizationPolicy):
        self.policy = policy

    def build_plan(self, requested_mode: str | None = None) -> ModePlan:
        execution = self.policy.execution
        requested = requested_mode or execution.default_mode
        candidate_modes = list(dict.fromkeys([*execution.allowed_modes, *execution.exploration_modes]))
        warnings: list[str] = []

        if requested == "auto":
            active = execution.default_mode
            rationale = f"Auto selected default bounded mode '{active}'."
        elif requested in candidate_modes:
            active = requested
            rationale = f"Requested mode '{active}' is allowed by execution policy."
        else:
            active = execution.default_mode
            rationale = f"Requested mode '{requested}' is not allowed; using '{active}'."
            warnings.append(f"unsupported_mode:{requested}")

        promotion_allowed = active not in EXPLORATION_MODES or execution.allow_global_promotion
        if active in EXPLORATION_MODES and not execution.allow_global_promotion:
            warnings.append("global_exploration_no_auto_promotion")
            rationale += " Global exploration can generate evidence only."

        return ModePlan(
            active_mode=active,
            candidate_modes=candidate_modes,
            promotion_allowed=promotion_allowed,
            rationale=rationale,
            warnings=warnings,
        )

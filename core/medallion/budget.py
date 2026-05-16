"""
core/medallion/budget.py — Per-run USD budget tracking and cap enforcement (P4).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# USD per million tokens: (input_rate, output_rate)
# Update when model pricing changes.
PRICING: dict[str, tuple[float, float]] = {
    "api:claude-opus-4-7":         (15.0, 75.0),
    "api:claude-sonnet-4-6":       (3.0,  15.0),
    "api:claude-haiku-4-5-20251001": (0.25, 1.25),
    "gemini-api:gemini-2.0-flash": (0.075, 0.30),
    "gemini-api:gemma-4":          (0.0,   0.0),   # open weights — free on own infra
    "__default__":                 (3.0,  15.0),
}


class BudgetExceeded(RuntimeError):
    pass


class BudgetTracker:
    def __init__(self, max_usd: float, *, state_path: Path):
        self.max_usd = max_usd
        self.state_path = state_path
        self.spent = 0.0
        self.history: list[dict[str, Any]] = []

    def charge(
        self,
        *,
        engine: str,
        model_id: str,
        in_tokens: int,
        out_tokens: int,
    ) -> float:
        key = f"{engine}:{model_id}"
        rate = PRICING.get(key) or PRICING.get("__default__") or (3.0, 15.0)
        usd = (in_tokens / 1_000_000) * rate[0] + (out_tokens / 1_000_000) * rate[1]
        self.spent += usd
        self.history.append({
            "engine": engine,
            "model_id": model_id,
            "usd": round(usd, 6),
            "tokens": in_tokens + out_tokens,
        })
        self._persist()
        return usd

    def remaining(self) -> float:
        return max(self.max_usd - self.spent, 0.0)

    def burn_rate(self) -> float:
        return self.spent / max(self.max_usd, 0.01)

    def should_deescalate(self, tables_done: int, tables_total: int) -> bool:
        progress = tables_done / max(tables_total, 1)
        return self.burn_rate() > 0.6 and progress < 0.6

    def exceeded(self) -> bool:
        return self.spent > self.max_usd

    def check(self, tables_done: int, tables_total: int) -> None:
        """Raise BudgetExceeded if over budget."""
        if self.exceeded():
            raise BudgetExceeded(
                f"Budget exceeded: ${self.spent:.4f} > ${self.max_usd:.4f}. "
                f"Completed {tables_done} of {tables_total} tables. "
                f"Use --force-with-blockers or increase max_usd_per_run to resume."
            )

    def _persist(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {"max_usd": self.max_usd, "spent": self.spent, "history": self.history},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

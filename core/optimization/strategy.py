from typing import Optional

class BaseDecisionStrategy:
    def decide(self, metric: Optional[float], state: dict, task: dict) -> str:
        raise NotImplementedError

class SingleMetricDecisionStrategy(BaseDecisionStrategy):
    def decide(self, metric: Optional[float], state: dict, task: dict) -> str:
        if metric is None:
            return "crash"

        best = state.get("best_metric")
        # Default to "higher" to match the loop/memory default. With no default a
        # direction-less task left direction=None, so the higher/lower branches
        # were both False and EVERY candidate after the first was discarded — the
        # optimizer never converged. Ref: core-audit optimization.md.
        direction = task.get("direction") or "higher"

        improved = (best is None or 
                    (direction == "higher" and metric > best) or 
                    (direction == "lower" and metric < best))

        if improved:
            return "keep"
        else:
            return "discard"

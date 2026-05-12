from typing import Optional

class BaseDecisionStrategy:
    def decide(self, metric: Optional[float], state: dict, task: dict) -> str:
        raise NotImplementedError

class SingleMetricDecisionStrategy(BaseDecisionStrategy):
    def decide(self, metric: Optional[float], state: dict, task: dict) -> str:
        if metric is None:
            return "crash"

        best = state.get("best_metric")
        direction = task.get("direction")
        
        improved = (best is None or 
                    (direction == "higher" and metric > best) or 
                    (direction == "lower" and metric < best))

        if improved:
            return "keep"
        else:
            return "discard"

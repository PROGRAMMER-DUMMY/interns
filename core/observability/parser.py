from abc import ABC, abstractmethod
from typing import Optional

class MetricParser(ABC):
    @abstractmethod
    def parse_metric(self, log_content: str, metric_key: str) -> Optional[float]:
        pass

    @abstractmethod
    def parse_all_metrics(self, log_content: str) -> dict[str, float]:
        pass

class RegexLogParser(MetricParser):
    def parse_metric(self, log_content: str, metric_key: str) -> Optional[float]:
        for line in log_content.splitlines():
            if line.startswith(f"{metric_key}:"):
                try:
                    return float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        return None

    def parse_all_metrics(self, log_content: str) -> dict[str, float]:
        in_block = False
        metrics: dict[str, float] = {}
        for line in log_content.splitlines():
            if line.strip() == "---":
                in_block = not in_block
                continue
            if in_block and ":" in line:
                key, _, val = line.partition(":")
                try:
                    metrics[key.strip()] = float(val.strip())
                except ValueError:
                    pass
        return metrics

class StubMetricParser(MetricParser):
    def __init__(self, metric: Optional[float] = None, metrics: Optional[dict[str, float]] = None):
        self.metric = metric
        self.metrics = metrics or {}

    def parse_metric(self, log_content: str, metric_key: str) -> Optional[float]:
        return self.metric

    def parse_all_metrics(self, log_content: str) -> dict[str, float]:
        return self.metrics

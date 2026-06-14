"""Telemetry and metric parsing package."""
from core.observability.parser import MetricParser, RegexLogParser, StubMetricParser
from core.observability.telemetry_backend import (
    DatabricksTelemetry,
    LocalTelemetry,
    TelemetryBackend,
    build_telemetry_backend,
)
from core.observability.log_redaction import (
    RedactionFilter,
    install_log_redaction,
    redact,
)

# Wire PII/PHI/secret redaction into the root logger at package import time.
# install_log_redaction() is idempotent: importing this package more than once
# never adds duplicate filters.
install_log_redaction()

__all__ = [
    "DatabricksTelemetry",
    "LocalTelemetry",
    "MetricParser",
    "RedactionFilter",
    "RegexLogParser",
    "StubMetricParser",
    "TelemetryBackend",
    "build_telemetry_backend",
    "install_log_redaction",
    "redact",
]

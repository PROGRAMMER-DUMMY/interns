"""
Enterprise policy contracts for governed optimization.

These contracts are intentionally small value objects. They make the optimizer's
operational assumptions explicit before candidate generation, evaluation, and
promotion decisions run.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


PRODUCTION_MODES = {"sql", "polars", "sql_polars_hybrid"}
EXPLORATION_MODES = {"global_exploration"}
ALL_MODES = PRODUCTION_MODES | EXPLORATION_MODES


@dataclass(frozen=True)
class DowncastPolicy:
    integer: str = "lossless_minmax"
    float: str = "explicit_approval_required"
    decimal: str = "explicit_approval_required"
    require_exact_bounds: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DowncastPolicy":
        data = data or {}
        return cls(
            integer=data.get("integer", "lossless_minmax"),
            float=data.get("float", "explicit_approval_required"),
            decimal=data.get("decimal", "explicit_approval_required"),
            require_exact_bounds=bool(data.get("require_exact_bounds", True)),
        )


@dataclass(frozen=True)
class FailurePolicy:
    correctness: str = "fail_closed"
    security: str = "fail_closed"
    semantic_contract: str = "fail_closed"
    downcast_proof: str = "fail_closed"
    telemetry: str = "fail_soft"
    optional_metadata: str = "fail_soft"
    state_persistence: str = "fail_closed"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FailurePolicy":
        data = data or {}
        defaults = asdict(cls())
        return cls(**{field: data.get(field, defaults[field]) for field in defaults})

    def fail_closed_domains(self) -> set[str]:
        return {
            name
            for name, value in asdict(self).items()
            if str(value).lower() == "fail_closed"
        }


@dataclass(frozen=True)
class SLAContract:
    max_runtime_seconds: float | None = None
    max_cost_score: float | None = None
    min_matching_score: float = 100.0
    max_metric_regression_pct: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SLAContract":
        data = data or {}
        return cls(
            max_runtime_seconds=data.get("max_runtime_seconds"),
            max_cost_score=data.get("max_cost_score"),
            min_matching_score=float(data.get("min_matching_score", 100.0)),
            max_metric_regression_pct=float(data.get("max_metric_regression_pct", 0.0)),
        )


@dataclass(frozen=True)
class ApprovalContract:
    required_for_production: bool = True
    required_change_types: tuple[str, ...] = (
        "join_rewrite",
        "aggregation_rewrite",
        "case_simplification",
        "downcast_float",
        "downcast_decimal",
        "global_exploration",
    )
    auto_approve_safe_improvements: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ApprovalContract":
        data = data or {}
        defaults = cls()
        required_change_types = data.get("required_change_types", defaults.required_change_types)
        return cls(
            required_for_production=bool(data.get("required_for_production", True)),
            required_change_types=tuple(required_change_types),
            auto_approve_safe_improvements=bool(data.get("auto_approve_safe_improvements", False)),
        )


@dataclass(frozen=True)
class ExecutionContract:
    production_mode: str = "bounded"
    allowed_modes: tuple[str, ...] = ("sql", "polars", "sql_polars_hybrid")
    exploration_modes: tuple[str, ...] = ("global_exploration",)
    default_mode: str = "sql"
    allow_global_promotion: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExecutionContract":
        data = data or {}
        allowed = tuple(data.get("allowed_modes", cls.allowed_modes))
        exploration = tuple(data.get("exploration_modes", cls.exploration_modes))
        invalid = [mode for mode in (*allowed, *exploration) if mode not in ALL_MODES]
        if invalid:
            raise ValueError(f"Unsupported execution modes: {', '.join(invalid)}")
        return cls(
            production_mode=data.get("production_mode", "bounded"),
            allowed_modes=allowed,
            exploration_modes=exploration,
            default_mode=data.get("default_mode", allowed[0] if allowed else "sql"),
            allow_global_promotion=bool(data.get("allow_global_promotion", False)),
        )


@dataclass(frozen=True)
class OptimizationPolicy:
    execution: ExecutionContract = field(default_factory=ExecutionContract)
    downcast: DowncastPolicy = field(default_factory=DowncastPolicy)
    failure: FailurePolicy = field(default_factory=FailurePolicy)
    approval: ApprovalContract = field(default_factory=ApprovalContract)
    sla: SLAContract = field(default_factory=SLAContract)
    bounds_source_order: tuple[str, ...] = (
        "catalog_stats",
        "delta_log_stats",
        "parquet_row_group_stats",
        "sample_profile",
        "exact_scan",
    )

    @classmethod
    def from_task(cls, task: dict[str, Any]) -> "OptimizationPolicy":
        spec = task.get("optimization_policy", {}) or {}
        return cls(
            execution=ExecutionContract.from_dict(spec.get("execution")),
            downcast=DowncastPolicy.from_dict(spec.get("downcast")),
            failure=FailurePolicy.from_dict(spec.get("failure")),
            approval=ApprovalContract.from_dict(spec.get("approval")),
            sla=SLAContract.from_dict(spec.get("sla")),
            bounds_source_order=tuple(spec.get("bounds_source_order", cls.bounds_source_order)),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "execution": asdict(self.execution),
            "downcast": asdict(self.downcast),
            "failure": asdict(self.failure),
            "approval": asdict(self.approval),
            "sla": asdict(self.sla),
            "bounds_source_order": list(self.bounds_source_order),
        }

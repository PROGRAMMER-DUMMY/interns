"""Shared hardware and resource preflight layer.

The manager is intentionally standard-library only. It gives ingestion,
profiling, and transformation code one conservative place to ask: how large can
this local run be, how many workers should it use, and should it block before
filling disk or memory.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class HardwareProfile:
    cpu_count: int
    total_memory_bytes: int | None
    available_memory_bytes: int | None
    workspace_disk_free_bytes: int
    workspace_disk_total_bytes: int
    temp_disk_free_bytes: int
    temp_disk_total_bytes: int
    platform: str
    temp_dir: str
    detection_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceBudget:
    max_memory_fraction: float = 0.60
    min_free_disk_fraction: float = 0.15
    max_min_free_disk_bytes: int = 5_000_000_000
    disk_safety_multiplier: float = 2.5
    max_api_concurrency: int = 8
    max_local_workers: int | None = None
    max_single_json_bytes: int = 20_000_000

    @classmethod
    def from_env(cls) -> "ResourceBudget":
        return cls(
            max_memory_fraction=_env_float("AUTORESEARCH_MAX_MEMORY_FRACTION", 0.60),
            min_free_disk_fraction=_env_float("AUTORESEARCH_MIN_FREE_DISK_FRACTION", 0.15),
            max_min_free_disk_bytes=_env_int("AUTORESEARCH_MAX_MIN_FREE_DISK_BYTES", 5_000_000_000),
            disk_safety_multiplier=_env_float("AUTORESEARCH_DISK_SAFETY_MULTIPLIER", 2.5),
            max_api_concurrency=_env_int("AUTORESEARCH_MAX_API_CONCURRENCY", 8),
            max_local_workers=_env_optional_int("AUTORESEARCH_MAX_LOCAL_WORKERS"),
            max_single_json_bytes=_env_int("AUTORESEARCH_MAX_SINGLE_JSON_BYTES", 20_000_000),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceDecision:
    status: str
    mode: str
    recommended_workers: int
    recommended_api_concurrency: int
    max_stream_chunk_bytes: int
    warnings: list[str]
    blockers: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfilingResourceSettings:
    sample_rows: int
    exact_profile: bool
    expensive_checks: bool
    mode: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransformationResourceSettings:
    mode: str
    target_engine_recommendation: str
    local_execution_allowed: bool
    sql_strategy: str
    polars_strategy: str
    pyspark_strategy: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceReport:
    artifact_type: str
    version: int
    workspace: str
    hardware: dict[str, Any]
    budget: dict[str, Any]
    decision: dict[str, Any]
    checks: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceManager:
    def __init__(
        self,
        workspace: Path,
        *,
        repo_root: Path | None = None,
        budget: ResourceBudget | None = None,
        hardware: HardwareProfile | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.repo_root = (repo_root or workspace).resolve()
        self.layout = WorkspaceLayout(self.workspace)
        self.budget = budget or ResourceBudget.from_env()
        self.hardware = hardware or detect_hardware(self.workspace)

    def decide(self, *, estimated_bytes: int = 0, workload: str = "generic") -> ResourceDecision:
        warnings: list[str] = []
        blockers: list[str] = []
        cpu = max(1, self.hardware.cpu_count)
        local_worker_cap = self.budget.max_local_workers or max(1, cpu - 1)
        workers = max(1, min(cpu, local_worker_cap))
        api_concurrency = max(1, min(self.budget.max_api_concurrency, max(1, workers * 2)))

        disk_required = int(max(0, estimated_bytes) * self.budget.disk_safety_multiplier)
        min_free_after = min(
            int(self.hardware.workspace_disk_total_bytes * self.budget.min_free_disk_fraction),
            self.budget.max_min_free_disk_bytes,
        )
        if disk_required and self.hardware.workspace_disk_free_bytes - disk_required < min_free_after:
            blockers.append(
                "workspace_disk_budget_exceeded:"
                f"required={disk_required},free={self.hardware.workspace_disk_free_bytes},"
                f"min_free_after={min_free_after}"
            )

        if self.hardware.available_memory_bytes is None:
            warnings.append("available_memory_unknown")
            memory_budget = 512 * 1024 * 1024
        else:
            memory_budget = int(self.hardware.available_memory_bytes * self.budget.max_memory_fraction)
            if estimated_bytes and estimated_bytes > memory_budget and workload in {"catalog_index", "profile", "transform"}:
                warnings.append(f"estimated_bytes_exceed_memory_budget:{estimated_bytes}>{memory_budget}")

        if cpu <= 2:
            warnings.append("low_cpu_count")
        if self.hardware.workspace_disk_free_bytes < min_free_after:
            blockers.append("workspace_free_disk_below_minimum")

        if blockers:
            mode = "local_blocked_remote_recommended"
        elif warnings:
            mode = "local_streaming"
        else:
            mode = "local_standard"
        return ResourceDecision(
            status="blocked" if blockers else "ok",
            mode=mode,
            recommended_workers=workers,
            recommended_api_concurrency=api_concurrency,
            max_stream_chunk_bytes=max(1_048_576, min(16_777_216, memory_budget // 16)),
            warnings=warnings,
            blockers=blockers,
        )

    def check_disk_budget(self, *, estimated_bytes: int, workload: str) -> ResourceDecision:
        return self.decide(estimated_bytes=estimated_bytes, workload=workload)

    def write_report(self, *, estimated_bytes: int = 0, workload: str = "generic") -> dict[str, str]:
        self.layout.ensure_runtime_dirs()
        decision = self.decide(estimated_bytes=estimated_bytes, workload=workload)
        checks = [
            {
                "name": "workspace_disk",
                "status": "error" if any("workspace" in item for item in decision.blockers) else "ok",
                "free_bytes": self.hardware.workspace_disk_free_bytes,
                "total_bytes": self.hardware.workspace_disk_total_bytes,
            },
            {
                "name": "memory",
                "status": "warning" if "available_memory_unknown" in decision.warnings else "ok",
                "available_bytes": self.hardware.available_memory_bytes,
                "total_bytes": self.hardware.total_memory_bytes,
            },
            {"name": "cpu", "status": "ok", "cpu_count": self.hardware.cpu_count},
        ]
        report = ResourceReport(
            artifact_type="resource_preflight.json",
            version=1,
            workspace=_rel(self.workspace, self.repo_root),
            hardware=self.hardware.to_dict(),
            budget=self.budget.to_dict(),
            decision=decision.to_dict(),
            checks=checks,
        )
        json_path = self.layout.evidence_dir / "resource_preflight.json"
        md_path = self.layout.reports_dir / "resource_preflight.md"
        json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        md_path.write_text(_render_resource_report(report), encoding="utf-8")
        return {"resource_preflight": _rel(json_path, self.repo_root), "report": _rel(md_path, self.repo_root)}

    def profiling_settings(
        self,
        *,
        requested_sample_rows: int,
        requested_exact: bool = False,
        estimated_bytes: int = 0,
    ) -> ProfilingResourceSettings:
        decision = self.decide(estimated_bytes=estimated_bytes, workload="profile")
        if decision.status == "blocked":
            return ProfilingResourceSettings(
                sample_rows=max(1_000, min(requested_sample_rows, 10_000)),
                exact_profile=False,
                expensive_checks=False,
                mode=decision.mode,
                reason="resource_blocked_profile_remote_recommended",
            )
        if decision.mode == "local_streaming":
            return ProfilingResourceSettings(
                sample_rows=max(1_000, min(requested_sample_rows, 25_000)),
                exact_profile=False,
                expensive_checks=False,
                mode=decision.mode,
                reason="memory_pressure_reduced_rows_and_expensive_checks",
            )
        return ProfilingResourceSettings(
            sample_rows=requested_sample_rows,
            exact_profile=requested_exact,
            expensive_checks=True,
            mode=decision.mode,
            reason="resource_standard_profile",
        )

    def transformation_settings(
        self,
        *,
        target_engine: str,
        estimated_bytes: int = 0,
    ) -> TransformationResourceSettings:
        decision = self.decide(estimated_bytes=estimated_bytes, workload="transform")
        if decision.status == "blocked":
            return TransformationResourceSettings(
                mode=decision.mode,
                target_engine_recommendation="databricks" if target_engine in {"sql", "polars", "hybrid"} else target_engine,
                local_execution_allowed=False,
                sql_strategy="remote_required",
                polars_strategy="blocked_remote_recommended",
                pyspark_strategy="remote_cluster",
                reason="local_transform_resource_blocked",
            )
        if decision.mode == "local_streaming":
            return TransformationResourceSettings(
                mode=decision.mode,
                target_engine_recommendation="sql" if target_engine == "hybrid" else target_engine,
                local_execution_allowed=True,
                sql_strategy="bounded_views_and_limit_pushdown",
                polars_strategy="lazy_streaming",
                pyspark_strategy="remote_optional",
                reason="memory_pressure_streaming_strategy",
            )
        return TransformationResourceSettings(
            mode=decision.mode,
            target_engine_recommendation=target_engine,
            local_execution_allowed=True,
            sql_strategy="standard_local",
            polars_strategy="lazy_standard",
            pyspark_strategy="remote_optional",
            reason="resource_standard_transform",
        )


def detect_hardware(workspace: Path) -> HardwareProfile:
    notes: list[str] = []
    cpu_count = max(1, os.cpu_count() or 1)
    total_memory, available_memory, memory_notes = _detect_memory()
    notes.extend(memory_notes)
    workspace_usage = shutil.disk_usage(workspace if workspace.exists() else workspace.parent)
    temp_dir = Path(os.environ.get("TMP") or os.environ.get("TEMP") or ".").resolve()
    temp_usage = shutil.disk_usage(temp_dir if temp_dir.exists() else workspace)
    return HardwareProfile(
        cpu_count=cpu_count,
        total_memory_bytes=total_memory,
        available_memory_bytes=available_memory,
        workspace_disk_free_bytes=workspace_usage.free,
        workspace_disk_total_bytes=workspace_usage.total,
        temp_disk_free_bytes=temp_usage.free,
        temp_disk_total_bytes=temp_usage.total,
        platform=platform.platform(),
        temp_dir=str(temp_dir),
        detection_notes=notes,
    )


def _detect_memory() -> tuple[int | None, int | None, list[str]]:
    env_total = _env_optional_int("AUTORESEARCH_TOTAL_MEMORY_BYTES")
    env_available = _env_optional_int("AUTORESEARCH_AVAILABLE_MEMORY_BYTES")
    if env_total is not None or env_available is not None:
        return env_total, env_available, ["memory_from_env_override"]
    if os.name == "nt":
        return _detect_windows_memory()
    return _detect_posix_memory()


def _detect_windows_memory() -> tuple[int | None, int | None, list[str]]:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except Exception:
        return None, None, ["memory_detection_failed"]
    if not ok:
        return None, None, ["memory_detection_failed"]
    return int(status.ullTotalPhys), int(status.ullAvailPhys), []


def _detect_posix_memory() -> tuple[int | None, int | None, list[str]]:
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            total = int(pages * page_size)
            return total, None, ["available_memory_unknown"]
        except (OSError, ValueError):
            pass
    return None, None, ["memory_detection_failed"]


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    return int(value)


def _env_optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    return float(value)


def _render_resource_report(report: ResourceReport) -> str:
    decision = report.decision
    hardware = report.hardware
    lines = [
        "# Resource Preflight",
        "",
        f"- Workspace: `{report.workspace}`",
        f"- Status: `{decision['status']}`",
        f"- Mode: `{decision['mode']}`",
        f"- CPU cores: `{hardware['cpu_count']}`",
        f"- Recommended workers: `{decision['recommended_workers']}`",
        f"- API concurrency: `{decision['recommended_api_concurrency']}`",
        f"- Workspace disk free: `{hardware['workspace_disk_free_bytes']}` bytes",
        f"- Available memory: `{hardware['available_memory_bytes']}` bytes",
        "",
    ]
    if decision["blockers"]:
        lines.extend(["## Blockers", ""])
        lines.extend(f"- `{item}`" for item in decision["blockers"])
        lines.append("")
    if decision["warnings"]:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- `{item}`" for item in decision["warnings"])
        lines.append("")
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)

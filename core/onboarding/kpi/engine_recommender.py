"""Complexity- and size-aware engine recommendation.

SQL is always the default (simplest, most portable, and the only engine that
today renders every KPI shape). This recommender advises *when to switch* to
Polars, a SQL+Polars hybrid, or PySpark, based on signals it can actually
measure: KPI feature count, join depth, dimension count, window/ratio
complexity, and the size of the datasets the KPI touches.

Output is explainable — every recommendation carries the signals and the
reasons that produced it, so a user can see *why* a heavier engine is suggested.
All signals are derived from the feature mapping, profiles, and `kpi_intent`;
no domain vocabulary is hardcoded.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.onboarding.kpi.kpi_intent import parse_intent
from core.onboarding.kpi.sql_generator import _feature_source_refs, _repo_path, _rel
from core.storage.workspace_layout import WorkspaceLayout

# Size tiers in bytes (rough proxies for local feasibility).
_SMALL_MAX = 500 * 1024 * 1024          # 500 MB — comfortable in-memory
_MEDIUM_MAX = 10 * 1024 * 1024 * 1024   # 10 GB — streamable locally
_SUPPORTED_ENGINES = ("sql", "polars", "hybrid", "pyspark")


@dataclass(frozen=True)
class ComplexitySignals:
    feature_count: int
    join_depth: int
    dim_count: int
    has_window: bool
    has_ratio: bool
    unsupported_window: str
    estimated_bytes: int

    @property
    def size_tier(self) -> str:
        if self.estimated_bytes >= _MEDIUM_MAX:
            return "large"
        if self.estimated_bytes >= _SMALL_MAX:
            return "medium"
        return "small"

    @property
    def complexity_score(self) -> int:
        return (
            self.feature_count
            + 2 * self.join_depth
            + self.dim_count
            + (3 if self.has_window else 0)
            + (2 if self.has_ratio else 0)
        )


@dataclass(frozen=True)
class EngineRecommendation:
    kpi_id: str
    default_engine: str
    recommended_engine: str
    size_tier: str
    complexity_score: int
    signals: dict[str, Any]
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def recommend(kpi_id: str, signals: ComplexitySignals, *, local_blocked: bool = False) -> EngineRecommendation:
    """Pure, deterministic recommendation. SQL is always the default."""
    tier = signals.size_tier
    reasons: list[str] = []
    recommended = "sql"

    if signals.unsupported_window:
        recommended = "sql"
        reasons.append(
            f"window pattern `{signals.unsupported_window}` currently renders correctly only in SQL; "
            "keep SQL until the imperative engines support it"
        )
    elif local_blocked or tier == "large":
        recommended = "pyspark"
        reasons.append(
            "dataset size or resource budget exceeds safe local execution → distributed PySpark "
            "(or Databricks) for the heavy shuffle/scan"
        )
    elif tier == "medium" and (signals.join_depth >= 2 or signals.has_window or signals.complexity_score >= 8):
        recommended = "hybrid"
        reasons.append(
            "medium data with multiple joins or window/ratio logic → hybrid: SQL expresses the shape, "
            "Polars streams the scale"
        )
    elif tier == "medium":
        recommended = "polars"
        reasons.append("medium data with a single-pass shape → Polars lazy streaming avoids loading it all")
    else:
        recommended = "sql"
        reasons.append("small data and a simple shape → SQL is the simplest, fastest, most portable choice")

    if recommended != "sql":
        reasons.append("SQL remains the correctness baseline; switch only for the performance win above")

    return EngineRecommendation(
        kpi_id=kpi_id,
        default_engine="sql",
        recommended_engine=recommended,
        size_tier=tier,
        complexity_score=signals.complexity_score,
        signals=asdict(signals),
        reasons=reasons,
    )


class KPIEngineRecommender:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def recommend_all(self, *, local_blocked: bool = False) -> list[EngineRecommendation]:
        mapping = self._load_mapping()
        profiles = self._profile_map()
        out: list[EngineRecommendation] = []
        for kpi in mapping.get("kpis", []):
            kpi_id = str(kpi.get("kpi_id") or "")
            signals = self._signals(kpi, profiles)
            out.append(recommend(kpi_id, signals, local_blocked=local_blocked))
        return out

    def write(self, *, local_blocked: bool = False) -> dict[str, Any]:
        recs = self.recommend_all(local_blocked=local_blocked)
        payload = {
            "artifact_type": "engine_recommendation.json",
            "version": 1,
            "workspace": _rel(self.workspace, self.repo_root),
            "default_engine": "sql",
            "recommendations": [r.summary() for r in recs],
        }
        self.layout.ensure_runtime_dirs()
        report_dir = self.layout.reports_dir / "engine_recommendation"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "current.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (report_dir / "current.md").write_text(_render_md(recs), encoding="utf-8")
        return payload

    def _signals(self, kpi: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> ComplexitySignals:
        intent = parse_intent(kpi)
        refs = [r for f in kpi.get("features", []) for r in _feature_source_refs(f, self.repo_root)]
        datasets = {r["dataset"] for r in refs if r.get("dataset")}
        estimated = sum(self._dataset_bytes(profiles.get(ds, {})) for ds in datasets)
        return ComplexitySignals(
            feature_count=len(kpi.get("features", [])),
            join_depth=max(0, len(datasets) - 1),
            dim_count=len(intent.dims),
            has_window=intent.share is not None,
            has_ratio=intent.ratio is not None,
            unsupported_window=intent.unsupported_window,
            estimated_bytes=estimated,
        )

    @staticmethod
    def _dataset_bytes(profile: dict[str, Any]) -> int:
        for key in ("size_bytes", "file_size_bytes", "bytes"):
            if isinstance(profile.get(key), int):
                return int(profile[key])
        rows = int(profile.get("row_count") or 0)
        cols = len(profile.get("schema") or {}) or 1
        return rows * cols * 16  # rough proxy when no byte size is recorded

    def _profile_map(self) -> dict[str, dict[str, Any]]:
        index = self.layout.profiles_dir / "profile_index.json"
        if not index.exists():
            return {}
        data = json.loads(index.read_text(encoding="utf-8"))
        return {
            _repo_path(str(p.get("path") or ""), self.repo_root): p
            for p in data.get("profiles", [])
            if p.get("path")
        }

    def _load_mapping(self) -> dict[str, Any]:
        path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not path.exists():
            raise FileNotFoundError(f"feature mapping not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))


def _render_md(recs: list[EngineRecommendation]) -> str:
    lines = [
        "# Engine Recommendation",
        "",
        "Default engine is **SQL** for every KPI. Switch to the recommended engine only for the "
        "stated performance reason.",
        "",
        "| KPI | Default | Recommended | Size | Complexity | Why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in recs:
        why = "; ".join(r.reasons)
        lines.append(
            f"| {r.kpi_id} | {r.default_engine} | **{r.recommended_engine}** | {r.size_tier} "
            f"| {r.complexity_score} | {why} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recommend per-KPI execution engine (SQL default; Polars/hybrid/PySpark for scale)."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--local-blocked", action="store_true",
        help="Signal that local execution is disallowed (forces distributed recommendation).",
    )
    args = parser.parse_args(argv)
    payload = KPIEngineRecommender(args.repo_root, args.workspace).write(local_blocked=args.local_blocked)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

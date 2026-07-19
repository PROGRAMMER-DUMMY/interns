"""Structured engine-routing memory for SQL/Polars/PySpark decisions."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.metadata_store import MetadataStore, build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class EngineEvolutionRecord:
    stage: str
    engine: str
    workload_signature: str
    resource_mode: str
    input_rows: int | None = None
    output_rows: int | None = None
    input_bytes: int | None = None
    elapsed_seconds: float | None = None
    status: str = "unknown"
    validation_status: str = "unknown"
    failure_type: str = ""
    workload_shape: dict[str, Any] = field(default_factory=dict)
    decision_analysis: dict[str, Any] = field(default_factory=dict)
    bottlenecks: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    promotion: dict[str, Any] = field(default_factory=dict)
    next_experiment: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EngineEvolutionMemory:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        metadata_store: MetadataStore | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(self.workspace)
        self.metadata_store = metadata_store or build_metadata_store(self.layout, repo_root=self.repo_root)

    @property
    def memory_path(self) -> Path:
        return self.layout.memory_dir / "engine_evolution.json"

    @property
    def markdown_path(self) -> Path:
        return self.layout.memory_dir / "evolution.md"

    def record(self, record: EngineEvolutionRecord) -> dict[str, str]:
        self.layout.ensure_runtime_dirs()
        payload = self._load_payload()
        payload["records"].append(record.to_dict())
        payload["lessons"] = self._derive_lessons(payload["records"])
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.memory_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        _append_or_replace_engine_evolution_section(self.markdown_path, _render_engine_evolution_markdown(payload))
        self.metadata_store.upsert(
            "engine_evolution",
            "current",
            payload,
            workspace=_rel(self.workspace, self.repo_root),
        )
        return {
            "engine_evolution": _rel(self.memory_path, self.repo_root),
            "evolution": _rel(self.markdown_path, self.repo_root),
        }

    def lessons(self) -> list[dict[str, Any]]:
        return list(self._load_payload().get("lessons") or [])

    def recommendation_for(self, *, stage: str, resource_mode: str, target_engine: str) -> dict[str, Any]:
        lessons = [
            lesson
            for lesson in self.lessons()
            if lesson.get("stage") == stage and lesson.get("resource_mode") in {resource_mode, "any"}
        ]
        if not lessons:
            return {
                "engine": target_engine,
                "confidence": "low",
                "reason": "no_engine_evolution_lesson",
                "evidence_count": 0,
            }
        best = sorted(
            lessons,
            key=lambda item: (
                -float(item.get("score") or 0),
                -int(item.get("success_count") or 0),
                str(item.get("engine") or ""),
            ),
        )[0]
        return {
            "engine": best.get("engine") or target_engine,
            "confidence": best.get("confidence", "low"),
            "reason": best.get("reason", "engine_evolution_lesson"),
            "evidence_count": best.get("success_count", 0),
            "lesson": best,
        }

    def _load_payload(self) -> dict[str, Any]:
        if self.memory_path.exists():
            try:
                data = json.loads(self.memory_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("artifact_type", "engine_evolution.json")
        data.setdefault("version", 1)
        data.setdefault("workspace", _rel(self.workspace, self.repo_root))
        data.setdefault("records", [])
        data.setdefault("lessons", [])
        return data

    def _derive_lessons(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for record in records:
            if record.get("status") != "success" or record.get("validation_status") not in {"ok", "passed"}:
                continue
            workload_shape = _record_workload_shape(record)
            key = (
                str(record.get("stage") or ""),
                str(record.get("resource_mode") or "any"),
                str(record.get("engine") or ""),
                _workload_family(workload_shape),
            )
            grouped.setdefault(key, []).append(record)
        lessons: list[dict[str, Any]] = []
        for (stage, resource_mode, engine, workload_family), rows in grouped.items():
            elapsed = [float(row.get("elapsed_seconds")) for row in rows if row.get("elapsed_seconds") is not None]
            if not elapsed:
                continue
            avg_elapsed = sum(elapsed) / len(elapsed)
            success_count = len(rows)
            total_rows = [int(row.get("input_rows")) for row in rows if row.get("input_rows") is not None]
            total_bytes = [int(row.get("input_bytes")) for row in rows if row.get("input_bytes") is not None]
            bottleneck_counts = _count_values(
                str(item.get("type") or item.get("name") or "")
                for row in rows
                for item in (row.get("bottlenecks") or [])
                if isinstance(item, dict)
            )
            rejected_alternatives = _count_values(
                str(item.get("engine") or "")
                for row in rows
                for item in (row.get("alternatives") or [])
                if isinstance(item, dict)
            )
            score = success_count / max(avg_elapsed, 0.001)
            confidence = _lesson_confidence(success_count, rows)
            lessons.append(
                {
                    "stage": stage,
                    "resource_mode": resource_mode,
                    "engine": engine,
                    "workload_family": workload_family,
                    "success_count": success_count,
                    "avg_elapsed_seconds": round(avg_elapsed, 6),
                    "avg_input_rows": round(sum(total_rows) / len(total_rows), 2) if total_rows else None,
                    "avg_input_bytes": round(sum(total_bytes) / len(total_bytes), 2) if total_bytes else None,
                    "common_bottlenecks": bottleneck_counts[:5],
                    "rejected_alternatives": rejected_alternatives[:5],
                    "score": round(score, 6),
                    "confidence": confidence,
                    "reason": (
                        f"{engine}_validated_for_{stage}_under_{resource_mode}"
                        f"_workload_{workload_family}"
                    ),
                    "promotion_state": _promotion_state(rows, confidence),
                    "next_experiment": _next_experiment(rows, engine),
                }
            )
        return sorted(lessons, key=lambda item: (-float(item["score"]), item["stage"], item["engine"]))


def _render_engine_evolution_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Engine Evolution",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- Records: `{len(payload.get('records') or [])}`",
        f"- Lessons: `{len(payload.get('lessons') or [])}`",
        "",
    ]
    for lesson in payload.get("lessons", []):
        lines.extend(
            [
                f"## {lesson.get('stage')} / {lesson.get('resource_mode')}",
                "",
                f"- Engine: `{lesson.get('engine')}`",
                f"- Workload family: `{lesson.get('workload_family', '')}`",
                f"- Confidence: `{lesson.get('confidence')}`",
                f"- Promotion state: `{lesson.get('promotion_state')}`",
                f"- Success count: `{lesson.get('success_count')}`",
                f"- Avg elapsed seconds: `{lesson.get('avg_elapsed_seconds')}`",
                f"- Reason: `{lesson.get('reason')}`",
                "",
            ]
        )
    return "\n".join(lines)


def _append_or_replace_engine_evolution_section(path: Path, section: str) -> None:
    marker_start = "<!-- engine-evolution:start -->"
    marker_end = "<!-- engine-evolution:end -->"
    wrapped = f"{marker_start}\n{section.rstrip()}\n{marker_end}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker_start in existing and marker_end in existing:
        before = existing.split(marker_start, 1)[0].rstrip()
        after = existing.split(marker_end, 1)[1].lstrip()
        path.write_text("\n\n".join(part for part in (before, wrapped.rstrip(), after.rstrip()) if part) + "\n", encoding="utf-8")
        return
    path.write_text((existing.rstrip() + "\n\n" + wrapped).lstrip(), encoding="utf-8")


def _record_workload_shape(record: dict[str, Any]) -> dict[str, Any]:
    shape = record.get("workload_shape")
    if isinstance(shape, dict):
        return shape
    return {}


def _workload_family(shape: dict[str, Any]) -> str:
    if not shape:
        return "unknown"
    operation = str(shape.get("operation_type") or shape.get("stage_type") or "transform")
    file_formats = "_".join(sorted(str(value) for value in shape.get("file_formats", []) if value))
    join_count = int(shape.get("join_count") or 0)
    group_by_count = int(shape.get("group_by_count") or 0)
    row_band = _size_band(int(shape.get("input_rows") or 0))
    parts = [operation, row_band]
    if file_formats:
        parts.append(file_formats)
    if join_count:
        parts.append(f"joins{join_count}")
    if group_by_count:
        parts.append(f"groupby{group_by_count}")
    return "_".join(parts)


def _size_band(value: int) -> str:
    if value >= 100_000_000:
        return "huge"
    if value >= 10_000_000:
        return "large"
    if value >= 1_000_000:
        return "medium"
    if value > 0:
        return "small"
    return "unknown_size"


def _count_values(values: Any) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _lesson_confidence(success_count: int, rows: list[dict[str, Any]]) -> str:
    promoted = any((row.get("promotion") or {}).get("promoted") is True for row in rows)
    if promoted or success_count >= 5:
        return "high"
    if success_count >= 2:
        return "medium"
    return "low"


def _promotion_state(rows: list[dict[str, Any]], confidence: str) -> str:
    if any((row.get("promotion") or {}).get("promoted") is True for row in rows):
        return "promoted"
    if confidence == "high":
        return "eligible"
    if confidence == "medium":
        return "candidate"
    return "observe_more"


def _next_experiment(rows: list[dict[str, Any]], engine: str) -> dict[str, Any]:
    explicit = [row.get("next_experiment") for row in rows if isinstance(row.get("next_experiment"), dict)]
    if explicit:
        return explicit[-1]
    return {
        "goal": "compare_engine_candidate",
        "compare_against": "sql" if engine != "sql" else "polars",
        "reason": "increase_engine_routing_confidence",
    }


def _json_arg(value: str | None) -> dict[str, Any] | list[dict[str, Any]]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, (dict, list)):
        raise ValueError("JSON argument must decode to object or list")
    return parsed


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)



@anchored("record-engine-evolution")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record SQL/Polars/PySpark engine evolution evidence.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--engine", required=True, choices=["sql", "polars", "pyspark", "databricks"])
    parser.add_argument("--workload-signature", required=True)
    parser.add_argument("--resource-mode", default="any")
    parser.add_argument("--input-rows", type=int)
    parser.add_argument("--output-rows", type=int)
    parser.add_argument("--input-bytes", type=int)
    parser.add_argument("--elapsed-seconds", type=float)
    parser.add_argument("--status", default="success")
    parser.add_argument("--validation-status", default="ok")
    parser.add_argument("--failure-type", default="")
    parser.add_argument("--workload-shape-json", default="")
    parser.add_argument("--decision-analysis-json", default="")
    parser.add_argument("--bottlenecks-json", default="")
    parser.add_argument("--alternatives-json", default="")
    parser.add_argument("--validation-json", default="")
    parser.add_argument("--promotion-json", default="")
    parser.add_argument("--next-experiment-json", default="")
    parser.add_argument("--evidence-json", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    memory = EngineEvolutionMemory(args.repo_root, args.workspace)
    artifacts = memory.record(
        EngineEvolutionRecord(
            stage=args.stage,
            engine=args.engine,
            workload_signature=args.workload_signature,
            resource_mode=args.resource_mode,
            input_rows=args.input_rows,
            output_rows=args.output_rows,
            input_bytes=args.input_bytes,
            elapsed_seconds=args.elapsed_seconds,
            status=args.status,
            validation_status=args.validation_status,
            failure_type=args.failure_type,
            workload_shape=_json_arg(args.workload_shape_json) if args.workload_shape_json else {},
            decision_analysis=_json_arg(args.decision_analysis_json) if args.decision_analysis_json else {},
            bottlenecks=_json_arg(args.bottlenecks_json) if args.bottlenecks_json else [],
            alternatives=_json_arg(args.alternatives_json) if args.alternatives_json else [],
            validation=_json_arg(args.validation_json) if args.validation_json else {},
            promotion=_json_arg(args.promotion_json) if args.promotion_json else {},
            next_experiment=_json_arg(args.next_experiment_json) if args.next_experiment_json else {},
            evidence=_json_arg(args.evidence_json) if args.evidence_json else {},
            notes=args.notes,
        )
    )
    payload = {"artifacts": artifacts, "lessons": memory.lessons()}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Engine evolution: {artifacts['engine_evolution']}")
        print(f"Lessons: {len(payload['lessons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Confidence-scored health report for workspace and team memory artifacts."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


MEMORY_HEALTH_VERSION = 1
ENTRY_COLLECTION_KEYS = {
    "cards",
    "decisions",
    "definitions",
    "entries",
    "memories",
    "memory",
    "reuse_cards",
    "user_confirmed_feature_decisions",
    "workspace_feature_definitions",
}
APPROVED_STATUS_MARKERS = {
    "approved",
    "approved_for_execution",
    "finalized",
    "proven_alias",
    "proven_direct",
    "proven_formula",
    "proven_join",
    "proven_taxonomy",
    "ready",
    "ready_for_sql",
    "user_confirmed",
}
WEAK_STATUS_MARKERS = {
    "blocked",
    "blocked_ambiguous",
    "blocked_missing_evidence",
    "candidate",
    "draft",
    "inferred_candidate",
    "needs_approval",
    "needs_data_validation",
    "needs_review",
    "rejected",
}


@dataclass(frozen=True)
class MemoryHealthResult:
    workspace: str
    ok: bool
    status: str
    current_json_path: str
    current_markdown_path: str
    evidence_path: str
    entry_count: int
    finding_count: int
    average_confidence: float

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "memory_health_result",
            "version": MEMORY_HEALTH_VERSION,
            "generated_by": "validate-memory-health",
            **asdict(self),
        }


class MemoryHealthValidator:
    """Validate local memory artifacts without reading datasets or remote state."""

    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        include_team_memory: bool = True,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.include_team_memory = include_team_memory

    def run(self) -> MemoryHealthResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        sources, load_findings = self._load_sources()
        entries = []
        for source in sources:
            entries.extend(_entries_from_source(source, self.repo_root))
        findings = load_findings + _health_findings(entries)
        summary = _summary(entries, findings)
        report = {
            "artifact_type": "memory_health/current.json",
            "version": MEMORY_HEALTH_VERSION,
            "generated_by": "validate-memory-health",
            "workspace": self.workspace_rel,
            "generated_at": _now(),
            "ok": summary["critical_count"] == 0,
            "status": _overall_status(summary),
            "summary": summary,
            "scan_scope": {
                "workspace_memory_glob": _rel(self.layout.memory_dir / "*.json", self.repo_root),
                "team_memory_glob": "state/team_memory/*.json" if self.include_team_memory else "",
                "excluded": ["raw datasets", "logs", "non-json memory notes"],
            },
            "sources": [
                {
                    "path": _rel(source["path"], self.repo_root),
                    "scope": source["scope"],
                    "entry_count": source["entry_count"],
                    "artifact_type": source["artifact_type"],
                }
                for source in sources
            ],
            "entries": entries,
            "findings": findings,
            "next_commands": [f"uv run validate-memory-health --workspace {self.workspace_rel}"],
        }
        report_dir = self.layout.reports_dir / "memory_health"
        evidence_dir = self.layout.evidence_dir / "memory_health"
        report_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        current_json = report_dir / "current.json"
        current_md = report_dir / "current.md"
        evidence_path = evidence_dir / "current.json"
        current_json.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        evidence_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")
        return MemoryHealthResult(
            workspace=self.workspace_rel,
            ok=bool(report["ok"]),
            status=str(report["status"]),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            evidence_path=_rel(evidence_path, self.repo_root),
            entry_count=int(summary["entry_count"]),
            finding_count=len(findings),
            average_confidence=float(summary["average_confidence"]),
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _load_sources(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        paths = sorted(self.layout.memory_dir.glob("*.json"))
        if self.include_team_memory:
            team_dir = self.repo_root / "state" / "team_memory"
            if team_dir.exists():
                paths.extend(sorted(team_dir.glob("*.json")))
        sources = []
        findings = []
        seen = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            scope = "team" if _is_under(resolved, self.repo_root / "state" / "team_memory") else "workspace"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                findings.append(
                    _finding(
                        "critical",
                        "invalid_json",
                        f"Memory artifact is not valid JSON: {exc.msg}",
                        artifact=_rel(path, self.repo_root),
                    )
                )
                continue
            if not isinstance(payload, (dict, list)):
                findings.append(
                    _finding(
                        "warning",
                        "unsupported_json_root",
                        "Memory artifact root is not an object or list.",
                        artifact=_rel(path, self.repo_root),
                    )
                )
                continue
            source = {
                "path": path,
                "scope": scope,
                "payload": payload,
                "artifact_type": payload.get("artifact_type", path.name) if isinstance(payload, dict) else path.name,
                "entry_count": 0,
            }
            source["entry_count"] = len(_candidate_items(payload))
            sources.append(source)
        return sources, findings


def _entries_from_source(source: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    entries = []
    for index, item in enumerate(_candidate_items(source["payload"]), start=1):
        normalized = _normalize_entry(
            item,
            source_path=source["path"],
            source_scope=source["scope"],
            source_artifact_type=str(source["artifact_type"]),
            repo_root=repo_root,
            ordinal=index,
        )
        entries.append(normalized)
    return entries


def _candidate_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    items = []
    if _looks_like_entry(payload):
        items.append(payload)
    for key, value in payload.items():
        if key not in ENTRY_COLLECTION_KEYS:
            continue
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            if _looks_like_entry(value):
                items.append(value)
            for nested in value.values():
                if isinstance(nested, list):
                    items.extend(item for item in nested if isinstance(item, dict))
    return items


def _looks_like_entry(item: dict[str, Any]) -> bool:
    marker_keys = {
        "approval_state",
        "canonical_name",
        "confidence",
        "definition",
        "evidence",
        "feature",
        "last_verified",
        "memory_id",
        "status",
        "state",
    }
    return bool(marker_keys.intersection(item))


def _normalize_entry(
    item: dict[str, Any],
    *,
    source_path: Path,
    source_scope: str,
    source_artifact_type: str,
    repo_root: Path,
    ordinal: int,
) -> dict[str, Any]:
    status = _first_text(item, "status", "state", "approval_state", "verification_status") or "unknown"
    confidence = _confidence(item, status)
    evidence = _evidence_items(item)
    last_verified = _first_text(item, "last_verified", "verified_at", "updated_at", "timestamp")
    expires_if = _first_value(item, "expires_if", "expires_at", "expiration", "review_after")
    days_since_verified = _days_since(last_verified)
    is_expired = _is_expired(expires_if)
    label = _first_text(
        item,
        "canonical_name",
        "feature",
        "name",
        "label",
        "memory_id",
        "card_id",
        "id",
    ) or f"{source_path.stem}_{ordinal}"
    memory_id = _first_text(item, "memory_id", "id", "card_id") or _slug(
        f"{source_scope}_{source_path.stem}_{label}_{ordinal}"
    )
    return {
        "memory_id": memory_id,
        "label": label,
        "memory_type": _first_text(item, "definition_type", "type", "artifact_type") or source_artifact_type,
        "scope": source_scope,
        "source_path": _rel(source_path, repo_root),
        "status": status,
        "confidence": confidence,
        "confidence_band": _confidence_band(confidence),
        "last_verified": last_verified,
        "days_since_verified": days_since_verified,
        "expires_if": expires_if,
        "is_expired": is_expired,
        "evidence": evidence,
        "evidence_count": len(evidence),
        "has_evidence": bool(evidence),
        "health": _entry_health(status, confidence, evidence, days_since_verified, is_expired),
    }


def _confidence(item: dict[str, Any], status: str) -> float:
    raw = _first_value(item, "confidence", "confidence_score", "score", "evidence_confidence")
    try:
        value = float(raw)
        if value > 1 and value <= 100:
            value = value / 100
        return round(max(0.0, min(1.0, value)), 3)
    except (TypeError, ValueError):
        pass
    normalized = _norm(status)
    if normalized in APPROVED_STATUS_MARKERS or normalized.startswith("proven_"):
        return 0.85
    if normalized in WEAK_STATUS_MARKERS or normalized.startswith("blocked"):
        return 0.4
    return 0.5


def _evidence_items(item: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = item.get("evidence")
    if evidence is None:
        evidence = item.get("sources") or item.get("evidence_paths") or item.get("evidence_files")
    if isinstance(evidence, list):
        normalized = []
        for value in evidence:
            if isinstance(value, dict):
                normalized.append(value)
            elif value:
                normalized.append({"path": str(value)})
        return normalized
    if isinstance(evidence, dict):
        return [evidence]
    if evidence:
        return [{"detail": str(evidence)}]
    return []


def _entry_health(
    status: str,
    confidence: float,
    evidence: list[dict[str, Any]],
    days_since_verified: int | None,
    is_expired: bool,
) -> str:
    normalized = _norm(status)
    if is_expired or normalized in {"rejected"} or normalized.startswith("blocked"):
        return "critical"
    if confidence < 0.5:
        return "critical"
    if (
        confidence < 0.7
        or not evidence
        or normalized in WEAK_STATUS_MARKERS
        or (days_since_verified is not None and days_since_verified > 180)
    ):
        return "warning"
    return "healthy"


def _health_findings(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for entry in entries:
        if entry["health"] == "critical":
            code = "expired_memory_entry" if entry["is_expired"] else "unhealthy_memory_entry"
            findings.append(
                _finding(
                    "critical",
                    code,
                    f"Memory `{entry['label']}` has status `{entry['status']}` and confidence `{entry['confidence']}`.",
                    artifact=entry["source_path"],
                    details={"memory_id": entry["memory_id"]},
                )
            )
        elif entry["health"] == "warning":
            if not entry["has_evidence"]:
                code = "missing_evidence"
            elif entry["days_since_verified"] is not None and entry["days_since_verified"] > 180:
                code = "stale_memory_verification"
            else:
                code = "low_confidence_memory"
            findings.append(
                _finding(
                    "warning",
                    code,
                    f"Memory `{entry['label']}` needs review before high-trust reuse.",
                    artifact=entry["source_path"],
                    details={"memory_id": entry["memory_id"], "confidence": entry["confidence"]},
                )
            )
    return findings


def _summary(entries: list[dict[str, Any]], findings: list[dict[str, Any]]) -> dict[str, Any]:
    confidences = [float(entry["confidence"]) for entry in entries]
    health_counts = {
        "healthy": sum(1 for entry in entries if entry["health"] == "healthy"),
        "warning": sum(1 for entry in entries if entry["health"] == "warning"),
        "critical": sum(1 for entry in entries if entry["health"] == "critical"),
    }
    return {
        "entry_count": len(entries),
        "average_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        "healthy_count": health_counts["healthy"],
        "warning_count": health_counts["warning"],
        "critical_count": health_counts["critical"],
        "finding_count": len(findings),
        "source_count": len({entry["source_path"] for entry in entries}),
    }


def _overall_status(summary: dict[str, Any]) -> str:
    if summary["critical_count"]:
        return "critical"
    if summary["warning_count"] or summary["entry_count"] == 0:
        return "warning"
    return "healthy"


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Memory Health",
        "",
        f"- Workspace: `{report['workspace']}`",
        f"- Status: `{report['status']}`",
        f"- Entries: `{summary['entry_count']}`",
        f"- Average confidence: `{summary['average_confidence']}`",
        f"- Healthy: `{summary['healthy_count']}`",
        f"- Warnings: `{summary['warning_count']}`",
        f"- Critical: `{summary['critical_count']}`",
        "",
        "## Sources",
        "",
    ]
    sources = report.get("sources") or []
    if sources:
        for source in sources:
            lines.append(
                f"- `{source['path']}` ({source['scope']}, entries: `{source['entry_count']}`)"
            )
    else:
        lines.append("- No JSON memory artifacts found.")
    lines.extend(["", "## Findings", ""])
    findings = report.get("findings") or []
    if findings:
        for finding in findings:
            lines.append(
                f"- `{finding['severity']}` `{finding['code']}`: {finding['message']} "
                f"Artifact: `{finding.get('artifact', '')}`"
            )
    else:
        lines.append("- No memory health findings.")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in report.get("next_commands") or [])
    lines.append("")
    return "\n".join(lines)


def _finding(
    severity: str,
    code: str,
    message: str,
    *,
    artifact: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "artifact": artifact,
        "details": details or {},
    }


def _first_text(item: dict[str, Any], *keys: str) -> str:
    value = _first_value(item, *keys)
    return "" if value is None else str(value)


def _first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _confidence_band(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.7:
        return "medium"
    if confidence >= 0.5:
        return "low"
    return "untrusted"


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")[:120] or "memory"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_since(value: Any) -> int | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    delta = datetime.now(timezone.utc) - parsed
    return max(0, delta.days)


def _is_expired(value: Any) -> bool:
    parsed = _parse_datetime(value)
    return parsed is not None and parsed < datetime.now(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate confidence-scored memory health.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--no-team-memory", action="store_true")
    args = parser.parse_args(argv)
    result = MemoryHealthValidator(
        args.repo_root,
        args.workspace,
        include_team_memory=not args.no_team_memory,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""State consolidator: ensure every workspace has ONE canonical source for each
piece of state, and projections to derived locations.

Two consolidations covered here:

1. Trajectory: `state/trajectory.jsonl` is canonical. `evidence/trajectory/current.json`
   and `reports/trajectory/current.{json,md}` are projections — they can be
   regenerated from the canonical log at any time and are safe to GC.

2. Wiki memory: `contracts/workspace_feature_definitions.json` is canonical.
   `memory/wiki_memory_candidates.json` and `reports/wiki_memory/current.{json,md}`
   are projections.

Workspace-agnostic. Uses WorkspaceLayout for all paths. The point of having a
consolidator (instead of multiple writers) is that ONE writer maintains ALL
copies — so they cannot drift, and the derived copies are guaranteed
regeneratable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


@dataclass
class ConsolidationReport:
    canonical_present: bool
    canonical_path: str
    derived_written: list[str] = field(default_factory=list)
    derived_skipped: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "canonical_present": self.canonical_present,
            "canonical_path": self.canonical_path,
            "derived_written": self.derived_written,
            "derived_skipped": self.derived_skipped,
        }


def _read_jsonl(path: Path, *, tail: int = 500) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return rows
    if tail and len(rows) > tail:
        rows = rows[-tail:]
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def consolidate_trajectory(layout: WorkspaceLayout, *, tail: int = 500) -> ConsolidationReport:
    """Project the canonical trajectory log to its derived locations."""
    canonical = layout.state_dir / "trajectory.jsonl"
    report = ConsolidationReport(
        canonical_present=canonical.exists(),
        canonical_path=str(canonical),
    )
    if not canonical.exists():
        return report

    rows = _read_jsonl(canonical, tail=tail)
    payload = {
        "artifact_type": "trajectory/current.json",
        "source_of_truth": "state/trajectory.jsonl",
        "tail_rows": len(rows),
        "events": rows,
    }

    evidence_json = layout.evidence_dir / "trajectory" / "current.json"
    evidence_json.parent.mkdir(parents=True, exist_ok=True)
    evidence_json.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    report.derived_written.append(str(evidence_json))

    report_json = layout.reports_dir / "trajectory" / "current.json"
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    report.derived_written.append(str(report_json))

    md_lines = [
        "# Trajectory (derived)",
        "",
        f"- Canonical source: `state/trajectory.jsonl`",
        f"- Tail events shown: {len(rows)}",
        "",
        "| Timestamp | Event | Status | Summary |",
        "| --- | --- | --- | --- |",
    ]
    for event in rows[-50:]:
        md_lines.append(
            "| {ts} | {ev} | {st} | {sm} |".format(
                ts=str(event.get("timestamp", "")),
                ev=str(event.get("event_type", "")),
                st=str(event.get("status", "")),
                sm=str(event.get("summary", "")).replace("|", "\\|"),
            )
        )
    report_md = layout.reports_dir / "trajectory" / "current.md"
    report_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    report.derived_written.append(str(report_md))

    return report


def consolidate_wiki_memory(layout: WorkspaceLayout) -> ConsolidationReport:
    """Project the canonical workspace_feature_definitions to derived wiki-memory locations."""
    canonical = layout.contracts_dir / "workspace_feature_definitions.json"
    report = ConsolidationReport(
        canonical_present=canonical.exists(),
        canonical_path=str(canonical),
    )
    if not canonical.exists():
        return report

    data = _read_json(canonical)
    definitions = data.get("definitions") or data.get("entries") or []

    memory_path = layout.memory_dir / "wiki_memory_candidates.json"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_payload = {
        "artifact_type": "wiki_memory_candidates.json",
        "source_of_truth": "contracts/workspace_feature_definitions.json",
        "candidate_count": len(definitions) if isinstance(definitions, list) else 0,
        "candidates": definitions if isinstance(definitions, list) else [],
    }
    memory_path.write_text(json.dumps(memory_payload, indent=2, default=str) + "\n", encoding="utf-8")
    report.derived_written.append(str(memory_path))

    reports_json = layout.reports_dir / "wiki_memory" / "current.json"
    reports_json.parent.mkdir(parents=True, exist_ok=True)
    reports_json.write_text(json.dumps(memory_payload, indent=2, default=str) + "\n", encoding="utf-8")
    report.derived_written.append(str(reports_json))

    md_lines = [
        "# Wiki Memory (derived)",
        "",
        "- Canonical source: `contracts/workspace_feature_definitions.json`",
        f"- Definition count: {memory_payload['candidate_count']}",
        "",
    ]
    for item in (definitions if isinstance(definitions, list) else [])[:50]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("feature") or item.get("name") or "")
        rule = str(item.get("rule") or item.get("definition") or "")
        md_lines.append(f"## `{name}`")
        md_lines.append("")
        if rule:
            md_lines.append(rule)
        md_lines.append("")
    reports_md = layout.reports_dir / "wiki_memory" / "current.md"
    reports_md.write_text("\n".join(md_lines), encoding="utf-8")
    report.derived_written.append(str(reports_md))

    return report


def consolidate_all(layout: WorkspaceLayout) -> dict[str, Any]:
    """Run all consolidations. Each returns its own per-area report."""
    return {
        "trajectory": consolidate_trajectory(layout).summary(),
        "wiki_memory": consolidate_wiki_memory(layout).summary(),
    }


__all__ = [
    "ConsolidationReport",
    "consolidate_all",
    "consolidate_trajectory",
    "consolidate_wiki_memory",
]

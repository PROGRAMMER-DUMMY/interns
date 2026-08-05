"""The schema-drift panel and the answers applied to it.

Conventions are borrowed wholesale from the KPI blocker / intake panels:
``current.json`` is the machine contract, ``current.md`` is what a human reads,
every option carries an id, markers are ASCII.

Two rules specific to drift:

* **A panel is only opened when a finding needs a decision.** Additive drift is
  reported in the command payload and never becomes a blocker; a panel left on
  disk after the drift stops needing action is deleted, so a stale file cannot
  block a pipeline forever.
* **Losing data is never recommended.** ``propagate`` is the recommended option
  for additive findings only. For a removal or a type change there is no safe
  default, so no option is recommended and quarantining/blocking additionally
  requires a real human via ``--confirmed-by``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.evolution.drift import (
    SEVERITY_INFO,
    DriftFinding,
    DriftReport,
    detect_drift,
)
from core.evolution.snapshot import (
    _rel,
    list_snapshots,
    load_snapshot,
    snapshot_discovery,
)
from core.governance.provenance import decision_source, is_agent_confirmer
from core.onboarding.panel_contract import attach_stage_routing
from core.storage.workspace_layout import WorkspaceLayout

PANEL_VERSION = 1
EXCLUSIONS_VERSION = 1
# STAGE_ROUTING key (core/onboarding/workspace/delegation.py) this panel belongs to.
ROUTING_STAGE = "schema_drift_review"
register_contract("schema_drift_panel/current.json", current_version=PANEL_VERSION)

OPTION_PROPAGATE = "propagate"
OPTION_QUARANTINE = "quarantine_column"
OPTION_BLOCK = "block_pipeline"
FINDING_OPTION_IDS: tuple[str, ...] = (OPTION_PROPAGATE, OPTION_QUARANTINE, OPTION_BLOCK)

# Options that change what the pipeline produces. An agent may not assert them.
HUMAN_ONLY_OPTIONS = frozenset({OPTION_QUARANTINE, OPTION_BLOCK})

OPTION_LABELS: dict[str, str] = {
    OPTION_PROPAGATE: (
        "Propagate: let the change flow downstream (bronze mergeSchema and silver "
        "on_schema_change already absorb it)"
    ),
    OPTION_QUARANTINE: (
        "Quarantine the column: exclude it from generated silver select-lists via "
        "interns/generated/contracts/schema_exclusions.json"
    ),
    OPTION_BLOCK: "Block the pipeline until the source change is explained or reverted",
}

FINDING_TEXT: dict[str, str] = {
    "added_table": "New table `{table}` appeared in the source.",
    "removed_table": "Table `{table}` is no longer present in the source.",
    "added_column": "Table `{table}`: new column `{column}`.",
    "removed_column": "Table `{table}`: column `{column}` is no longer present.",
    "type_change": "Table `{table}`: column `{column}` changed declared type.",
}

FINDING_WHY: dict[str, str] = {
    "added_table": "Nothing downstream reads it yet; it becomes work only if it should be modeled.",
    "removed_table": (
        "Every model that selects from it will fail or silently return nothing on the next run."
    ),
    "added_column": "Additive: existing select-lists and contracts keep working.",
    "removed_column": (
        "Any select-list, cast, join key or KPI feature bound to it breaks, and a KPI "
        "that silently loses an input is worse than one that fails."
    ),
    "type_change": (
        "Casts, comparisons and aggregations bound to the old type can silently change "
        "results (truncation, precision loss, or a string compare where a number was meant)."
    ),
}

PANEL_DIRNAME = "schema_drift_panel"
EXCLUSIONS_FILENAME = "schema_exclusions.json"
DECISIONS_FILENAME = "schema_drift_decisions.json"

STATUS_NO_DISCOVERY = "no_discovery"
STATUS_BASELINE = "baseline"
STATUS_NO_DRIFT = "no_drift"
STATUS_INFO_ONLY = "info_only"
STATUS_NEEDS_ANSWER = "needs_user_answer"


@dataclass(frozen=True)
class DriftPanelResult:
    status: str
    snapshot_path: str
    snapshot_created: bool
    snapshot_reason: str
    current_snapshot: str
    previous_snapshot: str
    current_json_path: str
    current_markdown_path: str
    action_needed_count: int
    info_count: int
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing(asdict(self), ROUTING_STAGE)


def prepare_drift_panel(repo_root: str | Path, workspace: str | Path) -> DriftPanelResult:
    """Snapshot the current discovery, diff it against the previous snapshot,
    and open a panel when something needs a human decision."""
    repo_root_path = Path(repo_root).resolve()
    workspace_path = (repo_root_path / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    layout.ensure_runtime_dirs()

    snap = snapshot_discovery(layout, repo_root=repo_root_path)
    snapshots = list_snapshots(layout)

    def _result(status: str, report: DriftReport | None = None) -> DriftPanelResult:
        _clear_panel(layout)
        return DriftPanelResult(
            status=status,
            snapshot_path=snap.snapshot_path,
            snapshot_created=snap.created,
            snapshot_reason=snap.reason,
            current_snapshot=snapshots[-1].name if snapshots else "",
            previous_snapshot=snapshots[-2].name if len(snapshots) > 1 else "",
            current_json_path="",
            current_markdown_path="",
            action_needed_count=0,
            info_count=len(report.informational) if report else 0,
            notes=list(report.notes) if report else [],
        )

    if not snapshots:
        return _result(STATUS_NO_DISCOVERY)
    if len(snapshots) == 1:
        return _result(STATUS_BASELINE)

    previous_name, current_name = snapshots[-2].name, snapshots[-1].name
    report = detect_drift(load_snapshot(snapshots[-2]), load_snapshot(snapshots[-1]))
    if not report.action_needed:
        return _result(STATUS_INFO_ONLY if report.has_drift else STATUS_NO_DRIFT, report)

    workspace_rel = _rel(workspace_path, repo_root_path)
    panel = {
        "artifact_type": "schema_drift_panel/current.json",
        "version": PANEL_VERSION,
        "generated_by": "prepare-drift-panel",
        "workspace": workspace_rel,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": STATUS_NEEDS_ANSWER,
        "current_snapshot": current_name,
        "previous_snapshot": previous_name,
        "action_needed_count": len(report.action_needed),
        "info_count": len(report.informational),
        "current_finding_id": _finding_id(report.action_needed[0]),
        "findings": [
            _finding_entry(finding, current_name, previous_name)
            for finding in report.action_needed
        ],
        "info_findings": [
            _finding_entry(finding, current_name, previous_name)
            for finding in report.informational
        ],
        "notes": list(report.notes),
        "apply_command": (
            f"uv run apply-drift-answer --workspace {workspace_rel} "
            "--finding <finding_id> --answer <option_id> --confirmed-by \"<name>\""
        ),
    }
    attach_stage_routing(panel, ROUTING_STAGE)

    panel_dir = layout.reports_dir / PANEL_DIRNAME
    panel_dir.mkdir(parents=True, exist_ok=True)
    current_json = panel_dir / "current.json"
    current_md = panel_dir / "current.md"
    current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
    current_md.write_text(render_markdown(panel), encoding="utf-8")

    return DriftPanelResult(
        status=STATUS_NEEDS_ANSWER,
        snapshot_path=snap.snapshot_path,
        snapshot_created=snap.created,
        snapshot_reason=snap.reason,
        current_snapshot=current_name,
        previous_snapshot=previous_name,
        current_json_path=_rel(current_json, repo_root_path),
        current_markdown_path=_rel(current_md, repo_root_path),
        action_needed_count=len(report.action_needed),
        info_count=len(report.informational),
        notes=list(report.notes),
    )


def apply_drift_answer(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    finding_id: str,
    answer: str,
    confirmed_by: str = "",
) -> dict[str, Any]:
    """Record one answer from the panel; write the exclusions contract when the
    answer is ``quarantine_column``."""
    repo_root_path = Path(repo_root).resolve()
    workspace_path = (repo_root_path / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)

    panel = load_drift_panel(layout)
    entries = list(panel.get("findings") or []) + list(panel.get("info_findings") or [])
    if not entries:
        raise ValueError(
            "no schema drift panel to answer; run `prepare-drift-panel` first "
            f"(expected {(layout.reports_dir / PANEL_DIRNAME / 'current.json')})"
        )
    by_id = {str(entry.get("finding_id")): entry for entry in entries}
    finding = by_id.get(str(finding_id).strip())
    if finding is None:
        raise ValueError(
            f"--finding must be one of {', '.join(sorted(by_id))}, got {finding_id!r}"
        )

    valid = [str(option["option_id"]) for option in finding.get("options") or []]
    normalized = str(answer or "").strip()
    if normalized not in valid:
        raise ValueError(
            f"finding `{finding['finding_id']}` accepts {', '.join(valid)}, got {answer!r}"
        )
    if normalized in HUMAN_ONLY_OPTIONS and is_agent_confirmer(confirmed_by):
        raise PermissionError(
            f"`{normalized}` changes what the pipeline produces and must be confirmed by a "
            "person: pass --confirmed-by \"<human name>\" (an empty or agent identity is "
            "recorded as agent-asserted and is refused here)"
        )

    evidence = finding.get("evidence") or {}
    table = str(evidence.get("table") or "")
    column = str(evidence.get("column") or "")
    decided_by = str(confirmed_by or "").strip()
    at = datetime.now(timezone.utc).isoformat()

    decisions_path = layout.contracts_dir / DECISIONS_FILENAME
    decisions = _load_json(decisions_path)
    decisions[str(finding["finding_id"])] = {
        "answer": normalized,
        "kind": str(finding.get("kind") or ""),
        "table": table,
        "column": column,
        "decided_by": decided_by,
        "source": decision_source(decided_by),
        "at": at,
        "snapshot": str(evidence.get("first_seen_snapshot") or ""),
    }
    _write_json(decisions_path, dict(sorted(decisions.items())))

    exclusions_path = ""
    excluded_columns: list[str] = []
    if normalized == OPTION_QUARANTINE:
        excluded_columns = _record_exclusion(layout, table, column, decided_by=decided_by, at=at)
        exclusions_path = _rel(layout.contracts_dir / EXCLUSIONS_FILENAME, repo_root_path)

    return {
        "ok": True,
        "status": "applied",
        "finding_id": str(finding["finding_id"]),
        "kind": str(finding.get("kind") or ""),
        "table": table,
        "column": column,
        "answer": normalized,
        "decided_by": decided_by,
        "source": decision_source(decided_by),
        "decisions_path": _rel(decisions_path, repo_root_path),
        "exclusions_path": exclusions_path,
        "excluded_columns": excluded_columns,
        "pipeline_blocked": normalized == OPTION_BLOCK,
    }


def load_drift_panel(layout: WorkspaceLayout) -> dict[str, Any]:
    return _load_json(layout.reports_dir / PANEL_DIRNAME / "current.json")


def load_schema_exclusions(layout: WorkspaceLayout) -> dict[str, Any]:
    """The contract the dbt generator reads. Absent file = no exclusions."""
    return _load_json(layout.contracts_dir / EXCLUSIONS_FILENAME)


def render_markdown(panel: dict[str, Any]) -> str:
    lines = [
        "# Schema Drift Panel",
        "",
        f"Workspace: `{panel.get('workspace', '')}` | "
        f"Needs a decision: {panel.get('action_needed_count', 0)} | "
        f"Informational: {panel.get('info_count', 0)}",
        "",
        f"Compared `{panel.get('previous_snapshot', '')}` -> `{panel.get('current_snapshot', '')}` "
        "(interns/generated/intake/discovery_history).",
        "",
        "## Needs a decision",
        "",
    ]
    for finding in panel.get("findings") or []:
        lines.extend(_finding_lines(finding, "[x]"))

    info = panel.get("info_findings") or []
    if info:
        lines.extend(["## Informational (no decision needed)", ""])
        for finding in info:
            lines.append(f"- [~] `{finding['finding_id']}` - {finding['text']}")
        lines.append("")

    notes = panel.get("notes") or []
    if notes:
        lines.extend(["## Notes", ""])
        lines.extend(f"- [~] {note}" for note in notes)
        lines.append("")

    lines.extend(
        [
            "## Apply",
            "",
            "```powershell",
            str(panel.get("apply_command", "")),
            "```",
            "",
            "Machine contract: `current.json`.",
        ]
    )
    return "\n".join(lines) + "\n"


def _finding_lines(finding: dict[str, Any], marker: str) -> list[str]:
    evidence = finding.get("evidence") or {}
    lines = [f"- {marker} `{finding['finding_id']}` - {finding['text']}"]
    detail = [f"table `{evidence.get('table', '')}`"]
    if evidence.get("column"):
        detail.append(f"column `{evidence['column']}`")
    if evidence.get("old_type") or evidence.get("new_type"):
        detail.append(f"type `{evidence.get('old_type', '')}` -> `{evidence.get('new_type', '')}`")
    detail.append(f"first seen in `{evidence.get('first_seen_snapshot', '')}`")
    lines.append(f"  Evidence: {', '.join(detail)}")
    lines.append(f"  Why it matters: {finding.get('why_it_matters', '')}")
    lines.append("  Options:")
    recommended = str(finding.get("recommended_option_id") or "")
    for option in finding.get("options") or []:
        suffix = " [RECOMMENDED]" if option["option_id"] == recommended else ""
        lines.append(f"  - `{option['option_id']}`{suffix} | {option['label']}")
    if not recommended:
        lines.append("  - No option is recommended: this change loses or reshapes data.")
    lines.append("")
    return lines


def _finding_entry(
    finding: DriftFinding, current_snapshot: str, previous_snapshot: str
) -> dict[str, Any]:
    options = [
        {"option_id": option_id, "label": OPTION_LABELS[option_id]}
        for option_id in FINDING_OPTION_IDS
        if option_id != OPTION_QUARANTINE or finding.is_column_scoped
    ]
    recommended = OPTION_PROPAGATE if finding.severity == SEVERITY_INFO else ""
    return {
        "finding_id": _finding_id(finding),
        "kind": finding.kind,
        "severity": finding.severity,
        "text": FINDING_TEXT[finding.kind].format(table=finding.table, column=finding.column),
        "why_it_matters": FINDING_WHY[finding.kind],
        "options": options,
        "recommended_option_id": recommended,
        "evidence": {
            "table": finding.table,
            "column": finding.column,
            "old_type": finding.old_type,
            "new_type": finding.new_type,
            "first_seen_snapshot": current_snapshot,
            "compared_to_snapshot": previous_snapshot,
            "source": f"interns/generated/intake/discovery_history/{current_snapshot}",
        },
    }


def _finding_id(finding: DriftFinding) -> str:
    parts = [finding.kind, finding.table]
    if finding.column:
        parts.append(finding.column)
    return ":".join(parts)


def _record_exclusion(
    layout: WorkspaceLayout, table: str, column: str, *, decided_by: str, at: str
) -> list[str]:
    """Merge one column into ``schema_exclusions.json``.

    Contract (consumed by the dbt generator's silver select-lists)::

        {"<table>": {"excluded_columns": ["col", ...], "decided_by": "...", "at": "..."}}

    Re-applying the same answer is a no-op down to the bytes, so a replay never
    rewrites the provenance stamp.
    """
    path = layout.contracts_dir / EXCLUSIONS_FILENAME
    contract = _load_json(path)
    entry = contract.get(table) if isinstance(contract.get(table), dict) else {}
    existing = [str(name) for name in (entry.get("excluded_columns") or []) if str(name)]
    merged = sorted(set(existing) | {column})
    if merged == existing and entry:
        return existing
    contract[table] = {
        "excluded_columns": merged,
        "decided_by": decided_by,
        "at": at,
    }
    _write_json(path, {key: contract[key] for key in sorted(contract)})
    return merged


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _clear_panel(layout: WorkspaceLayout) -> None:
    """A panel that no longer matches the latest comparison must not linger."""
    for name in ("current.json", "current.md"):
        path = layout.reports_dir / PANEL_DIRNAME / name
        if path.exists():
            path.unlink()


__all__ = [
    "DECISIONS_FILENAME",
    "EXCLUSIONS_FILENAME",
    "FINDING_OPTION_IDS",
    "HUMAN_ONLY_OPTIONS",
    "OPTION_BLOCK",
    "OPTION_PROPAGATE",
    "OPTION_QUARANTINE",
    "PANEL_VERSION",
    "DriftPanelResult",
    "apply_drift_answer",
    "load_drift_panel",
    "load_schema_exclusions",
    "prepare_drift_panel",
    "render_markdown",
]

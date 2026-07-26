"""Read-only health report over a workspace's own state/audit files
(trajectory.jsonl, audit_chain.jsonl, session.json files under
workflow_sessions/*, run.log) -- surfaces unbounded growth before it becomes
a problem, since none of these files rotate today (see
docs/core_audit/PROD_SECURITY_GAPS.md Gap 7 residual risk 2, and the
explicitly-deferred rotation note in this session's P7 profiler/audit-chain
work: audit_chain.jsonl can't be naively truncated without a chain-continuity
design, so no automatic rotation exists yet -- this tool's job is visibility,
not remediation).

Discovery is glob-based over WorkspaceLayout.state_dir, not a hardcoded list
of the several independent modules that each write their own session.json
(flow.py, wiki_memory.py, generation_workflow.py, external_intake_workflow.py)
-- "derive don't curate": a new module that starts writing its own state file
under state_dir is picked up automatically, bucketed as "other" until named.

Read-only: never mutates, deletes, or rotates any scanned file. Only writes
its own current.json/current.md report artifacts, matching the convention
every other generated artifact pair in this repo already uses.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout

VERSION = 1

# Size threshold above which a file is flagged "large" -- a nudge to look
# closer, not a hard limit. This tool never deletes/rotates anything.
_LARGE_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
_STALE_DAYS = 90

# Filenames this tool knows the PURPOSE of, used to label the report
# meaningfully. Anything else under state_dir is still counted and shown,
# just bucketed as "other" -- no file is ever silently excluded.
_KNOWN_KINDS: dict[str, str] = {
    "trajectory.jsonl": "trajectory_log",
    "audit_chain.jsonl": "audit_chain",
    "trajectory_summary_state.json": "trajectory_counter",
    "session.json": "workflow_session",
    "run.log": "run_log",
    "workspace_settings.json": "workspace_settings",
}


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _classify(path: Path) -> str:
    return _KNOWN_KINDS.get(path.name, "other")


def _file_entry(path: Path, now: datetime, root: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    age_days = (now - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)).days
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = str(path)
    return {
        "path": rel,
        "kind": _classify(path),
        "size_bytes": stat.st_size,
        "size_human": _human_bytes(stat.st_size),
        "modified_days_ago": age_days,
        "large": stat.st_size >= _LARGE_FILE_BYTES,
        "stale": age_days >= _STALE_DAYS,
    }


def scan_workspace_state(layout: WorkspaceLayout, *, repo_root: Path) -> dict[str, Any]:
    """Read-only scan of ``layout.state_dir``. Never mutates anything."""
    now = datetime.now(timezone.utc)
    files: list[dict[str, Any]] = []
    if layout.state_dir.exists():
        for path in layout.state_dir.rglob("*"):
            if path.is_file():
                entry = _file_entry(path, now, repo_root)
                if entry is not None:
                    files.append(entry)

    files.sort(key=lambda f: f["size_bytes"], reverse=True)

    by_kind: dict[str, dict[str, Any]] = {}
    for f in files:
        bucket = by_kind.setdefault(f["kind"], {"count": 0, "total_bytes": 0})
        bucket["count"] += 1
        bucket["total_bytes"] += f["size_bytes"]
    for bucket in by_kind.values():
        bucket["total_bytes_human"] = _human_bytes(bucket["total_bytes"])

    session_files = [f for f in files if f["kind"] == "workflow_session"]
    total_bytes = sum(f["size_bytes"] for f in files)
    return {
        "total_bytes": total_bytes,
        "total_bytes_human": _human_bytes(total_bytes),
        "file_count": len(files),
        "by_kind": by_kind,
        "session_file_count": len(session_files),
        "stale_session_count": sum(1 for f in session_files if f["stale"]),
        "large_files": [f for f in files if f["large"]],
        "top_files": files[:15],
    }


@dataclass(frozen=True)
class StateHealthResult:
    workspace: str
    report_json_path: str
    report_markdown_path: str
    total_bytes: int
    large_file_count: int
    stale_session_count: int


def _render_markdown(workspace_rel: str, report: dict[str, Any]) -> str:
    lines = [
        "# Workspace State-File Health",
        "",
        f"- **Workspace:** `{workspace_rel}`",
        f"- **Generated at:** `{report['generated_at']}`",
        f"- **Total size:** {report['total_bytes_human']} across {report['file_count']} files",
        "",
        "## By kind",
        "",
        "| Kind | Files | Total size |",
        "| --- | --- | --- |",
    ]
    for kind, bucket in sorted(report["by_kind"].items(), key=lambda kv: -kv[1]["total_bytes"]):
        lines.append(f"| {kind} | {bucket['count']} | {bucket['total_bytes_human']} |")
    lines.append("")

    if report["large_files"]:
        lines += ["## Large files (>=10MB) -- nothing here rotates automatically today", ""]
        for f in report["large_files"]:
            lines.append(f"- `{f['path']}` ({f['size_human']}, {f['kind']})")
        lines.append("")

    if report["stale_session_count"]:
        lines.append(
            f"## {report['stale_session_count']} workflow_session file(s) untouched for "
            f"{_STALE_DAYS}+ days -- likely abandoned sessions accumulating on disk"
        )
        lines.append("")

    lines += ["## Top files by size", ""]
    lines.append("| Path | Size | Kind | Age (days) |")
    lines.append("| --- | --- | --- | --- |")
    for f in report["top_files"]:
        lines.append(f"| `{f['path']}` | {f['size_human']} | {f['kind']} | {f['modified_days_ago']} |")
    lines.append("")
    return "\n".join(lines)


def record_state_health(repo_root: str | Path, workspace: str | Path) -> StateHealthResult:
    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()
    if not workspace_path.exists():
        raise FileNotFoundError(f"workspace not found: {workspace}")
    layout = WorkspaceLayout(project_root=workspace_path)
    layout.ensure_runtime_dirs()
    try:
        workspace_rel = workspace_path.relative_to(repo_root).as_posix()
    except ValueError:
        workspace_rel = str(workspace_path)

    scan = scan_workspace_state(layout, repo_root=repo_root)
    report = {
        "artifact_type": "state_health/current.json",
        "version": VERSION,
        "generated_by": "workspace-state-health",
        "workspace": workspace_rel,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **scan,
    }
    report_dir = layout.reports_dir / "state_health"
    evidence_dir = layout.evidence_dir / "state_health"
    report_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_json = report_dir / "current.json"
    report_md = report_dir / "current.md"
    evidence_json = evidence_dir / "current.json"
    payload = json.dumps(report, indent=2, default=str) + "\n"
    report_json.write_text(payload, encoding="utf-8")
    evidence_json.write_text(payload, encoding="utf-8")
    report_md.write_text(_render_markdown(workspace_rel, report), encoding="utf-8")

    return StateHealthResult(
        workspace=workspace_rel,
        report_json_path=str(report_json.relative_to(repo_root).as_posix()),
        report_markdown_path=str(report_md.relative_to(repo_root).as_posix()),
        total_bytes=scan["total_bytes"],
        large_file_count=len(scan["large_files"]),
        stale_session_count=scan["stale_session_count"],
    )


def scan_all_workspaces(repo_root: str | Path) -> list[dict[str, Any]]:
    """Repo-wide rollup: scan every workspaces/* directory and return one
    summary dict per workspace, sorted largest-first. Read-only; does not
    write any artifact (there is no natural single-workspace home for a
    cross-workspace report) -- callers print/consume this directly."""
    repo_root = Path(repo_root).resolve()
    workspaces_dir = repo_root / "workspaces"
    if not workspaces_dir.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for entry in sorted(workspaces_dir.iterdir()):
        if not entry.is_dir():
            continue
        layout = WorkspaceLayout(project_root=entry)
        scan = scan_workspace_state(layout, repo_root=repo_root)
        summaries.append(
            {
                "workspace": entry.relative_to(repo_root).as_posix(),
                "total_bytes": scan["total_bytes"],
                "total_bytes_human": scan["total_bytes_human"],
                "file_count": scan["file_count"],
                "large_file_count": len(scan["large_files"]),
                "stale_session_count": scan["stale_session_count"],
            }
        )
    summaries.sort(key=lambda s: s["total_bytes"], reverse=True)
    return summaries


@anchored("workspace-state-health")
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Read-only health report over a workspace's own state/audit files."
    )
    parser.add_argument("--workspace", help="Single workspace to scan (relative to --repo-root)")
    parser.add_argument("--all-workspaces", action="store_true", help="Scan every workspaces/* and print a rollup")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    if args.all_workspaces:
        summaries = scan_all_workspaces(args.repo_root)
        print(json.dumps({"artifact_type": "state_health/rollup", "version": VERSION, "workspaces": summaries}, indent=2))
        return

    if not args.workspace:
        raise SystemExit("--workspace or --all-workspaces is required")
    result = record_state_health(args.repo_root, args.workspace)
    print(json.dumps(
        {
            "artifact_type": "state_health_result",
            "version": VERSION,
            "workspace": result.workspace,
            "report_json_path": result.report_json_path,
            "report_markdown_path": result.report_markdown_path,
            "total_bytes": result.total_bytes,
            "large_file_count": result.large_file_count,
            "stale_session_count": result.stale_session_count,
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()

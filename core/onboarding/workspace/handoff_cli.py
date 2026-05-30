"""CLI to fetch delegation handoff docs — the orchestrator->side-agent context bridge.

Delegations write a compact handoff to ``interns/state/handoffs/<stage>__<agent>.md``
(see core.onboarding.workspace.delegation). A side agent runs:

    uv run handoff latest --workspace workspaces/<project>            # newest handoff
    uv run handoff latest --workspace workspaces/<project> --stage kpi_output_verification
    uv run handoff render --workspace workspaces/<project> --stage <stage> --agent <agent>

so it reads its brief from a file instead of the orchestrator pasting it — keeping the
orchestrator's context lean.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core.storage.workspace_layout import WorkspaceLayout


def _handoffs_dir(repo_root: str | Path, workspace: str | Path) -> Path:
    layout = WorkspaceLayout(project_root=(Path(repo_root) / workspace).resolve())
    return layout.state_dir / "handoffs"


def _latest(directory: Path, stage: str) -> Path | None:
    if not directory.exists():
        return None
    pattern = f"{stage}*.md" if stage else "*.md"
    # Sort by nanosecond mtime, then by name as a deterministic tiebreaker: two files
    # written within the same coarse mtime tick must not resolve to an arbitrary
    # glob order (that made "latest" flaky). On an exact mtime tie the lexicographically
    # greater name wins.
    files = sorted(
        directory.glob(pattern),
        key=lambda f: (f.stat().st_mtime_ns, f.name),
        reverse=True,
    )
    return files[0] if files else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch delegation handoff documents.")
    sub = parser.add_subparsers(dest="command", required=True)

    latest = sub.add_parser("latest", help="Print the most recent handoff (optionally by stage).")
    latest.add_argument("--workspace", required=True)
    latest.add_argument("--repo-root", default=".")
    latest.add_argument("--stage", default="", help="Filter to handoffs for this stage prefix.")
    latest.add_argument("--path-only", action="store_true", help="Print the path, not the content.")

    render = sub.add_parser("render", help="Print a specific stage+agent handoff.")
    render.add_argument("--workspace", required=True)
    render.add_argument("--repo-root", default=".")
    render.add_argument("--stage", required=True)
    render.add_argument("--agent", required=True)

    args = parser.parse_args(argv)
    directory = _handoffs_dir(args.repo_root, args.workspace)

    if args.command == "latest":
        target = _latest(directory, args.stage)
        if target is None:
            print("(no handoff found)")
            return 1
        print(str(target) if args.path_only else target.read_text(encoding="utf-8"))
        return 0

    if args.command == "render":
        target = directory / f"{args.stage}__{args.agent}.md"
        if not target.exists():
            print(f"(no handoff: {target.name})")
            return 1
        print(target.read_text(encoding="utf-8"))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

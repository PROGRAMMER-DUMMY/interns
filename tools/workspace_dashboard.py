"""`workspace-dashboard` CLI: serve or export the per-workspace BI dashboard.

Generic across workspaces. Reads dashboard specs from
`workspaces/<ws>/dashboard/`, refreshes machine_defaults from the KPI
registry (preserving user_overrides), and either:

- starts a Dash app on the chosen port (default 8060), or
- writes static HTML to `workspaces/<ws>/dashboard/exports/` with --export.

The refresh happens on every invocation so the dashboard is always
consistent with the latest registry/spec changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.dashboard.export import export_static_html
from core.dashboard.renderer import build_dash_app
from core.dashboard.spec import refresh_workspace_dashboard
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace-dashboard")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--port",
        type=int,
        default=8060,
        help="Port for the live Dash server (default 8060). Ignored with --export.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the Dash server (default 127.0.0.1).",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Write static HTML to dashboard/exports/ and exit. Skips the live server.",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip the machine_defaults refresh. Use to render exactly what's on disk.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable refresh/export summary instead of human text.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    workspace = (repo_root / args.workspace).resolve()
    if not workspace.exists():
        print(f"workspace not found: {workspace}", file=sys.stderr)
        return 2
    layout = WorkspaceLayout(project_root=workspace)

    refresh_summary = None
    if not args.no_refresh:
        refresh_summary = refresh_workspace_dashboard(layout)

    if args.export:
        export_summary = export_static_html(repo_root, args.workspace)
        result = {"action": "export", "refresh": refresh_summary, "export": export_summary}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Dashboard exported to: {workspace}/{export_summary['export_dir']}/index.html")
            print(f"KPIs exported: {len(export_summary['files']) - 1}")
        return 0

    app = build_dash_app(repo_root, args.workspace)
    if args.json:
        print(
            json.dumps(
                {
                    "action": "serve",
                    "refresh": refresh_summary,
                    "url": f"http://{args.host}:{args.port}/",
                    "workspace": args.workspace,
                },
                indent=2,
            )
        )
    else:
        print(f"Workspace dashboard: http://{args.host}:{args.port}/ (workspace: {args.workspace})")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

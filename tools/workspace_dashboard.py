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
import hmac
import json
import os
import sys
from pathlib import Path

from core.dashboard.export import export_static_html
from core.dashboard.renderer import build_dash_app
from core.dashboard.spec import refresh_workspace_dashboard
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

DASHBOARD_TOKEN_ENV = "AUTORESEARCH_DASHBOARD_TOKEN"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_host(host: str) -> bool:
    """True when the bind host only accepts connections from this machine."""
    host = (host or "").strip().lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def attach_token_auth(app, token: str) -> None:
    """Require a bearer/query token on every request to the Dash app.

    Used when the server is bound to a non-loopback host: dashboards render
    row-level workspace data, which must not be open to the whole network.
    """
    from flask import abort, request

    @app.server.before_request
    def _require_dashboard_token():  # pragma: no cover - exercised via test client
        supplied = ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer "):]
        if not supplied:
            supplied = request.args.get("token", "")
        if not hmac.compare_digest(supplied, token):
            abort(401)


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
        "--live",
        action="store_true",
        help=(
            "Serve the live Power BI-style dashboard (core.dashboard.ui) read from "
            "the validated medallion gold layer, instead of the legacy renderer. "
            "Runs the gold parity gate first and refuses on failure unless --force."
        ),
    )
    parser.add_argument(
        "--theme",
        default="claude",
        help="Live dashboard theme: claude (light) or dark. Used with --live.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --live, serve even if the gold parity gate reports failures.",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Write static HTML to dashboard/exports/ and exit. Skips the live server.",
    )
    parser.add_argument(
        "--screen",
        action="store_true",
        help=(
            "Export, screenshot every page, run deterministic visual checks, "
            "and write interns/reports/dashboard_screener/. Exits nonzero on findings."
        ),
    )
    parser.add_argument(
        "--record-vision-review",
        action="store_true",
        help=(
            "Mark the latest screener report's vision review done (the agent/human "
            "looked at the staged screenshots). Empty --reviewed-by records source: agent."
        ),
    )
    parser.add_argument("--reviewed-by", default="", help="Reviewer name for --record-vision-review.")
    parser.add_argument("--notes", default="", help="Vision-review findings/notes.")
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

    if args.record_vision_review:
        from core.dashboard.screener import record_vision_review

        try:
            review = record_vision_review(
                repo_root, args.workspace,
                reviewed_by=args.reviewed_by, notes=args.notes,
            )
        except FileNotFoundError as exc:
            print(f"[x] {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"action": "record_vision_review", **review}, indent=2))
        else:
            print(f"[ok] vision review recorded (source: {review['source']})")
        return 0

    if args.screen:
        from core.dashboard.screener import screen_dashboard

        screen_summary = screen_dashboard(repo_root, args.workspace)
        if args.json:
            print(json.dumps({"action": "screen", **screen_summary}, indent=2))
        else:
            marker = "[ok]" if screen_summary["ok"] else "[x]"
            print(f"{marker} screener: {screen_summary['page_count']} pages, "
                  f"{screen_summary['error_count']} finding(s)")
            print(f"report: {screen_summary['report_md']}")
            print(f"screenshots (for agent vision review): {screen_summary['screenshot_dir']}")
        return 0 if screen_summary["ok"] else 1

    if args.export:
        export_summary = export_static_html(repo_root, args.workspace)
        result = {"action": "export", "refresh": refresh_summary, "export": export_summary}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Dashboard exported to: {workspace}/{export_summary['export_dir']}/index.html")
            print(f"KPIs exported: {len(export_summary['files']) - 1}")
        return 0

    token = os.environ.get(DASHBOARD_TOKEN_ENV, "")
    if not is_loopback_host(args.host) and not token:
        print(
            f"refused: --host {args.host} exposes row-level workspace data to the "
            f"network without authentication. Set {DASHBOARD_TOKEN_ENV} to a strong "
            "token to enable token-gated non-loopback serving, or keep the default "
            "--host 127.0.0.1.",
            file=sys.stderr,
        )
        return 2

    if args.live:
        # Generate a vendored-MinusAnalyst project + certified-clean data for this
        # workspace, then launch the real MinusAnalyst app on it (DQ-gated).
        from core.dashboard.minus_adapter import generate, minus_root

        res = generate(layout, force=args.force)
        if not res.get("ok"):
            print(f"[x] {res.get('reason')}; refusing to serve the live dashboard "
                  "(use --force to override).", file=sys.stderr)
            for chk in (res.get("failed") or []):
                print(f"    - {chk.get('check')}: {chk.get('detail')}", file=sys.stderr)
            return 1
        if res.get("force"):
            print("[~] published with --force despite DQ findings.", file=sys.stderr)

        vendor_dir = repo_root / "vendor"
        if str(vendor_dir) not in sys.path:
            sys.path.insert(0, str(vendor_dir))
        from minus.render.app import create_app  # vendored MinusAnalyst

        app, state = create_app(minus_root(layout))
        if not is_loopback_host(args.host):
            attach_token_auth(app, token)
            print(
                f"non-loopback bind: requests require Authorization: Bearer "
                f"<{DASHBOARD_TOKEN_ENV}> (or ?token=...)."
            )
        print(f"Live dashboard (MinusAnalyst): http://{args.host}:{args.port}/ "
              f"(workspace: {args.workspace}, {len(state.pages)} pages, "
              f"{res['rows']} rows)")
        app.run(host=args.host, port=args.port, debug=False)
        return 0

    app = build_dash_app(repo_root, args.workspace)
    if not is_loopback_host(args.host):
        attach_token_auth(app, token)
        print(
            f"non-loopback bind: requests require Authorization: Bearer <{DASHBOARD_TOKEN_ENV}> "
            "(or ?token=...)."
        )
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

"""`workspace-dashboard` CLI: serve / refresh / screen the live MinusAnalyst dashboard.

Generic across workspaces. The per-workspace dashboard IS the vendored
MinusAnalyst app (Power BI-style), built on a DQ-certified conformed model from
the medallion gold layer. The legacy static-HTML export + its Dash renderer were
removed; the live app is the single deliverable.

Modes:
- (default) / --live : generate the MinusAnalyst project + certified data, then
  serve it on a Dash server (loopback by default).
- --refresh          : regenerate the project + data and exit (for a scheduler).
- --screen           : serve headless, screenshot every page, run visual checks,
  write interns/reports/dashboard_screener/, exit nonzero on findings.
- --record-vision-review : mark the latest screener report's vision review done.

The machine_defaults spec refresh still runs each invocation (preserving
user_overrides) because the MinusAnalyst panel selection reads that per-KPI spec.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import hmac
import json
import os
import sys
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Server lifecycle: a per-workspace PID file lets `--stop` find and terminate a
# running live server without the user hunting PIDs by hand. Generic + cross
# platform (Windows taskkill / POSIX signals); a port scan is the fallback for
# orphaned servers started without a PID file.
# ---------------------------------------------------------------------------


def _pidfile(layout: WorkspaceLayout) -> Path:
    # Same location as the MinusAnalyst root (gitignored); no heavy import needed.
    return layout.state_dir / "minus" / ".dashboard_server.json"


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _terminate_pid(pid: int) -> bool:
    try:
        if os.name == "nt":
            import subprocess
            return subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                  capture_output=True, text=True).returncode == 0
        import signal
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def _pids_on_port(port: int) -> list[int]:
    """PIDs listening on ``port`` (best-effort; covers servers with no PID file)."""
    import subprocess
    pids: set[int] = set()
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if "LISTENING" in line and f":{port} " in line:
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        pids.add(int(parts[-1]))
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout
            pids.update(int(p) for p in out.split() if p.strip().isdigit())
    except Exception:
        pass
    return sorted(pids)


def _write_pidfile(layout: WorkspaceLayout, host: str, port: int) -> Path:
    pf = _pidfile(layout)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port}),
                  encoding="utf-8")
    return pf


def _stop_server(layout: WorkspaceLayout, port: int) -> int:
    """Terminate the live server for this workspace (PID file first, then a scan
    of ``port`` for orphans). Idempotent: a no-op when nothing is running."""
    killed: list[int] = []
    pf = _pidfile(layout)
    if pf.exists():
        try:
            info = json.loads(pf.read_text(encoding="utf-8"))
            pid = int(info.get("pid"))
            port = int(info.get("port", port))
        except Exception:
            pid = 0
        if pid and _pid_alive(pid) and _terminate_pid(pid):
            killed.append(pid)
        try:
            pf.unlink()
        except OSError:
            pass
    for pid in _pids_on_port(port):
        if pid not in killed and _terminate_pid(pid):
            killed.append(pid)
    if killed:
        print(f"[ok] stopped dashboard server(s): {', '.join(map(str, killed))}")
    else:
        print(f"[~] no running dashboard server found (workspace pidfile + port {port})")
    return 0



@anchored("workspace-dashboard")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace-dashboard")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--port",
        type=int,
        default=8060,
        help="Port for the live Dash server (default 8060).",
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
            "Serve the live MinusAnalyst dashboard (the default action). Kept for "
            "backward compatibility; serving live is now the default when no other "
            "mode flag is given."
        ),
    )
    parser.add_argument(
        "--theme",
        default="claude",
        help="Live dashboard theme: claude (light) or dark.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With serve/--refresh, publish even if DQ certification reports failures.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Regenerate the MinusAnalyst project + certified-clean data for the "
            "workspace and exit. DQ-gated: on failure nothing is rewritten "
            "(last-good snapshot stays). For a scheduler to call after new data lands."
        ),
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help=(
            "Stop the running live server for this workspace (PID file, with a "
            "fallback scan of --port for orphans) and exit. Idempotent."
        ),
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=0,
        help=(
            "When serving, make the running dashboard re-read its data every N "
            "seconds (auto-pick-up of refreshed data). 0 = off."
        ),
    )
    parser.add_argument(
        "--screen",
        action="store_true",
        help=(
            "Serve headless, screenshot every page, run deterministic visual checks, "
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
        help="Skip the machine_defaults spec refresh. Use to render exactly what's on disk.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable refresh/screen summary instead of human text.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    workspace = (repo_root / args.workspace).resolve()
    if not workspace.exists():
        print(f"workspace not found: {workspace}", file=sys.stderr)
        return 2
    layout = WorkspaceLayout(project_root=workspace)

    # Stop is a pure lifecycle action: no regenerate, no serve.
    if args.stop:
        return _stop_server(layout, args.port)

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

    # Generate the MinusAnalyst project + certified-clean data (DQ-gated).
    from core.dashboard.minus_adapter import generate, minus_root

    res = generate(layout, force=args.force, refresh_seconds=args.refresh_seconds)

    if args.refresh:
        # Regenerate-and-exit (for a scheduled job after the pipeline lands new gold).
        if args.json:
            print(json.dumps({"action": "refresh", **res}, indent=2, default=str))
        elif res.get("ok"):
            print(f"[ok] refreshed: {res['rows']} rows, pages {res['pages']} "
                  f"(certified={res['certified']})")
        else:
            print(f"[x] refresh blocked: {res.get('reason')} (last-good kept; "
                  "use --force to override).", file=sys.stderr)
            for chk in (res.get("failed") or []):
                print(f"    - {chk.get('check')}: {chk.get('detail')}", file=sys.stderr)
        return 0 if res.get("ok") else 1

    # Default action: serve the live MinusAnalyst app.
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
    # Record this server so `--stop` can find and terminate it; clean up on exit.
    import atexit
    pidfile = _write_pidfile(layout, args.host, args.port)
    atexit.register(lambda: pidfile.unlink(missing_ok=True))

    print(f"Live dashboard (MinusAnalyst): http://{args.host}:{args.port}/ "
          f"(workspace: {args.workspace}, {len(state.pages)} pages, "
          f"{res['rows']} rows)")
    print(f"Stop with: uv run workspace-dashboard --workspace {args.workspace} --stop")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

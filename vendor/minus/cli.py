"""``minus`` command-line interface.

Commands:
  run       Launch the dashboard app.
  validate  Validate project.yaml + dashboards against the schema.
  schema    Export the JSON Schema (the agent's contract).
  new       Scaffold a project from a folder of CSVs (--from-data).
  edit      Ask your CLI agent to change the config (headless bridge).
  mcp       Serve the project to MCP clients over stdio.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows consoles default to cp1252; force UTF-8 so output never crashes.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

OK = "[ok]"
NO = "[x]"


def _cmd_run(args) -> int:
    from minus.render.app import create_app
    app, state = create_app(args.root)
    print(f"MinusAnalyst · {state.project.name}")
    print(f"  {len(state.pages)} page(s), {len(state.project.tables)} table(s)")
    print(f"  http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def _cmd_validate(args) -> int:
    from minus.config.loader import ConfigError, load_dashboards, load_project
    try:
        project = load_project(args.root)
        pages = load_dashboards(args.root, project)
    except ConfigError as exc:
        print(f"{NO} {exc}", file=sys.stderr)
        return 1
    print(f"{OK} project.yaml OK - {len(project.tables)} tables, "
          f"{len(project.measures)} measures, {len(pages)} dashboards")
    for p in pages:
        print(f"  - {p.id}: {len(p.widgets)} widgets, {len(p.filters)} filters")
    return 0


def _cmd_schema(args) -> int:
    from minus.config.loader import export_schema
    out = Path(args.out)
    export_schema(out)
    print(f"{OK} wrote JSON Schema -> {out}")
    return 0


def _cmd_new(args) -> int:
    from minus.scaffold import write_scaffold
    try:
        path = write_scaffold(args.from_data, args.root, force=args.force)
    except FileExistsError as exc:
        print(f"{NO} {exc}", file=sys.stderr)
        return 1
    # also (re)export the schema so the agent has the contract handy
    from minus.config.loader import export_schema
    export_schema(Path("minus/config/schema.json"))
    print(f"{OK} scaffolded project from {args.from_data}")
    print(f"  -> {path}")
    print("  run it with:  uv run minus run")
    return 0


def _cmd_edit(args) -> int:
    from minus.agent.bridge import build_prompt, run_agent
    from minus.config.loader import load_project
    from minus.data.model import SemanticModel
    request = " ".join(args.request).strip()
    if not request:
        print(f"{NO} provide a request, e.g. minus edit \"add a pie of amount by dept\"",
              file=sys.stderr)
        return 1
    project = load_project(args.root)
    model = SemanticModel(project, args.root)
    prompt = build_prompt(args.root, project, request, page_id=args.page, model=model)
    result = run_agent(project, args.root, prompt, dry_run=args.dry_run)
    print(result.message)
    return 0 if result.ok else 1


def _cmd_mcp(args) -> int:
    from minus.mcp.server import build_server
    server = build_server(args.root)
    print(f"{OK} MinusAnalyst MCP server on stdio (root={args.root})", file=sys.stderr)
    server.run()  # FastMCP stdio transport (blocks)
    return 0


def _cmd_alerts(args) -> int:
    import time

    from minus.notify import deliver, evaluate_alerts
    from minus.render.app import AppState
    st = AppState(args.root)

    def once() -> int:
        fired = evaluate_alerts(st.project, st.engine)
        if fired:
            status = deliver(st.project.notify, [f.line() for f in fired], args.root)
            print(f"{OK} {len(fired)} alert(s) firing — {status}")
        else:
            print(f"{OK} no alerts firing ({len(st.project.alerts)} checked)")
        return len(fired)

    if args.watch:
        print(f"watching alerts every {args.watch}s (Ctrl+C to stop)")
        try:
            while True:
                st.model.clear_cache()   # re-read source data each cycle
                once()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("stopped")
        return 0
    once()
    return 0


def _cmd_report(args) -> int:
    from minus.notify import snapshot
    from minus.render.app import AppState
    st = AppState(args.root)
    paths = snapshot(st.project, st.pages, st.engine, args.out)
    print(f"{OK} wrote {len(paths)} CSV snapshot(s) -> {args.out}")
    return 0


def _cmd_pptx(args) -> int:
    from minus_deck import build_deck
    res = build_deck(args.root, args.out, theme=args.theme, charts=args.charts)
    print(f"{OK} MinusDeck -> {res['path']} ({res['slides']} slides, {res['pages']} pages, "
          f"{res['native_charts']} native charts)")
    return 0


def _cmd_pdf(args) -> int:
    from minus_press import build_pdf
    res = build_pdf(args.root, args.out, theme=args.theme)
    print(f"{OK} MinusPress -> {res['path']} ({res['pages']} pages, {res['charts']} charts)")
    return 0


def _cmd_refresh(args) -> int:
    from minus.render.app import AppState
    st = AppState(args.root)
    st.model.clear_cache()
    for t in st.project.tables:
        print(f"  {t.name}: {len(st.model.table_df(t.name)):,} rows")
    print(f"{OK} data refreshed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minus", description=__doc__.splitlines()[0])
    p.add_argument("--root", default=".", help="Project root (holds project.yaml).")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Launch the dashboard app.")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8050)
    r.add_argument("--debug", action="store_true")
    r.set_defaults(func=_cmd_run)

    v = sub.add_parser("validate", help="Validate the config.")
    v.set_defaults(func=_cmd_validate)

    s = sub.add_parser("schema", help="Export the JSON Schema.")
    s.add_argument("--out", default="minus/config/schema.json")
    s.set_defaults(func=_cmd_schema)

    n = sub.add_parser("new", help="Scaffold from a CSV folder.")
    n.add_argument("--from-data", dest="from_data", required=True,
                   help="Folder of CSV files to introspect.")
    n.add_argument("--force", action="store_true", help="Overwrite existing project.yaml.")
    n.set_defaults(func=_cmd_new)

    e = sub.add_parser("edit", help="Ask your CLI agent to change the config.")
    e.add_argument("request", nargs="+", help="Plain-English change request.")
    e.add_argument("--page", default=None, help="Dashboard id being edited.")
    e.add_argument("--dry-run", action="store_true", help="Print the command, don't run it.")
    e.set_defaults(func=_cmd_edit)

    m = sub.add_parser("mcp", help="Serve the project to MCP clients over stdio.")
    m.set_defaults(func=_cmd_mcp)

    a = sub.add_parser("alerts", help="Evaluate threshold alerts and notify.")
    a.add_argument("--watch", type=int, default=0,
                   help="Re-check every N seconds (built-in scheduler). 0 = once.")
    a.set_defaults(func=_cmd_alerts)

    rp = sub.add_parser("report", help="Export every widget's data to CSV (a snapshot).")
    rp.add_argument("--out", default="snapshots", help="Output directory.")
    rp.set_defaults(func=_cmd_report)

    rf = sub.add_parser("refresh", help="Re-read source data and report row counts.")
    rf.set_defaults(func=_cmd_refresh)

    pp = sub.add_parser("pptx", help="Export an interactive PowerPoint (MinusDeck).")
    pp.add_argument("--out", default="MinusDeck.pptx", help="Output .pptx path.")
    pp.add_argument("--theme", default=None, help="Override theme (claude / dark).")
    pp.add_argument("--charts", choices=["native", "image"], default="native",
                    help="native = editable Excel-backed charts; image = dashboard images.")
    pp.set_defaults(func=_cmd_pptx)

    pd = sub.add_parser("pdf", help="Export a rich PDF (MinusPress).")
    pd.add_argument("--out", default="MinusPress.pdf", help="Output .pdf path.")
    pd.add_argument("--theme", default=None, help="Override theme (claude / dark).")
    pd.set_defaults(func=_cmd_pdf)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

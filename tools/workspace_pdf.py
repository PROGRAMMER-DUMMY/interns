"""``workspace-dashboard-pdf``: export a workspace's live MinusAnalyst dashboard
to a rich PDF (the MinusPress vendor).

Reads the same certified conformed model the live dashboard serves, then renders
a print-quality .pdf with an interactive outline (bookmarks) and clickable
contents links: a cover, a section per page (KPI scorecard + every chart
full-width with the data behind it + the analysis tables). Charts are the exact
figures the live app draws.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import sys
from pathlib import Path

from core.paths import PROJECT_ROOT
from tools.dashboard_export_common import (
    add_vendor_to_path,
    default_out,
    open_file,
    prepare_minus_root,
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass



@anchored("workspace-dashboard-pdf")
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="workspace-dashboard-pdf",
        description="Rich PDF export (MinusPress) of the live dashboard.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--repo-root", default=str(PROJECT_ROOT))
    ap.add_argument("--out", default=None, help="Output .pdf (default dashboard/exports/<name>.pdf).")
    ap.add_argument("--theme", default=None, help="Override theme (claude / dark).")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate + export even if DQ certification reports failures.")
    ap.add_argument("--no-refresh", action="store_true",
                    help="Skip regeneration; export the last-generated dashboard as-is.")
    ap.add_argument("--open", dest="open_after", action="store_true",
                    help="Open the .pdf when done.")
    ap.add_argument("--screen", action="store_true",
                    help="Build, rasterize every page, run deterministic checks, and "
                         "stage screenshots for vision review. Exits nonzero on findings.")
    ap.add_argument("--record-vision-review", action="store_true",
                    help="Mark the latest export-screener report's vision review done.")
    ap.add_argument("--reviewed-by", default="", help="Reviewer name (empty = agent).")
    ap.add_argument("--notes", default="", help="Vision-review notes/findings.")
    ap.add_argument("--json", action="store_true", help="Machine-readable summary.")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    workspace = (repo_root / args.workspace).resolve()

    if args.record_vision_review:
        from core.dashboard.export_screener import record_vision_review
        try:
            review = record_vision_review(repo_root, args.workspace,
                                          reviewed_by=args.reviewed_by, notes=args.notes)
        except FileNotFoundError as exc:
            print(f"[x] {exc}", file=sys.stderr)
            return 2
        print(f"[ok] export vision review recorded (source: {review['source']})")
        return 0

    if args.screen:
        from core.dashboard.export_screener import screen_exports
        summary = screen_exports(repo_root, args.workspace, kinds=("pdf",),
                                 theme=args.theme, force=args.force)
        if args.json:
            print(json.dumps({"action": "screen", **summary}, indent=2))
        else:
            marker = "[ok]" if summary["ok"] else "[x]"
            print(f"{marker} export screener: {summary['artifact_count']} artifact(s), "
                  f"{summary['error_count']} finding(s)")
            print(f"report: {summary['report_md']}")
            print(f"rendered pages (for agent vision review): {summary['screenshot_dir']}")
        return 0 if summary["ok"] else 1

    try:
        _layout, root, _gen, warnings = prepare_minus_root(
            repo_root, args.workspace, force=args.force, no_refresh=args.no_refresh)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else default_out(workspace, "pdf")
    add_vendor_to_path(repo_root)
    from minus_press import build_pdf

    res = build_pdf(root, out, theme=args.theme)
    for w in warnings:
        print(f"[~] {w}", file=sys.stderr)
    if args.json:
        print(json.dumps({"action": "pdf", **res, "warnings": warnings}, indent=2))
    else:
        print(f"[ok] MinusPress PDF -> {res['path']}")
        print(f"     {res['pages']} pages, {res['charts']} charts, {res['tables']} tables, "
              f"{res['kpis']} KPIs, theme {res['theme']}")
    if args.open_after:
        open_file(res["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

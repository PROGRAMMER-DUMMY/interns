"""
core/medallion/lineage_cli.py — `uv run medallion-lineage` CLI (P5).

Subcommands:
  trace   Trace a Gold column back to its Bronze sources.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.medallion.lineage import Lineage
from core.storage.workspace_layout import WorkspaceLayout


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="medallion-lineage",
        description="Query the medallion column-level lineage graph.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    trace = sub.add_parser("trace", help="Trace a column back to its source CSV columns.")
    trace.add_argument("--workspace", required=True,
                       help="Workspace path, e.g. workspaces/Healthcare-RCM-Data-Platform")
    trace.add_argument("--repo-root", default=None)
    trace.add_argument(
        "--column", required=True,
        help="Fully-qualified column, e.g. gold.fact_claim.payor_id_hash",
    )
    trace.add_argument("--json", action="store_true", help="Emit JSON output.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "trace":
        return _trace(args)
    return 1


def _trace(args) -> int:
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().resolve()
    workspace_path = (
        (repo_root / args.workspace).resolve()
        if not Path(args.workspace).is_absolute()
        else Path(args.workspace).resolve()
    )
    layout = WorkspaceLayout(project_root=workspace_path)
    lineage_path = layout.generated_dir / "medallion" / "lineage.json"

    if not lineage_path.exists():
        print(f"[medallion-lineage] lineage.json not found at {lineage_path}", file=sys.stderr)
        print("  Run: uv run design-medallion --workspace ...", file=sys.stderr)
        return 1

    data = json.loads(lineage_path.read_text(encoding="utf-8"))
    lineage = Lineage.from_dict(data)

    parts = args.column.split(".", 2)
    if len(parts) != 3:
        print(
            f"[medallion-lineage] --column must be layer.table.column, got: {args.column}",
            file=sys.stderr,
        )
        return 1

    layer, table, col = parts
    sources = lineage.trace_to_sources(layer, table, col)

    if getattr(args, "json", False):
        print(json.dumps({"column": args.column, "sources": [
            {"node": n, "column": c} for n, c in sources
        ]}, indent=2))
    else:
        print(args.column)
        if sources:
            for node, src_col in sources:
                print(f"  <- {node}.{src_col}")
        else:
            print("  (no sources found — lineage may be placeholder-level only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

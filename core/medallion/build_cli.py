"""
core/medallion/build_cli.py — `uv run build-medallion` CLI.

Mirrors design_cli.py in structure and help style.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.medallion.build import (
    build_medallion,
    MedallionBuildExit,
    EXIT_MEDALLION_BUILD_FAIL,
    EXIT_WORKSPACE_BUSY,
    EXIT_SQL_LINT_FAIL,
    EXIT_DESIGN_BLOCKERS,
)


_DESCRIPTION = (
    "Execute the Bronze -> Silver -> Gold medallion pipeline for a workspace. "
    "Reads the manifest produced by design-medallion, executes SQL in dependency "
    "order on DuckDB, runs Silver assertions, regenerates KPI SQL against Gold, "
    "and writes a per-run state file under interns/state/medallion/runs/<run_id>/."
)

_EPILOG = """\
examples:
  # full pipeline run:
  uv run build-medallion --workspace workspaces/Healthcare-RCM-Data-Platform

  # bronze only (skip silver/gold/kpi):
  uv run build-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --only-layer bronze

  # single table:
  uv run build-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --only-table patient

  # resume a partial run:
  uv run build-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --resume 20260516T103045Z-7bcda0fa

  # proceed even if design panel has unresolved entries (use carefully):
  uv run build-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --force-with-blockers

exit codes:
  0                      success
  MEDALLION_BUILD_FAIL   a pipeline stage failed (see run.json for details)
  WORKSPACE_BUSY         another build-medallion is running for this workspace
  SQL_LINT_FAIL          a SQL file failed lint checks before execution
  DESIGN_BLOCKERS        unresolved design panel entries (pass --force-with-blockers to override)
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build-medallion",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workspace", required=True,
        help="Workspace path relative to repo root, e.g. workspaces/Healthcare-RCM-Data-Platform",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="Repository root. Defaults to current working directory.",
    )
    parser.add_argument(
        "--target", default=None, choices=["duckdb", "delta", "auto"],
        help="Override manifest target substrate (P2 makes delta/auto real).",
    )
    parser.add_argument(
        "--only-layer", default=None, choices=["bronze", "silver", "gold", "kpi"],
        help="Restrict execution to one layer.",
    )
    parser.add_argument(
        "--only-table", default=None, metavar="NAME",
        help="Restrict execution to one table within the selected layer.",
    )
    parser.add_argument(
        "--resume", default=None, metavar="RUN_ID",
        help="Resume a partial run from state/medallion/runs/<RUN_ID>/.",
    )
    parser.add_argument(
        "--force-with-blockers", action="store_true",
        help="Proceed even if the design panel has unresolved entries. Logs a warning.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the result as a single JSON object (for piping).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().resolve()
    workspace = (
        (repo_root / args.workspace).resolve()
        if not Path(args.workspace).is_absolute()
        else Path(args.workspace).resolve()
    )

    if args.force_with_blockers:
        print(
            "[build-medallion] WARNING: --force-with-blockers set; "
            "proceeding with unresolved design panel entries.",
            file=sys.stderr,
        )

    cfg = None
    try:
        import core.config as core_config
        cfg = core_config.load()
    except Exception:
        pass

    try:
        state = build_medallion(
            workspace=workspace,
            repo_root=repo_root,
            cfg=cfg,
            only_layer=args.only_layer,
            only_table=args.only_table,
            resume=args.resume,
            force_with_blockers=args.force_with_blockers,
        )
    except MedallionBuildExit as exc:
        _emit_exit(exc, json_mode=args.json)
        return _exit_code_for(exc.code)

    if args.json:
        print(json.dumps(state.to_dict(), indent=2))
    else:
        _emit_human(state, workspace, repo_root)
    return 0


def _emit_human(state, workspace: Path, repo_root: Path) -> None:
    ws_label = str(workspace.relative_to(repo_root)) if workspace.is_relative_to(repo_root) else str(workspace)
    ok_count = sum(1 for t in state.per_table_status.values() if t.status == "ok")
    fail_count = sum(1 for t in state.per_table_status.values() if t.status == "failed")

    print(f"[build-medallion] workspace: {ws_label}")
    print(f"  run_id:          {state.run_id}")
    print(f"  target:          {state.target_actual}")
    print(f"  elapsed:         {state.elapsed_seconds:.1f}s")
    print(f"  tables_ok:       {ok_count}")
    print(f"  tables_failed:   {fail_count}")
    print(f"  degraded_run:    {state.degraded_run}")
    if state.kpi_diff:
        unequal = [k for k, v in state.kpi_diff.items() if not v.get("equal", True)]
        print(f"  kpi_diff_total:  {len(state.kpi_diff)} KPIs  ({len(unequal)} unequal)")
    if state.blocker_entries_added:
        print(f"  blockers_added:  {state.blocker_entries_added}")
    if fail_count == 0:
        print("\n  Build succeeded.")
    else:
        print("\n  Build FAILED — see run.json for details.")


def _emit_exit(exc: MedallionBuildExit, *, json_mode: bool) -> None:
    payload = {
        "ok": False,
        "exit_code": exc.code,
        "message": exc.message,
        "next_command": exc.next_command,
    }
    if json_mode:
        print(json.dumps(payload, indent=2))
    else:
        print(f"[build-medallion] {exc.code}: {exc.message}", file=sys.stderr)
        if exc.next_command:
            print(f"  next: {exc.next_command}", file=sys.stderr)


def _exit_code_for(code: str) -> int:
    return {
        EXIT_MEDALLION_BUILD_FAIL: 1,
        EXIT_WORKSPACE_BUSY:       6,
        EXIT_SQL_LINT_FAIL:        7,
        EXIT_DESIGN_BLOCKERS:      8,
    }.get(code, 1)


if __name__ == "__main__":
    sys.exit(main())

"""
core/medallion/design_cli.py — `uv run design-medallion` CLI.

Argparse style mirrors `uv run onboard-workspace`. Help text includes
example invocations and the exit-codes table (Section 19 of the PRD).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.paths import PROJECT_ROOT

import core.config as core_config
from core.agents.registry import InternRegistry
from core.medallion.design import (
    design_medallion,
    MedallionExit,
    EXIT_RUN_ONBOARD_FIRST,
    EXIT_NO_KPIS_DEFINED,
    EXIT_KPI_BLOCKERS_UNRESOLVED,
    EXIT_EMPTY_WORKSPACE,
    EXIT_WORKSPACE_BUSY,
    EXIT_BUDGET_EXCEEDED,
    EXIT_MODEL_SEARCH_FAILED,
    EXIT_INSUFFICIENT_MODEL_CAPABILITY,
)


_DESCRIPTION = (
    "Design the Bronze/Silver/Gold pipeline for a workspace. Reads onboarding "
    "artifacts (domain_model.json, kpi_registry.json, kpi_feature_mapping.json, "
    "profiles, derived_feature_reviews, semantic_contract, workspace_feature_definitions) "
    "and emits manifest.yaml + star_schema.{json,md} + silver_contract.{json,md} + "
    "lineage.{json,md} + per-target SQL files under "
    "workspaces/<ws>/interns/generated/medallion/."
)


_EPILOG = """\
examples:
  # cheap dry-run (computes inputs_hash, no LLM, no writes):
  uv run design-medallion --workspace workspaces/<your-workspace> --cheap --dry-run

  # full design pass against an LLM-backed agent (default):
  uv run design-medallion --workspace workspaces/<your-workspace>

  # force regeneration even if inputs_hash matches the prior manifest:
  uv run design-medallion --workspace workspaces/<your-workspace> --force

exit codes:
  0                          success
  RUN_ONBOARD_FIRST          missing onboarding artifacts; run onboard-workspace first
  NO_KPIS_DEFINED            kpi_registry.json missing or empty
  KPI_BLOCKERS_UNRESOLVED    KPI blocker panel has unresolved entries
  EMPTY_WORKSPACE            no source datasets
  WORKSPACE_BUSY             lockfile held by another run
  BUDGET_EXCEEDED            max_usd_per_run hit mid-run
  MODEL_SEARCH_FAILED        --no-search requested but no cached model classification exists
  INSUFFICIENT_MODEL_CAPABILITY no discovered tier can satisfy a required design task
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="design-medallion",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workspace", required=True,
        help="Workspace path relative to repo root, e.g. workspaces/<your-workspace>",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="Repository root. Defaults to detected project root.",
    )
    parser.add_argument(
        "--cheap", action="store_true",
        help="Skip the LLM call; use the deterministic seed proposal "
             "(useful for offline iteration and unit tests).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute inputs_hash and report what would change; do not write any files.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate even if inputs_hash matches the prior manifest.",
    )
    parser.add_argument(
        "--engine", default=None,
        choices=["api", "gemini-cli", "claude-code", "codex"],
        help="Override the LLM engine for this run. Defaults to config/lock.toml.",
    )
    parser.add_argument(
        "--model", default=None,
        help="Pin a specific model id (skips discovery, validated against the active engine).",
    )
    parser.add_argument(
        "--no-search", action="store_true",
        help="Use only cached model classifications; fail if the requested model is unfamiliar.",
    )
    parser.add_argument(
        "--calibrate", action="store_true",
        help="Record calibration intent in model routing metadata for this design run.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the result as a single JSON object (for piping).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else PROJECT_ROOT
    workspace = (repo_root / args.workspace).resolve() if not Path(args.workspace).is_absolute() else Path(args.workspace).resolve()

    intern = None
    if not args.cheap:
        try:
            cfg = core_config.load()
            registry = InternRegistry(cfg)
            intern = registry.get_intern("medallion_architect")
        except Exception as exc:
            print(f"[design-medallion] LLM intern unavailable ({exc}); falling back to seed proposal.", file=sys.stderr)
            intern = None

    try:
        result = design_medallion(
            workspace=workspace,
            repo_root=repo_root,
            intern=intern,
            cheap=args.cheap,
            dry_run=args.dry_run,
            force=args.force,
            engine=args.engine,
            model=args.model,
            no_search=args.no_search,
            calibrate=args.calibrate,
        )
    except MedallionExit as exc:
        _emit_exit(exc, json_mode=args.json)
        return _exit_code_for(exc.code)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _emit_human(result)
    return 0


def _emit_human(result) -> None:
    if result.cache_hit:
        print("[design-medallion] inputs_hash matches prior manifest — nothing to do.")
        print(f"  manifest: {result.manifest_path}")
        return
    print(f"[design-medallion] workspace: {result.workspace}")
    print(f"  inputs_hash:           {result.inputs_hash}")
    print(f"  manifest:              {result.manifest_path}")
    print(f"  star_schema:           {result.star_schema_path}")
    print(f"  silver_contract:       {result.silver_contract_path}")
    print(f"  lineage:               {result.lineage_path}")
    print(f"  bronze_sql_count:      {len(result.bronze_files)}")
    print(f"  silver_sql_count:      {len(result.silver_files)}")
    print(f"  llm_used:              {result.llm_used}")
    if result.unconfirmed_decisions:
        print(f"  unconfirmed_decisions: {len(result.unconfirmed_decisions)}")
        print(f"  design_panel:          {result.design_panel_path}")
        print("")
        print(f"  Next: review {result.design_panel_path} and ratify open decisions before build-medallion.")
    else:
        print("  unconfirmed_decisions: 0")
        print("")
        print(f"  Next: uv run build-medallion --workspace {result.workspace}")


def _emit_exit(exc: MedallionExit, *, json_mode: bool) -> None:
    payload = {
        "ok": False,
        "exit_code": exc.code,
        "message": exc.message,
        "next_command": exc.next_command,
    }
    if json_mode:
        print(json.dumps(payload, indent=2))
    else:
        print(f"[design-medallion] {exc.code}: {exc.message}", file=sys.stderr)
        if exc.next_command:
            print(f"  next: {exc.next_command}", file=sys.stderr)


def _exit_code_for(code: str) -> int:
    # POSIX-compatible numeric codes; the symbolic code is the diagnostic surface.
    mapping = {
        EXIT_RUN_ONBOARD_FIRST:       2,
        EXIT_NO_KPIS_DEFINED:         3,
        EXIT_KPI_BLOCKERS_UNRESOLVED: 4,
        EXIT_EMPTY_WORKSPACE:         5,
        EXIT_WORKSPACE_BUSY:          6,
        EXIT_BUDGET_EXCEEDED:         7,
        EXIT_MODEL_SEARCH_FAILED:     8,
        EXIT_INSUFFICIENT_MODEL_CAPABILITY: 9,
    }
    return mapping.get(code, 1)


if __name__ == "__main__":
    sys.exit(main())

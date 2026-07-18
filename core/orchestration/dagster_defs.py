"""Dagster software-defined asset graph over the medallion+KPI+dashboard pipeline.

The `STAGES` topology (pipeline_stages.py) is rendered into one Dagster asset per
stage, with `deps` wiring the dependency graph. This gives the thing the bespoke
CLI chain lacks: a single dependency-ordered trigger, stale-only
re-materialization, retries, run history, and freshness/observability -- without
rewriting the transforms (each asset runs the SAME governed command an operator
runs today).

Dagster is optional. Import is graceful: without `dagster` installed this module
still imports (documenting the shape) and the plain `run_pipeline()` sequential
runner works. With it installed, `dagster dev -m core.orchestration.dagster_defs`
serves the asset graph.

    pip install dagster
    AUTORESEARCH_PIPELINE_WORKSPACE=workspaces/<ws> dagster dev -m core.orchestration.dagster_defs
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from core.orchestration.pipeline_stages import (
    STAGES,
    Stage,
    command_for,
    topological_order,
)

_DEFAULT_WORKSPACE_ENV = "AUTORESEARCH_PIPELINE_WORKSPACE"
_MAIN_AGENT_ENV = "AUTORESEARCH_MAIN_AGENT"


def _run_command(command: str, *, cwd: Optional[Path] = None) -> dict[str, Any]:
    """Run one governed stage command; return a structured result. Never raises
    -- the caller decides how to handle a non-zero exit."""
    proc = subprocess.run(
        command, shell=True, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }


def run_pipeline(
    workspace: str,
    *,
    repo_root: Optional[Path] = None,
    on_stage: Optional[Callable[[str, dict[str, Any]], None]] = None,
    stop_on_failure: bool = True,
) -> dict[str, Any]:
    """Plain sequential runner (no Dagster needed): execute every stage in
    dependency order, short-circuiting on the first failure by default. This is
    the same DAG Dagster renders -- usable directly for a one-command run.

    This seam is the cost-ledger ANCHOR point (Phase 1a.1): one honestly-empty
    anchor row per stage, keyed by run_id/workspace_id/pipeline_stage and carrying
    the agent-native session id from the environment, is written BEFORE each stage
    runs (so a failing stage still leaves an anchor). Anchor-write failures are
    surfaced in ``_anchor_errors`` -- never silently swallowed -- so a telemetry
    fault cannot corrupt the run yet also cannot hide."""
    from datetime import datetime, timezone

    from core.observability.cost_ledger import (
        CostLedger,
        build_anchor,
        ledger_dir_for,
        liveness_check,
        new_run_id,
        resolve_agent_session,
    )

    env = os.environ
    configured_agent = env.get(_MAIN_AGENT_ENV)
    agent_session_id, _ = resolve_agent_session(env)
    run_id = new_run_id(workspace_id=workspace, agent_session_id=agent_session_id)
    ledger = CostLedger(ledger_dir_for(workspace, repo_root))

    results: dict[str, Any] = {"_run_id": run_id}
    smap = {s.key: s for s in STAGES}
    for key in topological_order():
        stage = smap[key]
        try:
            ledger.append(
                build_anchor(
                    run_id=run_id,
                    workspace_id=workspace,
                    pipeline_stage=key,
                    env=env,
                    configured_agent=configured_agent,
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        except Exception as exc:  # surfaced, not swallowed (no silent no-op)
            results.setdefault("_anchor_errors", []).append(f"{key}: {exc}")
        res = _run_command(command_for(stage, workspace), cwd=repo_root)
        results[key] = res
        if on_stage:
            on_stage(key, res)
        if not res["ok"] and stop_on_failure:
            results["_failed_stage"] = key
            break
    results["_ok"] = all(
        v.get("ok") for k, v in results.items() if not k.startswith("_")
    )
    try:
        ledger.write_summary(run_id)
        results["_cost_ledger"] = liveness_check(ledger.entries_for_run(run_id)).to_dict()
    except Exception as exc:  # surfaced, not swallowed
        results.setdefault("_anchor_errors", []).append(f"summary: {exc}")
    return results


# --------------------------------------------------------------------------
# Dagster rendering (optional dependency)
# --------------------------------------------------------------------------

def build_definitions(workspace: Optional[str] = None) -> Any:
    """Render STAGES into a Dagster `Definitions` (one asset per stage). Raises a
    clear error if Dagster is not installed."""
    try:
        from dagster import AssetExecutionContext, Definitions, MaterializeResult, asset
    except ImportError as exc:  # pragma: no cover - optional dep
        raise SystemExit(
            "Dagster is not installed. `pip install dagster` to serve the asset "
            "graph, or use core.orchestration.dagster_defs.run_pipeline() for a "
            "plain sequential run."
        ) from exc

    ws = workspace or os.environ.get(_DEFAULT_WORKSPACE_ENV, "")
    repo_root = Path(__file__).resolve().parents[2]

    def _make_asset(stage: Stage):
        @asset(name=stage.key, deps=list(stage.upstream), description=stage.description)
        def _stage_asset(context: "AssetExecutionContext"):  # type: ignore[name-defined]
            target_ws = ws or os.environ.get(_DEFAULT_WORKSPACE_ENV, "")
            if not target_ws:
                raise ValueError(
                    f"no workspace set; export {_DEFAULT_WORKSPACE_ENV}=workspaces/<ws>")
            res = _run_command(command_for(stage, target_ws), cwd=repo_root)
            context.log.info(res["stdout_tail"])
            if not res["ok"]:
                raise RuntimeError(
                    f"stage `{stage.key}` failed (exit {res['returncode']}):\n"
                    + res["stderr_tail"])
            return MaterializeResult(metadata={
                "command": res["command"],
                "returncode": res["returncode"],
            })
        return _stage_asset

    return Definitions(assets=[_make_asset(s) for s in STAGES])


# Dagster looks for a module-level `defs`. Build it lazily so importing this
# module never requires Dagster (the topology + run_pipeline stay usable).
try:  # pragma: no cover - only exercised when dagster is installed
    import dagster as _dagster  # noqa: F401

    defs = build_definitions()
except Exception:  # ImportError or missing workspace at import time
    defs = None


def main(argv: Optional[list[str]] = None) -> int:
    """One-command, dependency-ordered run of the whole pipeline (no Dagster
    needed). `uv run pipeline-run --workspace workspaces/<ws>`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="pipeline-run",
        description="Run the medallion+KPI+dashboard pipeline in dependency order.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--continue-on-failure", action="store_true",
                        help="Run every stage even if one fails (default: stop).")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]

    def _log(key: str, res: dict[str, Any]) -> None:
        mark = "[ok]" if res["ok"] else "[x]"
        print(f"{mark} {key}: exit {res['returncode']}")
        if not res["ok"]:
            print(res["stderr_tail"][-600:])

    results = run_pipeline(
        args.workspace, repo_root=repo_root, on_stage=_log,
        stop_on_failure=not args.continue_on_failure,
    )
    if results.get("_ok"):
        print("\nPipeline complete: all stages ok.")
        return 0
    failed = results.get("_failed_stage", "(see above)")
    print(f"\nPipeline failed at stage: {failed}")
    return 1


__all__ = ["run_pipeline", "build_definitions", "defs", "main"]

"""Governed CLI entry points for schema evolution.

    prepare-drift-panel -> snapshots discovery.json, diffs it against the
                           previous snapshot, opens interns/reports/
                           schema_drift_panel/current.{json,md} when a finding
                           needs a decision
    apply-drift-answer  -> records the answer + (for `quarantine_column`) the
                           interns/generated/contracts/schema_exclusions.json
                           contract

Thin adapters over [[core.onboarding.workspace.cli_runner]]'s
`run_workspace_command`, so locking, timing, trajectory events and idempotency
behave like every other prepare-*/apply-* command. Console-script registration
is deliberately not done here.
"""
from __future__ import annotations

import argparse
from typing import Any

from core.observability.cost_ledger import anchored
from core.paths import PROJECT_ROOT


@anchored("prepare-drift-panel")
def prepare_drift_panel_main(argv: list[str] | None = None) -> int:
    from core.evolution.panel import prepare_drift_panel
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser(
        description="Snapshot the current discovery and panel any schema drift that needs a decision."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)

    return run_workspace_command(
        command="prepare-drift-panel",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: prepare_drift_panel(args.repo_root, args.workspace),
        validation="validate-workspace-artifacts",
    )


@anchored("apply-drift-answer")
def apply_drift_answer_main(argv: list[str] | None = None) -> int:
    from core.evolution.panel import apply_drift_answer
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser(description="Record one answer from the schema drift panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--finding", required=True, help="finding_id from the panel.")
    parser.add_argument("--answer", required=True, help="propagate | quarantine_column | block_pipeline")
    parser.add_argument(
        "--confirmed-by",
        default="",
        help=(
            "Real human name. Empty (or an agent identity) records the decision as "
            "agent-asserted, which quarantine_column/block_pipeline refuse."
        ),
    )
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)

    holder: dict[str, Any] = {}

    def _run() -> Any:
        # A refusal (unknown finding, unsupported option, agent-asserted human
        # gate) is an expected outcome: a structured payload, not a traceback --
        # but it must not exit 0.
        try:
            holder["result"] = apply_drift_answer(
                args.repo_root,
                args.workspace,
                finding_id=args.finding,
                answer=args.answer,
                confirmed_by=args.confirmed_by,
            )
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            holder["result"] = {"ok": False, "status": "refused", "reason": str(exc)}
            print(f"[x] apply-drift-answer refused: {exc}")
        return holder["result"]

    code = run_workspace_command(
        command="apply-drift-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        op_args={
            "workspace": args.workspace,
            "finding": args.finding,
            "answer": args.answer,
            # The confirmer is part of the op identity: a refused agent-asserted
            # attempt must not make the human's retry look like a replay.
            "confirmed_by": args.confirmed_by,
        },
        allow_replay=args.allow_replay,
        decision=f"{args.finding}={args.answer}",
        metadata={"finding_id": args.finding, "confirmed_by": args.confirmed_by},
        record_idempotent=True,
    )
    if code != 0:
        return code
    result = holder.get("result") or {}
    return 0 if result.get("ok") else 1


__all__ = ["apply_drift_answer_main", "prepare_drift_panel_main"]


if __name__ == "__main__":
    raise SystemExit(prepare_drift_panel_main())

from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from pathlib import Path

from core.observability.events import time_command
from core.onboarding.harness.trajectory_recorder import record_trajectory_event_safe
from core.onboarding.kpi.blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel
from core.onboarding.workspace.idempotency import (
    compute_op_id,
    get_applied_op,
    record_op,
)
from core.storage.workspace_layout import WorkspaceLayout
from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock


def _resolve_workspace_path(repo_root: str, workspace: str) -> Path:
    return (Path(repo_root) / workspace).resolve()


def _current_panel_question_id(workspace_path: Path) -> str:
    """The panel's `question_id` (or `feature`) at call time -- must be part of
    the idempotency key. Without it, answering the same literal --answer text
    (e.g. `option_a`) for two DIFFERENT blockers in sequence collides on the
    same op_id and the second application is silently skipped as a replay of
    the first (found live: `shipments -> option_a` was skipped because
    `cargo_claims -> option_a` had already claimed that op_id).
    """
    path = WorkspaceLayout(project_root=workspace_path).reports_dir / "blocker_question_panel" / "current.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return str(data.get("question_id") or data.get("feature") or "")



@anchored("prepare-kpi-blocker-panel")
def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a validated KPI blocker question panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="generic")
    parser.add_argument("--force-onboard", action="store_true")
    parser.add_argument("--no-onboard-if-missing", action="store_true")
    args = parser.parse_args(argv)
    workspace_path = _resolve_workspace_path(args.repo_root, args.workspace)
    record_trajectory_event_safe(
        args.repo_root,
        args.workspace,
        event_type="tool_start",
        status="running",
        summary="Preparing KPI blocker panel.",
        metadata={"tool": "prepare-kpi-blocker-panel", "domain": args.domain},
    )
    try:
        with time_command(workspace_path, "prepare-kpi-blocker-panel") as event_details:
            event_details["domain"] = args.domain
            with workspace_lock(workspace_path):
                result = prepare_kpi_blocker_panel(
                    args.repo_root,
                    args.workspace,
                    domain=args.domain,
                    onboard_if_missing=not args.no_onboard_if_missing,
                    force_onboard=args.force_onboard,
                )
            event_details["question_count"] = result.question_count
            event_details["current_feature"] = result.current_feature
    except WorkspaceLockTimeout as exc:
        record_trajectory_event_safe(
            args.repo_root,
            args.workspace,
            event_type="tool_result",
            status="failed",
            summary="prepare-kpi-blocker-panel blocked by workspace lock",
            metadata={"tool": "prepare-kpi-blocker-panel", "error": str(exc)},
        )
        print(json.dumps({"error": "workspace_lock_timeout", "detail": str(exc)}, indent=2))
        return 2
    except Exception as exc:
        record_trajectory_event_safe(
            args.repo_root,
            args.workspace,
            event_type="tool_result",
            status="failed",
            summary=f"prepare-kpi-blocker-panel failed: {type(exc).__name__}",
            metadata={"tool": "prepare-kpi-blocker-panel", "error": str(exc)},
        )
        raise
    record_trajectory_event_safe(
        args.repo_root,
        args.workspace,
        event_type="tool_result",
        status="ok",
        summary="Prepared KPI blocker panel.",
        artifact=result.question_panel_path,
        validation="validate-workspace-artifacts",
        metadata={"tool": "prepare-kpi-blocker-panel", "result": result.summary()},
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


@anchored("apply-kpi-panel-answer")
def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an answer from the current KPI blocker panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="generic")
    parser.add_argument("--answer", required=True, help="Option id, exact label, or unambiguous friendly answer.")
    parser.add_argument(
        "--feature",
        default=None,
        help=(
            "Target a SPECIFIC blocker by feature name (from all_blockers.md) "
            "instead of the active `current` one. Lets you answer any listed "
            "decision directly."
        ),
    )
    parser.add_argument("--custom-definition", default="")
    parser.add_argument("--evidence-note", default="")
    parser.add_argument(
        "--via-cli-agent",
        action="store_true",
        help=(
            "Mark the resulting decision as `cli_agent_proposed` (awaiting user "
            "confirmation) instead of `user_confirmed`. Use this when the "
            "orchestrating CLI agent applies an answer derived from the "
            "cli_agent_evidence_pack; the user then runs "
            "`confirm-cli-agent-proposal` to finalize."
        ),
    )
    parser.add_argument(
        "--allow-replay",
        action="store_true",
        help="Re-apply even if a previous run with the same arguments succeeded (skips idempotency check).",
    )
    args = parser.parse_args(argv)
    workspace_path = _resolve_workspace_path(args.repo_root, args.workspace)

    # Idempotency: deterministic op_id from the user-visible arguments PLUS
    # which blocker is actually being answered (args.feature if targeting a
    # specific one, else whatever the panel's `current` question is right
    # now). The same literal --answer text (e.g. `option_a`) is common across
    # unrelated blockers, so the question identity must be part of the key --
    # otherwise the second answer collides with the first and is wrongly
    # skipped as an idempotent replay.
    op_id = compute_op_id(
        "apply-kpi-panel-answer",
        workspace=args.workspace,
        domain=args.domain,
        answer=args.answer,
        custom_definition=args.custom_definition,
        evidence_note=args.evidence_note,
        via_cli_agent=args.via_cli_agent,
        question_id=args.feature or _current_panel_question_id(workspace_path),
    )
    if not args.allow_replay:
        prior = get_applied_op(workspace_path, op_id)
        if prior is not None:
            print(
                json.dumps(
                    {
                        "status": "idempotent_replay",
                        "op_id": op_id,
                        "previously_applied_at": prior.applied_at,
                        "result": prior.payload,
                        "note": (
                            "Skipped re-apply because this exact call was already applied. "
                            "Pass --allow-replay to force re-execution."
                        ),
                    },
                    indent=2,
                )
            )
            return 0

    record_trajectory_event_safe(
        args.repo_root,
        args.workspace,
        event_type="tool_start",
        status="running",
        summary="Applying KPI blocker panel answer.",
        decision=args.answer,
        metadata={"tool": "apply-kpi-panel-answer", "domain": args.domain, "op_id": op_id},
    )
    try:
        with time_command(workspace_path, "apply-kpi-panel-answer") as event_details:
            event_details["domain"] = args.domain
            event_details["answer"] = args.answer
            event_details["op_id"] = op_id
            with workspace_lock(workspace_path):
                result = apply_kpi_panel_answer(
                    args.repo_root,
                    args.workspace,
                    answer=args.answer,
                    domain=args.domain,
                    custom_definition=args.custom_definition,
                    evidence_note=args.evidence_note,
                    via_cli_agent=args.via_cli_agent,
                    feature=args.feature,
                )
    except WorkspaceLockTimeout as exc:
        record_trajectory_event_safe(
            args.repo_root,
            args.workspace,
            event_type="tool_result",
            status="failed",
            summary="apply-kpi-panel-answer blocked by workspace lock",
            decision=args.answer,
            metadata={"tool": "apply-kpi-panel-answer", "error": str(exc), "op_id": op_id},
        )
        print(json.dumps({"error": "workspace_lock_timeout", "detail": str(exc)}, indent=2))
        return 2
    except Exception as exc:
        record_trajectory_event_safe(
            args.repo_root,
            args.workspace,
            event_type="tool_result",
            status="failed",
            summary=f"apply-kpi-panel-answer failed: {type(exc).__name__}",
            decision=args.answer,
            metadata={"tool": "apply-kpi-panel-answer", "error": str(exc), "op_id": op_id},
        )
        raise
    record_op(
        workspace_path,
        op_id=op_id,
        command="apply-kpi-panel-answer",
        payload=result.summary(),
    )
    record_trajectory_event_safe(
        args.repo_root,
        args.workspace,
        event_type="tool_result",
        status="ok",
        summary=f"Applied KPI blocker panel answer `{args.answer}`.",
        decision=args.answer,
        metadata={"tool": "apply-kpi-panel-answer", "result": result.summary(), "op_id": op_id},
    )
    print(json.dumps(result.summary(), indent=2))
    return 0

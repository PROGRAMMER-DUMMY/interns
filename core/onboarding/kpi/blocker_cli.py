from __future__ import annotations

import argparse
import json

from core.onboarding.harness.trajectory_recorder import record_trajectory_event_safe
from core.onboarding.kpi.blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a validated KPI blocker question panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="healthcare")
    parser.add_argument("--force-onboard", action="store_true")
    parser.add_argument("--no-onboard-if-missing", action="store_true")
    args = parser.parse_args(argv)
    record_trajectory_event_safe(
        args.repo_root,
        args.workspace,
        event_type="tool_start",
        status="running",
        summary="Preparing KPI blocker panel.",
        metadata={"tool": "prepare-kpi-blocker-panel", "domain": args.domain},
    )
    try:
        result = prepare_kpi_blocker_panel(
            args.repo_root,
            args.workspace,
            domain=args.domain,
            onboard_if_missing=not args.no_onboard_if_missing,
            force_onboard=args.force_onboard,
        )
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


def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an answer from the current KPI blocker panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="healthcare")
    parser.add_argument("--answer", required=True, help="Option id, exact label, or unambiguous friendly answer.")
    parser.add_argument("--custom-definition", default="")
    parser.add_argument("--evidence-note", default="")
    args = parser.parse_args(argv)
    record_trajectory_event_safe(
        args.repo_root,
        args.workspace,
        event_type="tool_start",
        status="running",
        summary="Applying KPI blocker panel answer.",
        decision=args.answer,
        metadata={"tool": "apply-kpi-panel-answer", "domain": args.domain},
    )
    try:
        result = apply_kpi_panel_answer(
            args.repo_root,
            args.workspace,
            answer=args.answer,
            domain=args.domain,
            custom_definition=args.custom_definition,
            evidence_note=args.evidence_note,
        )
    except Exception as exc:
        record_trajectory_event_safe(
            args.repo_root,
            args.workspace,
            event_type="tool_result",
            status="failed",
            summary=f"apply-kpi-panel-answer failed: {type(exc).__name__}",
            decision=args.answer,
            metadata={"tool": "apply-kpi-panel-answer", "error": str(exc)},
        )
        raise
    record_trajectory_event_safe(
        args.repo_root,
        args.workspace,
        event_type="tool_result",
        status="ok",
        summary=f"Applied KPI blocker panel answer `{args.answer}`.",
        decision=args.answer,
        metadata={"tool": "apply-kpi-panel-answer", "result": result.summary()},
    )
    print(json.dumps(result.summary(), indent=2))
    return 0

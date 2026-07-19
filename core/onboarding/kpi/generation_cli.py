from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse

from core.onboarding.kpi.generation_workflow import KPIGenerationWorkflow
from core.onboarding.workspace.cli_runner import run_workspace_command


def _workflow(repo_root: str, workspace: str) -> KPIGenerationWorkflow:
    return KPIGenerationWorkflow(repo_root, workspace)



@anchored("prepare-kpi-generation")
def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the KPI generation route/interview panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--context-file", action="append", default=[])
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="prepare-kpi-generation",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).prepare(
            context_files=args.context_file,
        ),
        validation="validate-workspace-artifacts",
    )


@anchored("apply-kpi-generation-answer")
def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an answer to the current KPI generation panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--context-file", action="append", default=[])
    parser.add_argument("--custom-note", default="")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="apply-kpi-generation-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).apply_answer(
            answer=args.answer,
            context_files=args.context_file,
            custom_note=args.custom_note,
        ),
        op_args={
            "workspace": args.workspace,
            "answer": args.answer,
            "context_files": sorted(args.context_file),
            "custom_note": args.custom_note,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer},
        record_idempotent=True,
    )


@anchored("finalize-kpi-generation")
def finalize_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize approved KPI generation draft.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--approve-final-preview", action="store_true")
    parser.add_argument("--output-registry", default="")
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="finalize-kpi-generation",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).finalize(
            approve_final_preview=args.approve_final_preview,
            output_registry=args.output_registry,
            replace_existing=args.replace_existing,
        ),
        op_args={
            "workspace": args.workspace,
            "approve_final_preview": args.approve_final_preview,
            "output_registry": args.output_registry,
            "replace_existing": args.replace_existing,
        },
        allow_replay=args.allow_replay,
        metadata={"approve_final_preview": args.approve_final_preview},
        record_idempotent=True,
    )

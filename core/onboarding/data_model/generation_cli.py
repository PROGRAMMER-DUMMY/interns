from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.onboarding.data_model.generation_workflow import DataModelGenerationWorkflow
from core.onboarding.workspace.cli_runner import run_workspace_command


def _workflow(repo_root: str, workspace: str) -> DataModelGenerationWorkflow:
    return DataModelGenerationWorkflow(Path(repo_root).resolve(), workspace)


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare governed data model generation panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="prepare-data-model-generation",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).prepare(),
        validation="validate-workspace-artifacts",
    )


def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a data model generation panel answer.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--custom-note", default="")
    parser.add_argument(
        "--operation",
        action="append",
        default=[],
        help="Structured JSON operation for add/remove/change decisions. May be repeated.",
    )
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    operations = [json.loads(item) for item in args.operation]
    return run_workspace_command(
        command="apply-data-model-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).apply_answer(
            answer=args.answer,
            custom_note=args.custom_note,
            operations=operations,
        ),
        op_args={
            "workspace": args.workspace,
            "answer": args.answer,
            "custom_note": args.custom_note,
            "operations": operations,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer},
        record_idempotent=True,
    )


def finalize_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize approved data model docs and contracts.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--approve-final-preview", action="store_true")
    parser.add_argument("--no-replace-existing", action="store_true")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="finalize-data-model-generation",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).finalize(
            approve_final_preview=args.approve_final_preview,
            replace_existing=not args.no_replace_existing,
        ),
        op_args={
            "workspace": args.workspace,
            "approve_final_preview": args.approve_final_preview,
            "replace_existing": not args.no_replace_existing,
        },
        allow_replay=args.allow_replay,
        metadata={"approve_final_preview": args.approve_final_preview},
        record_idempotent=True,
    )


def prepare_blocker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the next data model blocker panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="prepare-data-model-blocker-panel",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).prepare_blocker_panel(),
        validation="validate-workspace-artifacts",
    )


def apply_blocker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an answer to the current data model blocker panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--custom-note", default="")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="apply-data-model-blocker-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: _workflow(args.repo_root, args.workspace).apply_blocker_answer(
            answer=args.answer,
            custom_note=args.custom_note,
        ),
        op_args={
            "workspace": args.workspace,
            "answer": args.answer,
            "custom_note": args.custom_note,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer},
        record_idempotent=True,
    )

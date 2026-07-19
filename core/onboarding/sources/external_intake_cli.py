from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json

from core.onboarding.sources.external_intake_workflow import ExternalSourceIntakeWorkflow
from core.onboarding.workspace.cli_runner import run_workspace_command


def _workflow_workspace_hint(args: argparse.Namespace) -> str:
    """External intake may run before a workspace path exists.

    When the user has not yet picked a workspace we still want lock/event/
    idempotency to fire — keyed against the proposed-workspace bucket so a
    second concurrent intake against the same target is detected. Fall back
    to a generic ``external-intake`` placeholder otherwise.
    """

    return args.workspace or args.proposed_workspace or "external-intake"



@anchored("prepare-external-source-intake")
def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare external source intake route panel.")
    parser.add_argument("--external-root", required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--proposed-workspace", default="")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    workspace = _workflow_workspace_hint(args)
    if args.workspace or args.proposed_workspace:
        return run_workspace_command(
            command="prepare-external-source-intake",
            workspace=workspace,
            repo_root=args.repo_root,
            fn=lambda: ExternalSourceIntakeWorkflow(
                args.repo_root,
                external_root=args.external_root,
                workspace=args.workspace or None,
                proposed_workspace=args.proposed_workspace or None,
            ).prepare(),
            metadata={"external_root": args.external_root},
        )
    # No workspace context: fall back to plain prepare; nothing to lock.
    result = ExternalSourceIntakeWorkflow(
        args.repo_root,
        external_root=args.external_root,
        workspace=None,
        proposed_workspace=None,
    ).prepare()
    print(json.dumps(result.summary(), indent=2))
    return 0


@anchored("apply-external-source-intake")
def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an answer to the external source intake panel.")
    parser.add_argument("--external-root", required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--proposed-workspace", default="")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--existing-workspace", default="")
    parser.add_argument("--workspace-name", default="")
    parser.add_argument("--change-reason", default="")
    parser.add_argument("--save-as-default", action="store_true")
    parser.add_argument("--max-files", type=int, default=2000)
    parser.add_argument("--max-seconds", type=float, default=30.0)
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    # Plain dispatch when no workspace context exists yet — there's nothing
    # to lock and idempotency would key against a non-existent path.
    if not (args.workspace or args.proposed_workspace or args.existing_workspace):
        result = ExternalSourceIntakeWorkflow(
            args.repo_root,
            external_root=args.external_root,
            workspace=None,
            proposed_workspace=None,
        ).apply_answer(
            answer=args.answer,
            existing_workspace=args.existing_workspace,
            workspace_name=args.workspace_name,
            change_reason=args.change_reason,
            save_as_default=args.save_as_default,
            max_files=args.max_files,
            max_seconds=args.max_seconds,
        )
        print(json.dumps(result.summary(), indent=2))
        return 0
    workspace = _workflow_workspace_hint(args)
    return run_workspace_command(
        command="apply-external-source-intake",
        workspace=workspace,
        repo_root=args.repo_root,
        fn=lambda: ExternalSourceIntakeWorkflow(
            args.repo_root,
            external_root=args.external_root,
            workspace=args.workspace or None,
            proposed_workspace=args.proposed_workspace or None,
        ).apply_answer(
            answer=args.answer,
            existing_workspace=args.existing_workspace,
            workspace_name=args.workspace_name,
            change_reason=args.change_reason,
            save_as_default=args.save_as_default,
            max_files=args.max_files,
            max_seconds=args.max_seconds,
        ),
        op_args={
            "workspace": workspace,
            "external_root": args.external_root,
            "answer": args.answer,
            "existing_workspace": args.existing_workspace,
            "workspace_name": args.workspace_name,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer, "external_root": args.external_root},
        record_idempotent=True,
    )

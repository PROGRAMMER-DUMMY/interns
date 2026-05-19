from __future__ import annotations

import argparse
import json

from core.onboarding.external_source_intake_workflow import ExternalSourceIntakeWorkflow


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare external source intake route panel.")
    parser.add_argument("--external-root", required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--proposed-workspace", default="")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = ExternalSourceIntakeWorkflow(
        args.repo_root,
        external_root=args.external_root,
        workspace=args.workspace or None,
        proposed_workspace=args.proposed_workspace or None,
    ).prepare()
    print(json.dumps(result.summary(), indent=2))
    return 0


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
    args = parser.parse_args(argv)
    result = ExternalSourceIntakeWorkflow(
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
    )
    print(json.dumps(result.summary(), indent=2))
    return 0

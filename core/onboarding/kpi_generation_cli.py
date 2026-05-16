from __future__ import annotations

import argparse
import json

from core.onboarding.kpi_generation_workflow import KPIGenerationWorkflow


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the KPI generation route/interview panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--context-file", action="append", default=[])
    args = parser.parse_args(argv)
    result = KPIGenerationWorkflow(args.repo_root, args.workspace).prepare(
        context_files=args.context_file
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an answer to the current KPI generation panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--context-file", action="append", default=[])
    parser.add_argument("--custom-note", default="")
    args = parser.parse_args(argv)
    result = KPIGenerationWorkflow(args.repo_root, args.workspace).apply_answer(
        answer=args.answer,
        context_files=args.context_file,
        custom_note=args.custom_note,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


def finalize_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize approved KPI generation draft.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--approve-final-preview", action="store_true")
    parser.add_argument("--output-registry", default="")
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args(argv)
    result = KPIGenerationWorkflow(args.repo_root, args.workspace).finalize(
        approve_final_preview=args.approve_final_preview,
        output_registry=args.output_registry,
        replace_existing=args.replace_existing,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0

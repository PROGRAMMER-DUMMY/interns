from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.onboarding.data_model_generation_workflow import DataModelGenerationWorkflow


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare governed data model generation panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = DataModelGenerationWorkflow(Path(args.repo_root).resolve(), args.workspace).prepare()
    print(json.dumps(result.summary(), indent=2))
    return 0


def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a data model generation panel answer.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--custom-note", default="")
    args = parser.parse_args(argv)
    result = DataModelGenerationWorkflow(Path(args.repo_root).resolve(), args.workspace).apply_answer(
        answer=args.answer,
        custom_note=args.custom_note,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


def finalize_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize approved data model docs and contracts.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--approve-final-preview", action="store_true")
    parser.add_argument("--no-replace-existing", action="store_true")
    args = parser.parse_args(argv)
    result = DataModelGenerationWorkflow(Path(args.repo_root).resolve(), args.workspace).finalize(
        approve_final_preview=args.approve_final_preview,
        replace_existing=not args.no_replace_existing,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0

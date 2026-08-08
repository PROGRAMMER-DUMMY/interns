"""Governed CLI entry points for Phase 0/1.

    declare-source        -> workspace_settings.json `source_declaration`
    discover-source       -> interns/generated/intake/discovery.json
    prepare-intake-panel  -> interns/reports/intake_panel/current.{json,md}
    apply-intake-answer   -> interns/generated/intake/intake_answers.json

Each is a thin adapter over [[core.onboarding.workspace.cli_runner]]'s
`run_workspace_command`, so locking, timing, trajectory events and idempotency
behave exactly like every other prepare-*/apply-* command. Console-script
registration is deliberately not done here.
"""
from __future__ import annotations

import argparse

from core.observability.cost_ledger import anchored
from core.paths import PROJECT_ROOT


@anchored("declare-source")
def declare_source_main(argv: list[str] | None = None) -> int:
    from core.intake.declaration import SOURCE_TYPES, SourceDeclaration, save_source_declaration
    from core.onboarding.workspace.cli_runner import resolve_workspace_path, run_workspace_command
    from core.storage.workspace_layout import WorkspaceLayout

    parser = argparse.ArgumentParser(description="Declare where this workspace's data lives.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--type", required=True, choices=list(SOURCE_TYPES))
    parser.add_argument(
        "--location",
        required=True,
        help="Bucket/URI, JDBC url, broker list, <catalog>.<schema>, or a path.",
    )
    parser.add_argument("--format-hint", default="", help="e.g. parquet, csv, delta. Optional.")
    parser.add_argument(
        "--credential-ref",
        default="",
        help=(
            "NAME of the credential to use (secret scope/key, cloud profile, "
            "env-var name, UC storage credential). Never a secret value."
        ),
    )
    parser.add_argument(
        "--schema-registry-url",
        default="",
        help="Optional http(s) endpoint of the schema registry for this source's values.",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="This source is loaded ONCE (a historical backfill), not on a schedule.",
    )
    parser.add_argument("--declared-by", default="")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)

    declaration = SourceDeclaration(
        type=args.type,
        location=args.location,
        format_hint=args.format_hint,
        credential_ref=args.credential_ref,
        declared_by=args.declared_by,
        schema_registry_url=args.schema_registry_url,
        one_shot=args.one_shot,
    )
    layout = WorkspaceLayout(project_root=resolve_workspace_path(args.repo_root, args.workspace))

    return run_workspace_command(
        command="declare-source",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: save_source_declaration(layout, declaration),
        # The declaration itself is the op identity; the credential REFERENCE is a
        # name, so it is safe in the op key and in the trajectory metadata.
        op_args={
            "workspace": args.workspace,
            "type": args.type,
            "location": args.location,
            "credential_ref": args.credential_ref,
        },
        allow_replay=args.allow_replay,
        decision=f"{args.type}:{args.location}",
        metadata={"source_type": args.type, "credential_ref": args.credential_ref},
        record_idempotent=True,
    )


@anchored("discover-source")
def discover_source_main(argv: list[str] | None = None) -> int:
    from core.intake.discovery import run_discovery
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser(description="Scan the declared source read-only.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--max-items", type=int, default=2000)
    parser.add_argument("--max-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)

    return run_workspace_command(
        command="discover-source",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: run_discovery(
            args.repo_root,
            args.workspace,
            max_items=args.max_items,
            max_seconds=args.max_seconds,
        ),
    )


@anchored("prepare-intake-panel")
def prepare_intake_main(argv: list[str] | None = None) -> int:
    from core.intake.interview import IntakePanelBuilder
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser(description="Prepare the merged intake interview panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)

    return run_workspace_command(
        command="prepare-intake-panel",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: IntakePanelBuilder(args.repo_root, args.workspace).prepare(),
        validation="validate-workspace-artifacts",
    )


@anchored("apply-intake-answer")
def apply_intake_answer_main(argv: list[str] | None = None) -> int:
    from core.intake.interview import IntakeAnswerRecorder
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser(description="Record one answer from the intake panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--question", required=True, help="question_id from the panel.")
    parser.add_argument("--answer", required=True, help="Option id (or comma-separated ids), or free text.")
    parser.add_argument("--answered-by", default="", help="Human name; empty records the answer as agent-asserted.")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)

    return run_workspace_command(
        command="apply-intake-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: IntakeAnswerRecorder(args.repo_root, args.workspace).apply(
            args.question, args.answer, answered_by=args.answered_by
        ),
        op_args={"workspace": args.workspace, "question": args.question, "answer": args.answer},
        allow_replay=args.allow_replay,
        decision=f"{args.question}={args.answer}",
        metadata={"question_id": args.question, "answered_by": args.answered_by},
        record_idempotent=True,
    )


__all__ = [
    "apply_intake_answer_main",
    "declare_source_main",
    "discover_source_main",
    "prepare_intake_main",
]


@anchored("fetch-source-documents")
def fetch_source_documents_main(argv: list[str] | None = None) -> int:
    """Copy the declared source's documents into the workspace."""
    from core.intake.documents import fetch_documents

    parser = argparse.ArgumentParser(
        description=(
            "Fetch documents (KPI workbook, data model, dictionary) from the declared "
            "source into <workspace>/docs/, read through Unity Catalog so no local "
            "cloud credential is needed."
        )
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--docs-uri", default="",
        help="Override the documents prefix (default: a sibling docs/ of the declared data prefix).",
    )
    args = parser.parse_args(argv)

    holder: dict[str, Any] = {}

    def _run() -> Any:
        holder["result"] = fetch_documents(
            args.repo_root, args.workspace, docs_uri=args.docs_uri
        ).summary()
        return holder["result"]

    code = run_workspace_command(
        command="fetch-source-documents",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=_run,
        metadata={"docs_uri": args.docs_uri},
    )
    if code != 0:
        return code
    result = holder.get("result") or {}
    return 0 if result.get("ok") else 1

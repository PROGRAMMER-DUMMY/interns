from __future__ import annotations

import argparse
import json

from core.onboarding.kpi_blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a validated KPI blocker question panel.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="healthcare")
    parser.add_argument("--force-onboard", action="store_true")
    parser.add_argument("--no-onboard-if-missing", action="store_true")
    args = parser.parse_args(argv)
    result = prepare_kpi_blocker_panel(
        args.repo_root,
        args.workspace,
        domain=args.domain,
        onboard_if_missing=not args.no_onboard_if_missing,
        force_onboard=args.force_onboard,
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
    result = apply_kpi_panel_answer(
        args.repo_root,
        args.workspace,
        answer=args.answer,
        domain=args.domain,
        custom_definition=args.custom_definition,
        evidence_note=args.evidence_note,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0

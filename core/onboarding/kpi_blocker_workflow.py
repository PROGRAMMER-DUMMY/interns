"""Deterministic KPI blocker panel workflow wrappers."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.onboarding.blocker_question_panel import BlockerQuestionPanelBuilder
from core.onboarding.derived_feature_markdown import DerivedFeatureMarkdownConverter
from core.onboarding.kpi_feature_resolver import KPIFeatureResolver, apply_workspace_definition
from core.onboarding.workspace_artifact_validator import WorkspaceArtifactValidator
from core.onboarding.workspace_onboarding import WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class PreparedPanelResult:
    workspace: str
    onboarded: bool
    mapping_path: str
    question_panel_path: str
    question_panel_markdown_path: str
    current_feature: str
    validation: dict[str, Any]
    next_step: str

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "onboarded": self.onboarded,
            "mapping_path": self.mapping_path,
            "question_panel_path": self.question_panel_path,
            "question_panel_markdown_path": self.question_panel_markdown_path,
            "current_feature": self.current_feature,
            "validation": self.validation,
            "next_step": self.next_step,
        }


@dataclass(frozen=True)
class ApplyPanelAnswerResult:
    workspace: str
    accepted_option_id: str
    accepted_label: str
    feature: str
    apply_summary: dict[str, Any]
    prepared_panel: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "accepted_option_id": self.accepted_option_id,
            "accepted_label": self.accepted_label,
            "feature": self.feature,
            "apply_summary": self.apply_summary,
            "prepared_panel": self.prepared_panel,
        }


def prepare_kpi_blocker_panel(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    domain: str = "healthcare",
    onboard_if_missing: bool = True,
    force_onboard: bool = False,
) -> PreparedPanelResult:
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    onboarded = False
    if force_onboard or (onboard_if_missing and _onboarding_artifacts_missing(layout)):
        WorkspaceOnboarder(root, _rel(workspace_path, root)).run()
        onboarded = True

    resolver_result = KPIFeatureResolver(
        root,
        _rel(workspace_path, root),
        domain=domain,
        include_candidates=True,
    ).run()
    DerivedFeatureMarkdownConverter(root, _rel(workspace_path, root)).run()
    panel_result = BlockerQuestionPanelBuilder(root, _rel(workspace_path, root)).run()
    validation = WorkspaceArtifactValidator(root, _rel(workspace_path, root)).run()
    if not validation.ok:
        raise ValueError("workspace artifact validation failed:\n" + "\n".join(validation.errors))
    return PreparedPanelResult(
        workspace=_rel(workspace_path, root),
        onboarded=onboarded,
        mapping_path=resolver_result.mapping_path,
        question_panel_path=panel_result.current_json,
        question_panel_markdown_path=panel_result.current_markdown,
        current_feature=panel_result.current_feature,
        validation=validation.summary(),
        next_step=(
            f"Read {panel_result.current_markdown} and ask from {panel_result.current_json}."
            if panel_result.current_feature
            else "No blocker question remains."
        ),
    )


def apply_kpi_panel_answer(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    answer: str,
    domain: str = "healthcare",
    custom_definition: str = "",
    evidence_note: str = "",
) -> ApplyPanelAnswerResult:
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    panel_path = layout.reports_dir / "blocker_question_panel" / "current.json"
    if not panel_path.exists():
        raise FileNotFoundError(f"question panel not found: {_rel(panel_path, root)}")
    validation = WorkspaceArtifactValidator(root, _rel(workspace_path, root)).run()
    if not validation.ok:
        raise ValueError("workspace artifact validation failed:\n" + "\n".join(validation.errors))
    panel = json.loads(panel_path.read_text(encoding="utf-8"))
    option = _resolve_answer(panel, answer)
    feature = str(panel.get("feature") or "")
    apply_summary = _apply_option(
        root,
        _rel(workspace_path, root),
        feature=feature,
        option=option,
        panel=panel,
        custom_definition=custom_definition,
        evidence_note=evidence_note,
    )
    prepared = prepare_kpi_blocker_panel(
        root,
        _rel(workspace_path, root),
        domain=domain,
        onboard_if_missing=False,
    )
    return ApplyPanelAnswerResult(
        workspace=_rel(workspace_path, root),
        accepted_option_id=str(option.get("option_id") or ""),
        accepted_label=str(option.get("label") or ""),
        feature=feature,
        apply_summary=apply_summary,
        prepared_panel=prepared.summary(),
    )


def _apply_option(
    repo_root: Path,
    workspace: str,
    *,
    feature: str,
    option: dict[str, Any],
    panel: dict[str, Any],
    custom_definition: str,
    evidence_note: str,
) -> dict[str, Any]:
    option_id = str(option.get("option_id") or "")
    if option_id == "custom":
        if not custom_definition:
            raise ValueError("--custom-definition is required when applying the custom option")
        return apply_workspace_definition(
            repo_root,
            workspace,
            feature=feature,
            state="user_confirmed",
            resolution_type="custom_business_definition",
            definition=custom_definition,
            evidence_note=evidence_note or custom_definition,
            applies_to_kpis=panel.get("applies_to_kpis") or [],
        )
    if physical := option.get("physical_column_option"):
        dataset = str(physical.get("dataset") or "")
        column = str(physical.get("column") or "")
        definition = f"{dataset}.{column}" if dataset else column
        return apply_workspace_definition(
            repo_root,
            workspace,
            feature=feature,
            state="user_confirmed",
            resolution_type="physical_column",
            definition=definition,
            evidence_note=evidence_note or f"Accepted panel option {option_id}: {definition}",
            source_columns=[definition],
            applies_to_kpis=panel.get("applies_to_kpis") or [],
        )
    if derived := option.get("derived_feature_option"):
        formula = str(derived.get("formula") or "")
        if not formula:
            raise ValueError(f"{option_id} has no derived formula to apply")
        source_columns = [
            str(column.get("column") or "")
            for column in derived.get("input_columns") or []
            if column.get("column")
        ]
        return apply_workspace_definition(
            repo_root,
            workspace,
            feature=feature,
            state="user_confirmed",
            resolution_type="derived_formula",
            definition=formula,
            evidence_note=evidence_note or f"Accepted panel option {option_id}: {formula}",
            source_columns=source_columns,
            applies_to_kpis=panel.get("applies_to_kpis") or [],
        )
    expected = option.get("expected_answer_shape")
    if expected and not custom_definition:
        raise ValueError(
            f"{option_id} is an answer-shape placeholder, not a concrete definition. "
            "Pass --custom-definition with the accepted mapping or rule."
        )
    if expected:
        return apply_workspace_definition(
            repo_root,
            workspace,
            feature=feature,
            state="user_confirmed",
            resolution_type="custom_business_definition",
            definition=custom_definition,
            evidence_note=evidence_note or custom_definition,
            applies_to_kpis=panel.get("applies_to_kpis") or [],
        )
    raise ValueError(f"Unsupported panel option shape for {option_id}")


def _resolve_answer(panel: dict[str, Any], answer: str) -> dict[str, Any]:
    options = panel.get("options") or []
    if not options:
        raise ValueError("current panel has no options")
    raw = str(answer or "").strip()
    normalized = _norm(raw)
    if not normalized:
        raise ValueError("answer must not be empty")

    if normalized in {"recommendation", "recommended", "yourrecommendation"}:
        recommended = str(panel.get("recommended_option_id") or "")
        if not recommended:
            raise ValueError("panel has no recommended_option_id")
        return _one_option(options, lambda item: _norm(item.get("option_id", "")) == _norm(recommended), raw)
    if normalized == "yes":
        concrete = [option for option in options if option.get("json_backed")]
        if len(concrete) == 1:
            return concrete[0]
        recommended = str(panel.get("recommended_option_id") or "")
        if recommended and len([option for option in options if option.get("option_id") == recommended]) == 1:
            return _one_option(options, lambda item: item.get("option_id") == recommended, raw)
        raise ValueError("`yes` is ambiguous for this panel; use an option id or label")
    option_letter = re.match(r"^option\s+([a-z])\b", raw, flags=re.IGNORECASE)
    if option_letter:
        option_id = f"option_{option_letter.group(1).lower()}"
        return _one_option(options, lambda item: _norm(item.get("option_id", "")) == _norm(option_id), raw)

    return _one_option(
        options,
        lambda item: normalized in {
            _norm(item.get("option_id", "")),
            _norm(item.get("label", "")),
            _norm(f"{item.get('option_id', '')} {item.get('label', '')}"),
            _norm(str(item.get("label", "")).split(":")[-1]),
            _norm(str(item.get("label", "")).split(".")[-1]),
        },
        raw,
    )


def _one_option(options: list[dict[str, Any]], predicate: Any, answer: str) -> dict[str, Any]:
    matches = [option for option in options if predicate(option)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        labels = ", ".join(str(option.get("option_id")) for option in options)
        raise ValueError(f"answer `{answer}` did not match a current panel option. Valid options: {labels}")
    raise ValueError(f"answer `{answer}` is ambiguous across {len(matches)} panel options")


def _onboarding_artifacts_missing(layout: WorkspaceLayout) -> bool:
    return not (
        (layout.contracts_dir / "kpi_registry.json").exists()
        and (layout.profiles_dir / "profile_index.json").exists()
    )


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


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

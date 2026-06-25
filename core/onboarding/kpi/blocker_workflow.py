"""Deterministic KPI blocker panel workflow wrappers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.failures import WorkflowBlockedError, validation_blocker
from core.onboarding.kpi.blocker_question_panel import BlockerQuestionPanelBuilder
from core.onboarding.features.derived_markdown import DerivedFeatureMarkdownConverter
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver, apply_workspace_definition
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout
from core.wiki import WikiLayout, build_feature_scaffold, upsert_feature_note


@dataclass(frozen=True)
class PreparedPanelResult:
    workspace: str
    onboarded: bool
    mapping_path: str
    question_panel_path: str
    question_panel_markdown_path: str
    current_feature: str
    question_count: int
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
            "question_count": self.question_count,
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
    domain: str = "generic",
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

    # Relationship/lineage evidence feeds feature resolution (documented joins,
    # collision unification), so contracts are rebuilt before resolving.
    # Additive: a failure here degrades resolution (collisions block instead of
    # unifying) but must never break panel preparation.
    try:
        from core.onboarding.relationships.contracts import RelationshipContractBuilder

        RelationshipContractBuilder(root, _rel(workspace_path, root)).build()
    except Exception:  # pragma: no cover - relationship evidence is additive
        pass

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
        raise WorkflowBlockedError(
            validation_blocker(
                "prepare_kpi_blocker_panel.validation",
                "workspace artifact validation failed:\n" + "\n".join(validation.errors),
                next_command=f"uv run validate-workspace-artifacts --workspace {_rel(workspace_path, root)}",
            )
        )
    return PreparedPanelResult(
        workspace=_rel(workspace_path, root),
        onboarded=onboarded,
        mapping_path=resolver_result.mapping_path,
        question_panel_path=panel_result.current_json,
        question_panel_markdown_path=panel_result.current_markdown,
        current_feature=panel_result.current_feature,
        question_count=panel_result.question_count,
        validation=validation.summary(),
        next_step=(
            f"Render {panel_result.current_markdown} as-is, reading it ONCE. It is the compact "
            f"decision card (KPI + decision + options); the full evidence/proof render is in "
            f"{panel_result.current_full_markdown} for drill-down only. Do not summarize the card, "
            f"write a second blocker prompt, re-read it in another form, or delegate to a subagent "
            f"to read it: a `... first N lines hidden ...` notice means the read SUCCEEDED "
            f"(UI truncation, not a failure). Apply answers only from {panel_result.current_json}."
            if panel_result.current_feature
            else "No blocker question remains."
        ),
    )


def apply_kpi_panel_answer(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    answer: str,
    domain: str = "generic",
    custom_definition: str = "",
    evidence_note: str = "",
    via_cli_agent: bool = False,
) -> ApplyPanelAnswerResult:
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    panel_path = layout.reports_dir / "blocker_question_panel" / "current.json"
    if not panel_path.exists():
        raise FileNotFoundError(f"question panel not found: {_rel(panel_path, root)}")
    validation = WorkspaceArtifactValidator(root, _rel(workspace_path, root)).run()
    if not validation.ok:
        raise WorkflowBlockedError(
            validation_blocker(
                "apply_kpi_panel_answer.validation",
                "workspace artifact validation failed:\n" + "\n".join(validation.errors),
                next_command=f"uv run validate-workspace-artifacts --workspace {_rel(workspace_path, root)}",
            )
        )
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
        via_cli_agent=via_cli_agent,
    )
    _write_feature_wiki_note(
        root,
        workspace_path,
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


def _write_feature_wiki_note(
    root: Path,
    workspace_path: Path,
    *,
    feature: str,
    option: dict[str, Any],
    panel: dict[str, Any],
    custom_definition: str,
    evidence_note: str,
) -> None:
    if not feature:
        return
    contract_ref = (
        f"{_rel(workspace_path, root)}/interns/generated/contracts/"
        f"workspace_feature_definitions.json#{feature}"
    )
    scaffold = build_feature_scaffold(
        feature=feature,
        option=option,
        panel=panel,
        custom_definition=custom_definition,
        evidence_note=evidence_note,
        contract_ref=contract_ref,
    )
    upsert_feature_note(WikiLayout(project_root=workspace_path), feature, scaffold)


def _apply_option(
    repo_root: Path,
    workspace: str,
    *,
    feature: str,
    option: dict[str, Any],
    panel: dict[str, Any],
    custom_definition: str,
    evidence_note: str,
    via_cli_agent: bool = False,
) -> dict[str, Any]:
    option_id = str(option.get("option_id") or "")
    # CLI-agent two-step: when an answer is being applied by the orchestrating
    # CLI agent on the user's behalf, persist it as `cli_agent_proposed` so the
    # mapping does NOT become ``ready_for_sql`` until the user runs
    # ``confirm-cli-agent-proposal``. Direct human-driven applies remain
    # ``user_confirmed`` immediately as before.
    accepted_state = "cli_agent_proposed" if via_cli_agent else "user_confirmed"
    note_prefix = "CLI agent proposal: " if via_cli_agent else ""
    # Intent-contract facet answers (denominator_scope, temporal_anchor, etc.).
    # Recorded in kpi_intent_answers.json; denominator_scope also mirrors into
    # pipeline_decisions.json so it actually changes the generated SQL.
    if intent := option.get("intent_facet"):
        from core.onboarding.kpi.intent_contract import record_intent_answer

        value = str(intent.get("value") or "").strip() or custom_definition.strip()
        if not value:
            raise ValueError(
                "intent-facet answer requires a concrete value; pass "
                "--custom-definition for the custom option"
            )
        record = record_intent_answer(
            repo_root,
            workspace,
            kpi_id=str(intent.get("kpi_id") or ""),
            facet=str(intent.get("facet") or ""),
            value=value,
            source=("agent" if via_cli_agent else "human"),
            confirmed_by=("" if via_cli_agent else (evidence_note or "panel")),
            reason=evidence_note,
        )
        return {
            "resolution_type": "intent_facet",
            "feature": feature,
            "intent_facet": record,
            "state": accepted_state,
        }
    if option_id == "custom":
        if not custom_definition:
            raise ValueError("--custom-definition is required when applying the custom option")
        # A custom definition that is a SQL EXPRESSION (EXISTS/CASE/comparison/
        # function call) must be recorded as a derived FORMULA so the features
        # view materializes it under the feature's name — recording it as a
        # plain business definition made resolution fall back to a column
        # alias (a readmission flag column full of entity ids). Quoted
        # identifiers in the formula are its input columns, exactly like the
        # JSON-backed derived options.
        definition_text = str(custom_definition).strip()
        looks_like_formula = bool(
            re.search(r"[()<>=]|\bexists\b|\bcase\b", definition_text, re.IGNORECASE)
        )
        formula_inputs = (
            list(dict.fromkeys(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', definition_text)))
            if looks_like_formula
            else []
        )
        return apply_workspace_definition(
            repo_root,
            workspace,
            feature=feature,
            state=accepted_state,
            resolution_type=(
                "derived_formula" if looks_like_formula else "custom_business_definition"
            ),
            definition=definition_text,
            evidence_note=(note_prefix + (evidence_note or custom_definition)),
            source_columns=formula_inputs,
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
            state=accepted_state,
            resolution_type="physical_column",
            definition=definition,
            evidence_note=note_prefix + (evidence_note or f"Accepted panel option {option_id}: {definition}"),
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
            state=accepted_state,
            resolution_type="derived_formula",
            definition=formula,
            evidence_note=note_prefix + (evidence_note or f"Accepted panel option {option_id}: {formula}"),
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
            state=accepted_state,
            resolution_type="custom_business_definition",
            definition=custom_definition,
            evidence_note=note_prefix + (evidence_note or custom_definition),
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
        recommended = str(panel.get("recommended_option_id") or "")
        if recommended and len([option for option in options if option.get("option_id") == recommended]) == 1:
            return _one_option(options, lambda item: item.get("option_id") == recommended, raw)
        concrete = [option for option in options if option.get("json_backed")]
        if len(concrete) == 1:
            return concrete[0]
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
    from core.onboarding.kpi.blocker_cli import prepare_main as cli_prepare_main

    return cli_prepare_main(argv)


def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.kpi.blocker_cli import apply_main as cli_apply_main

    return cli_apply_main(argv)

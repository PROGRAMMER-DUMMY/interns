"""Build a stakeholder-friendly blocker question panel from KPI feature mapping."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


PANEL_VERSION = 1
READY_STATES = {
    "proven_direct",
    "proven_alias",
    "proven_join",
    "proven_formula",
    "proven_taxonomy",
    "user_confirmed",
}


@dataclass(frozen=True)
class BlockerQuestionPanelResult:
    output_dir: str
    current_json: str
    current_markdown: str
    index_json: str
    question_count: int
    current_feature: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class BlockerQuestionPanelBuilder:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        mapping_path: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.mapping_path = (
            (self.repo_root / mapping_path).resolve()
            if mapping_path
            else self.layout.contracts_dir / "kpi_feature_mapping.json"
        )
        self.output_dir = (
            (self.repo_root / output_dir).resolve()
            if output_dir
            else self.layout.reports_dir / "blocker_question_panel"
        )

    def run(self) -> BlockerQuestionPanelResult:
        if not self.mapping_path.exists():
            raise FileNotFoundError(f"feature mapping not found: {self.mapping_path}")
        mapping = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

        questions = _build_questions(mapping, self.workspace, self.repo_root)
        current = questions[0] if questions else _empty_panel(mapping, self.workspace, self.repo_root)

        current_json = self.output_dir / "current.json"
        current_markdown = self.output_dir / "current.md"
        index_json = self.output_dir / "index.json"
        current_json.write_text(json.dumps(current, indent=2, default=str) + "\n", encoding="utf-8")
        current_markdown.write_text(_render_markdown(current), encoding="utf-8")
        index_json.write_text(
            json.dumps(
                {
                    "version": PANEL_VERSION,
                    "workspace": _rel(self.workspace, self.repo_root),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "current_question": _rel(current_json, self.repo_root),
                    "questions": questions,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )

        return BlockerQuestionPanelResult(
            output_dir=_rel(self.output_dir, self.repo_root),
            current_json=_rel(current_json, self.repo_root),
            current_markdown=_rel(current_markdown, self.repo_root),
            index_json=_rel(index_json, self.repo_root),
            question_count=len(questions),
            current_feature=str(current.get("feature", "")),
        )


def _build_questions(
    mapping: dict[str, Any],
    workspace: Path,
    repo_root: Path,
) -> list[dict[str, Any]]:
    feature_items = _feature_items(mapping)
    clusters = mapping.get("blocker_clusters") or []
    if not clusters:
        clusters = _clusters_from_features(feature_items)
    questions = []
    for cluster in clusters:
        feature = str(cluster.get("feature") or "")
        if not feature:
            continue
        items = feature_items.get(_norm(feature), [])
        if not items:
            continue
        questions.append(_question_for_cluster(mapping, workspace, repo_root, cluster, items))
    return questions


def _feature_items(mapping: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    items: dict[str, list[dict[str, Any]]] = {}
    for kpi in mapping.get("kpis", []):
        for feature in kpi.get("features", []):
            if feature.get("state") in READY_STATES:
                continue
            name = str(feature.get("feature") or "")
            items.setdefault(_norm(name), []).append({"kpi": kpi, "feature": feature})
    return items


def _clusters_from_features(items: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    clusters = []
    for feature_items in items.values():
        first = feature_items[0]["feature"]
        clusters.append(
            {
                "feature": first.get("feature", ""),
                "count": len(feature_items),
                "risk": "unknown",
                "examples": [item["kpi"].get("kpi_id", "") for item in feature_items[:3]],
            }
        )
    clusters.sort(key=lambda item: (-int(item.get("count", 0)), str(item.get("feature", ""))))
    return clusters


def _question_for_cluster(
    mapping: dict[str, Any],
    workspace: Path,
    repo_root: Path,
    cluster: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    feature = str(cluster.get("feature") or items[0]["feature"].get("feature") or "")
    derived_options = _valid_derived_options(items)
    physical_options = _physical_column_options(items)
    applies_to = [str(item["kpi"].get("kpi_id") or "") for item in items]
    evidence_files = _evidence_files(items, repo_root)
    base = {
        "version": PANEL_VERSION,
        "workspace": _rel(workspace, repo_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question_id": f"workspace_{_slug(feature)}",
        "feature": feature,
        "applies_to_kpis": applies_to,
        "reuse_scope": "workspace_level" if len(applies_to) > 1 else "kpi_specific",
        "risk": cluster.get("risk", "unknown"),
        "status": "needs_user_answer",
        "source_mapping": _rel(workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json", repo_root),
        "evidence_files": evidence_files,
    }
    if derived_options:
        return {
            **base,
            "blocker": (
                f"`{feature}` is unresolved and has JSON-backed candidate derivation "
                f"option(s) for {len(applies_to)} KPI(s)."
            ),
            "question": f"Which JSON-backed definition should be accepted for `{feature}`?",
            "answer_type": "select_json_backed_option_or_custom_rule",
            "recommended_option_id": "option_a",
            "recommended_answer": "Review Option A and accept only if the formula matches the business definition.",
            "why": (
                "Option A is profile-backed and includes formula, inputs, observed values, evidence sources, "
                "and derivation reasoning. It is still candidate evidence, not ground truth."
            ),
            "options": [
                _derived_option_payload(option, idx)
                for idx, option in enumerate(derived_options, start=1)
            ]
            + [_custom_rule_option(feature)],
        }
    if physical_options:
        return {
            **base,
            "blocker": (
                f"`{feature}` is unresolved for {len(applies_to)} KPI(s), and multiple "
                "profile-backed physical column candidates are available."
            ),
            "question": f"Which physical column should define `{feature}` as a workspace-level mapping?",
            "answer_type": "select_physical_column_or_custom_rule",
            "recommended_option_id": "option_a",
            "recommended_answer": (
                "Review Option A and accept it only if the source and grain match the KPI intent."
            ),
            "why": (
                "These options come from schema/profile alias evidence. They are candidate mappings, "
                "not accepted business truth until confirmed."
            ),
            "options": [
                _physical_option_payload(option, idx)
                for idx, option in enumerate(physical_options, start=1)
            ]
            + [_custom_rule_option(feature)],
        }
    return {
        **base,
        "blocker": (
            f"`{feature}` is unresolved for {len(applies_to)} KPI(s), and no valid JSON-backed "
            "derived formula option is currently available."
        ),
        "question": f"What authoritative source, physical column, or accepted workspace rule defines `{feature}`?",
        "answer_type": "direct_mapping_or_business_rule",
        "recommended_option_id": "option_a",
        "recommended_answer": "Use a direct source-backed mapping or provide a data dictionary/business rule.",
        "why": (
            "A formula should not be invented when the resolver cannot produce a valid evidence-backed "
            "derived option. This answer can be saved as a reusable workspace definition."
        ),
        "options": [
            {
                "option_id": "option_a",
                "label": "Provide direct source-backed definition",
                "business_summary": (
                    f"Map `{feature}` to a physical column, data dictionary field, source-origin rule, "
                    "or accepted business definition."
                ),
                "expected_answer_shape": {
                    "feature": feature,
                    "resolution_type": "direct_column | source_origin_rule | business_formula | taxonomy",
                    "source_columns": [],
                    "formula": "",
                    "grain": "",
                    "evidence_source": "",
                    "applies_to_kpis": applies_to,
                },
                "json_backed": False,
            },
            _custom_rule_option(feature),
        ],
    }


def _valid_derived_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options = []
    seen = set()
    for item in items:
        for option in item["feature"].get("derived_feature_options") or []:
            if not option.get("input_columns"):
                continue
            if option.get("missing_inputs"):
                continue
            key = (
                option.get("derived_column_name"),
                option.get("formula"),
                option.get("source_pattern_id"),
            )
            if key in seen:
                continue
            seen.add(key)
            options.append(option)
    return options


def _physical_column_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options = []
    seen = set()
    for item in items:
        feature = item["feature"]
        for column in feature.get("source_columns") or []:
            key = (column.get("dataset"), column.get("column"))
            if key in seen:
                continue
            seen.add(key)
            options.append(
                {
                    "feature_state": feature.get("state"),
                    "resolution_type": feature.get("resolution_type"),
                    "dataset": column.get("dataset"),
                    "column": column.get("column"),
                    "dtype": column.get("dtype"),
                    "row_count": column.get("row_count"),
                    "profile_path": column.get("profile_path"),
                    "observed_values": column.get("observed_values") or [],
                    "value_profile": column.get("value_profile") or {},
                    "semantic_meaning_sources": column.get("semantic_meaning_sources") or [],
                    "evidence_state": "schema_profile_alias_candidate",
                    "reason": column.get("reason")
                    or "Column matched the unresolved KPI term through configured business/schema aliases.",
                }
            )
    options.sort(key=lambda item: (str(item.get("dataset") or ""), str(item.get("column") or "")))
    return options


def _physical_option_payload(option: dict[str, Any], idx: int) -> dict[str, Any]:
    option_id = f"option_{chr(ord('a') + idx - 1)}"
    label = f"{option.get('dataset', 'unknown')}.{option.get('column', 'unknown')}"
    return {
        "option_id": option_id,
        "label": label,
        "business_summary": (
            f"Use `{option.get('column')}` from `{option.get('dataset')}` as the accepted "
            "workspace mapping."
        ),
        "json_backed": True,
        "evidence_state": option.get("evidence_state"),
        "confidence": "medium",
        "needs_user_confirmation": True,
        "physical_column_option": option,
    }


def _derived_option_payload(option: dict[str, Any], idx: int) -> dict[str, Any]:
    option_id = f"option_{chr(ord('a') + idx - 1)}"
    return {
        "option_id": option_id,
        "label": f"Accept candidate formula from `{option.get('source_pattern_id', 'pattern')}`",
        "business_summary": option.get("business_meaning", ""),
        "formula": option.get("formula", ""),
        "json_backed": True,
        "evidence_state": option.get("evidence_state"),
        "confidence": option.get("confidence"),
        "needs_user_confirmation": option.get("needs_user_confirmation"),
        "derived_feature_option": option,
    }


def _custom_rule_option(feature: str) -> dict[str, Any]:
    return {
        "option_id": "custom",
        "label": "Enter a custom definition",
        "business_summary": f"Provide a different accepted rule for `{feature}`.",
        "expected_answer_shape": {
            "feature": feature,
            "formula": "",
            "input_columns": [],
            "evidence_source": "",
            "reason": "",
        },
        "json_backed": False,
    }


def _evidence_files(items: list[dict[str, Any]], repo_root: Path) -> list[str]:
    files = set()
    for item in items:
        source = item["kpi"].get("source")
        if source:
            files.add(str(source))
        for option in item["feature"].get("derived_feature_options") or []:
            for source_item in option.get("evidence_sources") or []:
                file = source_item.get("file")
                if file:
                    files.add(str(file))
            for column in option.get("input_columns") or []:
                profile = column.get("profile_path")
                if profile:
                    files.add(str(profile))
    return sorted(_rel(Path(file), repo_root) if Path(file).is_absolute() else file for file in files)


def _empty_panel(mapping: dict[str, Any], workspace: Path, repo_root: Path) -> dict[str, Any]:
    return {
        "version": PANEL_VERSION,
        "workspace": _rel(workspace, repo_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question_id": "none",
        "feature": "",
        "status": "no_blocker_questions",
        "blocker": "No unresolved blocker questions were found.",
        "question": "",
        "answer_type": "none",
        "recommended_answer": "",
        "why": "",
        "options": [],
        "summary": mapping.get("summary", {}),
    }


def _render_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# Blocker Question Panel: {panel.get('feature') or 'None'}",
        "",
        f"- Workspace: `{panel.get('workspace', '')}`",
        f"- Applies to KPIs: {', '.join(panel.get('applies_to_kpis') or []) or 'none'}",
        f"- Reuse scope: `{panel.get('reuse_scope', 'none')}`",
        f"- Answer type: `{panel.get('answer_type', '')}`",
        "",
        "## Blocker",
        "",
        str(panel.get("blocker", "")),
        "",
        "## Question",
        "",
        str(panel.get("question", "")),
        "",
        "## Recommended Answer",
        "",
        str(panel.get("recommended_answer", "")),
        "",
        "## Why",
        "",
        str(panel.get("why", "")),
        "",
        "## Options",
        "",
    ]
    for option in panel.get("options") or []:
        lines.extend(
            [
                f"### {option.get('option_id', '')}: {option.get('label', '')}",
                "",
                str(option.get("business_summary", "")),
                "",
            ]
        )
        if option.get("formula"):
            lines.extend(["```sql", str(option["formula"]), "```", ""])
        if option.get("json_backed"):
            evidence = option.get("derived_feature_option") or option.get("physical_column_option") or {}
            lines.extend(["```json", json.dumps(evidence, indent=2, default=str), "```", ""])
    if panel.get("evidence_files"):
        lines.extend(["## Evidence Files", ""])
        for file in panel["evidence_files"]:
            lines.append(f"- `{file}`")
        lines.append("")
    return "\n".join(lines)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_") or "item"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a stakeholder-friendly blocker question panel."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--mapping", help="Optional path to kpi_feature_mapping.json.")
    parser.add_argument("--out", help="Optional output directory.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args()

    result = BlockerQuestionPanelBuilder(
        args.repo_root,
        args.workspace,
        mapping_path=args.mapping,
        output_dir=args.out,
    ).run()
    if args.json:
        print(json.dumps(result.summary(), indent=2))
        return
    print(f"Wrote {result.question_count} blocker question panel(s) to {result.output_dir}")
    print(f"- {result.current_json}")
    print(f"- {result.current_markdown}")
    print(f"- {result.index_json}")


if __name__ == "__main__":
    main()

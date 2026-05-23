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
from core.wiki import WikiLayout, read_feature_note


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
        "artifact_type": "blocker_question_panel/current.json",
        "version": PANEL_VERSION,
        "generated_by": "blocker-question-panel",
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
    prior = _prior_wiki_decision(workspace, repo_root, feature)
    if prior:
        base["prior_decision_wiki"] = prior
    if any(item["feature"].get("resolution_type") == "kpi_definition_required" for item in items):
        return {
            **base,
            "blocker": (
                "The current KPI registry contains a seed placeholder KPI, not a concrete "
                "business metric that can be mapped to data."
            ),
            "question": (
                "Which concrete KPI should replace the seed? Include the business question, "
                "metric expression, grain/dimensions, owner, and acceptance tests."
            ),
            "answer_type": "kpi_definition_required",
            "recommended_option_id": "option_a",
            "recommended_answer": "Provide a concrete RCM KPI definition before mapping features.",
            "why": (
                "Executable KPI logic needs a proven metric and grain. Mapping placeholder words "
                "such as confirm, metric, or grain to columns would create invalid evidence."
            ),
            "options": [
                {
                    "option_id": "option_a",
                    "label": "Provide KPI definition",
                    "business_summary": (
                        "Replace the seed KPI with a concrete metric, for example a denial rate, "
                        "paid amount trend, AR aging, or claim volume KPI with defined grain."
                    ),
                    "expected_answer_shape": {
                        "business_question": "",
                        "metric": "",
                        "grain_or_cuts": "",
                        "owner": "",
                        "acceptance_tests": [],
                        "evidence_source": "",
                        "applies_to_kpis": applies_to,
                    },
                    "json_backed": False,
                },
                {
                    "option_id": "custom",
                    "label": "Restart KPI generation",
                    "business_summary": (
                        "Run KPI generation again with stakeholder context or a richer registry source."
                    ),
                    "expected_answer_shape": {
                        "context_file": "",
                        "generation_notes": "",
                        "applies_to_kpis": applies_to,
                    },
                    "json_backed": False,
                },
            ],
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


def _prior_wiki_decision(workspace: Path, repo_root: Path, feature: str) -> dict[str, Any] | None:
    note = read_feature_note(WikiLayout(project_root=workspace), feature)
    if note is None:
        return None
    fm = note.frontmatter or {}
    return {
        "path": _rel(note.path, repo_root),
        "summary": fm.get("summary") or "",
        "updated": fm.get("updated") or "",
        "user_why": note.why,
        "has_user_why": note.has_user_why,
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
                    "kpi_id": item["kpi"].get("kpi_id"),
                    "kpi_name": item["kpi"].get("name"),
                    "kpi_metric": item["kpi"].get("metric"),
                    "dataset": column.get("dataset"),
                    "column": column.get("column"),
                    "dtype": column.get("dtype"),
                    "row_count": column.get("row_count"),
                    "score": column.get("score"),
                    "profile_path": column.get("profile_path"),
                    "observed_values": column.get("observed_values") or [],
                    "value_profile": column.get("value_profile") or {},
                    "semantic_meaning_sources": column.get("semantic_meaning_sources") or [],
                    "mapping_proof": column.get("mapping_proof") or {},
                    "answer_demo": _answer_demo(item["kpi"], feature, column),
                    "evidence_state": "schema_profile_alias_candidate",
                    "reason": column.get("reason")
                    or "Column matched the unresolved KPI term through configured business/schema aliases.",
                }
            )
    options.sort(
        key=lambda item: (
            -float(item.get("score") or 0),
            str(item.get("dataset") or ""),
            str(item.get("column") or ""),
        )
    )
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


def _answer_demo(
    kpi: dict[str, Any],
    feature: dict[str, Any],
    selected_column: dict[str, Any],
) -> dict[str, Any]:
    dataset = str(selected_column.get("dataset") or "")
    table = Path(dataset).stem or "source_table"
    column = str(selected_column.get("column") or "")
    feature_label = _slug(str(feature.get("feature") or column))
    metric_text = " ".join(
        str(value or "")
        for value in [kpi.get("name"), kpi.get("metric"), kpi.get("cuts")]
    ).lower()
    same_dataset_features = [
        other
        for other in kpi.get("features", [])
        for source in other.get("source_columns") or []
        if str(source.get("dataset") or "") == dataset and source.get("column")
    ]
    cost_column = _first_matching_source_column(kpi, dataset, {"cost", "amount", "base", "claim"})
    select_items = [f'"{column}" AS "{feature_label}"']
    group_by = f'"{column}"'
    order_by = f'"{feature_label}"'
    if "top" in metric_text or "frequent" in metric_text or "number of times" in metric_text:
        select_items.append("COUNT(*) AS row_count")
        order_by = "row_count DESC"
    if cost_column:
        cost_alias = "average_base_cost" if "base cost" in metric_text else "average_cost"
        if "total claim cost" in metric_text:
            cost_alias = "average_total_claim_cost"
        select_items.append(f'AVG("{cost_column.get("column")}") AS {cost_alias}')
        if order_by == f'"{feature_label}"' and "highest" in metric_text:
            order_by = f"{cost_alias} DESC"
    query = (
        "SELECT\n  "
        + ",\n  ".join(select_items)
        + f'\nFROM "{table}"\nGROUP BY {group_by}\nORDER BY {order_by}\nLIMIT 5;'
    )
    output_rows = _demo_output_rows(selected_column, cost_column, feature_label, metric_text)
    source_samples = [_source_column_sample(selected_column)]
    if cost_column:
        source_samples.append(_source_column_sample(cost_column))
    for other in same_dataset_features:
        if len(source_samples) >= 4:
            break
        source = (other.get("source_columns") or [{}])[0]
        key = (source.get("dataset"), source.get("column"))
        if key not in {(item.get("dataset"), item.get("column")) for item in source_samples}:
            source_samples.append(_source_column_sample(source))
    return {
        "demo_type": "profile_sample_query_demo",
        "kpi_id": kpi.get("kpi_id"),
        "kpi_name": kpi.get("name"),
        "query": query,
        "sample_output": output_rows,
        "sample_output_table": _markdown_table(output_rows),
        "source_column_samples": source_samples,
        "note": (
            "Sample output is built from profile sample values to show the query shape. "
            "It is not the full aggregate result."
        ),
    }


def _first_matching_source_column(
    kpi: dict[str, Any],
    dataset: str,
    terms: set[str],
) -> dict[str, Any] | None:
    for feature in kpi.get("features", []):
        feature_name = _norm(str(feature.get("feature") or ""))
        for source in feature.get("source_columns") or []:
            column_name = _norm(str(source.get("column") or ""))
            if str(source.get("dataset") or "") == dataset and (
                feature_name in terms or any(term in column_name for term in terms)
            ):
                return source
    return None


def _demo_output_rows(
    selected_column: dict[str, Any],
    cost_column: dict[str, Any] | None,
    feature_label: str,
    metric_text: str,
) -> list[dict[str, Any]]:
    values = list(selected_column.get("observed_values") or [])
    if not values:
        values = [row.get(_norm(str(selected_column.get("column") or ""))) for row in (selected_column.get("mapping_proof") or {}).get("sample_output") or []]
    cost_values = list((cost_column or {}).get("observed_values") or [])
    rows = []
    for idx, value in enumerate(values[:5]):
        row: dict[str, Any] = {feature_label: value}
        if "top" in metric_text or "frequent" in metric_text or "number of times" in metric_text:
            row["row_count"] = "<computed from grouped rows>"
        if cost_column:
            alias = "average_base_cost" if "base cost" in metric_text else "average_cost"
            if "total claim cost" in metric_text:
                alias = "average_total_claim_cost"
            row[alias] = cost_values[idx] if idx < len(cost_values) else "<computed average>"
        rows.append(row)
    return rows


def _source_column_sample(column: dict[str, Any]) -> dict[str, Any]:
    proof = column.get("mapping_proof") or {}
    sample_output = proof.get("sample_output")
    return {
        "dataset": column.get("dataset"),
        "column": column.get("column"),
        "dictionary_evidence": proof.get("dictionary_evidence"),
        "sample_query": proof.get("sample_query"),
        "sample_output": sample_output,
        "sample_output_table": _markdown_table(sample_output or []),
    }


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    columns = list(rows[0].keys())
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        values = [_table_cell(row.get(column)) for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _table_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


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
        for column in item["feature"].get("source_columns") or []:
            profile = column.get("profile_path")
            if profile:
                files.add(str(profile))
            proof = column.get("mapping_proof") or {}
            for file in proof.get("source_files") or []:
                if file:
                    files.add(str(file))
            for meaning in column.get("semantic_meaning_sources") or []:
                file = meaning.get("file")
                if file:
                    files.add(str(file))
    return sorted(_rel(Path(file), repo_root) if Path(file).is_absolute() else file for file in files)


def _empty_panel(mapping: dict[str, Any], workspace: Path, repo_root: Path) -> dict[str, Any]:
    return {
        "artifact_type": "blocker_question_panel/current.json",
        "version": PANEL_VERSION,
        "generated_by": "blocker-question-panel",
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
    ]
    prior = panel.get("prior_decision_wiki") or {}
    if prior:
        lines.extend(
            [
                "## Prior Decision (from wiki)",
                "",
                f"- Note: `{prior.get('path', '')}`",
                f"- Summary: {prior.get('summary', '')}",
                f"- Last updated: {prior.get('updated', '')}",
                "",
            ]
        )
        if prior.get("has_user_why") and prior.get("user_why"):
            lines.extend(["### User-recorded *why*", "", str(prior.get("user_why")), ""])
    lines += [
        "## Blocker",
        "",
        str(panel.get("blocker", "")),
        "",
        "## Question",
        "",
        str(panel.get("question", "")),
        "",
        "## Instruction",
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

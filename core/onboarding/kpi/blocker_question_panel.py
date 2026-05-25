"""Build a stakeholder-friendly blocker question panel from KPI feature mapping."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout
from core.wiki import WikiLayout, read_feature_note


PANEL_VERSION = 1
INTERACTION_CONTRACT = {
    "display_mode": "project_blocker_panel",
    "primary_artifact": "current.md",
    "answer_source": "current.json",
    "generic_answer_picker_allowed": False,
    "preserve_panel_options_only": True,
    "instruction": (
        "Show current.md verbatim, or render options directly from current.json. "
        "Do not summarize the blocker panel and do not use a generic answer picker "
        "that adds options outside this artifact."
    ),
}
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
    source_truth = _kpi_source_truth(items, workspace, repo_root)
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
        "interaction_contract": INTERACTION_CONTRACT,
        "panel_contract": {
            "display_shape": "full_kpi_truth_packet",
            "source_truth_required": True,
            "option_proof_required": True,
            "default_code_preference": "sql",
            "must_include": [
                "absolute KPI source truth",
                "all KPI fields",
                "source cell or source artifact proof",
                "recommended option",
                "formula or derived logic when present",
                "column and dataset proof",
                "sample values",
                "SQL query or query sketch",
                "demo result table shape",
                "custom fallback option",
            ],
        },
        "default_code_preference": "sql",
        "kpi_source_truth": source_truth,
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
            "question": f"Which definition should be accepted for `{feature}`?",
            "answer_type": "select_json_backed_option_or_custom_rule",
            "recommended_option_id": "custom",
            "recommended_answer": (
                "Provide or confirm an authoritative business definition; accept a JSON-backed "
                "candidate only after confirming the formula, source, and grain."
            ),
            "why": (
                "JSON-backed derived options include formula, inputs, observed values, evidence sources, "
                "and derivation reasoning, but they remain candidate evidence rather than ground truth."
            ),
            "options": [
                _derived_option_payload(option, idx, source_truth)
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
                _physical_option_payload(option, idx, source_truth)
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


def _kpi_source_truth(items: list[dict[str, Any]], workspace: Path, repo_root: Path) -> list[dict[str, Any]]:
    registry = _load_json(workspace / "interns" / "generated" / "contracts" / "kpi_registry.json")
    registry_kpis = registry.get("kpis") if isinstance(registry.get("kpis"), list) else []
    registry_by_id = {
        f"kpi_{idx:03d}": kpi
        for idx, kpi in enumerate(registry_kpis, start=1)
        if isinstance(kpi, dict)
    }
    rows = []
    seen = set()
    for item in items:
        kpi = item["kpi"]
        kpi_id = str(kpi.get("kpi_id") or "")
        if kpi_id in seen:
            continue
        seen.add(kpi_id)
        source = kpi | {key: value for key, value in registry_by_id.get(kpi_id, {}).items() if value not in (None, "")}
        source_path = str(source.get("source") or "")
        cell_trace = _excel_cell_trace(source_path, source) if source_path else {}
        rows.append(
            {
                "kpi_id": kpi_id,
                "business_question": str(source.get("name") or ""),
                "description": str(source.get("description") or ""),
                "metric": str(source.get("metric") or ""),
                "cuts": _split_cuts(str(source.get("cuts") or "")),
                "source": _source_label(source_path, repo_root),
                "source_cells": cell_trace.get("cells", {}),
                "source_sheet": cell_trace.get("sheet", ""),
                "source_truth_note": (
                    "Source workbook text is authoritative for KPI question, description, "
                    "metric wording, cuts, filters, and continuation rows. It is not proof of "
                    "joins, derived formulas, or executable grain unless those are explicit."
                ),
            }
        )
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _split_cuts(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _source_label(path: str, repo_root: Path) -> str:
    if not path:
        return ""
    source_path = Path(path)
    return _rel(source_path, repo_root) if source_path.is_absolute() else path


def _excel_cell_trace(source_path: str, source: dict[str, Any]) -> dict[str, Any]:
    path = Path(source_path)
    if not path.exists() or path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return {}
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {}
    try:
        wb = load_workbook(path, data_only=False, read_only=False)
    except Exception:
        return {}
    target_question = str(source.get("name") or "").strip()
    if not target_question:
        return {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            first = row[0].value if row else None
            if str(first or "").strip() != target_question:
                continue
            row_number = row[0].row
            cells = {
                "business_question": row[0].coordinate,
                "description": row[1].coordinate if len(row) > 1 else "",
                "first_cut": row[2].coordinate if len(row) > 2 else "",
                "metric": row[3].coordinate if len(row) > 3 else "",
            }
            cut_cells = []
            for scan_row in range(row_number, ws.max_row + 1):
                next_question = ws.cell(scan_row, 1).value
                if scan_row != row_number and next_question not in (None, ""):
                    break
                cut_value = ws.cell(scan_row, 3).value
                if cut_value not in (None, ""):
                    cut_cells.append(ws.cell(scan_row, 3).coordinate)
            if cut_cells:
                cells["cuts"] = ":".join([cut_cells[0], cut_cells[-1]]) if len(cut_cells) > 1 else cut_cells[0]
            return {"sheet": ws.title, "cells": cells}
    return {}


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


def _physical_option_payload(
    option: dict[str, Any],
    idx: int,
    source_truth: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    option_id = f"option_{chr(ord('a') + idx - 1)}"
    label = f"{option.get('dataset', 'unknown')}.{option.get('column', 'unknown')}"
    proof = _physical_option_proof(option, source_truth or [])
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
        "proof_packet": proof,
        "sql_preference": "sql",
        "query": proof["query"],
        "result_demo_table": proof["result_demo_table"],
    }


def _physical_option_proof(option: dict[str, Any], source_truth: list[dict[str, Any]]) -> dict[str, Any]:
    column = str(option.get("column") or "")
    dataset = str(option.get("dataset") or "")
    samples = list(option.get("observed_values") or option.get("value_profile", {}).get("sample_values") or [])[:8]
    query = option.get("answer_demo", {}).get("query") or (
        f'SELECT "{column}", COUNT(*) AS row_count\n'
        f'FROM "{Path(dataset).stem or "source_table"}"\n'
        f'GROUP BY "{column}"\n'
        "ORDER BY row_count DESC\n"
        "LIMIT 10;"
    )
    return {
        "proof_type": "physical_column_candidate",
        "kpi_source_truth": source_truth,
        "required_columns": [
            {
                "business_field": str(source_truth[0]["business_question"] if source_truth else option.get("kpi_name") or ""),
                "physical_column": column,
                "dataset": dataset,
                "profile_path": option.get("profile_path"),
                "dtype": option.get("dtype"),
                "row_count": option.get("row_count"),
                "evidence_state": option.get("evidence_state"),
                "sample_values": samples,
            }
        ],
        "formula": "",
        "derived_logic": "",
        "proof_sources": option.get("semantic_meaning_sources") or option.get("mapping_proof", {}).get("source_files") or [],
        "sql_preference": "sql",
        "query": query,
        "result_demo_table": option.get("answer_demo", {}).get("sample_output_table") or _markdown_table(
            [{column or "value": value, "row_count": "<computed>"} for value in samples[:5]]
        ),
        "demo_note": option.get("answer_demo", {}).get("note")
        or "Demo rows show expected query output shape from profile samples, not full aggregate results.",
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


def _derived_option_payload(
    option: dict[str, Any],
    idx: int,
    source_truth: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    option_id = f"option_{chr(ord('a') + idx - 1)}"
    proof = _derived_option_proof(option, source_truth or [])
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
        "proof_packet": proof,
        "sql_preference": "sql",
        "query": proof["query"],
        "result_demo_table": proof["result_demo_table"],
    }


def _derived_option_proof(option: dict[str, Any], source_truth: list[dict[str, Any]]) -> dict[str, Any]:
    input_columns = [column for column in option.get("input_columns") or [] if isinstance(column, dict)]
    required_columns = [
        {
            "business_field": str(column.get("input_name") or ""),
            "physical_column": str(column.get("column") or ""),
            "dataset": str(column.get("dataset") or ""),
            "profile_path": column.get("profile_path"),
            "dtype": column.get("dtype"),
            "row_count": column.get("row_count"),
            "evidence_state": column.get("evidence_state") or "schema_profile_inferred",
            "sample_values": list(column.get("observed_values") or column.get("value_profile", {}).get("sample_values") or [])[:8],
            "reason": column.get("reason") or "",
        }
        for column in input_columns
    ]
    formula = str(option.get("formula") or "")
    query = _derived_sql_query(option)
    result_rows = _derived_demo_rows(option)
    return {
        "proof_type": "derived_formula_candidate",
        "kpi_source_truth": source_truth,
        "required_columns": required_columns,
        "formula": formula,
        "formula_templates": option.get("formula_templates") or {},
        "derived_logic": option.get("business_meaning") or "",
        "derivation_reasoning": option.get("derivation_reasoning") or {},
        "proof_sources": option.get("evidence_sources") or [],
        "synthetic_example": option.get("example") or {},
        "sql_preference": "sql",
        "query": query,
        "result_demo_table": _markdown_table(result_rows),
        "demo_note": "Demo rows show expected output shape from candidate formula evidence, not full aggregate results.",
    }


def _derived_sql_query(option: dict[str, Any]) -> str:
    formula = str(option.get("formula") or "")
    derived_column = str(option.get("derived_column_name") or "derived_value")
    inputs = [column for column in option.get("input_columns") or [] if isinstance(column, dict)]
    select_columns = [str(column.get("column") or column.get("input_name") or "") for column in inputs]
    select_columns = [column for column in select_columns if column]
    table = "joined_source"
    if inputs:
        table = Path(str(inputs[-1].get("dataset") or "")).stem or table
    select_exprs = [f'"{column}"' for column in select_columns]
    select_exprs.append(f"{formula} AS \"{derived_column}\"" if formula else f'NULL AS "{derived_column}"')
    return "SELECT\n  " + ",\n  ".join(select_exprs) + f'\nFROM "{table}"\nLIMIT 10;'


def _derived_demo_rows(option: dict[str, Any]) -> list[dict[str, Any]]:
    example = option.get("example") if isinstance(option.get("example"), dict) else {}
    input_values = example.get("input") if isinstance(example.get("input"), dict) else {}
    output_values = example.get("output") if isinstance(example.get("output"), dict) else {}
    if input_values or output_values:
        return [{**input_values, **output_values}]
    row: dict[str, Any] = {}
    for column in option.get("input_columns") or []:
        if not isinstance(column, dict):
            continue
        values = column.get("observed_values") or column.get("value_profile", {}).get("sample_values") or []
        row[str(column.get("column") or column.get("input_name") or "")] = values[0] if values else ""
    if row:
        row[str(option.get("derived_column_name") or "derived_value")] = "<computed>"
        return [row]
    return []


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
        "interaction_contract": INTERACTION_CONTRACT,
        "summary": mapping.get("summary", {}),
    }


def _render_markdown(panel: dict[str, Any]) -> str:
    allowed_option_ids = ", ".join(
        f"`{option.get('option_id', '')}`" for option in panel.get("options") or []
    )
    lines = [
        f"# Blocker Question Panel: {panel.get('feature') or 'None'}",
        "",
        f"- Workspace: `{panel.get('workspace', '')}`",
        f"- Applies to KPIs: {', '.join(panel.get('applies_to_kpis') or []) or 'none'}",
        f"- Reuse scope: `{panel.get('reuse_scope', 'none')}`",
        f"- Answer type: `{panel.get('answer_type', '')}`",
        "",
    ]
    interaction = panel.get("interaction_contract") or {}
    if interaction:
        lines.extend(
            [
                "## Interaction Contract",
                "",
                str(interaction.get("instruction", "")),
                "",
                f"- Display mode: `{interaction.get('display_mode', '')}`",
                f"- Generic answer picker allowed: `{interaction.get('generic_answer_picker_allowed')}`",
                "",
            ]
        )
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
    if panel.get("kpi_source_truth"):
        lines.extend(["## KPI Source Truth", ""])
        for truth in panel.get("kpi_source_truth") or []:
            lines.extend(
                [
                    f"### {truth.get('kpi_id', '')}",
                    "",
                    f"- Business question: {truth.get('business_question', '')}",
                    f"- Description: {truth.get('description', '')}",
                    f"- Metric from source: `{truth.get('metric', '')}`",
                    f"- Cuts / dimensions from source: {', '.join(truth.get('cuts') or [])}",
                    f"- Source: `{truth.get('source', '')}`",
                ]
            )
            if truth.get("source_sheet") or truth.get("source_cells"):
                lines.append(f"- Sheet: `{truth.get('source_sheet', '')}`")
                cells = truth.get("source_cells") or {}
                cell_text = ", ".join(f"{key}={value}" for key, value in cells.items() if value)
                if cell_text:
                    lines.append(f"- Source cells: {cell_text}")
            lines.extend(["", str(truth.get("source_truth_note", "")), ""])
    if panel.get("panel_contract"):
        contract = panel.get("panel_contract") or {}
        lines.extend(
            [
                "## Panel Contract",
                "",
                f"- Display shape: `{contract.get('display_shape', '')}`",
                f"- Default code preference: `{contract.get('default_code_preference', panel.get('default_code_preference', 'sql'))}`",
                "- Required sections: "
                + ", ".join(f"`{item}`" for item in contract.get("must_include", []) or []),
                "",
            ]
        )
    lines += [
        "## Required User-Facing Ask",
        "",
        "Use this section when asking the user for the blocker answer. Do not replace it with a freehand summary.",
        "",
        f"- Question: {panel.get('question', '')}",
        f"- Recommended option id: `{panel.get('recommended_option_id', '')}`",
        f"- Recommended answer: {panel.get('recommended_answer', '')}",
        f"- Allowed option ids: {allowed_option_ids}",
        "",
        "Do not state that another option is recommended unless `current.json` says so.",
        "",
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
        proof = option.get("proof_packet") or {}
        if proof:
            lines.extend(_render_option_proof(proof))
        if option.get("json_backed"):
            evidence = option.get("derived_feature_option") or option.get("physical_column_option") or {}
            lines.extend(["```json", json.dumps(evidence, indent=2, default=str), "```", ""])
    if panel.get("evidence_files"):
        lines.extend(["## Evidence Files", ""])
        for file in panel["evidence_files"]:
            lines.append(f"- `{file}`")
        lines.append("")
    return "\n".join(lines)


def _render_option_proof(proof: dict[str, Any]) -> list[str]:
    lines = ["#### Option Proof Packet", ""]
    if proof.get("derived_logic"):
        lines.extend(["Derived / business logic:", "", str(proof.get("derived_logic")), ""])
    if proof.get("formula"):
        lines.extend(["Formula:", "", "```sql", str(proof.get("formula")), "```", ""])
    required = proof.get("required_columns") or []
    if required:
        lines.extend(
            [
                "Column and dataset proof:",
                "",
                _markdown_table(
                    [
                        {
                            "Business field": row.get("business_field", ""),
                            "Physical column": row.get("physical_column", ""),
                            "Dataset": _source_label(str(row.get("dataset") or ""), PROJECT_ROOT),
                            "Evidence": row.get("evidence_state", ""),
                            "Sample values": ", ".join(str(value) for value in (row.get("sample_values") or [])[:5]),
                        }
                        for row in required
                    ]
                ),
                "",
            ]
        )
    if proof.get("synthetic_example"):
        lines.extend(["Example:", "", "```json", json.dumps(proof.get("synthetic_example"), indent=2, default=str), "```", ""])
    if proof.get("query"):
        lines.extend(
            [
                f"Code preference: `{proof.get('sql_preference', 'sql')}`",
                "",
                "Query for this option:",
                "",
                "```sql",
                str(proof.get("query")),
                "```",
                "",
            ]
        )
    if proof.get("result_demo_table"):
        lines.extend(["Result demo shape:", "", str(proof.get("result_demo_table")), ""])
    if proof.get("demo_note"):
        lines.extend([str(proof.get("demo_note")), ""])
    return lines


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
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT), help="Repository root. Defaults to current directory.")
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

"""All-KPI recommendation and proof packet reporting.

The first implementation is intentionally read-only. It summarizes current KPI
registry, mapping, SQL, execution, and validation evidence without applying
answers or running queries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.memory.workspace_definitions import READY_STATES
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


PACKET_VERSION = 1
SUPPORTED_MODES = {"recommend"}


@dataclass(frozen=True)
class KPIProofPacketResult:
    workspace: str
    mode: str
    packet_id: str
    report_path: str
    report_json_path: str
    evidence_path: str
    kpi_count: int
    ready_for_sql_count: int
    blocked_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class KPIProofPacketBuilder:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        domain: str = "general",
        mode: str = "recommend",
        refresh_from_source: bool = False,
    ) -> None:
        if mode not in SUPPORTED_MODES:
            raise ValueError(
                f"unsupported kpi proof packet mode `{mode}`; first version supports `recommend` only"
            )
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.domain = domain
        self.mode = mode
        self.refresh_from_source = refresh_from_source
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> KPIProofPacketResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        registry_path = self.layout.contracts_dir / "kpi_registry.json"
        mapping_path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        source_plan_path = self.layout.contracts_dir / "source_to_target_plan.json"
        relationship_path = self.layout.contracts_dir / "relationship_contracts.json"
        execution_path = self.layout.evidence_dir / "kpi_execution_harness.json"

        registry = _load_json(registry_path)
        mapping = _load_json(mapping_path)
        source_plan = _load_json(source_plan_path)
        relationships = _load_json(relationship_path)
        execution = _load_json(execution_path)
        validation = self._validation_summary()
        artifacts = {
            "registry": _artifact_state(registry_path, self.repo_root),
            "mapping": _artifact_state(mapping_path, self.repo_root),
            "source_to_target_plan": _artifact_state(source_plan_path, self.repo_root),
            "relationship_contracts": _artifact_state(relationship_path, self.repo_root),
            "execution_harness": _artifact_state(execution_path, self.repo_root),
        }
        packet_id = _packet_id(artifacts)
        kpis = self._kpi_cards(
            registry=registry,
            mapping=mapping,
            source_plan=source_plan,
            relationships=relationships,
            execution=execution,
            validation=validation,
        )
        packet = {
            "artifact_type": "kpi_proof_packet/current.json",
            "version": PACKET_VERSION,
            "generated_by": "kpi-proof-packet",
            "workspace": self.workspace_rel,
            "domain": self.domain,
            "mode": self.mode,
            "refresh_from_source_requested": self.refresh_from_source,
            "refresh_from_source_supported": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "packet_id": packet_id,
            "status": _packet_status(kpis, validation, artifacts),
            "summary": _summary(kpis),
            "artifacts": artifacts,
            "validation": validation,
            "kpis": kpis,
            "next_commands": _next_commands(self.workspace_rel, self.domain, packet_id),
        }

        report_dir = self.layout.reports_dir / "kpi_proof_packet"
        evidence_dir = self.layout.evidence_dir / "kpi_proof_packet"
        report_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "current.md"
        report_json_path = report_dir / "current.json"
        evidence_path = evidence_dir / "current.json"
        rendered = _render_markdown(packet)
        report_path.write_text(rendered, encoding="utf-8")
        report_json_path.write_text(json.dumps(packet, indent=2, default=str) + "\n", encoding="utf-8")
        evidence_path.write_text(json.dumps(packet, indent=2, default=str) + "\n", encoding="utf-8")
        summary = packet["summary"]
        return KPIProofPacketResult(
            workspace=self.workspace_rel,
            mode=self.mode,
            packet_id=packet_id,
            report_path=_rel(report_path, self.repo_root),
            report_json_path=_rel(report_json_path, self.repo_root),
            evidence_path=_rel(evidence_path, self.repo_root),
            kpi_count=int(summary["kpi_count"]),
            ready_for_sql_count=int(summary["ready_for_sql_count"]),
            blocked_count=int(summary["blocked_count"]),
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _validation_summary(self) -> dict[str, Any]:
        try:
            return WorkspaceArtifactValidator(self.repo_root, self.workspace_rel).run().summary()
        except Exception as exc:
            return {
                "ok": False,
                "error_count": 1,
                "warning_count": 0,
                "errors": [str(exc)],
                "warnings": [],
            }

    def _kpi_cards(
        self,
        *,
        registry: dict[str, Any],
        mapping: dict[str, Any],
        source_plan: dict[str, Any],
        relationships: dict[str, Any],
        execution: dict[str, Any],
        validation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        registry_kpis = registry.get("kpis") if isinstance(registry.get("kpis"), list) else []
        mapped_kpis = mapping.get("kpis") if isinstance(mapping.get("kpis"), list) else []
        by_id = {str(kpi.get("kpi_id") or f"kpi_{idx:03d}"): kpi for idx, kpi in enumerate(mapped_kpis, start=1)}
        all_ids = sorted(set(by_id) | {f"kpi_{idx:03d}" for idx, _ in enumerate(registry_kpis, start=1)})
        plan_by_id = {
            str(kpi.get("kpi_id") or ""): kpi
            for kpi in source_plan.get("kpis", [])
            if isinstance(kpi, dict)
        }
        exec_by_id = {
            str(record.get("kpi_id") or ""): record
            for record in execution.get("records", [])
            if isinstance(record, dict)
        }
        return [
            self._kpi_card(
                kpi_id,
                registry_kpis[_kpi_index(kpi_id) - 1] if 0 < _kpi_index(kpi_id) <= len(registry_kpis) else {},
                by_id.get(kpi_id, {}),
                plan_by_id.get(kpi_id, {}),
                relationships,
                exec_by_id.get(kpi_id, {}),
                validation,
            )
            for kpi_id in all_ids
        ]

    def _kpi_card(
        self,
        kpi_id: str,
        registry_kpi: dict[str, Any],
        mapped_kpi: dict[str, Any],
        plan_kpi: dict[str, Any],
        relationships: dict[str, Any],
        execution_record: dict[str, Any],
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        features = mapped_kpi.get("features") if isinstance(mapped_kpi.get("features"), list) else []
        mapping_rows = [_mapping_row(feature) for feature in features]
        gates = _reliability_gates(
            registry_kpi=registry_kpi,
            mapped_kpi=mapped_kpi,
            mapping_rows=mapping_rows,
            plan_kpi=plan_kpi,
            relationships=relationships,
            execution_record=execution_record,
            validation=validation,
            sql_path=self.layout.solutions_dir / f"{kpi_id}.sql",
            repo_root=self.repo_root,
        )
        status = _kpi_status(gates)
        return {
            "kpi_id": kpi_id,
            "status": status,
            "recommendation_class": _recommendation_class(features, gates),
            "business_question": str(mapped_kpi.get("name") or registry_kpi.get("name") or ""),
            "description": str(registry_kpi.get("description") or ""),
            "metric": str(mapped_kpi.get("metric") or registry_kpi.get("metric") or ""),
            "cuts": str(mapped_kpi.get("cuts") or registry_kpi.get("cuts") or ""),
            "source": str(registry_kpi.get("source") or mapped_kpi.get("source") or ""),
            "excel_traceability": _excel_traceability(registry_kpi, mapped_kpi),
            "mapping_rows": mapping_rows,
            "reliability_gates": gates,
            "planned_sql_shape": _planned_sql_shape(kpi_id, mapped_kpi or registry_kpi, mapping_rows),
            "generated_sql": _sql_summary(self.layout.solutions_dir / f"{kpi_id}.sql", self.repo_root),
            "execution": _execution_summary(execution_record),
            "next_action": _next_action(status, gates, self.workspace_rel, self.domain, kpi_id),
        }


def _mapping_row(feature: dict[str, Any]) -> dict[str, Any]:
    name = str(feature.get("feature") or "")
    state = str(feature.get("state") or "")
    resolution = str(feature.get("resolution_type") or "")
    source = _first_source(feature)
    recommendation = _feature_recommendation(feature, source)
    samples = source.get("observed_values") or source.get("value_profile", {}).get("sample_values") or []
    return {
        "feature": name,
        "state": state,
        "resolution_type": resolution,
        "recommendation": recommendation,
        "mapping_or_source_column": _mapping_label(feature, source),
        "dataset": _dataset_label(source),
        "profile_path": source.get("profile_path", ""),
        "confidence": str(feature.get("confidence") or source.get("confidence") or _confidence_from_state(state)),
        "evidence_state": str(feature.get("evidence_state") or source.get("evidence_state") or ""),
        "sample_values": samples[:8] if isinstance(samples, list) else [],
        "needs_user_confirmation": state not in READY_STATES,
    }


def _first_source(feature: dict[str, Any]) -> dict[str, Any]:
    for key in ("source_columns", "candidate_columns"):
        values = feature.get(key)
        if isinstance(values, list) and values and isinstance(values[0], dict):
            return values[0]
    for option in feature.get("derived_feature_options") or []:
        if isinstance(option, dict):
            return option
    return {}


def _mapping_label(feature: dict[str, Any], source: dict[str, Any]) -> str:
    if feature.get("resolution_type") == "derived_formula":
        return "Derived: " + str(feature.get("formula") or source.get("formula") or feature.get("feature") or "")
    column = source.get("column") or feature.get("column")
    if column:
        return str(column)
    if source.get("formula"):
        return "Derived: " + str(source.get("formula"))
    return "unresolved"


def _dataset_label(source: dict[str, Any]) -> str:
    dataset = str(source.get("dataset") or "")
    if not dataset:
        inputs = source.get("input_columns") or []
        datasets = sorted({_dataset_label(item) for item in inputs if isinstance(item, dict)})
        return " & ".join(item for item in datasets if item) or "unresolved"
    return Path(dataset).name


def _feature_recommendation(feature: dict[str, Any], source: dict[str, Any]) -> str:
    state = str(feature.get("state") or "")
    if state in READY_STATES:
        return "Accepted from current mapping evidence."
    if source:
        return "Review candidate before execution."
    return str(feature.get("question") or "Provide a physical mapping, derivation, or business definition.")


def _confidence_from_state(state: str) -> str:
    if state in READY_STATES:
        return "high"
    if state.startswith("candidate"):
        return "medium"
    return "blocked"


def _excel_traceability(registry_kpi: dict[str, Any], mapped_kpi: dict[str, Any]) -> list[dict[str, str]]:
    return [
        _trace_row("Key business question", registry_kpi.get("name"), mapped_kpi.get("name")),
        _trace_row("Description", registry_kpi.get("description"), mapped_kpi.get("description")),
        _trace_row("Metric", registry_kpi.get("metric"), mapped_kpi.get("metric")),
        _trace_row("Cuts", registry_kpi.get("cuts"), mapped_kpi.get("cuts")),
    ]


def _trace_row(field: str, source_value: Any, normalized_value: Any) -> dict[str, str]:
    source = str(source_value or "")
    normalized = str(normalized_value or source)
    return {
        "field": field,
        "value_from_excel_or_source": source,
        "normalized_value_used_by_engine": normalized,
        "differs": str(bool(source and normalized and source != normalized)).lower(),
    }


def _reliability_gates(
    *,
    registry_kpi: dict[str, Any],
    mapped_kpi: dict[str, Any],
    mapping_rows: list[dict[str, Any]],
    plan_kpi: dict[str, Any],
    relationships: dict[str, Any],
    execution_record: dict[str, Any],
    validation: dict[str, Any],
    sql_path: Path,
    repo_root: Path,
) -> list[dict[str, str]]:
    datasets = {row["dataset"] for row in mapping_rows if row.get("dataset") and row["dataset"] != "unresolved"}
    return [
        _gate("Excel/source traceability", bool(registry_kpi or mapped_kpi), "KPI registry/source row is available.", "Run onboarding or refresh from source."),
        _gate("Mapping proof", bool(mapping_rows) and all(row["state"] in READY_STATES for row in mapping_rows), "All features are accepted/proven.", "Resolve current KPI blocker panel."),
        _gate("Derivation proof", _derivations_confirmed(mapping_rows), "Derived fields are proven or user-confirmed.", "Review derived-feature evidence and apply an answer."),
        _gate(
            "Relationship proof",
            _relationship_gate_passed(datasets, relationships, plan_kpi),
            "Single-dataset KPI or executable relationship/source-to-target proof exists.",
            "Run build-relationship-contracts and plan-source-to-target.",
        ),
        _gate("SQL validation", sql_path.exists(), f"Generated SQL exists at {_rel(sql_path, repo_root)}.", "Run generate-kpi-sql after mappings pass."),
        _gate("Execution result", execution_record.get("status") == "passed", "Execution harness passed and produced a result table.", "Run run-kpi-execution-harness."),
        _gate("Artifact validation", validation.get("ok") is True, "Workspace artifact validator passed.", "Run validate-workspace-artifacts and fix errors."),
    ]


def _gate(name: str, passed: bool, pass_detail: str, fail_detail: str) -> dict[str, str]:
    return {
        "gate": name,
        "status": "passed" if passed else "failed",
        "detail": pass_detail if passed else fail_detail,
    }


def _derivations_confirmed(mapping_rows: list[dict[str, Any]]) -> bool:
    derived = [row for row in mapping_rows if "derived" in row["resolution_type"] or str(row["mapping_or_source_column"]).startswith("Derived:")]
    return not derived or all(row["state"] in READY_STATES for row in derived)


def _relationship_gate_passed(
    datasets: set[str],
    relationships: dict[str, Any],
    plan_kpi: dict[str, Any],
) -> bool:
    if len(datasets) <= 1:
        return True
    if plan_kpi.get("status") in {"ready_for_generation", "ready_for_sql"}:
        return True
    for relationship in relationships.get("relationships") or []:
        policy = relationship.get("executable_usage_policy") or {}
        if policy.get("allowed_in_sql_generation") is True:
            return True
        if relationship.get("state") in {"proven_data_model", "user_confirmed"}:
            return True
    return False


def _kpi_status(gates: list[dict[str, str]]) -> str:
    passed = {gate["gate"] for gate in gates if gate["status"] == "passed"}
    if len(passed) == len(gates):
        return "Ready for SQL"
    if "Mapping proof" not in passed:
        return "Mapping blocked"
    if "SQL validation" not in passed:
        return "Mapping complete, SQL not proven"
    if "Execution result" not in passed:
        return "SQL generated, execution not proven"
    return "Execution proven, validation blocked"


def _recommendation_class(features: list[dict[str, Any]], gates: list[dict[str, str]]) -> str:
    if not features:
        return "blocked"
    states = {str(feature.get("state") or "") for feature in features}
    if all(state in READY_STATES for state in states):
        return "auto-acceptable"
    if any(state.startswith("candidate") for state in states):
        return "review-needed"
    return "blocked"


def _planned_sql_shape(kpi_id: str, kpi: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    selected = [row["mapping_or_source_column"] for row in rows if row.get("mapping_or_source_column") != "unresolved"]
    if not selected:
        return f"-- {kpi_id}: SQL shape unavailable until feature mappings exist."
    select_lines = ",\n  ".join(f"{item} AS {row['feature']}" for item, row in zip(selected, rows))
    return "\n".join(
        [
            f"-- Planned SQL shape for {kpi_id}; not execution proof.",
            "SELECT",
            f"  {select_lines}",
            "FROM <resolved source joins>",
            "-- Apply metric, filters, grain, and cuts after gates pass.",
        ]
    )


def _sql_summary(sql_path: Path, repo_root: Path) -> dict[str, str]:
    if not sql_path.exists():
        return {"status": "missing", "path": _rel(sql_path, repo_root), "preview": ""}
    text = sql_path.read_text(encoding="utf-8")
    preview = "\n".join(text.splitlines()[:80])
    return {"status": "present", "path": _rel(sql_path, repo_root), "preview": preview}


def _execution_summary(record: dict[str, Any]) -> dict[str, Any]:
    if not record:
        return {"status": "missing", "sample_output_table": ""}
    return {
        "status": str(record.get("status") or ""),
        "row_count": record.get("row_count"),
        "columns": record.get("columns") or [],
        "sample_output_table": str(record.get("sample_output_table") or ""),
        "errors": record.get("errors") or [],
        "warnings": record.get("warnings") or [],
    }


def _next_action(status: str, gates: list[dict[str, str]], workspace: str, domain: str, kpi_id: str) -> str:
    first_failed = next((gate["gate"] for gate in gates if gate["status"] != "passed"), "")
    if first_failed == "Excel/source traceability":
        return f"uv run onboard-workspace --workspace {workspace}"
    if first_failed == "Mapping proof":
        return f"uv run prepare-kpi-blocker-panel --workspace {workspace} --domain {domain}"
    if first_failed == "Relationship proof":
        return f"uv run build-relationship-contracts --workspace {workspace}"
    if first_failed == "SQL validation":
        return f"uv run generate-kpi-sql --workspace {workspace} --kpi-id {kpi_id}"
    if first_failed == "Execution result":
        return f"uv run run-kpi-execution-harness --workspace {workspace}"
    if first_failed == "Artifact validation":
        return f"uv run validate-workspace-artifacts --workspace {workspace}"
    return "No next action; KPI is ready."


def _summary(kpis: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "kpi_count": len(kpis),
        "ready_for_sql_count": sum(1 for kpi in kpis if kpi["status"] == "Ready for SQL"),
        "mapping_complete_count": sum(1 for kpi in kpis if kpi["status"].startswith("Mapping complete")),
        "sql_generated_count": sum(1 for kpi in kpis if kpi["status"].startswith("SQL generated")),
        "blocked_count": sum(1 for kpi in kpis if "blocked" in kpi["status"].lower()),
        "auto_acceptable_count": sum(1 for kpi in kpis if kpi["recommendation_class"] == "auto-acceptable"),
        "review_needed_count": sum(1 for kpi in kpis if kpi["recommendation_class"] == "review-needed"),
    }


def _packet_status(kpis: list[dict[str, Any]], validation: dict[str, Any], artifacts: dict[str, Any]) -> str:
    if not artifacts["mapping"]["exists"]:
        return "missing_mapping"
    if any(kpi["status"] == "Mapping blocked" for kpi in kpis):
        return "needs_mapping_review"
    if validation.get("ok") is not True:
        return "validation_blocked"
    if all(kpi["status"] == "Ready for SQL" for kpi in kpis):
        return "all_ready_for_sql"
    return "partial_proof"


def _next_commands(workspace: str, domain: str, packet_id: str) -> dict[str, list[str]]:
    return {
        "review": [f"uv run kpi-proof-packet --workspace {workspace} --domain {domain}"],
        "resolve_blockers": [f"uv run prepare-kpi-blocker-panel --workspace {workspace} --domain {domain}"],
        "future_apply_safe": [
            f"uv run kpi-proof-packet --workspace {workspace} --domain {domain} --mode apply-safe --approve-safe-recommendations --packet-id {packet_id}"
        ],
        "future_execute": [
            f"uv run kpi-proof-packet --workspace {workspace} --domain {domain} --mode execute --approve-execution --packet-id {packet_id}"
        ],
    }


def _artifact_state(path: Path, repo_root: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": _rel(path, repo_root),
        "exists": exists,
        "sha256": _sha256(path) if exists else "",
        "bytes": path.stat().st_size if exists else 0,
    }


def _packet_id(artifacts: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for name in sorted(artifacts):
        digest.update(name.encode("utf-8"))
        digest.update(str(artifacts[name].get("sha256") or "").encode("utf-8"))
    return digest.hexdigest()[:16]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _kpi_index(kpi_id: str) -> int:
    try:
        return int(kpi_id.rsplit("_", 1)[1])
    except Exception:
        return 0


def _render_markdown(packet: dict[str, Any]) -> str:
    lines = [
        "# KPI Proof Packet",
        "",
        f"- Workspace: `{packet['workspace']}`",
        f"- Mode: `{packet['mode']}`",
        f"- Packet ID: `{packet['packet_id']}`",
        f"- Status: `{packet['status']}`",
        "",
        "## Executive Summary",
        "",
        _summary_table(packet["kpis"]),
        "",
    ]
    if packet.get("refresh_from_source_requested"):
        lines.extend(
            [
                "> `--refresh-from-source` was requested, but the first recommend-only version does not rebuild KPI definitions yet.",
                "",
            ]
        )
    for kpi in packet["kpis"]:
        lines.extend(_render_kpi_card(kpi))
    lines.extend(["## Next Commands", ""])
    for group, commands in packet["next_commands"].items():
        lines.append(f"### {group}")
        lines.extend(f"- `{command}`" for command in commands)
        lines.append("")
    return "\n".join(lines)


def _summary_table(kpis: list[dict[str, Any]]) -> str:
    rows = [
        [
            kpi["kpi_id"],
            kpi["status"],
            kpi["recommendation_class"],
            kpi["business_question"],
            kpi["next_action"],
        ]
        for kpi in kpis
    ]
    return render_markdown_table(["KPI", "Status", "Recommendation", "Question", "Next action"], rows)


def _render_kpi_card(kpi: dict[str, Any]) -> list[str]:
    lines = [
        f"## KPI: {kpi['kpi_id']} ({kpi['status']})",
        "",
        f"Description: {kpi['business_question']}",
        f"Metric: {kpi['metric']}",
        f"Cuts: {kpi['cuts']}",
        "",
        "### Source Row Traceability",
        "",
        render_markdown_table(
            ["Field", "Value from Excel/source", "Normalized value used by engine"],
            [
                [
                    row["field"],
                    row["value_from_excel_or_source"],
                    row["normalized_value_used_by_engine"],
                ]
                for row in kpi["excel_traceability"]
            ],
        ),
        "",
        "### Final Column Mapping / Recommendations",
        "",
        render_markdown_table(
            ["Feature", "Mapping / Source Column", "Dataset", "State", "Confidence"],
            [
                [
                    row["feature"],
                    row["mapping_or_source_column"],
                    row["dataset"],
                    row["state"],
                    row["confidence"],
                ]
                for row in kpi["mapping_rows"]
            ],
        )
        if kpi["mapping_rows"]
        else "_No mapping rows are available yet._",
        "",
        "### Reliability Gates",
        "",
        render_markdown_table(
            ["Gate", "Status", "Detail"],
            [[gate["gate"], gate["status"], gate["detail"]] for gate in kpi["reliability_gates"]],
        ),
        "",
        "### Planned SQL Shape",
        "",
        "```sql",
        kpi["planned_sql_shape"],
        "```",
        "",
    ]
    if kpi["generated_sql"]["status"] == "present":
        lines.extend(["### Generated SQL", "", "```sql", kpi["generated_sql"]["preview"], "```", ""])
    if kpi["execution"].get("sample_output_table"):
        lines.extend(["### Output Preview", "", kpi["execution"]["sample_output_table"], ""])
    sample_rows = _sample_rows(kpi["mapping_rows"])
    if sample_rows:
        lines.extend(
            [
                "### Sample Values",
                "",
                render_markdown_table(["Feature", "Sample values", "Profile"], sample_rows),
                "",
            ]
        )
    lines.extend(["### Next Action", "", f"`{kpi['next_action']}`", ""])
    return lines


def _sample_rows(mapping_rows: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for row in mapping_rows:
        samples = row.get("sample_values") or []
        if samples:
            rows.append(
                [
                    str(row["feature"]),
                    ", ".join(str(item) for item in samples[:5]),
                    str(row.get("profile_path") or ""),
                ]
            )
    return rows


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a read-only all-KPI proof packet.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--domain", default="general")
    parser.add_argument("--mode", default="recommend", choices=sorted(SUPPORTED_MODES))
    parser.add_argument("--refresh-from-source", action="store_true")
    args = parser.parse_args(argv)
    result = KPIProofPacketBuilder(
        args.repo_root,
        args.workspace,
        domain=args.domain,
        mode=args.mode,
        refresh_from_source=args.refresh_from_source,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

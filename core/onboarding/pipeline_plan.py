from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.onboarding.catalog_contract import CatalogContractBuilder
from core.onboarding.data_quality import DataQualityHarness
from core.onboarding.source_family_contracts import SourceFamilyContractBuilder
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout
from core.contracts.versioning import register_contract

register_contract("pipeline_plan.json", current_version=1)


@dataclass(frozen=True)
class RouteResult:
    json_path: str
    markdown_path: str
    selected_track: str
    start_layer: str

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class PipelinePlanResult:
    json_path: str
    markdown_path: str
    selected_track: str
    table_format: str
    status: str

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class PipelineDecisionRecorder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.path = self.layout.contracts_dir / "pipeline_decisions.json"

    def _load(self) -> dict[str, Any]:
        return _load_json(self.path) or {
            "artifact_type": "pipeline_decisions.json",
            "version": 1,
            "generated_by": "apply-pipeline-decision",
            "workspace": _rel(self.workspace, self.repo_root),
            "table_format": "",
            "percentage_denominator_scopes": {},
        }

    def _write(self, data: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return data

    def record_table_format(self, table_format: str, *, reason: str = "") -> dict[str, Any]:
        data = self._load()
        data["table_format"] = table_format
        data["table_format_reason"] = reason
        return self._write(data)

    def record_denominator_scope(self, kpi_id: str, scope: str, *, reason: str = "") -> dict[str, Any]:
        data = self._load()
        data.setdefault("percentage_denominator_scopes", {})[kpi_id] = scope
        data.setdefault("percentage_denominator_scope_reasons", {})[kpi_id] = reason
        return self._write(data)

    def record_grain_bucketing(self, kpi_id: str, decision: str, *, reason: str = "") -> dict[str, Any]:
        """Record a grain-bucketing decision for a share metric cut by a raw
        continuous dimension (e.g. ``band_continuous_cuts`` or
        ``exact_value_grain``). Once recorded, the generator stops blocking and
        emits the view. Mirrors record_denominator_scope."""
        data = self._load()
        data.setdefault("grain_bucketing_decisions", {})[kpi_id] = decision
        data.setdefault("grain_bucketing_reasons", {})[kpi_id] = reason
        return self._write(data)


class PipelineFormatPanel:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def prepare(self) -> dict[str, Any]:
        panel = {
            "artifact_type": "pipeline_format/current.json",
            "version": 1,
            "generated_by": "prepare-pipeline-format-panel",
            "question": "What table/file format should medallion outputs use?",
            "options": [
                {"option_id": "option_a", "label": "Delta", "value": "delta"},
                {"option_id": "option_b", "label": "Local Parquet", "value": "local_parquet"},
            ],
            "recommended_option_id": "option_a",
        }
        out = self.layout.reports_dir / "pipeline_format"
        out.mkdir(parents=True, exist_ok=True)
        (out / "current.json").write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        (out / "current.md").write_text("# Pipeline Format\n\n- Option A: Delta\n- Option B: Local Parquet\n", encoding="utf-8")
        return panel

    def apply(self, answer: str) -> dict[str, Any]:
        table_format = "delta" if answer in {"option_a", "delta", "Delta"} else "local_parquet"
        return PipelineDecisionRecorder(self.repo_root, _rel(self.workspace, self.repo_root)).record_table_format(
            table_format,
            reason=f"Accepted {answer}",
        )


class DataEngineeringRoutePlanner:
    def __init__(self, repo_root: str | Path, workspace: str | Path, *, track: str = "auto", target_engine: str = "sql") -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.track = track
        self.target_engine = target_engine
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def build(self) -> RouteResult:
        self.layout.ensure_runtime_dirs()
        if not (self.layout.contracts_dir / "catalog_contract.json").exists():
            CatalogContractBuilder(self.repo_root, _rel(self.workspace, self.repo_root)).build()
        kpis = _load_json(self.layout.contracts_dir / "kpi_registry.json").get("kpis", [])
        catalog = _load_json(self.layout.contracts_dir / "catalog_contract.json")
        quality = DataQualityHarness(self.repo_root, _rel(self.workspace, self.repo_root)).run().summary()
        selected, reason = _select_route(self.track, kpis, catalog, quality)
        source_family_summary: dict[str, Any] = {"family_count": 0}
        if selected == "medallion" and not kpis:
            SourceFamilyContractBuilder(self.repo_root, _rel(self.workspace, self.repo_root)).build()
            source_family_summary = _load_json(self.layout.contracts_dir / "source_family_contracts.json").get("summary", {})
        route = {
            "artifact_type": "data_engineering_route.json",
            "version": 1,
            "generated_by": "prepare-data-engineering-route",
            "workspace": _rel(self.workspace, self.repo_root),
            "requested_track": self.track,
            "selected_track": selected,
            "start_layer": "raw",
            "target_engine": self.target_engine,
            "catalog_object_count": len(catalog.get("objects", [])),
            "source_family_summary": source_family_summary,
            "catalog_contract": _rel(self.layout.contracts_dir / "catalog_contract.json", self.repo_root),
            "quality_assessment": quality,
            "route_reason": reason,
            "decision_inputs": {
                "kpi_count": len(kpis),
                "catalog_object_count": len(catalog.get("objects", [])),
                "data_quality_status": quality.get("status", ""),
                "unresolved_quality_findings": quality.get("unresolved_finding_count", 0),
            },
            "remote_policy": {"mode": "local_first", "remote_mutation_requires_explicit_approval": True},
            "status": "ready_for_pipeline_plan",
        }
        json_path = self.layout.contracts_dir / "data_engineering_route.json"
        md_path = self.layout.reports_dir / "data_engineering_route.md"
        json_path.write_text(json.dumps(route, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(
            "# Data Engineering Route\n\n"
            f"- Requested track: `{self.track}`\n"
            f"- Selected track: `{selected}`\n"
            f"- Reason: {reason}\n"
            f"- Data quality status: `{quality.get('status', '')}`\n"
            f"- Catalog objects: `{len(catalog.get('objects', []))}`\n",
            encoding="utf-8",
        )
        return RouteResult(_rel(json_path, self.repo_root), _rel(md_path, self.repo_root), selected, "raw")


class PipelinePlanner:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        track: str = "auto",
        target_engine: str = "sql",
        table_format: str = "auto",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.track = track
        self.target_engine = target_engine
        self.table_format = table_format
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def build(self) -> PipelinePlanResult:
        self.layout.ensure_runtime_dirs()
        route = _load_json(self.layout.contracts_dir / "data_engineering_route.json")
        selected = self.track if self.track != "auto" else route.get("selected_track", "kpi_only")
        decisions = _load_json(self.layout.contracts_dir / "pipeline_decisions.json")
        table_format = self.table_format if self.table_format != "auto" else decisions.get("table_format", "")
        blockers: list[dict[str, Any]] = []
        if selected in {"medallion", "etl", "elt", "ingestion"} and not table_format:
            table_format = "unresolved"
            blockers.append({"type": "pipeline_table_format_unresolved"})
            PipelineFormatPanel(self.repo_root, _rel(self.workspace, self.repo_root)).prepare()
        if selected == "existing_gold_validation":
            blockers.append({"type": "existing_gold_validation_missing_source_plan"})
        source_plan = _load_json(self.layout.contracts_dir / "source_to_target_plan.json")
        for kpi in source_plan.get("kpis", []):
            text = f"{kpi.get('business_question', '')} {kpi.get('metric', '')}".lower()
            if "percentage" in text and kpi.get("kpi_id") not in decisions.get("percentage_denominator_scopes", {}):
                blockers.append({"type": "percentage_denominator_scope_unresolved", "kpi_id": kpi.get("kpi_id")})
        status = "blocked" if blockers else "ready_for_generation"
        plan = {
            "artifact_type": "pipeline_plan.json",
            "version": 1,
            "generated_by": "prepare-pipeline-plan",
            "workspace": _rel(self.workspace, self.repo_root),
            "selected_track": selected,
            "target_engine": self.target_engine,
            "table_format": table_format,
            "catalog_contract": _rel(self.layout.contracts_dir / "catalog_contract.json", self.repo_root),
            "route_contract": _rel(self.layout.contracts_dir / "data_engineering_route.json", self.repo_root),
            "policy": {
                "remote_mutation_requires_explicit_approval": True,
                "raw_paths_allowed_only_for_bronze_ingestion": True,
            },
            "status": status,
            "decisions": decisions,
            "blockers": blockers,
            "layers": _layers(table_format),
            "quality_gates": ["dedup_application_approval_gated", "raw_paths_limited_to_ingestion_bootstrap"],
        }
        json_path = self.layout.contracts_dir / "pipeline_plan.json"
        md_path = self.layout.reports_dir / "pipeline_plan.md"
        json_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(f"# Pipeline Plan\n\nStatus: `{status}`\n", encoding="utf-8")
        return PipelinePlanResult(_rel(json_path, self.repo_root), _rel(md_path, self.repo_root), selected, table_format, status)


def _layers(table_format: str) -> list[dict[str, Any]]:
    return [
        {"layer": "bronze", "objects": [{"name": "bronze_source", "audit_columns_required": True, "table_format": table_format}]},
        {"layer": "silver", "objects": [{"name": "silver_source", "deduplication": {"application": "approval_gated"}}]},
        {"layer": "gold", "objects": [{"name": "gold_kpi"}]},
    ]


def _select_route(
    requested_track: str,
    kpis: list[dict[str, Any]],
    catalog: dict[str, Any],
    quality: dict[str, Any],
) -> tuple[str, str]:
    if requested_track != "auto":
        return requested_track, f"User explicitly requested `{requested_track}`."
    objects = catalog.get("objects") if isinstance(catalog.get("objects"), list) else []
    unresolved_quality = int(quality.get("unresolved_finding_count") or 0)
    if not kpis:
        return "medallion", "No KPI registry is present, so start with governed source-to-layer modelling."
    if unresolved_quality:
        return "medallion", "Data-quality findings require a Bronze/Silver/Gold path before KPI/reporting outputs."
    if len(objects) > 1:
        return "medallion", "Multiple raw source objects need governed Bronze ingestion, Silver conformance, and Gold KPI views."
    return "kpi_only", "Single-source KPI proof is sufficient; medallion remains optional."


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--track", default="auto")
    parser.add_argument("--target-engine", default="sql")
    parser.add_argument("--table-format", default="auto")
    args = parser.parse_args(argv)
    result = PipelinePlanner(args.repo_root, args.workspace, track=args.track, target_engine=args.target_engine, table_format=args.table_format).build()
    print(json.dumps(result.summary(), indent=2))
    return 0


def route_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--track", default="auto")
    parser.add_argument("--target-engine", default="sql")
    args = parser.parse_args(argv)
    result = DataEngineeringRoutePlanner(
        args.repo_root,
        args.workspace,
        track=args.track,
        target_engine=args.target_engine,
    ).build()
    print(json.dumps(result.summary(), indent=2))
    return 0


def pipeline_main(argv: list[str] | None = None) -> int:
    return main(argv)


def format_panel_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="prepare-pipeline-format-panel",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: PipelineFormatPanel(args.repo_root, args.workspace).prepare(),
        validation="validate-workspace-artifacts",
    )


def format_answer_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--answer", required=True)
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="apply-pipeline-format-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: PipelineFormatPanel(args.repo_root, args.workspace).apply(args.answer),
        op_args={"workspace": args.workspace, "answer": args.answer},
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer},
        record_idempotent=True,
    )


def decision_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--kpi-id", required=True)
    parser.add_argument("--percentage-denominator-scope", default=None)
    parser.add_argument(
        "--grain-bucketing", default=None,
        help=(
            "Grain decision for a share metric cut by a raw continuous dimension: "
            "`band_continuous_cuts` (optionally `band_continuous_cuts:<width>`) or "
            "`exact_value_grain`. Direct path that does not require the full blocker "
            "panel, so it never deadlocks on a not-yet-resolved grain block."
        ),
    )
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)

    scope = args.percentage_denominator_scope
    grain = args.grain_bucketing
    if (scope is None) == (grain is None):
        parser.error(
            "pass exactly one of --percentage-denominator-scope or --grain-bucketing"
        )

    recorder = PipelineDecisionRecorder(args.repo_root, args.workspace)
    if scope is not None:
        return run_workspace_command(
            command="apply-pipeline-decision",
            workspace=args.workspace,
            repo_root=args.repo_root,
            fn=lambda: recorder.record_denominator_scope(
                args.kpi_id, scope,
                reason="Accepted percentage denominator scope.",
            ),
            op_args={
                "workspace": args.workspace,
                "kpi_id": args.kpi_id,
                "percentage_denominator_scope": scope,
            },
            allow_replay=args.allow_replay,
            decision=scope,
            metadata={"kpi_id": args.kpi_id, "percentage_denominator_scope": scope},
            record_idempotent=True,
        )
    return run_workspace_command(
        command="apply-pipeline-decision",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: recorder.record_grain_bucketing(
            args.kpi_id, grain,
            reason="Accepted grain-bucketing decision.",
        ),
        op_args={
            "workspace": args.workspace,
            "kpi_id": args.kpi_id,
            "grain_bucketing": grain,
        },
        allow_replay=args.allow_replay,
        decision=grain,
        metadata={"kpi_id": args.kpi_id, "grain_bucketing": grain},
        record_idempotent=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())

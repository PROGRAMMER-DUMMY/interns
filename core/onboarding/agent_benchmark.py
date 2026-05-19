"""Project-native benchmark scorecard and release gates for AI data-agent workspaces."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


BENCHMARK_VERSION = 1


@dataclass(frozen=True)
class AgentBenchmarkResult:
    workspace: str
    scorecard_path: str
    release_gate_path: str
    current_json_path: str
    current_markdown_path: str
    core_readiness_score: float
    product_maturity_score: float
    blocked_gate_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class AgentBenchmarkScorecardBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path, *, domain: str = "general") -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.domain = domain
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.scorecard_path = self.layout.contracts_dir / "agent_benchmark_scorecard.json"
        self.release_gate_path = self.layout.contracts_dir / "release_gate_status.json"
        self.current_json_path = self.layout.reports_dir / "benchmarks" / "current.json"
        self.current_markdown_path = self.layout.reports_dir / "benchmarks" / "current.md"

    def prepare(self) -> AgentBenchmarkResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        components = self._components()
        core_readiness = _weighted_score(
            components,
            {
                "kpi_readiness": 25,
                "data_model_readiness": 25,
                "relationship_proof": 20,
                "source_to_target_readiness": 20,
                "validation_status": 10,
            },
        )
        product_maturity = _weighted_score(
            components,
            {
                "presentation_readiness": 25,
                "wiki_reuse": 25,
                "workflow_checkpoint": 25,
                "autopilot_safety": 25,
            },
        )
        release_gates = self._release_gates(components, core_readiness, product_maturity)
        scorecard = {
            "artifact_type": "agent_benchmark_scorecard.json",
            "version": BENCHMARK_VERSION,
            "generated_by": "prepare-agent-benchmark",
            "workspace": self.workspace_rel,
            "domain": self.domain,
            "generated_at": _now(),
            "benchmark_scope": "project_native_artifact_scorecard",
            "external_benchmarks": {
                "status": "not_executed_in_v1",
                "reason": "v1 scores current project artifacts only; TPC/Spider/BIRD can plug into this contract later.",
            },
            "scores": {
                "core_readiness": core_readiness,
                "product_maturity": product_maturity,
                "overall": round((core_readiness * 0.75) + (product_maturity * 0.25), 2),
            },
            "components": components,
            "release_gates": release_gates,
            "blocker_routes": _blocker_routes(self.workspace_rel, self.domain, components, release_gates),
        }
        release_gate = {
            "artifact_type": "release_gate_status.json",
            "version": BENCHMARK_VERSION,
            "generated_by": "prepare-agent-benchmark",
            "workspace": self.workspace_rel,
            "generated_at": scorecard["generated_at"],
            "core_readiness_score": core_readiness,
            "product_maturity_score": product_maturity,
            "gates": release_gates,
            "blocked_gates": [gate for gate in release_gates if gate["status"] == "blocked"],
            "allowed_gates": [gate for gate in release_gates if gate["status"] == "allowed"],
        }
        self._write_json(self.scorecard_path, scorecard)
        self._write_json(self.release_gate_path, release_gate)
        self._write_json(self.current_json_path, scorecard)
        self.current_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        self.current_markdown_path.write_text(_render_markdown(scorecard), encoding="utf-8")
        return AgentBenchmarkResult(
            workspace=self.workspace_rel,
            scorecard_path=_rel(self.scorecard_path, self.repo_root),
            release_gate_path=_rel(self.release_gate_path, self.repo_root),
            current_json_path=_rel(self.current_json_path, self.repo_root),
            current_markdown_path=_rel(self.current_markdown_path, self.repo_root),
            core_readiness_score=core_readiness,
            product_maturity_score=product_maturity,
            blocked_gate_count=len(release_gate["blocked_gates"]),
        )

    def _components(self) -> dict[str, dict[str, Any]]:
        validation = self._validation_component()
        return {
            "kpi_readiness": self._kpi_readiness_component(),
            "data_model_readiness": self._data_model_readiness_component(),
            "relationship_proof": self._relationship_component(),
            "source_to_target_readiness": self._source_to_target_component(),
            "validation_status": validation,
            "presentation_readiness": self._presentation_component(),
            "wiki_reuse": self._wiki_component(),
            "workflow_checkpoint": self._workflow_component(),
            "autopilot_safety": self._autopilot_component(validation),
        }

    def _kpi_readiness_component(self) -> dict[str, Any]:
        mapping_path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        mapping = _load_json(mapping_path)
        if mapping:
            summary = mapping.get("summary") or {}
            total = int(summary.get("kpi_count") or len(mapping.get("kpis", [])) or 0)
            ready = int(summary.get("ready_kpi_count") or 0)
            blocked = int(summary.get("blocked_kpi_count") or max(0, total - ready))
            return _component(
                score=_ratio_score(ready, total),
                status="ready" if total and blocked == 0 else "blocked" if blocked else "needs_review",
                evidence=[_rel(mapping_path, self.repo_root)],
                blockers=[] if blocked == 0 else [f"{blocked} KPI(s) still have unresolved feature mappings."],
                details={"kpi_count": total, "ready_kpi_count": ready, "blocked_kpi_count": blocked},
            )
        registry_path = self.layout.contracts_dir / "kpi_registry.json"
        registry = _load_json(registry_path)
        if registry:
            kpis = registry.get("kpis", [])
            return _component(
                score=45.0 if kpis else 20.0,
                status="needs_review",
                evidence=[_rel(registry_path, self.repo_root)],
                blockers=["KPI registry exists but feature mapping is not resolved."],
                details={"kpi_count": len(kpis)},
            )
        return _missing_component("KPI registry and feature mapping are missing.", "onboard-workspace")

    def _data_model_readiness_component(self) -> dict[str, Any]:
        final_path = self.layout.contracts_dir / "data_model_contract.json"
        draft_path = self.layout.requirements_dir / "data_model_draft.json"
        path = final_path if final_path.exists() else draft_path
        model = _load_json(path)
        if not model:
            return _missing_component("Data-model draft or contract is missing.", "prepare-data-model-generation")
        readiness = model.get("readiness") or {}
        score = float((readiness.get("overall") or {}).get("score") or (90 if path == final_path else 55))
        blockers = []
        for values in (readiness.get("blockers") or {}).values():
            blockers.extend(str(item) for item in values)
        if path == draft_path:
            blockers.append("Data model is still draft and needs final preview approval before production use.")
        return _component(
            score=score,
            status="ready" if score >= 80 and path == final_path else "needs_review" if score >= 60 else "blocked",
            evidence=[_rel(path, self.repo_root)],
            blockers=blockers,
            details={
                "state": "finalized" if path == final_path else "draft",
                "table_count": len(model.get("tables", [])),
                "relationship_count": len(model.get("relationships", [])),
            },
        )

    def _relationship_component(self) -> dict[str, Any]:
        path = self.layout.contracts_dir / "relationship_contracts.json"
        payload = _load_json(path)
        if not payload:
            return _component(
                score=35.0,
                status="blocked",
                evidence=[],
                blockers=["Relationship contracts are missing; multi-dataset executable generation must block."],
                details={},
            )
        summary = payload.get("summary") or {}
        total = int(summary.get("relationship_count") or len(payload.get("relationships", [])) or 0)
        executable = int(summary.get("executable_relationship_count") or 0)
        candidates = int(summary.get("candidate_relationship_count") or max(0, total - executable))
        return _component(
            score=100.0 if total == 0 else _ratio_score(executable, total),
            status="ready" if candidates == 0 else "blocked",
            evidence=[_rel(path, self.repo_root)],
            blockers=[] if candidates == 0 else [f"{candidates} relationship(s) are candidate-only and not executable."],
            details={"relationship_count": total, "executable_relationship_count": executable, "candidate_relationship_count": candidates},
        )

    def _source_to_target_component(self) -> dict[str, Any]:
        path = self.layout.contracts_dir / "source_to_target_plan.json"
        payload = _load_json(path)
        if not payload:
            return _component(
                score=30.0,
                status="blocked",
                evidence=[],
                blockers=["Source-to-target plan is missing; executable SQL/ETL/medallion generation must block."],
                details={},
            )
        summary = payload.get("summary") or {}
        total = int(summary.get("kpi_count") or len(payload.get("kpis", [])) or 0)
        ready = int(summary.get("ready_kpi_count") or 0)
        blocked = int(summary.get("blocked_kpi_count") or max(0, total - ready))
        return _component(
            score=_ratio_score(ready, total),
            status="ready" if total and blocked == 0 else "blocked",
            evidence=[_rel(path, self.repo_root)],
            blockers=[] if blocked == 0 else [f"{blocked} KPI source-to-target plan(s) are blocked."],
            details={"kpi_count": total, "ready_kpi_count": ready, "blocked_kpi_count": blocked},
        )

    def _validation_component(self) -> dict[str, Any]:
        try:
            from core.onboarding.workspace_artifact_validator import WorkspaceArtifactValidator

            result = WorkspaceArtifactValidator(self.repo_root, self.workspace_rel).run().summary()
            return _component(
                score=100.0 if result.get("ok") else 45.0,
                status="ready" if result.get("ok") else "blocked",
                evidence=result.get("checked_files", []),
                blockers=list(result.get("errors", [])),
                warnings=list(result.get("warnings", [])),
                details={
                    "checked_file_count": result.get("checked_file_count", 0),
                    "error_count": result.get("error_count", 0),
                    "warning_count": result.get("warning_count", 0),
                },
            )
        except Exception as exc:
            return _component(
                score=0.0,
                status="blocked",
                evidence=[],
                blockers=[f"Artifact validator failed: {type(exc).__name__}: {exc}"],
                details={},
            )

    def _presentation_component(self) -> dict[str, Any]:
        manifest_path = self.layout.reports_dir / "presentation" / "presentation_manifest.json"
        manifest = _load_json(manifest_path)
        generated = manifest.get("generated_paths", []) if manifest else []
        required = {"data-model.svg", "data-model.mermaid.md", "kpi_registry.xlsx"}
        present = {Path(str(path)).name for path in generated if (self.repo_root / str(path)).exists()}
        score = _ratio_score(len(required.intersection(present)), len(required))
        return _component(
            score=score,
            status="ready" if score == 100 else "needs_review",
            evidence=[_rel(manifest_path, self.repo_root)] if manifest else [],
            blockers=[] if score == 100 else ["Presentation SVG/Mermaid/XLSX bundle is incomplete."],
            details={"required": sorted(required), "present": sorted(present)},
        )

    def _wiki_component(self) -> dict[str, Any]:
        path = self.layout.reports_dir / "wiki_memory" / "current.json"
        payload = _load_json(path)
        if not payload:
            return _component(
                score=25.0,
                status="needs_review",
                evidence=[],
                blockers=["Wiki memory reuse report is missing."],
                details={},
            )
        cards = int(payload.get("card_count") or len(payload.get("cards", [])) or 0)
        conflicts = int(payload.get("conflict_count") or 0)
        auto_fill = int(payload.get("auto_fill_count") or 0)
        score = 100.0 if cards and conflicts == 0 else 70.0 if cards else 50.0
        return _component(
            score=score,
            status="ready" if conflicts == 0 else "needs_review",
            evidence=[_rel(path, self.repo_root)],
            blockers=[] if conflicts == 0 else [f"{conflicts} wiki reuse conflict(s) need review."],
            details={"card_count": cards, "conflict_count": conflicts, "auto_fill_count": auto_fill},
        )

    def _workflow_component(self) -> dict[str, Any]:
        path = self.layout.reports_dir / "workflow" / "current.json"
        payload = _load_json(path)
        if not payload:
            return _component(
                score=25.0,
                status="needs_review",
                evidence=[],
                blockers=["Workspace workflow checkpoint is missing."],
                details={},
            )
        return _component(
            score=90.0 if payload.get("status") == "needs_user_choice" else 70.0,
            status="ready",
            evidence=[_rel(path, self.repo_root)],
            blockers=[],
            details={"mode": payload.get("mode", ""), "status": payload.get("status", "")},
        )

    def _autopilot_component(self, validation: dict[str, Any]) -> dict[str, Any]:
        workflow = _load_json(self.layout.reports_dir / "workflow" / "current.json")
        boundaries = workflow.get("autopilot_boundaries") or {}
        must_stop = set(boundaries.get("must_stop_before") or [])
        required_stops = {"final approval", "delete/cleanup", "remote execution", "relationship approval", "executable DDL/dbt/SQL generation"}
        missing_stops = sorted(required_stops - must_stop)
        blockers = []
        if validation.get("status") == "blocked":
            blockers.append("Autopilot cannot finalize while validation is blocked.")
        blockers.extend(f"Autopilot boundary missing: {item}" for item in missing_stops)
        score = 100.0 if not blockers else 55.0
        return _component(
            score=score,
            status="ready" if not blockers else "blocked",
            evidence=[_rel(self.layout.reports_dir / "workflow" / "current.json", self.repo_root)] if workflow else [],
            blockers=blockers,
            details={"required_stops": sorted(required_stops), "configured_stops": sorted(must_stop)},
        )

    def _release_gates(
        self,
        components: dict[str, dict[str, Any]],
        core_readiness: float,
        product_maturity: float,
    ) -> list[dict[str, Any]]:
        kpi_ok = components["kpi_readiness"]["status"] == "ready"
        model_ok = components["data_model_readiness"]["score"] >= 70
        rel_ok = components["relationship_proof"]["status"] == "ready"
        stt_ok = components["source_to_target_readiness"]["status"] == "ready"
        validation_ok = components["validation_status"]["status"] == "ready"
        autopilot_ok = components["autopilot_safety"]["status"] == "ready"
        return [
            _gate("business_review", True, "Business/owner review can continue with current evidence.", []),
            _gate("presentation_review", product_maturity >= 50, "Presentation review can continue when maturity artifacts exist.", []),
            _gate("source_to_target_planning", kpi_ok and model_ok and validation_ok, "Source-to-target planning requires KPI/model/validation proof.", ["prepare-kpi-blocker-panel", "prepare-data-model-blocker-panel", "validate-workspace-artifacts"]),
            _gate("executable_sql_generation", kpi_ok and model_ok and rel_ok and stt_ok and validation_ok, "Executable SQL generation requires KPI, model, relationship, source-to-target, and validation proof.", ["build-relationship-contracts", "plan-source-to-target"]),
            _gate("medallion_or_etl_generation", kpi_ok and model_ok and rel_ok and stt_ok and validation_ok, "Medallion/ETL generation uses the same proof boundary as executable SQL.", ["build-relationship-contracts", "plan-source-to-target"]),
            _gate("bounded_autopilot", core_readiness >= 70 and autopilot_ok and validation_ok, "Autopilot may continue only when validation and safety boundaries are intact.", ["prepare-workspace-workflow"]),
            _gate("production_promotion", core_readiness >= 85 and validation_ok and rel_ok and stt_ok, "Production promotion needs high core readiness and no executable blockers.", ["prepare-agent-benchmark"]),
        ]

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _component(
    *,
    score: float,
    status: str,
    evidence: list[str],
    blockers: list[str],
    details: dict[str, Any],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "score": round(float(score), 2),
        "status": status,
        "evidence_paths": evidence,
        "blockers": blockers,
        "warnings": warnings or [],
        "details": details,
    }


def _missing_component(blocker: str, command: str) -> dict[str, Any]:
    return _component(
        score=0.0,
        status="blocked",
        evidence=[],
        blockers=[blocker],
        details={"recommended_command": command},
    )


def _gate(name: str, allowed: bool, reason: str, unblocker_tools: list[str]) -> dict[str, Any]:
    return {
        "gate": name,
        "status": "allowed" if allowed else "blocked",
        "reason": reason,
        "unblocker_tools": unblocker_tools,
    }


def _blocker_routes(
    workspace: str,
    domain: str,
    components: dict[str, dict[str, Any]],
    gates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    routes = []
    if components["kpi_readiness"]["status"] != "ready":
        routes.append(
            {
                "blocker_type": "kpi_readiness",
                "next_command": f"uv run prepare-kpi-blocker-panel --workspace {workspace} --domain {domain}",
                "reason": "Resolve KPI feature mappings and business definitions.",
            }
        )
    if components["data_model_readiness"]["status"] != "ready":
        routes.append(
            {
                "blocker_type": "data_model_readiness",
                "next_command": f"uv run prepare-data-model-blocker-panel --workspace {workspace}",
                "reason": "Resolve grain, primary key, temporal anchor, SCD, or relationship blockers.",
            }
        )
    if components["relationship_proof"]["status"] != "ready":
        routes.append(
            {
                "blocker_type": "relationship_proof",
                "next_command": f"uv run build-relationship-contracts --workspace {workspace}",
                "reason": "Build executable relationship proof before multi-dataset generation.",
            }
        )
    if components["source_to_target_readiness"]["status"] != "ready":
        routes.append(
            {
                "blocker_type": "source_to_target_readiness",
                "next_command": f"uv run plan-source-to-target --workspace {workspace} --target-engine sql",
                "reason": "Create source-to-target proof before SQL/ETL/medallion generation.",
            }
        )
    if components["validation_status"]["status"] != "ready":
        routes.append(
            {
                "blocker_type": "validation_status",
                "next_command": f"uv run validate-workspace-artifacts --workspace {workspace}",
                "reason": "Fix schema or contract errors before relying on generated artifacts.",
            }
        )
    if components["wiki_reuse"]["status"] != "ready":
        routes.append(
            {
                "blocker_type": "wiki_reuse",
                "next_command": f"uv run prepare-wiki-memory --workspace {workspace} --domain {domain}",
                "reason": "Refresh shared definition reuse and conflict evidence.",
            }
        )
    blocked_gates = [gate["gate"] for gate in gates if gate["status"] == "blocked"]
    for route in routes:
        route["blocked_gates"] = blocked_gates
    return routes


def _weighted_score(components: dict[str, dict[str, Any]], weights: dict[str, int]) -> float:
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0.0
    score = sum(float(components[key]["score"]) * weight for key, weight in weights.items()) / total_weight
    return round(score, 2)


def _ratio_score(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 100.0
    return round(max(0.0, min(100.0, 100.0 * numerator / denominator)), 2)


def _render_markdown(scorecard: dict[str, Any]) -> str:
    scores = scorecard.get("scores", {})
    lines = [
        "# Agent Benchmark Scorecard",
        "",
        f"- Workspace: `{scorecard.get('workspace', '')}`",
        f"- Domain: `{scorecard.get('domain', '')}`",
        f"- Core readiness: `{scores.get('core_readiness', 0)}`",
        f"- Product maturity: `{scores.get('product_maturity', 0)}`",
        f"- Overall: `{scores.get('overall', 0)}`",
        "",
        "## Release Gates",
        "",
    ]
    for gate in scorecard.get("release_gates", []):
        lines.append(f"- `{gate.get('gate')}`: **{gate.get('status')}** - {gate.get('reason')}")
    lines.extend(["", "## Capability Scores", ""])
    for name, component in scorecard.get("components", {}).items():
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Score: `{component.get('score', 0)}`",
                f"- Status: `{component.get('status', '')}`",
            ]
        )
        blockers = component.get("blockers") or []
        if blockers:
            lines.append("- Blockers:")
            lines.extend(f"  - {blocker}" for blocker in blockers[:8])
        evidence = component.get("evidence_paths") or []
        if evidence:
            lines.append("- Evidence:")
            lines.extend(f"  - `{path}`" for path in evidence[:8])
        lines.append("")
    lines.extend(["## Blocker Routes", ""])
    routes = scorecard.get("blocker_routes") or []
    if not routes:
        lines.append("- No blocker routes are currently required.")
    for route in routes:
        lines.extend(
            [
                f"- `{route.get('blocker_type')}`: {route.get('reason')}",
                f"  - `{route.get('next_command')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## External Benchmarks",
            "",
            "TPC/Spider/BIRD execution is not part of v1. This scorecard is the project-native release gate over current governed artifacts.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a project-native AI data-agent benchmark scorecard.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="general")
    args = parser.parse_args(argv)
    result = AgentBenchmarkScorecardBuilder(args.repo_root, args.workspace, domain=args.domain).prepare()
    print(json.dumps(result.summary(), indent=2))
    return 0


"""Run the governed project harness and write scoreable proof artifacts."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.onboarding.evidence_graph import WorkspaceEvidenceGraphBuilder
from core.onboarding.harness.ai_cli_harness import AICLIHarness
from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness
from core.onboarding.benchmark.agent_benchmark import AgentBenchmarkScorecardBuilder
from core.onboarding.kpi.execution_harness import KPIExecutionHarness
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.storage.workspace_layout import WorkspaceLayout
from tools.git_hygiene import collect_staged_paths, render_issues, validate_paths


HARNESS_VERSION = 1


@dataclass(frozen=True)
class ProjectHarnessResult:
    workspace: str
    ok: bool
    score: float
    threshold: float
    status: str
    manifest_path: str
    report_path: str
    checks: dict[str, Any]
    blockers: list[str]
    warnings: list[str]
    next_commands: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "project_harness.json",
            "version": HARNESS_VERSION,
            "generated_by": "validate-project-harness",
            **asdict(self),
        }


class ProjectHarness:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        domain: str = "general",
        threshold: float = 95.0,
        sample_limit: int = 10,
        ai_cli_dataset: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.domain = domain
        self.threshold = threshold
        self.sample_limit = sample_limit
        self.ai_cli_dataset = ai_cli_dataset
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> ProjectHarnessResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        kpi_execution = self._run_kpi_execution()
        validation = WorkspaceArtifactValidator(self.repo_root, self.workspace_rel).run().summary()
        benchmark = AgentBenchmarkScorecardBuilder(
            self.repo_root,
            self.workspace_rel,
            domain=self.domain,
        ).prepare()
        benchmark_payload = _load_json(self.repo_root / benchmark.scorecard_path)
        release_payload = _load_json(self.repo_root / benchmark.release_gate_path)
        git_hygiene = self._git_hygiene()
        ai_cli_harness = self._run_ai_cli_harness()
        workflow_guardrails = self._run_workflow_guardrails()
        evidence_graph = self._run_evidence_graph_health()
        layered_pipeline = self._run_layered_pipeline()
        pipeline_execution_harness = self._run_pipeline_execution_harness()
        data_quality_harness = self._run_data_quality_harness()

        checks = {
            "workspace_artifacts": validation,
            "kpi_execution": kpi_execution,
            "agent_benchmark": {
                **benchmark.summary(),
                "scorecard": benchmark_payload,
                "release_gate": release_payload,
            },
            "git_hygiene": git_hygiene,
            "ai_cli_harness": ai_cli_harness,
            "workflow_guardrails": workflow_guardrails,
            "evidence_graph": evidence_graph,
            "layered_pipeline": layered_pipeline,
            "pipeline_execution_harness": pipeline_execution_harness,
            "data_quality_harness": data_quality_harness,
        }
        blockers = _collect_blockers(checks)
        warnings = _collect_warnings(checks)
        next_commands = _next_commands(self.workspace_rel, self.domain, checks)
        score = _score(checks)
        ok = score >= self.threshold and not _hard_blockers(checks)
        status = "passed" if ok else "blocked"
        manifest_path = self.layout.evidence_dir / "project_harness.json"
        report_path = self.layout.reports_dir / "project_harness.md"
        result = ProjectHarnessResult(
            workspace=self.workspace_rel,
            ok=ok,
            score=score,
            threshold=self.threshold,
            status=status,
            manifest_path=_rel(manifest_path, self.repo_root),
            report_path=_rel(report_path, self.repo_root),
            checks=checks,
            blockers=blockers,
            warnings=warnings,
            next_commands=next_commands,
        )
        manifest_path.write_text(json.dumps(result.summary(), indent=2, default=str) + "\n", encoding="utf-8")
        report_path.write_text(_render_markdown(result), encoding="utf-8")
        return result

    def _run_kpi_execution(self) -> dict[str, Any]:
        if not self.layout.solutions_dir.exists() or not list(self.layout.solutions_dir.glob("kpi_*.sql")):
            return {
                "status": "skipped",
                "ok": True,
                "reason": "No generated KPI SQL files found.",
                "score": 100.0,
            }
        result = KPIExecutionHarness(
            self.repo_root,
            self.workspace_rel,
            sample_limit=self.sample_limit,
        ).run()
        return result.summary()

    def _git_hygiene(self) -> dict[str, Any]:
        try:
            paths = collect_staged_paths(self.repo_root)
        except Exception as exc:
            return {
                "status": "skipped",
                "ok": True,
                "checked_path_count": 0,
                "issue_count": 0,
                "issues": [],
                "summary": f"Git hygiene skipped: {type(exc).__name__}: {exc}",
            }
        issues = validate_paths(paths, repo_root=self.repo_root, max_bytes=25 * 1024 * 1024)
        return {
            "status": "passed" if not issues else "blocked",
            "ok": not issues,
            "checked_path_count": len(paths),
            "issue_count": len(issues),
            "issues": [asdict(issue) for issue in issues],
            "summary": render_issues(issues),
        }

    def _run_ai_cli_harness(self) -> dict[str, Any]:
        if not self.ai_cli_dataset:
            return {
                "status": "skipped",
                "ok": True,
                "reason": "No AI CLI harness dataset provided.",
            }
        result = AICLIHarness(
            self.repo_root,
            self.workspace_rel,
            dataset=self.ai_cli_dataset,
        ).run()
        return result.summary()

    def _run_workflow_guardrails(self) -> dict[str, Any]:
        try:
            result = WorkflowGuardHarness(self.repo_root, self.workspace_rel).run()
            report = _load_json(self.repo_root / result.current_json_path)
            return {
                **result.summary(),
                "report": report,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _run_evidence_graph_health(self) -> dict[str, Any]:
        try:
            return WorkspaceEvidenceGraphBuilder(self.repo_root, self.workspace_rel).health().summary()
        except Exception as exc:
            return {
                "status": "failed",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _run_layered_pipeline(self) -> dict[str, Any]:
        pipeline_sql = self.layout.generated_dir / "pipeline" / "pipeline_layers.sql"
        if not pipeline_sql.exists():
            return {"status": "skipped", "ok": True, "reason": "No generated pipeline SQL found."}
        return {
            "status": "present",
            "ok": True,
            "path": _rel(pipeline_sql, self.repo_root),
        }

    def _run_pipeline_execution_harness(self) -> dict[str, Any]:
        pipeline_plan = self.layout.contracts_dir / "pipeline_plan.json"
        pipeline_sql = self.layout.generated_dir / "pipeline" / "pipeline_layers.sql"
        path = self.layout.evidence_dir / "pipeline_execution_harness" / "current.json"
        if not pipeline_plan.exists() and not pipeline_sql.exists() and not path.exists():
            return {"status": "skipped", "ok": True, "reason": "No pipeline plan or generated SQL found."}
        if not path.exists():
            return {
                "status": "failed",
                "ok": False,
                "passed": 0,
                "failed": 1,
                "error": "pipeline_execution_harness/current.json is required for generated pipeline work.",
            }
        payload = _load_json(path)
        summary = payload.get("summary") or {}
        return {
            "status": str(payload.get("status") or ("passed" if payload.get("ok") else "failed")),
            "ok": payload.get("ok") is True,
            "passed": int(summary.get("passed_count") or 0),
            "failed": int(summary.get("failed_count") or 0),
            "path": _rel(path, self.repo_root),
        }

    def _run_data_quality_harness(self) -> dict[str, Any]:
        path = self.layout.evidence_dir / "data_quality_harness" / "current.json"
        if not path.exists():
            alt = self.layout.evidence_dir / "data_quality" / "current.json"
            if alt.exists():
                path = alt
        if not path.exists():
            return {"status": "skipped", "ok": True, "reason": "No data quality harness artifact found."}
        payload = _load_json(path)
        summary = payload.get("summary") or {}
        return {
            "status": str(payload.get("status") or ("passed" if payload.get("ok") else "failed")),
            "ok": payload.get("ok") is True,
            "passed": int(summary.get("passed_count") or payload.get("finding_count") or 0),
            "failed": int(summary.get("failed_count") or payload.get("unresolved_finding_count") or 0),
            "path": _rel(path, self.repo_root),
        }

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _score(checks: dict[str, Any]) -> float:
    validation = checks["workspace_artifacts"]
    kpi_execution = checks["kpi_execution"]
    benchmark = checks["agent_benchmark"]
    git_hygiene = checks["git_hygiene"]
    workflow = checks.get("workflow_guardrails", {})
    graph = checks.get("evidence_graph", {})
    release = benchmark.get("release_gate") or {}
    gates = release.get("gates") or []
    allowed = [gate for gate in gates if gate.get("status") == "allowed"]
    core = float(benchmark.get("core_readiness_score") or 0)
    validation_score = 100.0 if validation.get("ok") else 0.0
    execution_score = 100.0 if kpi_execution.get("ok") else 0.0
    gate_score = 100.0 if not gates else round(len(allowed) / len(gates) * 100.0, 2)
    hygiene_score = 100.0 if git_hygiene.get("ok") else 0.0
    workflow_score = 100.0 if workflow.get("ok", True) else 0.0
    graph_score = 100.0 if graph.get("ok", True) else 0.0
    return round(
        (validation_score * 0.17)
        + (execution_score * 0.21)
        + (core * 0.21)
        + (gate_score * 0.17)
        + (hygiene_score * 0.09)
        + (workflow_score * 0.10)
        + (graph_score * 0.05),
        2,
    )


def _hard_blockers(checks: dict[str, Any]) -> list[str]:
    blockers = []
    if not checks["workspace_artifacts"].get("ok"):
        blockers.append("workspace artifact validation failed")
    if not checks["kpi_execution"].get("ok"):
        blockers.append("KPI execution harness failed")
    if not checks["git_hygiene"].get("ok"):
        blockers.append("git hygiene failed")
    if not checks.get("workflow_guardrails", {}).get("ok", True):
        blockers.append("workflow guardrails failed")
    if not checks.get("evidence_graph", {}).get("ok", True):
        blockers.append("evidence graph health failed")
    if not checks.get("pipeline_execution_harness", {}).get("ok", True):
        blockers.append("pipeline execution harness failed")
    if not checks.get("data_quality_harness", {}).get("ok", True):
        blockers.append("data quality harness failed")
    if not checks.get("ai_cli_harness", {}).get("ok", True):
        blockers.append("AI CLI harness failed")
    critical_gates = {"source_to_target_planning", "executable_sql_generation", "production_promotion"}
    for gate in (checks["agent_benchmark"].get("release_gate") or {}).get("blocked_gates") or []:
        if gate.get("gate") in critical_gates:
            blockers.append(f"critical release gate blocked: {gate.get('gate')}")
    return blockers


def _collect_blockers(checks: dict[str, Any]) -> list[str]:
    blockers = []
    blockers.extend(str(error) for error in checks["workspace_artifacts"].get("errors") or [])
    for issue in checks["git_hygiene"].get("issues") or []:
        blockers.append(f"git hygiene: {issue.get('path')} ({issue.get('rule')})")
    ai_cli = checks.get("ai_cli_harness", {})
    if ai_cli and ai_cli.get("ok") is False:
        blockers.append(f"AI CLI harness `{ai_cli.get('status')}`")
    workflow = checks.get("workflow_guardrails", {})
    if workflow and workflow.get("ok") is False:
        report = workflow.get("report") or {}
        for finding in report.get("findings") or []:
            if finding.get("severity") == "error":
                blockers.append(f"workflow guardrail: {finding.get('code')} - {finding.get('message')}")
    graph = checks.get("evidence_graph", {})
    if graph and graph.get("ok") is False:
        blockers.append(f"evidence graph: {graph.get('error') or graph.get('status')}")
    pipeline = checks.get("pipeline_execution_harness", {})
    if pipeline and pipeline.get("ok") is False:
        blockers.append(f"pipeline execution harness `{pipeline.get('status')}`")
    data_quality = checks.get("data_quality_harness", {})
    if data_quality and data_quality.get("ok") is False:
        blockers.append(f"data quality harness `{data_quality.get('status')}`")
    kpi = checks["kpi_execution"]
    for record in kpi.get("records") or []:
        for error in record.get("errors") or []:
            blockers.append(f"{record.get('kpi_id')}: {error}")
    release = (checks["agent_benchmark"].get("release_gate") or {}).get("blocked_gates") or []
    for gate in release:
        blockers.append(f"release gate `{gate.get('gate')}` blocked: {gate.get('reason')}")
    return blockers


def _collect_warnings(checks: dict[str, Any]) -> list[str]:
    warnings = []
    warnings.extend(str(item) for item in checks["workspace_artifacts"].get("warnings") or [])
    for record in checks["kpi_execution"].get("records") or []:
        for warning in record.get("warnings") or []:
            warnings.append(f"{record.get('kpi_id')}: {warning}")
    workflow = checks.get("workflow_guardrails", {})
    if workflow:
        report = workflow.get("report") or {}
        for finding in report.get("findings") or []:
            if finding.get("severity") == "warning":
                warnings.append(f"workflow guardrail: {finding.get('code')} - {finding.get('message')}")
    graph = checks.get("evidence_graph", {})
    for warning in graph.get("warnings") or []:
        warnings.append(f"evidence graph: {warning}")
    return warnings


def _next_commands(workspace: str, domain: str, checks: dict[str, Any]) -> list[str]:
    commands = []
    if not checks["workspace_artifacts"].get("ok"):
        commands.append(f"uv run validate-workspace-artifacts --workspace {workspace}")
    if not checks["kpi_execution"].get("ok"):
        commands.append(f"uv run run-kpi-execution-harness --workspace {workspace}")
    if checks["git_hygiene"].get("issues"):
        commands.append("uv run validate-git-hygiene")
    if checks.get("ai_cli_harness", {}).get("ok") is False:
        commands.append(f"uv run run-ai-cli-harness --workspace {workspace} --dataset <dataset>")
    if checks.get("workflow_guardrails", {}).get("ok") is False:
        commands.append(f"uv run validate-workflow-guardrails --workspace {workspace}")
    if checks.get("evidence_graph", {}).get("ok") is False:
        commands.append(f"uv run build-workspace-evidence-graph --workspace {workspace}")
    if checks.get("pipeline_execution_harness", {}).get("ok") is False:
        commands.append(f"uv run run-pipeline-execution-harness --workspace {workspace}")
    if checks.get("data_quality_harness", {}).get("ok") is False:
        commands.append(f"uv run run-data-quality-harness --workspace {workspace}")
    for route in (checks["agent_benchmark"].get("scorecard") or {}).get("blocker_routes") or []:
        command = str(route.get("next_command") or "")
        if command and command not in commands:
            commands.append(command)
    if not commands:
        commands.append(f"uv run validate-project-harness --workspace {workspace} --domain {domain}")
    return commands


def _render_markdown(result: ProjectHarnessResult) -> str:
    checks = result.checks
    benchmark = checks.get("agent_benchmark", {})
    release = benchmark.get("release_gate") or {}
    lines = [
        "# Project Harness",
        "",
        f"- Workspace: `{result.workspace}`",
        f"- Status: `{result.status}`",
        f"- Score: `{result.score}`",
        f"- Threshold: `{result.threshold}`",
        "",
        "## Checks",
        "",
        f"- Workspace artifacts: `{'passed' if checks['workspace_artifacts'].get('ok') else 'blocked'}`",
        f"- KPI execution: `{'passed' if checks['kpi_execution'].get('ok') else 'blocked'}`",
        f"- Agent benchmark core readiness: `{benchmark.get('core_readiness_score', 0)}`",
        f"- Git hygiene: `{checks['git_hygiene'].get('status')}`",
        f"- Workflow guardrails: `{checks.get('workflow_guardrails', {}).get('status', 'skipped')}`",
        f"- Evidence graph: `{checks.get('evidence_graph', {}).get('status', 'skipped')}`",
        f"- Layered pipeline: `{checks.get('layered_pipeline', {}).get('status', 'skipped')}`",
        f"- Pipeline execution harness: `{checks.get('pipeline_execution_harness', {}).get('status', 'skipped')}`",
        f"- Data quality harness: `{checks.get('data_quality_harness', {}).get('status', 'skipped')}`",
        f"- AI CLI harness: `{checks.get('ai_cli_harness', {}).get('status', 'skipped')}`",
        "",
        "## Release Gates",
        "",
    ]
    for gate in release.get("gates") or []:
        lines.append(f"- `{gate.get('gate')}`: `{gate.get('status')}`")
    if result.blockers:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {blocker}" for blocker in result.blockers)
    if result.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in result.warnings)
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in result.next_commands)
    return "\n".join(lines).rstrip() + "\n"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run all project harness checks and score the workspace.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--domain", default="general")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--threshold", type=float, default=95.0)
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument("--ai-cli-dataset")
    args = parser.parse_args(argv)

    result = ProjectHarness(
        args.repo_root,
        args.workspace,
        domain=args.domain,
        threshold=args.threshold,
        sample_limit=args.sample_limit,
        ai_cli_dataset=args.ai_cli_dataset,
    ).run()
    print(json.dumps(result.summary(), indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

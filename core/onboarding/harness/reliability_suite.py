"""Run local-safe reliability checks for a workspace."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.harness.project_harness import ProjectHarness
from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness
from core.storage.workspace_layout import WorkspaceLayout


SUITE_VERSION = 1
PROJECT_HARNESS_REQUIRED_ARTIFACTS = [
    "interns/generated/requirements/input_inventory.json",
    "interns/generated/profiles/profile_index.json",
    "interns/generated/contracts/domain_model.json",
    "interns/generated/contracts/kpi_registry.json",
    "interns/generated/contracts/kpi_feature_mapping.json",
]


@dataclass(frozen=True)
class ReliabilitySuiteResult:
    workspace: str
    ok: bool
    status: str
    current_json_path: str
    current_markdown_path: str
    evidence_path: str
    check_count: int
    failed_count: int
    skipped_count: int
    checks: list[dict[str, Any]]
    next_commands: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "reliability_suite_result",
            "version": SUITE_VERSION,
            "generated_by": "run-reliability-suite",
            **asdict(self),
        }


class ReliabilitySuite:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        domain: str = "general",
        project_harness: str = "auto",
        threshold: float = 95.0,
        sample_limit: int = 10,
        command_log: str | Path | None = None,
        ai_cli_dataset: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.domain = domain
        self.project_harness = project_harness
        self.threshold = threshold
        self.sample_limit = sample_limit
        self.command_log = command_log
        self.ai_cli_dataset = ai_cli_dataset
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> ReliabilitySuiteResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        checks = [
            self._run_workflow_guardrails(),
            # P7: validate the harness EVIDENCE artifacts so a failed/malformed/
            # missing-when-required harness blocks the suite (it previously only
            # ran guardrails + evidence-graph + project-harness, so a recorded
            # harness failure went undetected). Ref: ob-harness.md.
            self._run_harness_artifact_check(
                "run-layered-pipeline-harness",
                "interns/generated/evidence/layered_pipeline_harness/current.json",
                required=self._has_contract("pipeline_plan.json"),
            ),
            self._run_harness_artifact_check(
                "pipeline-execution-harness",
                "interns/generated/evidence/pipeline_execution_harness/current.json",
                required=self._has_contract("pipeline_plan.json"),
            ),
            self._run_harness_artifact_check(
                "data-quality-harness",
                "interns/generated/evidence/data_quality_harness/current.json",
                required=self._has_contract("catalog_contract.json"),
            ),
            self._run_evidence_graph(),
            self._run_project_harness(),
        ]
        failed = [check for check in checks if check["status"] == "failed"]
        skipped = [check for check in checks if check["status"] == "skipped"]
        ok = not failed
        status = "passed" if ok else "blocked"
        report = {
            "artifact_type": "reliability_suite/current.json",
            "version": SUITE_VERSION,
            "generated_by": "run-reliability-suite",
            "workspace": self.workspace_rel,
            "generated_at": _now(),
            "ok": ok,
            "status": status,
            "summary": {
                "check_count": len(checks),
                "failed_count": len(failed),
                "skipped_count": len(skipped),
            },
            "checks": checks,
            "next_commands": self._next_commands(checks),
        }
        report_dir = self.layout.reports_dir / "reliability_suite"
        evidence_dir = self.layout.evidence_dir / "reliability_suite"
        report_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        current_json = report_dir / "current.json"
        current_md = report_dir / "current.md"
        evidence_path = evidence_dir / "current.json"
        current_json.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        evidence_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")
        return ReliabilitySuiteResult(
            workspace=self.workspace_rel,
            ok=ok,
            status=status,
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            evidence_path=_rel(evidence_path, self.repo_root),
            check_count=len(checks),
            failed_count=len(failed),
            skipped_count=len(skipped),
            checks=checks,
            next_commands=report["next_commands"],
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _run_workflow_guardrails(self) -> dict[str, Any]:
        try:
            result = WorkflowGuardHarness(
                self.repo_root,
                self.workspace_rel,
                command_log=self.command_log,
            ).run()
            return _check(
                "validate-workflow-guardrails",
                "passed" if result.ok else "failed",
                result.ok,
                "Workflow guardrails passed." if result.ok else "Workflow guardrails found blockers.",
                result.summary(),
                artifacts=[
                    result.current_json_path,
                    result.current_markdown_path,
                    result.evidence_path,
                ],
            )
        except Exception as exc:
            return _failed_check("validate-workflow-guardrails", exc)

    def _run_evidence_graph(self) -> dict[str, Any]:
        try:
            from core.onboarding.evidence_graph import WorkspaceEvidenceGraphBuilder
        except Exception as exc:
            return _check(
                "build-workspace-evidence-graph",
                "skipped",
                True,
                f"Evidence graph builder is unavailable: {type(exc).__name__}: {exc}",
                {},
            )
        try:
            result = WorkspaceEvidenceGraphBuilder(self.repo_root, self.workspace_rel).build()
            return _check(
                "build-workspace-evidence-graph",
                "passed",
                True,
                "Evidence graph built.",
                result.summary(),
                artifacts=[result.graph_path, result.current_markdown_path],
            )
        except Exception as exc:
            return _failed_check("build-workspace-evidence-graph", exc)

    def _run_project_harness(self) -> dict[str, Any]:
        if self.project_harness == "skip":
            return _check(
                "validate-project-harness",
                "skipped",
                True,
                "Project harness skipped by request.",
                {"mode": self.project_harness},
            )
        missing = self._missing_project_harness_artifacts()
        if missing and self.project_harness == "auto":
            return _check(
                "validate-project-harness",
                "skipped",
                True,
                "Project harness skipped because required generated artifacts are missing.",
                {
                    "mode": self.project_harness,
                    "missing_required_artifacts": missing,
                },
            )
        try:
            result = ProjectHarness(
                self.repo_root,
                self.workspace_rel,
                domain=self.domain,
                threshold=self.threshold,
                sample_limit=self.sample_limit,
                ai_cli_dataset=self.ai_cli_dataset,
            ).run()
            return _check(
                "validate-project-harness",
                "passed" if result.ok else "failed",
                result.ok,
                "Project harness passed." if result.ok else "Project harness found blockers.",
                result.summary(),
                artifacts=[result.manifest_path, result.report_path],
            )
        except Exception as exc:
            return _failed_check("validate-project-harness", exc)

    def _has_contract(self, name: str) -> bool:
        return (self.layout.contracts_dir / name).exists()

    def _run_harness_artifact_check(
        self, name: str, artifact_rel: str, *, required: bool
    ) -> dict[str, Any]:
        """Validate a harness evidence artifact: ok=True -> passed; ok=False or
        malformed (no `ok`) -> failed; missing -> failed when required else
        skipped. Surfaces passed/failed counts from the artifact summary."""
        path = self.workspace / artifact_rel
        if not path.exists():
            if required:
                return _check(
                    name, "failed", False,
                    f"required harness artifact missing: {artifact_rel}",
                    {"missing_artifact": artifact_rel, "required": True},
                )
            return _check(
                name, "skipped", True,
                "harness not run (artifact absent and not required)",
                {"artifact": artifact_rel, "required": False},
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return _check(
                name, "failed", False,
                f"harness artifact unreadable/malformed: {type(exc).__name__}",
                {"artifact": artifact_rel, "error": type(exc).__name__},
            )
        # Harness artifacts spell the success flag either `ok` (data-quality) or
        # `pass` (pipeline-execution). Neither present -> malformed.
        if not isinstance(data, dict) or ("ok" not in data and "pass" not in data):
            return _check(
                name, "failed", False,
                "harness artifact malformed (missing `ok`/`pass`)",
                {"artifact": artifact_rel, "malformed": True},
            )
        summary = data.get("summary") or {}
        details = {
            "passed": summary.get("passed_count"),
            "failed": summary.get("failed_count"),
            "artifact": artifact_rel,
        }
        ok = bool(data["ok"] if "ok" in data else data["pass"])
        return _check(
            name,
            "passed" if ok else "failed",
            ok,
            "harness passed" if ok else f"harness reported failure ({data.get('status', 'failed')})",
            details,
            artifacts=[artifact_rel],
        )

    def _missing_project_harness_artifacts(self) -> list[str]:
        return [
            item
            for item in PROJECT_HARNESS_REQUIRED_ARTIFACTS
            if not (self.workspace / item).exists()
        ]

    def _next_commands(self, checks: list[dict[str, Any]]) -> list[str]:
        commands = []
        for check in checks:
            if check["status"] == "failed":
                if check["name"] == "validate-project-harness":
                    commands.append(
                        f"uv run validate-project-harness --workspace {self.workspace_rel} --domain {self.domain}"
                    )
                elif check["name"] == "build-workspace-evidence-graph":
                    commands.append(f"uv run build-workspace-evidence-graph --workspace {self.workspace_rel}")
                else:
                    commands.append(f"uv run harness workflow-guardrails --workspace {self.workspace_rel}")
            for command in (check.get("details") or {}).get("next_commands") or []:
                if isinstance(command, str):
                    commands.append(command)
        if not commands:
            commands.append(f"uv run harness reliability --workspace {self.workspace_rel} --domain {self.domain}")
        return _dedupe(commands)


def _check(
    name: str,
    status: str,
    ok: bool,
    summary: str,
    details: dict[str, Any],
    *,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "ok": ok,
        "summary": summary,
        "artifacts": artifacts or [],
        "details": details,
    }


def _failed_check(name: str, exc: Exception) -> dict[str, Any]:
    return _check(
        name,
        "failed",
        False,
        f"{name} failed: {type(exc).__name__}: {exc}",
        {"error": f"{type(exc).__name__}: {exc}"},
    )


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Reliability Suite",
        "",
        f"- Workspace: `{report['workspace']}`",
        f"- Status: `{report['status']}`",
        f"- Checks: `{report['summary']['check_count']}`",
        f"- Failed: `{report['summary']['failed_count']}`",
        f"- Skipped: `{report['summary']['skipped_count']}`",
        "",
        "## Checks",
        "",
    ]
    for check in report.get("checks") or []:
        lines.append(f"- `{check['name']}`: `{check['status']}` - {check['summary']}")
        for artifact in check.get("artifacts") or []:
            lines.append(f"  - `{artifact}`")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in report.get("next_commands") or [])
    return "\n".join(lines).rstrip() + "\n"


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local-safe workspace reliability checks.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--domain", default="general")
    parser.add_argument("--project-harness", choices=["auto", "run", "skip"], default="auto")
    parser.add_argument("--threshold", type=float, default=95.0)
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument("--command-log")
    parser.add_argument("--ai-cli-dataset")
    args = parser.parse_args(argv)
    result = ReliabilitySuite(
        args.repo_root,
        args.workspace,
        domain=args.domain,
        project_harness=args.project_harness,
        threshold=args.threshold,
        sample_limit=args.sample_limit,
        command_log=args.command_log,
        ai_cli_dataset=args.ai_cli_dataset,
    ).run()
    print(json.dumps(result.summary(), indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

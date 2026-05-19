"""Detect workspace-level product bugs and write governed bug reports."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout
from tools.list_workspace_files import list_workspace_files


BLOCKING_SEVERITIES = {"critical", "high"}


@dataclass(frozen=True)
class WorkspaceBug:
    bug_id: str
    title: str
    severity: str
    status: str
    finding: str
    how_created: list[str]
    expected_behavior: list[str]
    impact: str
    suspected_cause: str
    fix_direction: str
    acceptance_criteria: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_workflow(self) -> bool:
        return self.severity.lower() in BLOCKING_SEVERITIES


@dataclass(frozen=True)
class WorkspaceBugReport:
    workspace: str
    status: str
    bug_count: int
    blocking_bug_count: int
    bugs: list[WorkspaceBug]
    evidence_summary: dict[str, Any]

    @property
    def blocks_workflow(self) -> bool:
        return self.blocking_bug_count > 0

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "status": self.status,
            "bug_count": self.bug_count,
            "blocking_bug_count": self.blocking_bug_count,
            "blocks_workflow": self.blocks_workflow,
            "evidence_summary": self.evidence_summary,
            "bugs": [asdict(bug) | {"blocks_workflow": bug.blocks_workflow} for bug in self.bugs],
        }


class WorkspaceBugDetector:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace_rel = _normalize_workspace(workspace)
        self.workspace = (self.repo_root / self.workspace_rel).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> WorkspaceBugReport:
        listing = list_workspace_files(self.repo_root, self.workspace_rel).to_dict()
        inventory = _load_json(self.layout.requirements_dir / "input_inventory.json")
        profile_index = _load_json(self.layout.profiles_dir / "profile_index.json")
        kpi_registry = _load_json(self.layout.contracts_dir / "kpi_registry.json")
        evidence = _evidence_summary(listing, inventory, profile_index, kpi_registry)
        bugs: list[WorkspaceBug] = []

        bug = self._detect_listing_onboarding_contradiction(evidence)
        if bug:
            bugs.append(bug)

        blocking = sum(1 for item in bugs if item.blocks_workflow)
        status = "blocked" if blocking else ("bugs_detected" if bugs else "ok")
        return WorkspaceBugReport(
            workspace=self.workspace_rel,
            status=status,
            bug_count=len(bugs),
            blocking_bug_count=blocking,
            bugs=bugs,
            evidence_summary=evidence,
        )

    def write_report(self, report: WorkspaceBugReport) -> dict[str, str]:
        self.layout.ensure_runtime_dirs()
        bugs_dir = self.layout.reports_dir / "bugs"
        bugs_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.layout.evidence_dir / "bug_report.json"
        markdown_path = bugs_dir / "current.md"
        json_path.write_text(json.dumps(report.summary(), indent=2), encoding="utf-8")
        markdown_path.write_text(_markdown_report(report), encoding="utf-8")
        return {
            "json": _rel(json_path, self.repo_root),
            "markdown": _rel(markdown_path, self.repo_root),
        }

    def _detect_listing_onboarding_contradiction(
        self,
        evidence: dict[str, Any],
    ) -> WorkspaceBug | None:
        if not evidence["artifacts"]["input_inventory_present"]:
            return None
        listing_has_inputs = (
            evidence["listing"]["dataset_evidence_count"] > 0
            or evidence["listing"]["kpi_input_count"] > 0
            or evidence["listing"]["data_model_input_count"] > 0
        )
        onboarding_empty = (
            evidence["onboarding"]["data_file_count"] == 0
            and evidence["onboarding"]["kpi_registry_count"] == 0
            and evidence["onboarding"]["data_model_count"] == 0
        )
        profiles_empty = (
            evidence["artifacts"]["profile_index_present"]
            and evidence["onboarding"]["profile_count"] == 0
            and evidence["listing"]["dataset_evidence_count"] > 0
        )
        kpis_empty = (
            evidence["artifacts"]["kpi_registry_present"]
            and evidence["onboarding"]["kpi_count"] == 0
            and evidence["listing"]["kpi_input_count"] > 0
        )
        if not (listing_has_inputs and (onboarding_empty or profiles_empty or kpis_empty)):
            return None
        return WorkspaceBug(
            bug_id="WS-BUG-001",
            title="Workspace listing finds inputs but onboarding artifacts are empty",
            severity="critical",
            status="open",
            finding=(
                "The workspace file classifier detected usable dataset, KPI, or data-model "
                "inputs, but onboarding generated empty input/profile/KPI artifacts."
            ),
            how_created=[
                f"Run `uv run list-workspace-files --workspace {self.workspace_rel}`.",
                f"Run `uv run onboard-workspace --workspace {self.workspace_rel}`.",
                "Inspect `interns/generated/requirements/input_inventory.json` and generated profiles/KPIs.",
            ],
            expected_behavior=[
                "Root-level dataset evidence should be included in onboarding `data_files`.",
                "Root-level KPI intent files should be preserved as KPI/context evidence.",
                "Root-level data model and dictionary files should be preserved as model/context evidence.",
                "Profiles should be generated when dataset evidence is present.",
            ],
            impact=(
                "Fresh client workspaces can appear valid at selection time but become empty "
                "during onboarding, blocking KPI generation and making later readiness states unsafe."
            ),
            suspected_cause=(
                "Workspace listing and onboarding use different discovery rules. Listing understands "
                "flat root-level workspaces, while onboarding still depends on older docs/datasets layout assumptions."
            ),
            fix_direction=(
                "Centralize discovery so listing, onboarding, validation, KPI generation, and kickstart "
                "share the same classified file evidence."
            ),
            acceptance_criteria=[
                "Onboarding returns non-empty `data_files` when root CSV datasets exist.",
                "Generated `profile_index.json` contains profiles for discovered dataset evidence.",
                "KPI/context and data-model/context files from the listing remain available downstream.",
                "Kickstart does not mark an empty-evidence workspace as ready for SQL.",
            ],
            evidence=evidence,
        )


def _evidence_summary(
    listing: dict[str, Any],
    inventory: dict[str, Any] | None,
    profile_index: dict[str, Any] | None,
    kpi_registry: dict[str, Any] | None,
) -> dict[str, Any]:
    classifications = listing.get("classifications") or []
    role_counts = {
        "dataset_evidence_count": _role_count(classifications, "dataset_evidence"),
        "kpi_input_count": _role_count(classifications, "kpi_input"),
        "data_model_input_count": _role_count(classifications, "data_model_input"),
        "context_doc_count": _role_count(classifications, "context_doc"),
    }
    inventory = inventory or {}
    profile_index = profile_index or {}
    kpi_registry = kpi_registry or {}
    return {
        "listing": {
            "file_count": listing.get("file_count", 0),
            "dataset_roots": listing.get("dataset_roots", []),
            **role_counts,
        },
        "artifacts": {
            "input_inventory_present": bool(inventory),
            "profile_index_present": bool(profile_index),
            "kpi_registry_present": bool(kpi_registry),
        },
        "onboarding": {
            "data_file_count": len(inventory.get("data_files") or []),
            "kpi_registry_count": len(inventory.get("kpi_registries") or []),
            "data_model_count": len(inventory.get("data_models") or []),
            "profile_count": len(profile_index.get("profiles") or []),
            "kpi_count": len(kpi_registry.get("kpis") or []),
        },
    }


def _role_count(classifications: list[dict[str, Any]], role: str) -> int:
    return sum(1 for item in classifications if role in item.get("roles", []))


def _markdown_report(report: WorkspaceBugReport) -> str:
    lines = [
        "# Workspace Bug Report",
        "",
        f"- Workspace: `{report.workspace}`",
        f"- Status: `{report.status}`",
        f"- Bugs: {report.bug_count}",
        f"- Blocking bugs: {report.blocking_bug_count}",
        "",
        "## Evidence Summary",
        "",
        "```json",
        json.dumps(report.evidence_summary, indent=2),
        "```",
        "",
    ]
    if not report.bugs:
        lines.extend(["## Bugs", "", "No workspace product bugs detected.", ""])
        return "\n".join(lines)
    for bug in report.bugs:
        lines.extend(
            [
                f"## {bug.bug_id}: {bug.title}",
                "",
                f"- Severity: `{bug.severity}`",
                f"- Status: `{bug.status}`",
                f"- Blocks workflow: `{str(bug.blocks_workflow).lower()}`",
                "",
                "Finding:",
                bug.finding,
                "",
                "How It Is Created:",
                *[f"{idx}. {step}" for idx, step in enumerate(bug.how_created, start=1)],
                "",
                "Expected Behavior:",
                *[f"- {item}" for item in bug.expected_behavior],
                "",
                "Impact:",
                bug.impact,
                "",
                "Suspected Cause:",
                bug.suspected_cause,
                "",
                "Fix Direction:",
                bug.fix_direction,
                "",
                "Acceptance Criteria:",
                *[f"- {item}" for item in bug.acceptance_criteria],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _normalize_workspace(workspace: str | Path) -> str:
    value = str(workspace).replace("\\", "/").strip().strip('"').strip("'").rstrip("/")
    if value.startswith("workspaces/"):
        return value
    return f"workspaces/{value}"


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect workspace product bugs and write bug reports.")
    parser.add_argument("--workspace", required=True, help="Workspace path under workspaces/<project>.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--no-write", action="store_true", help="Detect only; do not write bug artifacts.")
    args = parser.parse_args()

    detector = WorkspaceBugDetector(args.repo_root, args.workspace)
    report = detector.run()
    payload = report.summary()
    if not args.no_write:
        payload["artifacts"] = detector.write_report(report)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

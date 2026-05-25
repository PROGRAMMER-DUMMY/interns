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
PANEL_ARTIFACT_FEATURES = {
    "average",
    "base",
    "claim",
    "count",
    "maximum",
    "minimum",
    "sum",
    "total",
}


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
        mapping = _load_json(self.layout.contracts_dir / "kpi_feature_mapping.json")
        panel = _load_json(self.layout.reports_dir / "blocker_question_panel" / "current.json")
        definitions = _load_json(self.layout.contracts_dir / "workspace_feature_definitions.json")
        requirements = _load_json(self.layout.requirements_dir / "requirements.json")
        evidence = _evidence_summary(listing, inventory, profile_index, kpi_registry)
        bugs: list[WorkspaceBug] = []

        bug = self._detect_listing_onboarding_contradiction(evidence)
        if bug:
            bugs.append(bug)
        bug = self._detect_panel_artifact_question(panel, mapping)
        if bug:
            bugs.append(bug)
        bug = self._detect_scoped_definition_overwrite_risk(requirements, definitions)
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

    def _detect_panel_artifact_question(
        self,
        panel: dict[str, Any] | None,
        mapping: dict[str, Any] | None,
    ) -> WorkspaceBug | None:
        if not panel or panel.get("status") != "needs_user_answer":
            return None
        feature = str(panel.get("feature") or "")
        normalized = _normalize_feature(feature)
        if normalized not in PANEL_ARTIFACT_FEATURES:
            return None
        affected = panel.get("applies_to_kpis") or []
        return WorkspaceBug(
            bug_id="WS-BUG-002",
            title="Blocker panel asks about parser artifact or operator term",
            severity="high",
            status="open",
            finding=(
                f"The current blocker panel asks the user to define `{feature}` as if it were "
                "a source feature. This looks like an aggregation/operator fragment or metric "
                "phrase fragment, not a durable business term."
            ),
            how_created=[
                f"Run `uv run prepare-kpi-blocker-panel --workspace {self.workspace_rel} --domain <domain>`.",
                "Inspect `interns/reports/blocker_question_panel/current.json`.",
                "Check whether the panel feature is an operator or fragment such as average/base/total/claim.",
            ],
            expected_behavior=[
                "Feature extraction should preserve meaningful metric phrases before blocker clustering.",
                "Aggregation operators such as average should be represented as formulas over a measure, not physical source columns.",
                "The blocker panel should ask for the measure and grain when an operator is ambiguous.",
            ],
            impact=(
                "Agents can accept examples such as AVG() as standalone definitions, producing "
                "invalid mappings and repeated blocker panels."
            ),
            suspected_cause=(
                "KPI metric text is tokenized too literally before semantic grouping, so phrase "
                "components are promoted into blocker features."
            ),
            fix_direction=(
                "Improve KPI expression extraction to recognize metric phrases and operator/measure "
                "pairs before generating blocker clusters."
            ),
            acceptance_criteria=[
                "Questions do not ask users to map average/base/total/claim as standalone source columns.",
                "Average-cost style KPIs ask for the measure column plus aggregation grain.",
                "Validation or bug detection blocks parser-artifact blocker panels.",
            ],
            evidence={
                "panel_feature": feature,
                "applies_to_kpis": affected,
                "mapping_summary": (mapping or {}).get("summary", {}),
            },
        )

    def _detect_scoped_definition_overwrite_risk(
        self,
        requirements: dict[str, Any] | None,
        definitions: dict[str, Any] | None,
    ) -> WorkspaceBug | None:
        history = (requirements or {}).get("workspace_feature_definitions") or []
        if not isinstance(history, list):
            return None
        latest_by_feature_scope: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        for item in history:
            if not isinstance(item, dict):
                continue
            feature = str(item.get("feature") or "")
            if not feature:
                continue
            scope = tuple(sorted(str(kpi) for kpi in item.get("applies_to_kpis") or []))
            latest_by_feature_scope[(_normalize_feature(feature), scope)] = item
        by_feature: dict[str, list[dict[str, Any]]] = {}
        for (feature, _scope), item in latest_by_feature_scope.items():
            by_feature.setdefault(feature, []).append(item)
        stored = (definitions or {}).get("definitions") or []
        stored_counts: dict[str, int] = {}
        if isinstance(stored, list):
            for item in stored:
                if isinstance(item, dict):
                    stored_counts[_normalize_feature(str(item.get("feature") or ""))] = (
                        stored_counts.get(_normalize_feature(str(item.get("feature") or "")), 0) + 1
                    )
        for feature, records in by_feature.items():
            distinct_scoped_definitions = {
                (
                    str(record.get("definition") or ""),
                    tuple(sorted(str(kpi) for kpi in record.get("applies_to_kpis") or [])),
                )
                for record in records
            }
            if len(distinct_scoped_definitions) <= 1:
                continue
            if stored_counts.get(feature, 0) >= len(distinct_scoped_definitions):
                continue
            return WorkspaceBug(
                bug_id="WS-BUG-003",
                title="Scoped workspace feature definitions can overwrite each other",
                severity="high",
                status="open",
                finding=(
                    f"Multiple distinct scoped definitions were recorded for `{feature}`, but "
                    "the stored workspace definitions do not preserve all scoped alternatives."
                ),
                how_created=[
                    "Answer a blocker with different definitions for the same feature across KPI subsets.",
                    "Apply the decisions through workspace definition APIs or panel answers.",
                    "Inspect `interns/generated/requirements/requirements.json` and `workspace_feature_definitions.json`.",
                ],
                expected_behavior=[
                    "The same feature name may have multiple definitions when scoped to disjoint KPI sets.",
                    "Definition storage should key by feature plus scope or preserve scoped alternatives.",
                    "Applying one scoped definition must not erase another scoped definition for the same feature.",
                ],
                impact=(
                    "Later blocker prep can re-open already answered questions or apply the wrong "
                    "definition to a KPI."
                ),
                suspected_cause=(
                    "`workspace_feature_definitions.json` upserts by normalized feature name only, "
                    "ignoring `applies_to_kpis` when replacing records."
                ),
                fix_direction=(
                    "Change workspace definition identity to include feature and scope, or store "
                    "multiple scoped definitions under one feature."
                ),
                acceptance_criteria=[
                    "Two definitions for the same feature with disjoint KPI scopes both persist.",
                    "Re-running blocker prep applies the correct scoped definition to each KPI.",
                    "Bugfinder reports no overwrite risk after scoped storage is fixed.",
                ],
                evidence={
                    "feature": feature,
                    "recorded_scoped_definition_count": len(distinct_scoped_definitions),
                    "stored_definition_count": stored_counts.get(feature, 0),
                },
            )
        return None


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


def _normalize_feature(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


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

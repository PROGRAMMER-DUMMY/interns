from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class DataQualityResult:
    current_json_path: str
    current_markdown_path: str
    ok: bool
    status: str = "blocked"
    evidence_path: str = ""
    contract_path: str = ""
    finding_count: int = 0
    unresolved_finding_count: int = 0

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DataQualityHarness:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> DataQualityResult:
        self.layout.ensure_runtime_dirs()
        decisions = _load_decisions(self.layout.contracts_dir / "duplicate_decisions.json")
        resolved = bool(decisions)
        duplicate_findings = _detect_duplicate_pk_candidates(self.workspace, self.layout)
        findings = []
        for finding in duplicate_findings:
            unreadable = bool(finding.get("unreadable"))
            findings.append(
                {
                    # An unreadable dataset is always unresolved (a duplicate
                    # decision can't "resolve" a read failure) so the gate can't
                    # report ok=True on an error swallowed during the scan.
                    "code": "dataset_unreadable" if unreadable else "duplicate_rows_detected",
                    "severity": "high" if unreadable else "medium",
                    "status": "unresolved" if unreadable else ("resolved" if resolved else "unresolved"),
                    "dataset": finding["dataset"],
                    "column": finding["column"],
                    "query": finding["query"],
                    "duplicate_key_count": finding["duplicate_key_count"],
                    "sample_output_table": "<redacted>",
                    "sample_redacted": True,
                }
            )
        query = findings[0]["query"] if findings else (
            "-- No candidate primary-key columns found in profiled datasets; "
            "no generic duplicate query to run."
        )
        unresolved_count = sum(1 for finding in findings if finding["status"] == "unresolved")
        payload = {
            "artifact_type": "data_quality_harness/current.json",
            "version": 1,
            "generated_by": "run-data-quality-harness",
            "ok": unresolved_count == 0,
            "finding_count": len(findings),
            "unresolved_finding_count": unresolved_count,
            "findings": findings,
        }
        contract = {
            "artifact_type": "data_quality_contract.json",
            "version": 1,
            "generated_by": "run-data-quality-harness",
            "policy": {
                "duplicate_policy": {
                    "sql_mutation_in_milestone_1": False,
                    "requires_user_review": True,
                }
            },
        }
        out = self.layout.reports_dir / "data_quality"
        ev = self.layout.evidence_dir / "data_quality"
        out.mkdir(parents=True, exist_ok=True)
        ev.mkdir(parents=True, exist_ok=True)
        current_json = out / "current.json"
        current_md = out / "current.md"
        evidence_json = ev / "current.json"
        contract_json = self.layout.contracts_dir / "data_quality_contract.json"
        current_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        evidence_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        contract_json.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(
            "# Data Quality\n\n## Detection Query\n\n"
            f"```sql\n{query}\n```\n\n## Redacted Result Sample\n\n<redacted>\n",
            encoding="utf-8",
        )
        return DataQualityResult(
            _rel(current_json, self.repo_root),
            _rel(current_md, self.repo_root),
            unresolved_count == 0,
            "passed" if unresolved_count == 0 else "blocked",
            _rel(evidence_json, self.repo_root),
            _rel(contract_json, self.repo_root),
            len(findings),
            unresolved_count,
        )


class DuplicateReviewPanel:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def prepare(self) -> DataQualityResult:
        harness = DataQualityHarness(self.repo_root, _rel(self.workspace, self.repo_root)).run()
        report = _load_json(self.repo_root / harness.evidence_path)
        finding = (report.get("findings") or [{}])[0]
        panel = {
            "artifact_type": "duplicate_review/current.json",
            "version": 1,
            "generated_by": "prepare-duplicate-review-panel",
            "status": "needs_user_answer",
            "recommended_option_id": "option_a",
            "query": finding.get("query", ""),
            "sample_output_table": finding.get("sample_output_table", ""),
            "finding_count": report.get("finding_count", 0),
            "options": [
                {"option_id": "option_a", "label": "Preserve duplicates as source truth"},
                {"option_id": "option_b", "label": "Quarantine duplicates before Silver"},
                {"option_id": "option_c", "label": "Apply approved deduplication rule"},
            ],
        }
        panel_dir = self.layout.reports_dir / "duplicate_review"
        panel_dir.mkdir(parents=True, exist_ok=True)
        current_json = panel_dir / "current.json"
        current_md = panel_dir / "current.md"
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(
            "# Duplicate Review\n\n## Detection Query\n\n"
            f"```sql\n{panel['query']}\n```\n\n"
            f"## Redacted Result Sample\n\n{panel['sample_output_table']}\n",
            encoding="utf-8",
        )
        return DataQualityResult(
            _rel(current_json, self.repo_root),
            _rel(current_md, self.repo_root),
            False,
            "needs_user_answer",
            harness.evidence_path,
            harness.contract_path,
            harness.finding_count,
            harness.unresolved_finding_count,
        )


class DuplicateDecisionRecorder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def record(self, answer: str, *, reason: str = "") -> dict[str, Any]:
        path = self.layout.contracts_dir / "duplicate_decisions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        action = {
            "option_a": "preserve",
            "option_b": "quarantine",
            "option_c": "deduplicate",
        }.get(answer, "custom")
        payload = {
            "artifact_type": "duplicate_decisions.json",
            "version": 1,
            "generated_by": "apply-duplicate-review-answer",
            "workspace": _rel(self.workspace, self.repo_root),
            "decisions": [
                {
                    "answer": answer,
                    "action": action,
                    "reason": reason,
                    "approved_for_sql_mutation": False,
                }
            ],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        data_quality = DataQualityHarness(self.repo_root, _rel(self.workspace, self.repo_root)).run()
        panel = DuplicateReviewPanel(self.repo_root, _rel(self.workspace, self.repo_root)).prepare()
        return {
            "decision_path": _rel(path, self.repo_root),
            "data_quality": data_quality.summary(),
            "next_panel": panel.summary(),
        }

    def apply(self, answer: str, *, custom_rule: str = "") -> dict[str, Any]:
        return self.record(answer, reason=custom_rule or "Accepted duplicate review answer.")


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_decisions(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    decisions = data.get("decisions")
    return decisions if isinstance(decisions, list) else []


def _detect_duplicate_pk_candidates(
    workspace: Path,
    layout: WorkspaceLayout | None = None,
) -> list[dict[str, Any]]:
    """Generic duplicate detection across every profiled dataset in the workspace.

    Iterates over the profile_index, picks PK candidate columns per dataset
    (columns named like *ID / *Id / Id whose values are unique, OR the first
    column when no convention applies), and reports any dataset+column pair
    where duplicate values exist.

    Workspace-agnostic by design — no table or column name is hardcoded.
    """
    findings: list[dict[str, Any]] = []
    candidate_datasets = _profiled_datasets(layout) if layout is not None else []
    if not candidate_datasets:
        candidate_datasets = [
            (path, _infer_pk_candidates_from_csv_header(path))
            for path in sorted(workspace.rglob("*.csv"))
            if layout is None or layout.is_dataset_allowed(path)
        ]
    for path, candidate_columns in candidate_datasets:
        if not candidate_columns:
            continue
        for column in candidate_columns:
            duplicate_count = _count_duplicate_values(path, column)
            if duplicate_count == DATASET_UNREADABLE:
                # A candidate dataset we could not read is NOT clean — surface it
                # as a finding so the DQ gate cannot report ok=True on an error.
                findings.append(
                    {
                        "dataset": str(path),
                        "column": column,
                        "duplicate_key_count": 0,
                        "unreadable": True,
                        "query": (
                            f"-- dataset could not be read for duplicate check: "
                            f"{path.as_posix()} (column {column})"
                        ),
                    }
                )
                continue
            if duplicate_count <= 0:
                continue
            table_alias = path.stem
            findings.append(
                {
                    "dataset": str(path),
                    "column": column,
                    "duplicate_key_count": duplicate_count,
                    "query": (
                        f'SELECT "{column}", COUNT(*) AS duplicate_count '
                        f'FROM read_csv_auto(\'{path.as_posix()}\') AS "{table_alias}" '
                        f'GROUP BY "{column}" HAVING COUNT(*) > 1'
                    ),
                }
            )
    return findings


_PK_COLUMN_PATTERN = re.compile(r"(?:^|[_\W])(id)$|^id$|^_?key$", re.IGNORECASE)


def _looks_like_pk_column(column: str) -> bool:
    if not column:
        return False
    cleaned = column.strip()
    if cleaned.lower() == "id":
        return True
    return bool(_PK_COLUMN_PATTERN.search(cleaned))


def _profiled_datasets(layout: WorkspaceLayout) -> list[tuple[Path, list[str]]]:
    """Return [(dataset_path, candidate_pk_columns), ...] from the profile_index."""
    index_path = layout.profile_index_path
    if not index_path.exists():
        return []
    try:
        data = _load_json(index_path)
    except (json.JSONDecodeError, OSError):
        return []
    out: list[tuple[Path, list[str]]] = []
    for profile in data.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        raw_path = str(profile.get("path") or "")
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = (layout.project_root.parent.parent / raw_path).resolve()
        if not path.exists():
            continue
        if layout is not None and not layout.is_dataset_allowed(path):
            continue
        columns = _columns_from_profile(profile)
        if not columns:
            continue
        pk_candidates = [c for c in columns if _looks_like_pk_column(c)]
        if not pk_candidates and columns:
            pk_candidates = [columns[0]]
        out.append((path, pk_candidates))
    return out


def _columns_from_profile(profile: dict[str, Any]) -> list[str]:
    schema = profile.get("schema")
    if isinstance(schema, dict):
        return list(schema.keys())
    columns = profile.get("columns")
    if isinstance(columns, list):
        names: list[str] = []
        for entry in columns:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("column")
                if name:
                    names.append(str(name))
            elif entry:
                names.append(str(entry))
        return names
    return []


def _infer_pk_candidates_from_csv_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    pk_candidates = [col for col in header if _looks_like_pk_column(col)]
    return pk_candidates or ([header[0]] if header else [])


# Sentinel: the dataset could not be READ (distinct from "0 duplicates found").
# A read failure must NOT be treated as a clean dataset. Ref: onboarding-root.md.
DATASET_UNREADABLE = -1


def _count_duplicate_values(path: Path, column: str) -> int:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or column not in reader.fieldnames:
                return 0
            seen: set[str] = set()
            duplicates: set[str] = set()
            for row in reader:
                value = str(row.get(column) or "").strip()
                if not value:
                    continue
                if value in seen:
                    duplicates.add(value)
                seen.add(value)
            return len(duplicates)
    except (OSError, UnicodeDecodeError, csv.Error):
        # Read/parse failure — signal unreadable so the harness records a finding
        # instead of reporting the dataset as clean (false "ok").
        return DATASET_UNREADABLE


def main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="run-data-quality-harness",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DataQualityHarness(args.repo_root, args.workspace).run(),
    )


def run_main(argv: list[str] | None = None) -> int:
    return main(argv)



@anchored("prepare-duplicate-review-panel")
def panel_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="prepare-duplicate-review-panel",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DuplicateReviewPanel(args.repo_root, args.workspace).prepare(),
        validation="validate-workspace-artifacts",
    )


@anchored("apply-duplicate-review-answer")
def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--answer", required=True)
    parser.add_argument("--custom-rule", default="")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="apply-duplicate-review-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DuplicateDecisionRecorder(args.repo_root, args.workspace).apply(
            args.answer,
            custom_rule=args.custom_rule,
        ),
        op_args={
            "workspace": args.workspace,
            "answer": args.answer,
            "custom_rule": args.custom_rule,
        },
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer},
        record_idempotent=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())

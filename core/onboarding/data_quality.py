from __future__ import annotations

import argparse
import csv
import json
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
        duplicate_keys = _detect_duplicate_keys(self.workspace, self.layout)
        has_duplicates = bool(duplicate_keys)
        resolved = bool(decisions)
        query = "SELECT TransactionID, COUNT(*) AS duplicate_count FROM transactions GROUP BY TransactionID HAVING COUNT(*) > 1"
        findings = []
        if has_duplicates:
            findings.append(
                {
                    "code": "duplicate_rows_detected",
                    "severity": "medium",
                    "status": "resolved" if resolved else "unresolved",
                    "query": query,
                    "sample_output_table": "<redacted>",
                    "sample_redacted": True,
                    "duplicate_key_count": len(duplicate_keys),
                }
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


def _detect_duplicate_keys(workspace: Path, layout: WorkspaceLayout | None = None) -> set[str]:
    duplicates: set[str] = set()
    candidates = sorted(workspace.rglob("*.csv"))
    for path in candidates:
        if layout is not None and not layout.is_dataset_allowed(path):
            continue
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or "TransactionID" not in reader.fieldnames:
                    continue
                seen: set[str] = set()
                for row in reader:
                    key = str(row.get("TransactionID") or "").strip()
                    if not key:
                        continue
                    if key in seen:
                        duplicates.add(key)
                    seen.add(key)
        except OSError:
            continue
    return duplicates


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

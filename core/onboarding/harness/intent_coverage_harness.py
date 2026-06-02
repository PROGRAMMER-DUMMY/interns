"""KPI intent-coverage harness.

A high-level guardrail that, for every KPI with generated SQL, asserts the
generated result view realizes the KPI's *declared* intent: grain dimensions,
the metric aggregation, and explicit filters. The per-KPI logic lives in
``core.onboarding.kpi.intent_coverage`` and is re-derived independently of the
generator's own parser, so a generator bug (BUG-024: a declared cut dropped
from the grain) or a hand-edit that removes a dimension is caught rather than
silently passed.

Enforcement: any error-severity finding fails the harness (exit 1). The same
grain subset is also enforced inside the execution harness, so a red coverage
result blocks review/completion through the existing proof boundary.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.kpi.intent_coverage import CoverageFinding, evaluate_intent_coverage
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


HARNESS_VERSION = 1
_KPI_SQL_PATTERN = re.compile(r"^kpi_\d{3}(?:_[a-z0-9_]+)?\.sql$", re.IGNORECASE)
_KPI_ID_RE = re.compile(r"^(kpi_\d{3})", re.IGNORECASE)


@dataclass
class KPICoverageRecord:
    kpi_id: str
    sql_path: str
    status: str  # "passed" | "failed" | "missing_sql"
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True)
class IntentCoverageResult:
    workspace: str
    ok: bool
    status: str
    records: list[KPICoverageRecord]
    report_json_path: str
    report_md_path: str
    evidence_path: str
    error_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "kpi_intent_coverage_result",
            "version": HARNESS_VERSION,
            "generated_by": "validate-kpi-intent-coverage",
            "workspace": self.workspace,
            "ok": self.ok,
            "status": self.status,
            "error_count": self.error_count,
            "kpi_count": len(self.records),
            "records": [asdict(record) for record in self.records],
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "evidence_path": self.evidence_path,
        }


class KPIIntentCoverageHarness:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> IntentCoverageResult:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        self.layout.ensure_runtime_dirs()
        registry = self._kpi_registry_by_id()
        feature_mapping = self._feature_mapping_by_id()
        sql_by_kpi = self._sql_by_kpi()

        records: list[KPICoverageRecord] = []
        for kpi_id, kpi in registry.items():
            sql_path = sql_by_kpi.get(kpi_id)
            if not sql_path:
                records.append(
                    KPICoverageRecord(
                        kpi_id=kpi_id,
                        sql_path="",
                        status="missing_sql",
                        findings=[
                            asdict(
                                CoverageFinding(
                                    severity="error",
                                    code="generated_sql_missing",
                                    kind="grain",
                                    expected=f"{kpi_id}.sql",
                                    message=f"no generated SQL found for `{kpi_id}`",
                                )
                            )
                        ],
                    )
                )
                continue
            kpi_with_id = dict(kpi)
            kpi_with_id.setdefault("kpi_id", kpi_id)
            # The registry carries cuts but not features (those live in the
            # feature mapping); enrich so cut tokens resolve to their physical
            # columns instead of false-flagging as absent.
            if not kpi_with_id.get("features"):
                mapped = feature_mapping.get(kpi_id) or {}
                if mapped.get("features"):
                    kpi_with_id["features"] = mapped["features"]
            sql = sql_path.read_text(encoding="utf-8")
            findings = evaluate_intent_coverage(kpi_with_id, sql)
            errors = [f for f in findings if f.severity == "error"]
            records.append(
                KPICoverageRecord(
                    kpi_id=kpi_id,
                    sql_path=_rel(sql_path, self.repo_root),
                    status="failed" if errors else "passed",
                    findings=[asdict(f) for f in findings],
                )
            )

        error_count = sum(
            1
            for record in records
            for finding in record.findings
            if finding.get("severity") == "error"
        )
        ok = error_count == 0
        report = {
            "artifact_type": "kpi_intent_coverage/current.json",
            "version": HARNESS_VERSION,
            "generated_by": "validate-kpi-intent-coverage",
            "workspace": self.workspace_rel,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "status": "passed" if ok else "failed",
            "error_count": error_count,
            "kpi_count": len(records),
            "records": [asdict(record) for record in records],
        }

        report_dir = self.layout.reports_dir / "kpi_intent_coverage"
        evidence_dir = self.layout.evidence_dir / "kpi_intent_coverage"
        report_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        report_json = report_dir / "current.json"
        report_md = report_dir / "current.md"
        evidence_path = evidence_dir / "current.json"
        payload = json.dumps(report, indent=2, default=str) + "\n"
        report_json.write_text(payload, encoding="utf-8")
        evidence_path.write_text(payload, encoding="utf-8")
        report_md.write_text(_render_markdown(report), encoding="utf-8")

        return IntentCoverageResult(
            workspace=self.workspace_rel,
            ok=ok,
            status="passed" if ok else "failed",
            records=records,
            report_json_path=_rel(report_json, self.repo_root),
            report_md_path=_rel(report_md, self.repo_root),
            evidence_path=_rel(evidence_path, self.repo_root),
            error_count=error_count,
        )

    def _kpi_registry_by_id(self) -> dict[str, dict[str, Any]]:
        return self._load_kpis_by_id("kpi_registry.json")

    def _feature_mapping_by_id(self) -> dict[str, dict[str, Any]]:
        return self._load_kpis_by_id("kpi_feature_mapping.json")

    def _load_kpis_by_id(self, filename: str) -> dict[str, dict[str, Any]]:
        path = self.layout.contracts_dir / filename
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for idx, kpi in enumerate(data.get("kpis") or [], start=1):
            if isinstance(kpi, dict):
                out[str(kpi.get("kpi_id") or f"kpi_{idx:03d}")] = kpi
        return out

    def _sql_by_kpi(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        if not self.layout.solutions_dir.exists():
            return out
        for path in sorted(self.layout.solutions_dir.glob("kpi_*.sql")):
            if not _KPI_SQL_PATTERN.match(path.name) or path.name == "kpi_metrics.sql":
                continue
            match = _KPI_ID_RE.match(path.stem)
            kpi_id = match.group(1).lower() if match else path.stem.lower()
            out.setdefault(kpi_id, path)
        return out


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# KPI Intent Coverage",
        "",
        f"- Workspace: `{report['workspace']}`",
        f"- Status: `{report['status']}`",
        f"- Errors: `{report['error_count']}`",
        f"- KPIs checked: `{report['kpi_count']}`",
        "",
    ]
    rows: list[list[str]] = []
    for record in report.get("records") or []:
        if record.get("findings"):
            for finding in record["findings"]:
                rows.append(
                    [
                        record["kpi_id"],
                        record["status"],
                        finding.get("severity", ""),
                        finding.get("code", ""),
                        finding.get("message", ""),
                    ]
                )
        else:
            rows.append([record["kpi_id"], record["status"], "", "", "intent fully realized"])
    lines.append(
        render_markdown_table(["KPI", "Status", "Severity", "Code", "Detail"], rows)
        if rows
        else "_No KPIs with generated SQL found._"
    )
    lines.append("")
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify generated KPI SQL realizes each KPI's declared intent (grain/metric/filters)."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print a compact per-KPI status summary + report path instead of full JSON.",
    )
    args = parser.parse_args(argv)
    result = KPIIntentCoverageHarness(args.repo_root, args.workspace).run()
    if args.quiet:
        status = "[ok] passed" if result.ok else "[x] failed"
        print(f"{status} kpi-intent-coverage: {result.workspace} ({len(result.records)} KPIs)")
        for record in result.records:
            marker = "[ok]" if record.ok else "[x]"
            print(f"  {marker} {record.kpi_id}: {record.status}")
            for finding in record.findings:
                if finding.get("severity") == "error":
                    print(f"      [x] {finding.get('message')}")
        print(f"detail: {result.report_md_path}")
    else:
        print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

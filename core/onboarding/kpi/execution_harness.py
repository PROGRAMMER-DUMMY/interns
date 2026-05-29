"""Execute generated KPI SQL and prove that final result views exist."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


RESULT_VIEW_PATTERN = re.compile(
    r"create\s+or\s+replace\s+(?:temp\s+|temporary\s+)?view\s+[`\"]?{view}[`\"]?\s+as",
    re.IGNORECASE,
)
KPI_SQL_PATTERN = re.compile(r"^kpi_\d{3}(?:_[a-z0-9_]+)?\.sql$", re.IGNORECASE)
SUM_INPUT_PATTERN = re.compile(
    r"sum\s*\(\s*(?:(?:distinct|disitnct)\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


@dataclass
class KPIExecutionRecord:
    kpi_id: str
    sql_path: str
    status: str
    result_view: str = ""
    sql_sha256: str = ""
    semantic_checks: list[str] = field(default_factory=list)
    row_count: int | None = None
    columns: list[str] = field(default_factory=list)
    sample_output_table: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "passed" and not self.errors

    def summary(self) -> dict[str, Any]:
        return {
            "kpi_id": self.kpi_id,
            "sql_path": self.sql_path,
            "status": self.status,
            "result_view": self.result_view,
            "sql_sha256": self.sql_sha256,
            "semantic_checks": self.semantic_checks,
            "row_count": self.row_count,
            "columns": self.columns,
            "sample_output_table": self.sample_output_table,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class KPIExecutionHarnessResult:
    workspace: str
    records: list[KPIExecutionRecord]
    manifest_path: str
    report_path: str

    @property
    def ok(self) -> bool:
        return all(record.ok for record in self.records)

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "kpi_execution_harness.json",
            "version": 1,
            "generated_by": "run-kpi-execution-harness",
            "workspace": self.workspace,
            "ok": self.ok,
            "kpi_count": len(self.records),
            "passed_count": sum(1 for record in self.records if record.ok),
            "failed_count": sum(1 for record in self.records if not record.ok),
            "records": [record.summary() for record in self.records],
            "manifest_path": self.manifest_path,
            "report_path": self.report_path,
        }


class KPIExecutionHarness:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        sample_limit: int = 20,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.sample_limit = sample_limit

    def run(self) -> KPIExecutionHarnessResult:
        self.layout.ensure_runtime_dirs()
        records = self._execute_records()
        manifest_path = self.layout.evidence_dir / "kpi_execution_harness.json"
        report_path = self.layout.reports_dir / "kpi_execution_harness.md"
        result = KPIExecutionHarnessResult(
            workspace=_rel(self.workspace, self.repo_root),
            records=records,
            manifest_path=_rel(manifest_path, self.repo_root),
            report_path=_rel(report_path, self.repo_root),
        )
        manifest_path.write_text(json.dumps(result.summary(), indent=2), encoding="utf-8")
        report_path.write_text(_render_report(result), encoding="utf-8")
        return result

    def _execute_records(self) -> list[KPIExecutionRecord]:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - environment dependent
            return [
                KPIExecutionRecord(
                    kpi_id="workspace",
                    sql_path="",
                    status="failed",
                    errors=[f"duckdb is required to run KPI SQL harness: {exc}"],
                )
            ]

        sql_files = self._sql_files()
        if not sql_files:
            return [
                KPIExecutionRecord(
                    kpi_id="workspace",
                    sql_path=_rel(self.layout.solutions_dir, self.repo_root),
                    status="failed",
                    errors=["no generated KPI SQL files found"],
                )
            ]

        conn = duckdb.connect(":memory:")
        old_cwd = Path.cwd()
        try:
            import os

            os.chdir(self.repo_root)
            return [self._execute_one(conn, sql_path) for sql_path in sql_files]
        finally:
            import os

            os.chdir(old_cwd)
            conn.close()

    def _sql_files(self) -> list[Path]:
        if not self.layout.solutions_dir.exists():
            return []
        return [
            path
            for path in sorted(self.layout.solutions_dir.glob("kpi_*.sql"))
            if KPI_SQL_PATTERN.match(path.name) and path.name != "kpi_metrics.sql"
        ]

    def _execute_one(self, conn: Any, sql_path: Path) -> KPIExecutionRecord:
        kpi_id = _kpi_id_from_path(sql_path)
        result_view = f"{kpi_id}_results"
        record = KPIExecutionRecord(
            kpi_id=kpi_id,
            sql_path=_rel(sql_path, self.repo_root),
            status="failed",
            result_view=result_view,
        )
        sql = sql_path.read_text(encoding="utf-8")
        record.sql_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if not sql_defines_result_view(sql, result_view):
            record.errors.append(
                f"SQL must define final result view `{result_view}`; feature/staging views are not enough"
            )
            return record
        semantic_errors = self._semantic_errors(kpi_id, sql)
        if semantic_errors:
            record.semantic_checks.extend(semantic_errors)
            record.errors.extend(semantic_errors)
            return record
        try:
            conn.execute(sql)
            views = {
                str(row[0]).lower()
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.views WHERE lower(table_name) = lower(?)",
                    [result_view],
                ).fetchall()
            }
            if result_view.lower() not in views:
                record.errors.append(f"final result view `{result_view}` was not created")
                return record
            record.row_count = int(
                conn.execute(f'SELECT COUNT(*) FROM "{result_view}"').fetchone()[0]
            )
            cursor = conn.execute(f'SELECT * FROM "{result_view}" LIMIT {int(self.sample_limit)}')
            record.columns = [str(description[0]) for description in cursor.description or []]
            rows = cursor.fetchall()
            record.sample_output_table = render_markdown_table(record.columns, rows)
            if not record.columns:
                record.errors.append(f"final result view `{result_view}` has no columns")
                return record
            if _placeholder_result_columns(record.columns):
                record.errors.append(
                    f"final result view `{result_view}` exposes only placeholder readiness columns"
                )
                return record
            if record.row_count == 0:
                record.warnings.append(f"final result view `{result_view}` returned zero rows")
            record.status = "passed"
            return record
        except Exception as exc:
            record.errors.append(str(exc))
            return record

    def _semantic_errors(self, kpi_id: str, sql: str) -> list[str]:
        kpi = self._kpi_registry_by_id().get(kpi_id)
        if not kpi:
            return []
        errors: list[str] = []
        metric = str(kpi.get("metric") or "")
        cuts = str(kpi.get("cuts") or "")
        name = str(kpi.get("name") or kpi.get("description") or "")
        lowered_sql = sql.lower()
        lowered_metric = metric.lower()
        if "sum(" in lowered_metric:
            if "sum(" not in lowered_sql:
                errors.append(f"SQL does not implement workbook metric `{metric}`")
            for column in _metric_input_columns(metric):
                if column.lower() not in lowered_sql:
                    errors.append(f"SQL does not reference metric input `{column}` from `{metric}`")
        for cut_token in _quoted_cut_filter_tokens(cuts):
            if cut_token.lower() not in lowered_sql:
                errors.append(
                    f"SQL does not implement filter token `{cut_token}` from KPI cuts"
                )
        top_n_match = re.search(r"\btop\s+(\d+)\b", name.lower())
        if top_n_match:
            limit_value = top_n_match.group(1)
            if not re.search(rf"\blimit\s+{limit_value}\b", lowered_sql):
                errors.append(
                    f"SQL does not implement top {limit_value} ranking limit from KPI name"
                )
        return errors

    def _kpi_registry_by_id(self) -> dict[str, dict[str, Any]]:
        path = self.layout.contracts_dir / "kpi_registry.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        result = {}
        for idx, kpi in enumerate(data.get("kpis") or [], start=1):
            if not isinstance(kpi, dict):
                continue
            kpi_id = str(kpi.get("kpi_id") or f"kpi_{idx:03d}")
            result[kpi_id] = kpi
        return result


def sql_defines_result_view(sql: str, result_view: str) -> bool:
    pattern = RESULT_VIEW_PATTERN.pattern.format(view=re.escape(result_view))
    return re.search(pattern, sql, flags=re.IGNORECASE) is not None


def _placeholder_result_columns(columns: list[str]) -> bool:
    normalized = {str(column).strip().lower() for column in columns}
    return normalized and normalized.issubset({"ready_marker"})


def _kpi_id_from_path(path: Path) -> str:
    match = re.match(r"^(kpi_\d{3})", path.stem, flags=re.IGNORECASE)
    return match.group(1).lower() if match else path.stem.lower()


def _render_report(result: KPIExecutionHarnessResult) -> str:
    lines = [
        "# KPI Execution Harness",
        "",
        f"- Workspace: `{result.workspace}`",
        f"- Status: `{'passed' if result.ok else 'failed'}`",
        f"- KPI SQL files checked: `{len(result.records)}`",
        "",
    ]
    for record in result.records:
        lines.extend(
            [
                f"## {record.kpi_id}",
                "",
                f"- SQL: `{record.sql_path}`",
                f"- Result view: `{record.result_view}`",
                f"- Status: `{record.status}`",
                f"- Row count: `{record.row_count if record.row_count is not None else ''}`",
            ]
        )
        for error in record.errors:
            lines.append(f"- Error: {error}")
        for warning in record.warnings:
            lines.append(f"- Warning: {warning}")
        if record.sample_output_table:
            lines.extend(["", record.sample_output_table])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _quoted_cut_filter_tokens(cuts: str) -> list[str]:
    """Extract literal filter values from a KPI cuts string.

    Generic — looks for quoted literals or equality clauses inside the cuts
    text rather than hardcoding any domain word. Examples it picks up:
      "LineOfBusiness = 'Medicare'"  -> ["Medicare"]
      "status = \"open\""             -> ["open"]
      "region IN ('NA', 'EU')"        -> ["NA", "EU"]
    Returns [] for cuts that only list dimension names (no literal filter).
    """
    if not cuts:
        return []
    tokens: list[str] = []
    for match in re.finditer(r"['\"]([^'\"]{1,80})['\"]", cuts):
        value = match.group(1).strip()
        if value:
            tokens.append(value)
    return tokens


def _metric_input_columns(metric: str) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for match in SUM_INPUT_PATTERN.finditer(metric):
        column = match.group(1)
        key = column.lower()
        if key not in seen:
            columns.append(column)
            seen.add(key)
    return columns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute generated KPI SQL and verify final result views."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--sample-limit", type=int, default=20, help="Rows to show per KPI result sample.")
    args = parser.parse_args(argv)

    result = KPIExecutionHarness(args.repo_root, args.workspace, sample_limit=args.sample_limit).run()
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

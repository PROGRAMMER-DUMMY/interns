"""Execute generated KPI SQL and prove that final result views exist."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.onboarding.kpi.intent_coverage import (
    denominator_scope_findings,
    grain_coverage_findings,
    join_correctness_findings,
    prose_filter_findings,
    _load_proven_join_pairs,
)
from core.onboarding.kpi.result_view_builder import parse_kpi
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


RESULT_VIEW_PATTERN = re.compile(
    r"create\s+or\s+replace\s+(?:temp\s+|temporary\s+)?view\s+[`\"]?{view}[`\"]?\s+as",
    re.IGNORECASE,
)
KPI_SQL_PATTERN = re.compile(r"^kpi_\d{3}(?:_[a-z0-9_]+)?\.sql$", re.IGNORECASE)
# An intent-decision block (e.g. grain bucketing) is a PENDING USER DECISION, not
# an execution failure. The generator emits a `-- BLOCKED: <facet> decision
# required` marker and a passthrough result view. Detect it generically so the
# harness can classify the KPI as `blocked_pending_decision` instead of `failed`
# — otherwise the artifact validator hard-fails and the very command that records
# the decision (apply-kpi-panel-answer / apply-pipeline-decision) deadlocks.
INTENT_BLOCK_PATTERN = re.compile(
    r"^--\s*BLOCKED:.*decision required", re.IGNORECASE | re.MULTILINE
)
BLOCKED_PENDING_STATUS = "blocked_pending_decision"
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
            "blocked_pending_count": sum(
                1 for record in self.records
                if record.status == BLOCKED_PENDING_STATUS
            ),
            "failed_count": sum(
                1 for record in self.records
                if not record.ok and record.status != BLOCKED_PENDING_STATUS
            ),
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
        self._registry_cache: dict[str, dict[str, Any]] | None = None
        self._mapping_cache: dict[str, dict[str, Any]] | None = None
        self._proven_pairs_cache: set[frozenset[str]] | None = None
        # Cached pipeline_decisions.json (None = not yet loaded; {} = absent/unparseable)
        self._pipeline_decisions_cache: dict[str, Any] | None = None

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

    def execute_only(self) -> list[KPIExecutionRecord]:
        """Execute the generated KPI SQL and return records WITHOUT writing the
        manifest/report. Used by the artifact validator to re-verify an on-disk
        manifest against real execution (tamper detection) with no side effects.
        """
        return self._execute_records()

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
        # Deterministic temporal semantics: pin UTC so date bucketing does not
        # depend on the machine's session timezone (see flow._write_result_preview).
        try:
            conn.execute("SET TimeZone='UTC'")
        except Exception:
            pass
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
        files = [
            path
            for path in sorted(self.layout.solutions_dir.glob("kpi_*.sql"))
            if KPI_SQL_PATTERN.match(path.name) and path.name != "kpi_metrics.sql"
        ]
        # Orphan guard: a solutions file whose kpi_id is no longer in the
        # current feature mapping is a stale leftover from an earlier registry
        # (e.g. extraction noise since cleaned up). Executing it pollutes the
        # harness/results with junk rows, so skip it. Empty/missing mapping
        # applies no filter (mapping-less workspaces still execute everything).
        known_ids = self._mapping_kpi_ids()
        if known_ids:
            files = [path for path in files if _kpi_id_from_path(path) in known_ids]
        return files

    def _mapping_kpi_ids(self) -> set[str]:
        path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not path.exists():
            return set()
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return set()
        return {
            str(kpi.get("kpi_id") or "")
            for kpi in (mapping.get("kpis") or [])
            if isinstance(kpi, dict) and kpi.get("kpi_id")
        }

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
        if sql_is_intent_blocked(sql):
            # Pending user decision, not a failure: record it as such and skip
            # execution/semantic checks. The validator treats this as non-fatal so
            # the decision that unblocks it can actually be recorded.
            record.status = BLOCKED_PENDING_STATUS
            record.warnings.append(_block_reason(sql))
            return record
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
        # The verifier must parse the KPI the SAME way the generator did: with the
        # recorded grain-bucketing decision. Without it, parse_kpi short-circuits
        # at the grain block and returns no aggregations, which would make the
        # metric check below spuriously fail for a share-by-continuous-cut KPI.
        grain_decision = (
            self._pipeline_decisions_by_id().get("grain_bucketing_decisions") or {}
        ).get(kpi_id)
        if "sum(" in lowered_metric:
            # An aggregation over DISTINCT values is rendered by the generator as
            # COUNT(DISTINCT ...) (summing distinct values is meaningless), so a
            # metric's sum(distinct X) legitimately appears as COUNT(DISTINCT X)
            # in the SQL. Ask the SAME canonical parser the generator uses whether
            # the metric is a distinct aggregation, rather than re-scanning the
            # raw metric text here. This keeps the gate consistent with the
            # generator for every workspace and inherits its metric-token handling
            # (no metric-spelling rules duplicated in the verifier).
            parsed_check = parse_kpi(kpi, grain_bucketing=grain_decision)
            expects_distinct_count = any(
                agg.distinct for agg in parsed_check.aggregations
            )
            # Single-attribution shares render the distinct-entity count as an
            # attribution CTE (ROW_NUMBER ... __attribution_rn = 1 + COUNT(*)):
            # one row per entity IS the distinct count. The legacy windowed
            # COUNT(DISTINCT ...) rendering stays accepted for older SQL.
            attribution_expected = parsed_check.share_attribution is not None
            implements_sum = (
                "sum(" in lowered_sql
                or ((expects_distinct_count or attribution_expected)
                    and "count(distinct" in lowered_sql)
                or (attribution_expected and "__attribution_rn" in lowered_sql)
            )
            if not implements_sum:
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
        # Grain coverage (BUG-024): every declared cut/grain dimension must be
        # realized in the result view, not silently dropped. Derived by an
        # INDEPENDENT tokenizer so a generator that drops a cut cannot pass a
        # check that reused its own parser. Scope is the result view, so a cut
        # present only in an upstream features view does not count.
        # The registry KPI carries cuts but not features (those live in the
        # feature mapping), so enrich it so cut tokens like "Department Name"
        # resolve to their physical column ("Name") instead of false-flagging.
        coverage_kpi = dict(kpi)
        coverage_kpi.setdefault("kpi_id", kpi_id)
        if not coverage_kpi.get("features"):
            mapped = self._feature_mapping_by_id().get(kpi_id) or {}
            if mapped.get("features"):
                coverage_kpi["features"] = mapped["features"]
        errors.extend(
            finding.message
            for finding in grain_coverage_findings(coverage_kpi, sql)
            if finding.severity == "error"
        )

        # JOIN-CORRECTNESS: every JOIN ON clause must correspond to a proven
        # relationship.  Loads relationship_contracts.json once per harness run.
        # If the file is absent (single-dataset workspaces produce no joins),
        # the check is skipped — missing contracts are not an error here.
        proven_pairs = self._proven_join_pairs()
        if proven_pairs is not None:
            errors.extend(
                finding.message
                for finding in join_correctness_findings(sql, proven_pairs, kpi_id=kpi_id)
                if finding.severity == "error"
            )

        # FILTER-REALIZATION (prose): high-confidence prose filters declared in
        # the KPI name must appear in the generated SQL.  Conservative patterns
        # only — "for <Value> <lob-word>" and "above/over N year[s]".
        errors.extend(
            finding.message
            for finding in prose_filter_findings(coverage_kpi, sql)
            if finding.severity == "error"
        )

        # DENOMINATOR-SCOPE (design/kpi_intent_contract.md §5): a recorded
        # within-group denominator-scope decision must be realized in the
        # generated SQL as OVER (PARTITION BY <group>), NOT a bare OVER ().
        # A decision that is recorded in pipeline_decisions.json but silently
        # ignored by the generator is a hard error — it was exactly this gap
        # that let the kpi_002 review rubber-stamp unchanged SQL (BUG-025).
        pipeline_decisions = self._pipeline_decisions_by_id()
        scope = (pipeline_decisions.get("percentage_denominator_scopes") or {}).get(kpi_id)
        errors.extend(
            f"{finding.code}: {finding.message}"
            for finding in denominator_scope_findings(coverage_kpi, sql, scope)
            if finding.severity == "error"
        )

        return errors

    def _kpi_registry_by_id(self) -> dict[str, dict[str, Any]]:
        if self._registry_cache is None:
            self._registry_cache = self._load_kpis_by_id("kpi_registry.json")
        return self._registry_cache

    def _feature_mapping_by_id(self) -> dict[str, dict[str, Any]]:
        if self._mapping_cache is None:
            self._mapping_cache = self._load_kpis_by_id("kpi_feature_mapping.json")
        return self._mapping_cache

    def _proven_join_pairs(self) -> set[frozenset[str]] | None:
        """Proven join column-pairs from relationship_contracts.json, cached.

        Returns None when the contracts file is absent (single-dataset or test
        workspaces produce no joins to prove), so the caller skips the
        join-correctness check rather than flagging every join. When the file
        exists, returns the (possibly empty) set of proven column pairs.
        """
        if self._proven_pairs_cache is None:
            path = self.layout.contracts_dir / "relationship_contracts.json"
            if not path.exists():
                return None
            self._proven_pairs_cache = _load_proven_join_pairs(path)
        return self._proven_pairs_cache

    def _pipeline_decisions_by_id(self) -> dict[str, Any]:
        """Load ``pipeline_decisions.json`` once per harness run, cached.

        Returns the full decisions dict (e.g. ``{"percentage_denominator_scopes":
        {"kpi_002": "within_department"}}``) or an empty dict when the file is
        absent, unreadable, or contains invalid JSON.  The harness uses this to
        retrieve the per-KPI ``denominator_scope`` for the enforcement check.
        """
        if self._pipeline_decisions_cache is None:
            path = self.layout.contracts_dir / "pipeline_decisions.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._pipeline_decisions_cache = data if isinstance(data, dict) else {}
                except (json.JSONDecodeError, OSError):
                    self._pipeline_decisions_cache = {}
            else:
                self._pipeline_decisions_cache = {}
        return self._pipeline_decisions_cache

    def _load_kpis_by_id(self, filename: str) -> dict[str, dict[str, Any]]:
        path = self.layout.contracts_dir / filename
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


def sql_is_intent_blocked(sql: str) -> bool:
    """True when generated SQL carries an intent-decision block marker.

    Generic across facets (grain bucketing today, any future `... decision
    required` block) — it keys off the marker convention, not a domain word.
    """
    return bool(INTENT_BLOCK_PATTERN.search(sql or ""))


def _block_reason(sql: str) -> str:
    for line in (sql or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("-- reason:"):
            return "blocked pending decision: " + stripped[len("-- reason:"):].strip()
    return "KPI generation is blocked pending a user decision (e.g. grain bucketing)."


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
    parser.add_argument(
        "--quiet", action="store_true",
        help="Print a compact per-KPI status summary + report path instead of the full JSON.",
    )
    args = parser.parse_args(argv)

    result = KPIExecutionHarness(args.repo_root, args.workspace, sample_limit=args.sample_limit).run()
    if args.quiet:
        status = "[ok] passed" if result.ok else "[x] failed"
        print(f"{status} kpi-execution: {result.workspace} ({len(result.records)} KPI SQL files)")
        for record in result.records:
            marker = "[ok]" if record.status in {"ok", "passed"} else "[x]"
            rows = "" if record.row_count is None else f" - {record.row_count} rows"
            print(f"  {marker} {record.kpi_id}: {record.status}{rows}")
            for error in record.errors:
                print(f"      [x] {error}")
        report_path = _rel(Path(args.repo_root).resolve() / args.workspace / "interns/reports/kpi_execution_harness.md", Path(args.repo_root).resolve())
        print(f"detail: {report_path}")
    else:
        print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

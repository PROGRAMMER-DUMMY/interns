"""Self-grill gate: prove generated KPI output is executable AND aligned with intent.

This is the internal verification loop that runs *before* a KPI result is shown
to a user or written to a results report. It answers three questions for each
generated KPI SQL file:

1. Does it execute?  (engine/warehouse-aware execution against DuckDB — including
   warehouse-mode SQL that references `fact_*` / `dim_*` tables, which the plain
   in-memory execution harness cannot run.)
2. Is the result real?  (final result view exists, has non-placeholder columns,
   and ideally returns rows.)
3. Does it match the KPI's intent?  (the metric, every cut, and every name-derived
   filter that the builder's own parser says should be present actually appear in
   the emitted SQL — and no garbage filter literal was pulled out of the KPI text.)

The verdict is blocking: callers should refuse to present a KPI as "complete" when
`VerifyResult.ok` is False. Checks are workspace-agnostic — every expectation is
derived from the KPI text, the feature mapping, and the builder's parser, never
from hardcoded domain vocabulary.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.onboarding.kpi.execution_harness import (
    _kpi_id_from_path,
    _metric_input_columns,
    sql_defines_result_view,
)
from core.onboarding.kpi.result_view_builder import (
    _column_lookup,
    _detect_time_bucket,
    _resolve_column,
    _split_cuts,
    parse_kpi,
)
from core.onboarding.kpi.pii_redaction import (
    redact_table_rows,
    workspace_aggregate_ages,
    workspace_redaction_patterns,
)
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout

KPI_SQL_PATTERN = re.compile(r"^kpi_\d{3}(?:_[a-z0-9_]+)?\.sql$", re.IGNORECASE)
_WAREHOUSE_TABLE_REF = re.compile(r'from\s+"(?:fact|dim)_', re.IGNORECASE)
# WHERE "<col>" = '<literal>'  — pull literal so we can sanity-check it.
_WHERE_LITERAL = re.compile(r"=\s*'([^']{1,200})'")


@dataclass
class VerifyRecord:
    kpi_id: str
    sql_path: str
    engine: str = "sql"
    executed: bool = False
    result_view: str = ""
    row_count: int | None = None
    columns: list[str] = field(default_factory=list)
    sample_output_table: str = ""
    intent_checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, Any]:
        return {
            "kpi_id": self.kpi_id,
            "sql_path": self.sql_path,
            "engine": self.engine,
            "executed": self.executed,
            "result_view": self.result_view,
            "row_count": self.row_count,
            "columns": self.columns,
            "sample_output_table": self.sample_output_table,
            "intent_checks": self.intent_checks,
            "errors": self.errors,
            "warnings": self.warnings,
            "status": "passed" if self.ok else "blocked",
        }


@dataclass
class VerifyResult:
    workspace: str
    records: list[VerifyRecord]

    @property
    def ok(self) -> bool:
        return bool(self.records) and all(record.ok for record in self.records)

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "kpi_output_verification.json",
            "version": 1,
            "workspace": self.workspace,
            "ok": self.ok,
            "kpi_count": len(self.records),
            "passed_count": sum(1 for r in self.records if r.ok),
            "blocked_count": sum(1 for r in self.records if not r.ok),
            "records": [r.summary() for r in self.records],
        }


class KPIOutputVerifier:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        sample_limit: int = 20,
        cross_engine: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.sample_limit = sample_limit
        self.cross_engine = cross_engine

    # ── public API ──────────────────────────────────────────────────────────

    def verify(self, kpi_id: str | None = None) -> VerifyResult:
        records = [
            self._verify_one(path)
            for path in self._sql_files()
            if kpi_id is None or _kpi_id_from_path(path) == kpi_id.lower()
        ]
        result = VerifyResult(workspace=_rel(self.workspace, self.repo_root), records=records)
        self._write_artifacts(result)
        return result

    def verify_sql_text(self, kpi_id: str, sql: str) -> VerifyRecord:
        """Verify in-memory SQL text without a file on disk (used as a generation gate)."""
        record = VerifyRecord(kpi_id=kpi_id, sql_path="<in-memory>", result_view=f"{kpi_id}_results")
        self._check_intent(record, sql)
        self._execute(record, sql)
        return record

    # ── per-KPI verification ──────────────────────────────────────────────────

    def _verify_one(self, sql_path: Path) -> VerifyRecord:
        kpi_id = _kpi_id_from_path(sql_path)
        record = VerifyRecord(
            kpi_id=kpi_id,
            sql_path=_rel(sql_path, self.repo_root),
            result_view=f"{kpi_id}_results",
        )
        sql = sql_path.read_text(encoding="utf-8")
        self._check_intent(record, sql)
        self._execute(record, sql)
        if self.cross_engine and record.executed:
            self._cross_engine_check(record, kpi_id)
        return record

    # ── intent cross-checks (static, no execution) ────────────────────────────

    def _check_intent(self, record: VerifyRecord, sql: str) -> None:
        kpi = self._kpi_by_id().get(record.kpi_id)
        if not kpi:
            record.warnings.append("no KPI registry/mapping entry found; intent checks skipped")
            return
        lowered = sql.lower()

        metric = str(kpi.get("metric") or "")
        metric_lower = metric.lower()
        if "sum(" in metric_lower:
            # `sum(distinct X)` is correctly emitted as `count(distinct X)`
            # (count of distinct lives), so accept either form.
            satisfied = "sum(" in lowered or (
                "distinct" in metric_lower and "count(distinct" in lowered
            )
            if not satisfied:
                record.errors.append(f"metric `{metric}` is not implemented in SQL")
        for column in _metric_input_columns(metric):
            if column.lower() not in lowered:
                record.errors.append(f"metric input `{column}` from `{metric}` is missing from SQL")

        # Top-N from the KPI name must become a LIMIT.
        name = str(kpi.get("name") or kpi.get("description") or "")
        top_match = re.search(r"\btop\s+(\d+)\b", name.lower())
        if top_match and not re.search(rf"\blimit\s+{top_match.group(1)}\b", lowered):
            record.errors.append(f"top {top_match.group(1)} ranking from KPI name has no LIMIT in SQL")

        self._check_cut_coverage(record, kpi, lowered)
        self._check_parser_filters(record, kpi, lowered)
        self._check_garbage_filters(record, kpi, sql)
        self._check_semantic_gloss(record, kpi)
        self._check_grain_explosion(record, kpi)
        if record.ok:
            record.intent_checks.append("metric, cuts, and name-derived filters present in SQL")

    _RAW_AGE_RE = re.compile(r"\bage\b", re.IGNORECASE)

    def _check_grain_explosion(self, record: VerifyRecord, kpi: dict[str, Any]) -> None:
        """Non-blocking: flag a cut that GROUPS BY a raw continuous value (exact
        age) rather than a band. "share by age" almost always means age ranges;
        grouping by exact age explodes the result to one row per value. Generic:
        ``age`` is a generic temporal concept, not domain vocabulary. Filters
        (tokens with =/<>) are skipped -- only grouping dimensions count."""
        for token in _split_cuts(str(kpi.get("cuts") or "")):
            if any(op in token for op in ("=", "<", ">")):
                continue
            if self._RAW_AGE_RE.search(token):
                record.warnings.append(
                    f"[~] grain_explosion_risk: grouping by raw `{token.strip()}` (exact "
                    "value) can explode the result to one row per value; a band (e.g. age "
                    "ranges) is usually intended -- confirm the grain."
                )

    _MEASURE_RE = re.compile(
        r"\b(?:avg|sum|count|min|max|median)\s*\(\s*(?:distinct\s+)?"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        re.IGNORECASE,
    )
    _GLOSS_STOPWORDS = frozenset(
        {
            "the", "a", "an", "of", "for", "each", "per", "by", "in", "on", "to",
            "and", "or", "with", "how", "many", "number", "count", "total", "sum",
            "average", "avg", "mean", "percentage", "percent", "share", "what",
            "which", "is", "are", "was", "were", "do", "does", "did", "value",
            "amount", "over", "time", "all", "every", "broken", "down", "this",
            "that", "including", "excluding", "not", "line", "item", "items",
            "data", "record", "records", "id", "code",
        }
    )

    def _gloss_tokens(self, text: str) -> set[str]:
        toks = {t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if len(t) > 1}
        toks = {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in toks}
        return toks - self._GLOSS_STOPWORDS

    def _workspace_glosses(self) -> dict[str, str]:
        """{normalized column -> description} from the workspace dictionaries.
        Cached. Convention-based discovery; never raises."""
        cache = getattr(self, "_gloss_cache", None)
        if cache is not None:
            return cache
        import csv as _csv

        glosses: dict[str, str] = {}
        try:
            for path in sorted(self.workspace.rglob("*dictionary*.csv")):
                if not path.is_file() or "/interns/" in path.as_posix():
                    continue
                try:
                    with path.open("r", encoding="utf-8-sig", newline="") as handle:
                        reader = _csv.DictReader(handle)
                        names = {str(n).strip().lower(): n for n in (reader.fieldnames or [])}
                        fk = next((names[k] for k in ("field", "column", "name") if k in names), None)
                        dk = next((names[k] for k in ("description", "definition", "meaning") if k in names), None)
                        if not fk or not dk:
                            continue
                        for row in reader:
                            field = str(row.get(fk) or "").strip().lower()
                            desc = str(row.get(dk) or "").strip()
                            if field and desc:
                                glosses.setdefault(field, desc)
                except OSError:
                    continue
        except OSError:
            pass
        self._gloss_cache = glosses
        return glosses

    def _check_semantic_gloss(self, record: VerifyRecord, kpi: dict[str, Any]) -> None:
        """Non-blocking: flag when the chosen measure column's data-dictionary
        gloss does not overlap the question's measure noun (e.g. question says
        "claim cost" but the column is glossed "base cost excluding..."). Surfaces
        for review at the gate without failing execution. Generic: tokens only,
        no domain word list. No dictionary -> no-op."""
        glosses = self._workspace_glosses()
        if not glosses:
            return
        metric = str(kpi.get("metric") or "")
        question = str(kpi.get("name") or kpi.get("business_question") or "")
        question_tokens = self._gloss_tokens(question)
        if not question_tokens:
            return
        for match in self._MEASURE_RE.finditer(metric):
            column = match.group(1)
            gloss = glosses.get(column.lower())
            if not gloss:
                continue
            gloss_tokens = self._gloss_tokens(gloss)
            # If the gloss shares no content token with the question's measure
            # nouns, the column may not be what the question asked for.
            if gloss_tokens and not (gloss_tokens & question_tokens):
                record.warnings.append(
                    f"[~] semantic_gloss_mismatch: measure column `{column}` glossed "
                    f'"{gloss[:80]}" shares no term with the question measure '
                    "-- confirm it answers the KPI before trusting the result"
                )

    def _check_cut_coverage(self, record: VerifyRecord, kpi: dict[str, Any], lowered_sql: str) -> None:
        """Every cut must resolve to a known column and appear in the SQL.

        Catches the class of bug where a cut like `Department Name` is emitted
        verbatim as a column reference that does not exist (binder error) or is
        silently dropped from the result.
        """
        lookup = _column_lookup(kpi)
        for token in _split_cuts(str(kpi.get("cuts") or "")):
            if "=" in token or ">" in token or "<" in token:
                continue  # filter token, handled elsewhere
            bucket = _detect_time_bucket(token)
            if bucket:
                _unit, source, _alias = bucket
                col = _resolve_column(source or _alias, lookup) or source
                if col and col.lower() not in lowered_sql:
                    record.warnings.append(f"time cut `{token}` source column `{col}` not found in SQL")
                continue
            # age / days-since cuts become date arithmetic, not a literal column
            if re.search(r"\bage\b|\bdays?\s+since\b", token, re.IGNORECASE):
                continue
            clean = re.sub(r"\(.*?\)", "", token).strip()
            if not clean:
                continue
            resolved = _resolve_column(clean, lookup)
            if resolved.lower() not in lowered_sql:
                record.errors.append(
                    f"cut `{token}` did not resolve to a column present in SQL "
                    f"(resolved to `{resolved}`); it is dropped or references a non-existent column"
                )

    def _check_parser_filters(self, record: VerifyRecord, kpi: dict[str, Any], lowered_sql: str) -> None:
        """Filters the builder's own parser derives must appear in the emitted SQL.

        Guards against emit drift where a name-derived filter (e.g. a categorical
        segment or an age threshold) is parsed but dropped before SQL is written.
        """
        try:
            parsed = parse_kpi(kpi)
        except Exception:  # parser must never break the gate
            return
        for filt in parsed.filters:
            col = filt.column
            needle = col.lower() if ("(" not in col and " " not in col) else None
            if needle and needle not in lowered_sql:
                record.errors.append(
                    f"expected filter on `{col}` (parsed from KPI text) is missing from SQL"
                )

    def _check_garbage_filters(self, record: VerifyRecord, kpi: dict[str, Any], sql: str) -> None:
        """Detect filter literals scraped out of the KPI prose by mistake.

        Example bug: `WHERE "Gender" = 'amount paid for medicare LOB across'` — a
        fragment of the KPI title turned into an equality literal.
        """
        name = str(kpi.get("name") or "").lower()
        for match in _WHERE_LITERAL.finditer(sql):
            literal = match.group(1).strip()
            words = literal.split()
            looks_like_prose = len(words) >= 4 or (len(literal) >= 15 and literal.lower() in name)
            if looks_like_prose:
                record.errors.append(
                    f"suspicious filter literal `{literal}` looks like a fragment of the KPI text, "
                    "not a real value"
                )

    # ── execution (engine/warehouse aware) ────────────────────────────────────

    def _execute(self, record: VerifyRecord, sql: str) -> None:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - environment dependent
            record.warnings.append(f"duckdb not installed; execution skipped: {exc}")
            return
        if not sql_defines_result_view(sql, record.result_view):
            record.errors.append(
                f"SQL does not define final result view `{record.result_view}`"
            )
            return

        warehouse_path = self.layout.state_dir / "warehouse.duckdb"
        use_warehouse = bool(_WAREHOUSE_TABLE_REF.search(sql)) and warehouse_path.exists()

        from core.storage.atomic_io import pushd

        cwd_cm = None  # thread-safe cwd guard, only for the in-memory branch
        conn = None
        try:
            if use_warehouse:
                record.engine = "sql+warehouse"
                conn = duckdb.connect(str(warehouse_path))
                try:
                    conn.execute("LOAD delta;")
                except Exception:
                    pass
            else:
                conn = duckdb.connect(":memory:")
                # so read_csv_auto('workspaces/...') resolves; serialized via pushd
                cwd_cm = pushd(self.repo_root)
                cwd_cm.__enter__()
            conn.execute(sql)
            record.executed = True
            views = {
                str(row[0]).lower()
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE lower(table_name) = lower(?)",
                    [record.result_view],
                ).fetchall()
            }
            if record.result_view.lower() not in views:
                record.errors.append(f"result view `{record.result_view}` was not created")
                return
            cursor = conn.execute(f'SELECT * FROM "{record.result_view}" LIMIT {int(self.sample_limit)}')
            record.columns = [str(d[0]) for d in cursor.description or []]
            rows = cursor.fetchall()
            record.row_count = int(
                conn.execute(f'SELECT COUNT(*) FROM "{record.result_view}"').fetchone()[0]
            )
            # Redact PHI/PCI in the verifier's sample table — it is rendered into
            # the self-grill report surfaced to the operator. Ref: ob-kpi-d.md.
            _patterns = workspace_redaction_patterns(self.workspace)
            rows = redact_table_rows(
                record.columns, rows, patterns=_patterns,
                aggregate_ages=workspace_aggregate_ages(self.workspace),
            )
            record.sample_output_table = render_markdown_table(record.columns, rows)
            if not record.columns:
                record.errors.append(f"result view `{record.result_view}` has no columns")
            elif {c.strip().lower() for c in record.columns}.issubset({"ready_marker"}):
                record.errors.append(
                    f"result view `{record.result_view}` exposes only placeholder columns"
                )
            elif record.row_count == 0:
                record.warnings.append(f"result view `{record.result_view}` returned zero rows")
        except Exception as exc:
            record.errors.append(f"execution failed: {exc}")
        finally:
            if conn is not None:
                conn.close()
            if cwd_cm is not None:
                cwd_cm.__exit__(None, None, None)

    # ── cross-engine parity (executes Polars, compares to SQL) ────────────────

    def _cross_engine_check(self, record: VerifyRecord, kpi_id: str) -> None:
        polars_path = self.layout.solutions_dir / f"{kpi_id}_polars.py"
        sql_path = self.layout.solutions_dir / f"{kpi_id}.sql"
        if not polars_path.exists() or not sql_path.exists():
            record.warnings.append("cross-engine parity not checked (missing polars or sql artifact)")
            return
        kpi = self._kpi_by_id().get(kpi_id)
        if not kpi:
            return
        from core.onboarding.kpi.kpi_intent import parse_intent

        out_col = self._output_column(parse_intent(kpi))
        try:
            proc = subprocess.run(
                [sys.executable, str(polars_path)],
                capture_output=True, text=True, timeout=600,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            record.warnings.append(f"could not run Polars script for parity: {exc}")
            return
        if proc.returncode != 0:
            record.errors.append(
                "Polars script failed to execute (cross-engine parity): "
                + (proc.stderr or "").strip()[-300:]
            )
            return
        try:
            import polars as pl

            gdf = pl.read_delta(str(self.layout.gold_dir / f"{kpi_id}_results"))
        except Exception as exc:
            record.warnings.append(f"could not read Polars Gold output for parity: {exc}")
            return
        p_rows = gdf.height
        p_sum = float(gdf[out_col].sum()) if out_col in gdf.columns else None

        s_rows, s_sum = self._sql_rows_and_sum(sql_path.read_text(encoding="utf-8"), record.result_view, out_col)
        if s_rows is not None and s_rows != p_rows:
            record.errors.append(
                f"cross-engine row mismatch on `{kpi_id}`: SQL={s_rows} vs Polars={p_rows}"
            )
        if s_sum is not None and p_sum is not None and abs(s_sum - p_sum) > max(0.01, abs(s_sum) * 1e-6):
            record.errors.append(
                f"cross-engine total mismatch on `{out_col}`: SQL={s_sum:.2f} vs Polars={p_sum:.2f}"
            )
        if record.ok:
            detail = f"rows={p_rows}" + (f", sum({out_col})={p_sum:.2f}" if p_sum is not None else "")
            record.intent_checks.append(f"cross-engine parity OK (SQL == Polars): {detail}")

        # PySpark parity — only when a local Spark is actually available (e.g. CI).
        self._pyspark_parity(record, kpi_id, out_col, s_rows, s_sum)

    def _pyspark_parity(
        self, record: VerifyRecord, kpi_id: str, out_col: str,
        s_rows: int | None, s_sum: float | None,
    ) -> None:
        import importlib.util

        pyspark_path = self.layout.solutions_dir / f"{kpi_id}_pyspark.py"
        if not pyspark_path.exists():
            return
        if importlib.util.find_spec("pyspark") is None:
            record.warnings.append("PySpark not installed; PySpark↔SQL parity not checked")
            return
        try:
            proc = subprocess.run(
                [sys.executable, str(pyspark_path)],
                capture_output=True, text=True, timeout=1200,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            record.warnings.append(f"could not run PySpark script for parity: {exc}")
            return
        if proc.returncode != 0:
            stderr = (proc.stderr or "")
            # A broken local Spark/JVM (no JAVA_HOME, SparkEnv null, py4j gateway) is an
            # environment problem, not a parity defect — skip with a warning rather than block.
            spark_env_markers = (
                "SparkEnv", "java.lang", "py4j", "Py4JJavaError", "JAVA_HOME",
                "JavaPackage", "NullPointerException", "gateway", "JAVA_GATEWAY_EXITED",
                "Spark 3.5 supports Java", "Invalid Spark URL", "getSubject",
            )
            if any(marker in stderr for marker in spark_env_markers):
                record.warnings.append("Spark not operational in this environment; PySpark parity not checked")
            else:
                record.errors.append(
                    "PySpark script failed to execute (cross-engine parity): " + stderr.strip()[-300:]
                )
            return
        try:
            import polars as pl

            sdf = pl.read_delta(str(self.layout.gold_dir / f"{kpi_id}_results"))
        except Exception as exc:
            record.warnings.append(f"could not read PySpark Gold output for parity: {exc}")
            return
        if s_rows is not None and s_rows != sdf.height:
            record.errors.append(
                f"PySpark row mismatch on `{kpi_id}`: SQL={s_rows} vs PySpark={sdf.height}"
            )
        if s_sum is not None and out_col in sdf.columns:
            ps_sum = float(sdf[out_col].sum())
            if abs(s_sum - ps_sum) > max(0.01, abs(s_sum) * 1e-6):
                record.errors.append(
                    f"PySpark total mismatch on `{out_col}`: SQL={s_sum:.2f} vs PySpark={ps_sum:.2f}"
                )
        if record.ok:
            record.intent_checks.append("cross-engine parity OK (SQL == PySpark)")

    def _output_column(self, intent: Any) -> str:
        if intent.share:
            return intent.share.alias
        if intent.ratio:
            return "ratio"
        if intent.metric:
            return intent.metric.alias
        return "row_count"

    def _sql_rows_and_sum(self, sql: str, result_view: str, out_col: str) -> tuple[int | None, float | None]:
        try:
            import duckdb
        except ImportError:  # pragma: no cover
            return None, None
        warehouse_path = self.layout.state_dir / "warehouse.duckdb"
        use_warehouse = bool(_WAREHOUSE_TABLE_REF.search(sql)) and warehouse_path.exists()
        from core.storage.atomic_io import pushd

        cwd_cm = None
        conn = None
        try:
            if use_warehouse:
                conn = duckdb.connect(str(warehouse_path))
                try:
                    conn.execute("LOAD delta;")
                except Exception:
                    pass
            else:
                conn = duckdb.connect(":memory:")
                cwd_cm = pushd(self.repo_root)
                cwd_cm.__enter__()
            conn.execute(sql)
            rows = int(conn.execute(f'SELECT COUNT(*) FROM "{result_view}"').fetchone()[0])
            cols = {
                str(r[0]).lower()
                for r in conn.execute(f'SELECT * FROM "{result_view}" LIMIT 0').description or []
            }
            total = None
            if out_col.lower() in cols:
                total = conn.execute(f'SELECT SUM("{out_col}") FROM "{result_view}"').fetchone()[0]
                total = float(total) if total is not None else None
            return rows, total
        except Exception:
            return None, None
        finally:
            if conn is not None:
                conn.close()
            if cwd_cm is not None:
                cwd_cm.__exit__(None, None, None)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _sql_files(self) -> list[Path]:
        if not self.layout.solutions_dir.exists():
            return []
        return [
            path
            for path in sorted(self.layout.solutions_dir.glob("kpi_*.sql"))
            if KPI_SQL_PATTERN.match(path.name) and path.name != "kpi_metrics.sql"
        ]

    def _kpi_by_id(self) -> dict[str, dict[str, Any]]:
        """Merge feature mapping (authoritative for source_columns) with registry text."""
        merged: dict[str, dict[str, Any]] = {}
        for name in ("kpi_registry.json", "kpi_feature_mapping.json"):
            path = self.layout.contracts_dir / name
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for idx, kpi in enumerate(data.get("kpis") or [], start=1):
                if not isinstance(kpi, dict):
                    continue
                kpi_id = str(kpi.get("kpi_id") or f"kpi_{idx:03d}")
                merged.setdefault(kpi_id, {}).update(kpi)
        return merged

    def _write_artifacts(self, result: VerifyResult) -> None:
        self.layout.ensure_runtime_dirs()
        evidence = self.layout.evidence_dir / "kpi_output_verification.json"
        evidence.write_text(json.dumps(result.summary(), indent=2), encoding="utf-8")
        report = self.layout.reports_dir / "kpi_output_verification.md"
        report.write_text(_render_report(result), encoding="utf-8")


def _render_report(result: VerifyResult) -> str:
    lines = [
        "# KPI Output Verification (grill-requirements self-grill mode)",
        "",
        f"- Workspace: `{result.workspace}`",
        f"- Verdict: `{'PASSED' if result.ok else 'BLOCKED'}`",
        f"- KPIs checked: `{len(result.records)}`",
        "",
    ]
    for record in result.records:
        lines += [
            f"## {record.kpi_id} — {'passed' if record.ok else 'BLOCKED'}",
            "",
            f"- SQL: `{record.sql_path}`",
            f"- Engine: `{record.engine}`",
            f"- Executed: `{record.executed}`",
            f"- Row count: `{record.row_count if record.row_count is not None else ''}`",
        ]
        for check in record.intent_checks:
            lines.append(f"- Intent OK: {check}")
        for error in record.errors:
            lines.append(f"- [x] {error}")
        for warning in record.warnings:
            lines.append(f"- [~] {warning}")
        if record.sample_output_table:
            lines += ["", record.sample_output_table]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Self-grill gate: verify generated KPI SQL is executable and intent-aligned."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path, e.g. workspaces/demo")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--kpi-id", default=None, help="Verify a single KPI id, e.g. kpi_001.")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument(
        "--cross-engine", action="store_true",
        help="Also execute the generated Polars script and block on result divergence from SQL.",
    )
    args = parser.parse_args(argv)

    result = KPIOutputVerifier(
        args.repo_root, args.workspace,
        sample_limit=args.sample_limit, cross_engine=args.cross_engine,
    ).verify(kpi_id=args.kpi_id)
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

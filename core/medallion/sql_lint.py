"""
core/medallion/sql_lint.py — SQL parse, plan, and hotspot lint for medallion SQL files.

Three passes:
  1. Parse validation via sqlglot (no DB needed).
  2. Plan cartesian-join check via DuckDB EXPLAIN on an in-memory connection.
  3. Hotspot check via tools/optimizer_finder.SQLOptimizer (uses a temp file).

Each pass appends LintFinding items; error-severity findings fail the build.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class LintFinding:
    file: str
    severity: str       # "error" | "warning"
    rule: str           # parse_failed | no_cartesian_join | hotspot | plan_error
    message: str


def lint_sql(sql: str, *, file_label: str, db_path: Optional[str] = None) -> list[LintFinding]:
    """
    Run all lint passes and return findings.
    db_path: real DuckDB file for plan checks; falls back to :memory: if None.
    """
    findings: list[LintFinding] = []
    findings.extend(_lint_parse(sql, file_label=file_label))
    if not any(f.rule == "parse_failed" for f in findings):
        findings.extend(_lint_plan(sql, file_label=file_label, db_path=db_path))
    findings.extend(_lint_hotspot(sql, file_label=file_label, db_path=db_path))
    return findings


# ── Pass 1: Parse validation ───────────────────────────────────────────────────

def _lint_parse(sql: str, *, file_label: str) -> list[LintFinding]:
    try:
        import sqlglot
        sqlglot.parse(sql)
        return []
    except ImportError:
        # sqlglot not installed — skip parse pass
        return []
    except Exception as exc:
        return [LintFinding(file_label, "error", "parse_failed", str(exc)[:300])]


# ── Pass 2: Plan cartesian check ──────────────────────────────────────────────

def _lint_plan(sql: str, *, file_label: str, db_path: Optional[str]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    conn_path = db_path if db_path else ":memory:"
    try:
        import duckdb
        con = duckdb.connect(conn_path)
        # Strip trailing semicolon — EXPLAIN doesn't accept it in some versions
        clean = sql.rstrip().rstrip(";").strip()
        plan_rows = con.execute(f"EXPLAIN {clean}").fetchall()
        plan_text = "\n".join(str(r) for r in plan_rows).lower()
        if "cross_product" in plan_text or "cartesian" in plan_text:
            findings.append(LintFinding(
                file_label, "error", "no_cartesian_join",
                "Query plan contains a CROSS/Cartesian product — add a join predicate."
            ))
    except Exception as exc:
        # EXPLAIN may fail if referenced tables don't exist yet; that's OK — not a lint error.
        msg = str(exc).lower()
        if "syntax" in msg or "parser" in msg:
            findings.append(LintFinding(file_label, "error", "plan_error", str(exc)[:300]))
    return findings


# ── Pass 3: Hotspot via optimizer_finder ──────────────────────────────────────

def _lint_hotspot(sql: str, *, file_label: str, db_path: Optional[str]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    try:
        from tools.optimizer_finder import SQLOptimizer
        db = db_path if db_path else ":memory:"
        with tempfile.NamedTemporaryFile(suffix=".sql", delete=False, mode="w", encoding="utf-8") as tf:
            tf.write(sql)
            tmp_path = tf.name
        result = SQLOptimizer(db_path=db, timeout_s=10).analyze(tmp_path)
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
        if result.status == "ok":
            for h in result.sql_hotspots + result.suggestions:
                sev = "error" if h.get("severity") == "critical" else "warning"
                findings.append(LintFinding(
                    file_label, sev, "hotspot",
                    h.get("message", str(h))[:300],
                ))
    except Exception:
        # Hotspot check is best-effort — never block the build on tool failure
        pass
    return findings

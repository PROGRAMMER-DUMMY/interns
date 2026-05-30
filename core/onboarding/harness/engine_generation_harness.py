"""Validate the generated multi-engine KPI outputs for coherence.

This harness does NOT execute Spark/Java or run any generated code. It performs
static coherence checks on the per-KPI engine artifacts written to
``interns/generated/solutions/``:

  * SQL (``kpi_<id>.sql``): defines its ``<kpi_id>_results`` result view
    (reusing ``sql_defines_result_view`` from the KPI execution harness).
  * Polars (``kpi_<id>_polars.py``): compiles as valid Python and is free of
    known-fatal patterns (must NOT contain ``pl.today(``).
  * PySpark (``kpi_<id>_pyspark.py``): compiles as valid Python and carries the
    Java/Hadoop preflight self-diagnosis (contains "Spark 3.5" or "JAVA_HOME").

When ``interns/reports/engine_generation/current.json`` exists, the harness also
cross-checks that every engine the report claims it generated for a KPI has a
matching file on disk.

The harness writes scoreable proof artifacts:
  * ``interns/reports/engine_generation_validation/current.json``
  * ``interns/reports/engine_generation_validation/current.md``
  * ``interns/generated/evidence/engine_generation_validation.json``

No domain vocabulary or workspace name is hardcoded; all KPI ids are discovered
from the generated filenames and the optional engine-generation report.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.kpi.execution_harness import sql_defines_result_view
from core.storage.workspace_layout import WorkspaceLayout

HARNESS_VERSION = 1
GENERATED_BY = "validate-engine-generation"
HARNESS_NAME = "engine_generation_validation"

# Generated filename patterns. ``kpi_<id>`` is the canonical id; SQL files may
# carry a trailing slug after the numeric id (kpi_001_some_slug.sql).
_SQL_PATTERN = re.compile(r"^(kpi_\d{3})(?:_[a-z0-9_]+)?\.sql$", re.IGNORECASE)
_POLARS_PATTERN = re.compile(r"^(kpi_\d{3})(?:_[a-z0-9_]+)?_polars\.py$", re.IGNORECASE)
_PYSPARK_PATTERN = re.compile(r"^(kpi_\d{3})(?:_[a-z0-9_]+)?_pyspark\.py$", re.IGNORECASE)

# A Polars fatal pattern: pl.today() does not exist in the Polars API and crashes
# at runtime. The generator must use pl.date(...)/datetime.now() style anchors.
_POLARS_FATAL_SUBSTRINGS = ("pl.today(",)
# The PySpark scripts embed a Java/Hadoop preflight so they self-diagnose env.
_PYSPARK_PREFLIGHT_MARKERS = ("Spark 3.5", "JAVA_HOME")


@dataclass
class EngineCheck:
    """One engine artifact check for one KPI."""

    engine: str
    path: str
    status: str  # "passed" | "failed"
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "passed" and not self.errors

    def summary(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "path": self.path,
            "status": self.status,
            "errors": list(self.errors),
        }


@dataclass
class KPIEngineRecord:
    """All engine checks for one KPI."""

    kpi_id: str
    engines_checked: list[str] = field(default_factory=list)
    checks: list[EngineCheck] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(check.ok for check in self.checks)

    @property
    def status(self) -> str:
        return "passed" if self.ok else "failed"

    def summary(self) -> dict[str, Any]:
        return {
            "kpi_id": self.kpi_id,
            "status": self.status,
            "engines_checked": list(self.engines_checked),
            "checks": [check.summary() for check in self.checks],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class EngineGenerationHarnessResult:
    workspace: str
    ok: bool
    status: str
    current_json_path: str
    current_markdown_path: str
    evidence_path: str
    kpi_count: int
    passed_count: int
    failed_count: int
    records: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": f"{HARNESS_NAME}_result",
            "version": HARNESS_VERSION,
            "generated_by": GENERATED_BY,
            **asdict(self),
        }


class EngineGenerationHarness:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> EngineGenerationHarnessResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()

        records = self._build_records()
        passed = sum(1 for record in records if record.ok)
        failed = len(records) - passed
        ok = failed == 0
        report = {
            "artifact_type": f"{HARNESS_NAME}/current.json",
            "version": HARNESS_VERSION,
            "generated_by": GENERATED_BY,
            "workspace": self.workspace_rel,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "status": "passed" if ok else "failed",
            "summary": {
                "kpi_count": len(records),
                "passed_count": passed,
                "failed_count": failed,
            },
            "records": [record.summary() for record in records],
            "next_commands": self._next_commands(ok),
        }

        report_dir = self.layout.reports_dir / HARNESS_NAME
        report_dir.mkdir(parents=True, exist_ok=True)
        current_json = report_dir / "current.json"
        current_md = report_dir / "current.md"
        evidence_path = self.layout.evidence_dir / f"{HARNESS_NAME}.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(report, indent=2, default=str) + "\n"
        current_json.write_text(serialized, encoding="utf-8")
        evidence_path.write_text(serialized, encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")

        return EngineGenerationHarnessResult(
            workspace=self.workspace_rel,
            ok=ok,
            status="passed" if ok else "failed",
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            evidence_path=_rel(evidence_path, self.repo_root),
            kpi_count=len(records),
            passed_count=passed,
            failed_count=failed,
            records=[record.summary() for record in records],
        )

    # ── checks ───────────────────────────────────────────────────────────────

    def _build_records(self) -> list[KPIEngineRecord]:
        solutions = self.layout.solutions_dir
        files_by_kpi = self._discover_files(solutions)
        claimed = self._claimed_engines()

        records: list[KPIEngineRecord] = []
        for kpi_id in sorted(set(files_by_kpi) | set(claimed)):
            files = files_by_kpi.get(kpi_id, {})
            record = KPIEngineRecord(kpi_id=kpi_id)
            for engine in ("sql", "polars", "pyspark"):
                path = files.get(engine)
                if path is None:
                    continue
                record.engines_checked.append(engine)
                record.checks.append(self._check_engine(kpi_id, engine, path))
            self._cross_check_report(record, files, claimed.get(kpi_id, set()))
            records.append(record)
        return records

    def _discover_files(self, solutions: Path) -> dict[str, dict[str, Path]]:
        files_by_kpi: dict[str, dict[str, Path]] = {}
        if not solutions.exists():
            return files_by_kpi
        for path in sorted(solutions.glob("*")):
            if not path.is_file():
                continue
            name = path.name
            for engine, pattern in (
                ("polars", _POLARS_PATTERN),
                ("pyspark", _PYSPARK_PATTERN),
                ("sql", _SQL_PATTERN),
            ):
                match = pattern.match(name)
                if match:
                    kpi_id = match.group(1).lower()
                    files_by_kpi.setdefault(kpi_id, {}).setdefault(engine, path)
                    break
        return files_by_kpi

    def _check_engine(self, kpi_id: str, engine: str, path: Path) -> EngineCheck:
        rel = _rel(path, self.repo_root)
        if engine == "sql":
            return self._check_sql(kpi_id, path, rel)
        if engine == "polars":
            return self._check_polars(path, rel)
        return self._check_pyspark(path, rel)

    def _check_sql(self, kpi_id: str, path: Path, rel: str) -> EngineCheck:
        errors: list[str] = []
        sql = _read_text(path)
        result_view = f"{kpi_id}_results"
        if not sql_defines_result_view(sql, result_view):
            errors.append(
                f"SQL does not define the result view `{result_view}` "
                "(expected `CREATE OR REPLACE VIEW <kpi_id>_results AS ...`)."
            )
        return EngineCheck(
            engine="sql",
            path=rel,
            status="passed" if not errors else "failed",
            errors=errors,
        )

    def _check_polars(self, path: Path, rel: str) -> EngineCheck:
        errors: list[str] = []
        src = _read_text(path)
        errors.extend(_compile_errors(src, str(path)))
        for fatal in _POLARS_FATAL_SUBSTRINGS:
            if fatal in src:
                errors.append(
                    f"Polars script contains the known-fatal pattern `{fatal}` "
                    "(not a valid Polars API; use a concrete date anchor)."
                )
        return EngineCheck(
            engine="polars",
            path=rel,
            status="passed" if not errors else "failed",
            errors=errors,
        )

    def _check_pyspark(self, path: Path, rel: str) -> EngineCheck:
        errors: list[str] = []
        src = _read_text(path)
        errors.extend(_compile_errors(src, str(path)))
        if not any(marker in src for marker in _PYSPARK_PREFLIGHT_MARKERS):
            errors.append(
                "PySpark script is missing the Java/Hadoop preflight self-diagnosis "
                f"(expected one of {list(_PYSPARK_PREFLIGHT_MARKERS)})."
            )
        return EngineCheck(
            engine="pyspark",
            path=rel,
            status="passed" if not errors else "failed",
            errors=errors,
        )

    def _cross_check_report(
        self,
        record: KPIEngineRecord,
        files: dict[str, Path],
        claimed_engines: set[str],
    ) -> None:
        for engine in sorted(claimed_engines):
            if engine not in files:
                record.errors.append(
                    f"engine_generation report claims `{engine}` was generated for "
                    f"{record.kpi_id}, but no matching file exists in the solutions dir."
                )

    def _claimed_engines(self) -> dict[str, set[str]]:
        path = self.layout.reports_dir / "engine_generation" / "current.json"
        payload = _load_json(path)
        claimed: dict[str, set[str]] = {}
        for kpi in payload.get("kpis") or []:
            if not isinstance(kpi, dict):
                continue
            kpi_id = str(kpi.get("kpi_id") or "").lower()
            if not kpi_id:
                continue
            engines = {
                str(engine).lower()
                for engine in (kpi.get("engines_generated") or [])
                if str(engine).strip()
            }
            if engines:
                claimed[kpi_id] = engines
        return claimed

    def _next_commands(self, ok: bool) -> list[str]:
        commands: list[str] = []
        if not ok:
            commands.append(f"uv run generate-kpi-engines --workspace {self.workspace_rel} --engine all")
        commands.append(f"uv run validate-engine-generation --workspace {self.workspace_rel}")
        return commands

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _compile_errors(src: str, path: str) -> list[str]:
    try:
        compile(src, path, "exec")
    except SyntaxError as exc:
        return [f"Python does not compile: {exc.__class__.__name__}: {exc.msg} (line {exc.lineno})."]
    except ValueError as exc:
        return [f"Python does not compile: {exc.__class__.__name__}: {exc}."]
    return []


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Engine Generation Validation",
        "",
        f"- Workspace: `{report['workspace']}`",
        f"- Status: `{report['status']}`",
        f"- KPIs checked: `{summary.get('kpi_count', 0)}`",
        f"- Passed: `{summary.get('passed_count', 0)}`",
        f"- Failed: `{summary.get('failed_count', 0)}`",
        "",
        "## KPIs",
        "",
    ]
    records = report.get("records") or []
    if not records:
        lines.append("_No generated engine files found._")
    for record in records:
        lines.append(
            f"### `{record.get('kpi_id')}` — `{record.get('status')}` "
            f"(engines: {', '.join(record.get('engines_checked') or []) or '—'})"
        )
        for check in record.get("checks") or []:
            mark = "passed" if check.get("status") == "passed" else "failed"
            lines.append(f"- `{check.get('engine')}` `{mark}` — `{check.get('path')}`")
            for error in check.get("errors") or []:
                lines.append(f"  - {error}")
        for error in record.get("errors") or []:
            lines.append(f"- {error}")
        lines.append("")
    lines.extend(["## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in report.get("next_commands") or [])
    lines.append("")
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate generated multi-engine KPI outputs for coherence (no Spark/Java execution)."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = EngineGenerationHarness(args.repo_root, args.workspace).run()
    print(json.dumps(result.summary(), indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

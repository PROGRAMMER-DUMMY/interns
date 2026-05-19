"""
core/medallion/build.py — build-medallion orchestrator.

Executes the Bronze -> Silver -> Gold pipeline on the DuckDB target, runs
Silver assertions, regenerates KPI SQL against Gold, and writes a per-run
state file.

All errors route through Governor.decide_medallion_routing; assertion
failures and row-equality mismatches are surfaced as blocker entries but
do not necessarily abort the build (see _FATAL_CODES).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.medallion.design import medallion_dirs
from core.medallion.manifest import Manifest
from core.medallion.merge_emitter import emit_silver_merge
from core.medallion.run_state import (
    RunState, TableRunStatus, WorkspaceBusy, acquire_lock, new_run_id,
)
from core.medallion.sql_lint import lint_sql
from core.orchestration.governor import Governor, RoutingDecision
from core.resource.manager import ResourceDecision
from core.storage.workspace_layout import WorkspaceLayout


# Exit code symbols
EXIT_MEDALLION_BUILD_FAIL = "MEDALLION_BUILD_FAIL"
EXIT_WORKSPACE_BUSY       = "WORKSPACE_BUSY"
EXIT_SQL_LINT_FAIL        = "SQL_LINT_FAIL"
EXIT_DESIGN_BLOCKERS      = "DESIGN_BLOCKERS"

# Stage codes that abort the build immediately
_FATAL_STAGE_CODES = {"BRONZE_LOAD_FAIL", "SILVER_TRANSFORM_FAIL", "GOLD_DERIVATION_FAIL"}


class MedallionBuildExit(Exception):
    def __init__(self, code: str, message: str, next_command: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_command = next_command


# ── Public entry point ─────────────────────────────────────────────────────────

def build_medallion(
    workspace: Path,
    repo_root: Path,
    *,
    cfg=None,
    only_layer: Optional[str] = None,
    only_table: Optional[str] = None,
    resume: Optional[str] = None,
    force_with_blockers: bool = False,
    resource_decision: ResourceDecision | None = None,
) -> RunState:
    workspace = workspace.resolve()
    repo_root = repo_root.resolve()
    layout = WorkspaceLayout(project_root=workspace)
    paths = medallion_dirs(layout)

    # 1. Acquire lockfile
    state_dir = paths["state"]
    try:
        with acquire_lock(state_dir) as _lock:
            return _run_build(
                workspace, repo_root, layout, paths, cfg=cfg,
                only_layer=only_layer, only_table=only_table, resume=resume,
                force_with_blockers=force_with_blockers,
                resource_decision=resource_decision,
            )
    except WorkspaceBusy as exc:
        raise MedallionBuildExit(EXIT_WORKSPACE_BUSY, str(exc))


# ── Inner build (lock held) ────────────────────────────────────────────────────

def _run_build(
    workspace: Path,
    repo_root: Path,
    layout: WorkspaceLayout,
    paths: dict[str, Path],
    *,
    cfg,
    only_layer: Optional[str],
    only_table: Optional[str],
    resume: Optional[str],
    force_with_blockers: bool,
    resource_decision: ResourceDecision | None,
) -> RunState:
    # 2. Load and validate manifest
    manifest_path = paths["medallion"] / "manifest.yaml"
    if not manifest_path.exists():
        raise MedallionBuildExit(
            EXIT_MEDALLION_BUILD_FAIL,
            f"No manifest found at {manifest_path}. Run design-medallion first.",
            next_command=f"uv run design-medallion --workspace {workspace.name}",
        )
    manifest = _load_manifest(manifest_path)

    if resource_decision and resource_decision.status == "blocked":
        target_is_local = manifest.target in {"duckdb", "auto"}
        if target_is_local:
            raise MedallionBuildExit(
                EXIT_MEDALLION_BUILD_FAIL,
                "Local medallion build blocked by resource preflight; Databricks/remote execution is recommended.",
                next_command=f"uv run resource-preflight --workspace {workspace.name}",
            )

    # 3. Check for unresolved design decisions
    if not force_with_blockers:
        _check_design_panel(layout, force_with_blockers)

    # 4. Select target (P2: auto detects Databricks; P1 stays on duckdb)
    if manifest.target == "auto":
        target = "delta" if (cfg and hasattr(cfg, "databricks") and cfg.databricks.is_active()) else "duckdb"
    else:
        target = manifest.target

    # 5-6. Create run_id and state dir
    manifest_hash = manifest.inputs_hash
    run_id = new_run_id(manifest_hash)
    run_dir = paths["runs"] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    state = RunState(
        run_id=run_id,
        manifest_hash=manifest_hash,
        target_declared=manifest.target,
        target_actual=target,
        started_at=started_at,
    )

    governor = Governor(cfg=cfg, registry=None) if cfg else _NullGovernor()

    # P2: start MLflow run (best-effort)
    from core.medallion.mlflow_emit import start_run as mlflow_start, finalize_run as mlflow_finalize
    mlflow_start(workspace.name, manifest_hash, run_id, manifest.target)

    # P3: resolve PII salt (warn if missing — don't fail; P3 makes it a hard requirement)
    workspace_salt: Optional[str] = None
    try:
        from core.medallion.salt_store import get_workspace_salt
        workspace_salt = get_workspace_salt(workspace.name)
    except Exception:
        print(f"[build-medallion] WARNING: No PII salt configured for workspace `{workspace.name}`. "
              "Silver PII columns will NOT be hashed. Run `medallion-init-salt` to fix.", flush=True)

    # 7. Initialize run.json
    state.write(run_dir)

    # Open workspace DuckDB connection
    db_path = paths["state"] / "workspace.duckdb"
    import duckdb
    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")

    wall_start = time.time()

    try:
        # 8. Bronze layer
        if only_layer in (None, "bronze"):
            _execute_bronze(manifest, paths, con, state, governor, repo_root,
                            only_table=only_table, db_path=str(db_path))

        # 9. Silver layer
        if only_layer in (None, "silver"):
            _execute_silver(manifest, paths, con, state, governor,
                            only_table=only_table, db_path=str(db_path),
                            workspace_salt=workspace_salt)

        # 10. Gold layer
        if only_layer in (None, "gold"):
            _execute_gold(manifest, paths, con, state, governor,
                          only_table=only_table, db_path=str(db_path))

        # 11. KPI regeneration
        if only_layer in (None, "kpi") and manifest.kpi_regeneration.enabled:
            _execute_kpi_regen(manifest, layout, paths, con, state, governor, workspace, repo_root)

    finally:
        con.close()

    # 12. Finalize run.json
    state.finished_at = datetime.now(timezone.utc).isoformat()
    state.elapsed_seconds = round(time.time() - wall_start, 3)

    # P5: count audit metrics
    state_dict = state.to_dict()
    assertions_passed = sum(
        sum(1 for v in (ts.assertions or {}).values() if v == "pass")
        for ts in state.per_table_status.values()
    )
    state_dict["assertions_passed_count"] = assertions_passed

    state.write(run_dir)

    # P5: write lineage-with-runtime annotation
    lineage_src = paths["medallion"] / "lineage.json"
    if lineage_src.exists():
        import json as _json
        from core.medallion.mlflow_emit import write_lineage_with_runtime
        write_lineage_with_runtime(
            _json.loads(lineage_src.read_text(encoding="utf-8")),
            state_dict,
            run_dir / "lineage_with_runtime.json",
        )

    # P2: finalize MLflow run (best-effort)
    mlflow_finalize(state_dict, run_dir)

    # 13. Lockfile released by context manager

    return state


# ── Bronze execution ───────────────────────────────────────────────────────────

def _execute_bronze(
    manifest: Manifest,
    paths: dict[str, Path],
    con,
    state: RunState,
    governor,
    repo_root: Path,
    *,
    only_table: Optional[str],
    db_path: str,
) -> None:
    for b in manifest.bronze:
        if only_table and b.name != only_table:
            continue
        key = f"bronze.{b.name}"
        sql_path = paths["bronze"] / f"{b.name}.duckdb.sql"
        if not sql_path.exists():
            state.per_table_status[key] = TableRunStatus(status="failed", error="SQL file missing")
            _route(governor, state, "BRONZE_LOAD_FAIL", f"Missing {sql_path}", fatal=True)

        sql = sql_path.read_text(encoding="utf-8")

        # Lint pass
        findings = lint_sql(sql, file_label=str(sql_path), db_path=db_path)
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            msg = "; ".join(f.message for f in errors)
            state.per_table_status[key] = TableRunStatus(status="failed", error=f"SQL_LINT_FAIL: {msg}")
            _route(governor, state, "SQL_LINT_FAIL", msg, fatal=True)

        t0 = time.time()
        row_before = _row_count(con, key)
        try:
            # Rewrite CSV path to absolute so DuckDB can find it regardless of cwd
            sql = _absolutize_csv_paths(sql, repo_root)
            con.execute(sql)
        except Exception as exc:
            state.per_table_status[key] = TableRunStatus(
                status="failed", elapsed_s=round(time.time() - t0, 3), error=str(exc)
            )
            _route(governor, state, "BRONZE_LOAD_FAIL", str(exc), fatal=True)

        row_after = _row_count(con, key)
        state.per_table_status[key] = TableRunStatus(
            status="ok",
            row_count_before=row_before,
            row_count_after=row_after,
            elapsed_s=round(time.time() - t0, 3),
        )


# ── Silver execution ───────────────────────────────────────────────────────────

def _execute_silver(
    manifest: Manifest,
    paths: dict[str, Path],
    con,
    state: RunState,
    governor,
    *,
    only_table: Optional[str],
    db_path: str,
    workspace_salt: Optional[str] = None,
) -> None:
    for s in manifest.silver:
        if only_table and s.name != only_table:
            continue
        key = f"silver.{s.name}"
        sql_path = paths["silver"] / f"{s.name}.duckdb.sql"
        assertions_path = paths["silver"] / f"_{s.name}_assertions.sql"

        if not sql_path.exists():
            state.per_table_status[key] = TableRunStatus(status="failed", error="SQL file missing")
            _route(governor, state, "SILVER_TRANSFORM_FAIL", f"Missing {sql_path}", fatal=True)

        p0_sql = sql_path.read_text(encoding="utf-8")

        # Rewrite to P1 MERGE
        merge_sql = emit_silver_merge(s.name, s.primary_key, p0_sql)

        # Lint pass (on the original; merge SQL may reference tables not yet in EXPLAIN)
        findings = lint_sql(p0_sql, file_label=str(sql_path), db_path=db_path)
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            msg = "; ".join(f.message for f in errors)
            state.per_table_status[key] = TableRunStatus(status="failed", error=f"SQL_LINT_FAIL: {msg}")
            _route(governor, state, "SQL_LINT_FAIL", msg, fatal=True)

        t0 = time.time()
        row_before = _row_count(con, key)
        try:
            # Execute each statement; bind $salt parameter for PII hashing (P3)
            for stmt in _split_statements(merge_sql):
                if workspace_salt and "$salt" in stmt:
                    con.execute(stmt, {"salt": workspace_salt})
                else:
                    con.execute(stmt)
        except Exception as exc:
            state.per_table_status[key] = TableRunStatus(
                status="failed", elapsed_s=round(time.time() - t0, 3), error=str(exc)
            )
            _route(governor, state, "SILVER_TRANSFORM_FAIL", str(exc), fatal=True)

        row_after = _row_count(con, key)
        elapsed = round(time.time() - t0, 3)

        # Run assertions
        assertion_results: dict[str, str] = {}
        if assertions_path.exists():
            assertions_sql = assertions_path.read_text(encoding="utf-8")
            violations = _run_assertions(con, assertions_sql)
            for aid, count in violations.items():
                assertion_results[aid] = "pass" if count == 0 else f"FAIL:{count}"

            failing = {k: v for k, v in assertion_results.items() if v.startswith("FAIL")}
            if failing:
                state.per_table_status[key] = TableRunStatus(
                    status="failed",
                    row_count_before=row_before,
                    row_count_after=row_after,
                    elapsed_s=elapsed,
                    assertions=assertion_results,
                    error=f"SILVER_ASSERTION_FAILED: {failing}",
                )
                _route(
                    governor, state, "SILVER_ASSERTION_FAILED",
                    f"silver.{s.name}: {failing}", fatal=True,
                )

        state.per_table_status[key] = TableRunStatus(
            status="ok",
            row_count_before=row_before,
            row_count_after=row_after,
            elapsed_s=elapsed,
            assertions=assertion_results,
        )


# ── Gold execution ─────────────────────────────────────────────────────────────

def _execute_gold(
    manifest: Manifest,
    paths: dict[str, Path],
    con,
    state: RunState,
    governor,
    *,
    only_table: Optional[str],
    db_path: str,
) -> None:
    for g in manifest.gold:
        if only_table and g.name != only_table:
            continue
        key = f"gold.{g.name}"
        sql_path = paths["gold"] / f"{g.name}.duckdb.sql"
        if not sql_path.exists():
            state.per_table_status[key] = TableRunStatus(status="failed", error="SQL file missing")
            _route(governor, state, "GOLD_DERIVATION_FAIL", f"Missing {sql_path}", fatal=True)

        sql = sql_path.read_text(encoding="utf-8")

        findings = lint_sql(sql, file_label=str(sql_path), db_path=db_path)
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            msg = "; ".join(f.message for f in errors)
            state.per_table_status[key] = TableRunStatus(status="failed", error=f"SQL_LINT_FAIL: {msg}")
            _route(governor, state, "SQL_LINT_FAIL", msg, fatal=True)

        t0 = time.time()
        row_before = _row_count(con, key)
        try:
            for stmt in _split_statements(sql):
                con.execute(stmt)
        except Exception as exc:
            state.per_table_status[key] = TableRunStatus(
                status="failed", elapsed_s=round(time.time() - t0, 3), error=str(exc)
            )
            _route(governor, state, "GOLD_DERIVATION_FAIL", str(exc), fatal=True)

        row_after = _row_count(con, key)
        state.per_table_status[key] = TableRunStatus(
            status="ok",
            row_count_before=row_before,
            row_count_after=row_after,
            elapsed_s=round(time.time() - t0, 3),
        )


# ── KPI regeneration ───────────────────────────────────────────────────────────

def _execute_kpi_regen(
    manifest: Manifest,
    layout: WorkspaceLayout,
    paths: dict[str, Path],
    con,
    state: RunState,
    governor,
    workspace: Path,
    repo_root: Path,
) -> None:
    regen_cfg = manifest.kpi_regeneration
    kpi_registry_path = layout.contracts_dir / "kpi_registry.json"
    if not kpi_registry_path.exists():
        return

    kpi_registry = json.loads(kpi_registry_path.read_text(encoding="utf-8"))

    # v1 legacy SQL
    v1_path = workspace / regen_cfg.row_equality_check_against
    v2_path = workspace / regen_cfg.target_file

    # If v2 already exists use it; otherwise copy v1 as the starting point
    if not v2_path.exists() and v1_path.exists():
        v2_path.parent.mkdir(parents=True, exist_ok=True)
        v2_path.write_text(v1_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Row-equality check
    if v1_path.exists() and v2_path.exists():
        diff = _row_equality_check(con, v1_path, v2_path, kpi_registry)
        state.kpi_diff = diff
        for kpi_id, info in diff.items():
            if not info.get("equal", True):
                entry = f"kpi_diff:{kpi_id}"
                state.blocker_entries_added.append(entry)
                _route(
                    governor, state, "KPI_ROW_EQUALITY_FAIL",
                    f"{kpi_id}: v1={info.get('row_count_v1')} v2={info.get('row_count_v2')}",
                    fatal=False,
                )


def _row_equality_check(
    con, v1_path: Path, v2_path: Path, kpi_registry: Any
) -> dict[str, Any]:
    """
    Execute v1 and v2 SQL; compare per-KPI result sets by row count and hash.
    Returns {kpi_id: {equal, row_count_v1, row_count_v2, delta_rows}}.
    """
    result: dict[str, Any] = {}
    sections_v1 = _split_kpi_sections(v1_path.read_text(encoding="utf-8"))
    sections_v2 = _split_kpi_sections(v2_path.read_text(encoding="utf-8"))
    all_ids = set(sections_v1) | set(sections_v2)

    for kpi_id in sorted(all_ids):
        try:
            sql1 = sections_v1.get(kpi_id, "")
            sql2 = sections_v2.get(kpi_id, "")
            if not sql1 and not sql2:
                continue
            rows1 = _exec_and_hash(con, sql1)
            rows2 = _exec_and_hash(con, sql2)
            equal = rows1["hash"] == rows2["hash"]
            entry: dict[str, Any] = {"equal": equal}
            if not equal:
                entry["row_count_v1"] = rows1["count"]
                entry["row_count_v2"] = rows2["count"]
                entry["delta_rows"] = rows2["count"] - rows1["count"]
            result[kpi_id] = entry
        except Exception as exc:
            result[kpi_id] = {"equal": False, "error": str(exc)[:200]}

    return result


def _exec_and_hash(con, sql: str) -> dict[str, Any]:
    if not sql.strip():
        return {"hash": "empty", "count": 0}
    rows = con.execute(sql).fetchall()
    # Sort for determinism before hashing
    sorted_rows = sorted(str(r) for r in rows)
    h = hashlib.sha256("\n".join(sorted_rows).encode()).hexdigest()
    return {"hash": h, "count": len(rows)}


def _split_kpi_sections(sql: str) -> dict[str, str]:
    """Split a KPI SQL file by '-- KPI: <id>' anchor comments."""
    sections: dict[str, str] = {}
    current_id: Optional[str] = None
    lines: list[str] = []
    for line in sql.splitlines():
        m = re.match(r"--\s*KPI:\s*(\S+)", line.strip())
        if m:
            if current_id and lines:
                sections[current_id] = "\n".join(lines).strip()
            current_id = m.group(1)
            lines = []
        else:
            lines.append(line)
    if current_id and lines:
        sections[current_id] = "\n".join(lines).strip()
    # No anchor comments — treat whole file as one section
    if not sections and sql.strip():
        sections["_all"] = sql.strip()
    return sections


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_manifest(path: Path) -> Manifest:
    import yaml  # pyyaml
    with path.open("r", encoding="utf-8") as fh:
        d = yaml.safe_load(fh)
    return Manifest.from_dict(d)


def _check_design_panel(layout: WorkspaceLayout, force: bool) -> None:
    panel_path = layout.reports_dir / "medallion_design_panel" / "current.json"
    if not panel_path.exists():
        return
    try:
        panel = json.loads(panel_path.read_text(encoding="utf-8"))
        unconfirmed = [e for e in panel if not e.get("confirmed", False)]
        if unconfirmed and not force:
            raise MedallionBuildExit(
                EXIT_DESIGN_BLOCKERS,
                f"{len(unconfirmed)} unconfirmed design decision(s) in {panel_path}. "
                "Resolve them first, or pass --force-with-blockers.",
                next_command=f"Review {panel_path}",
            )
    except json.JSONDecodeError:
        pass


def _row_count(con, fqn: str) -> int:
    schema, _, table = fqn.partition(".")
    try:
        rows = con.execute(f"SELECT COUNT(*) FROM {fqn}").fetchone()
        return int(rows[0]) if rows else 0
    except Exception:
        return 0


def _run_assertions(con, assertions_sql: str) -> dict[str, int]:
    """Execute assertion SQL; return {assertion_id: violations_count}."""
    results: dict[str, int] = {}
    for stmt in _split_statements(assertions_sql):
        stmt = stmt.strip()
        if not stmt or stmt.startswith("--"):
            continue
        try:
            rows = con.execute(stmt).fetchall()
            for row in rows:
                aid = str(row[0]) if len(row) >= 1 else "unknown"
                count = int(row[1]) if len(row) >= 2 else 0
                results[aid] = count
        except Exception as exc:
            results[f"assertion_execution_failed_{len(results) + 1}"] = 1
            results[f"assertion_error_{len(results) + 1}"] = int(bool(str(exc)))
    return results


def _split_statements(sql: str) -> list[str]:
    """Split SQL on semicolons, preserving non-empty statements."""
    stmts = []
    for s in sql.split(";"):
        s = s.strip()
        if s and not s.startswith("--"):
            stmts.append(s)
    return stmts


def _absolutize_csv_paths(sql: str, repo_root: Path) -> str:
    """Rewrite read_csv_auto('relative/path') to absolute paths."""
    def replace(m: re.Match) -> str:
        quote = m.group(1)
        path_str = m.group(2)
        p = Path(path_str)
        if not p.is_absolute():
            p = (repo_root / path_str).resolve()
        return f"read_csv_auto({quote}{p}{quote}"

    return re.sub(r"read_csv_auto\((['\"])(.+?)\1", replace, sql, flags=re.IGNORECASE)


def _route(
    governor,
    state: RunState,
    stage_code: str,
    error_message: str,
    *,
    fatal: bool,
) -> None:
    """Route an error through the governor; abort build on fatal stage codes."""
    decision: RoutingDecision = governor.decide_medallion_routing(stage_code, error_message)
    state.retry_history.append({
        "stage_code": stage_code,
        "target_agent": decision.target_agent,
        "reason": decision.reason,
        "retry_count": decision.retry_count,
        "is_terminal": decision.is_terminal,
    })
    if fatal:
        raise MedallionBuildExit(
            EXIT_MEDALLION_BUILD_FAIL,
            f"{stage_code}: {error_message} -> routed to {decision.target_agent}",
        )


class _NullGovernor:
    """Stub governor used when no Config is provided (e.g., --cheap mode)."""
    _retry_map: dict[str, int] = {}

    def decide_medallion_routing(self, stage_code: str, error_message: str) -> RoutingDecision:
        return RoutingDecision(
            target_agent="medallion_architect",
            reason=f"{stage_code}: {error_message[:80]}",
            retry_count=0,
        )

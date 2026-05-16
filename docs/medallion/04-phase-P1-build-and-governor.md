# 04 — Phase P1: Build + Governor

## Goal

After P1:

- `uv run build-medallion --workspace <ws>` executes the full Bronze → Silver → Gold pipeline on the DuckDB target.
- Silver MERGE-on-PK and Gold full-refresh semantics work correctly.
- Post-Silver assertions run on every Silver table and fail loudly on violation.
- Governor routes Medallion errors back to the correct specialist with retry caps.
- The SQL Specialist regenerates KPI SQL against the Gold schema as the final build step.
- A row-equality check compares the new `kpi_metrics_v2.sql` against the legacy `kpi_metrics.sql` and surfaces diffs as blocker entries.
- SQL lint (parse + plan + hotspot) runs before any SQL is committed to disk.

## Prerequisites

- P0 shipped (design pass works).
- The design pass produced zero unconfirmed decisions, OR the design panel was resolved and `design-medallion` was re-run, OR the user passed `--force-with-blockers` (off by default — see §6).

## Requirements (must-haves)

1. **`uv run build-medallion` CLI** with the same `--help` discipline as `design-medallion`.
2. **DuckDB execution** of every Bronze, Silver, Gold SQL file in the manifest, in dependency order.
3. **Silver MERGE semantics**: rewrite Silver SQL from P0's `CREATE OR REPLACE` to true MERGE-on-PK using DuckDB's `INSERT INTO ... ON CONFLICT` (or its current equivalent).
4. **Post-load assertions**: every Silver table runs its `_<table>_assertions.sql` after load; non-zero `violations` rows fail the table.
5. **Governor extension**: `MEDALLION_ROUTING` dict in `core/orchestration/governor.py`; new `Governor.decide_medallion_routing(stage, error)` method.
6. **KPI SQL regeneration**: at end of successful Gold load, SQL Specialist regenerates KPI SQL against Gold tables.
7. **Row-equality check**: compare `kpi_metrics_v2.sql` output to `kpi_metrics.sql` output row-by-row for each KPI; surface mismatches as `KPI_ROW_EQUALITY_FAIL` blocker entries.
8. **SQL lint pass**: parse-validation (sqlglot) and plan-validation (`EXPLAIN`) on DuckDB target; hotspot pass via `tools/optimizer_finder.py`. Failures route as `SQL_LINT_FAIL`.
9. **Per-run state directory**: `state/medallion/runs/<run_id>/run.json` with per-table status, row counts, timings, assertion results.
10. **Concurrency lockfile**: `state/medallion/.lock`; second concurrent `build-medallion` exits `WORKSPACE_BUSY`.

## Architecture for this phase

### Module additions

```
core/medallion/
├── build.py          # NEW — build-medallion orchestrator
├── build_cli.py      # NEW — argparse entrypoint
├── sql_lint.py       # NEW — parse + plan + hotspot lint
├── merge_emitter.py  # NEW — Silver MERGE SQL emitter (replaces P0 CREATE OR REPLACE in silver/*.duckdb.sql)
└── run_state.py      # NEW — per-run state dir reader/writer
```

### Governor extension

```python
# core/orchestration/governor.py

MEDALLION_ROUTING = {
    "BRONZE_LOAD_FAIL":         ("data_engineer",       2),
    "SILVER_TRANSFORM_FAIL":    ("sql_specialist",      2),
    "SILVER_ASSERTION_FAILED":  ("medallion_architect", 2),
    "GOLD_DERIVATION_FAIL":     ("medallion_architect", 2),
    "KPI_ROW_EQUALITY_FAIL":    ("medallion_architect", 1),
    "SQL_LINT_FAIL":            ("sql_specialist",      2),
}

class Governor:
    def decide_medallion_routing(self, stage_code: str, error_message: str) -> RoutingDecision:
        specialist, cap = MEDALLION_ROUTING.get(stage_code, ("medallion_architect", 1))
        retries = self._retry_map.get(stage_code, 0)
        if retries >= cap:
            return RoutingDecision(
                target_agent="human",
                reason=f"{stage_code} exceeded retry cap ({cap})",
                retry_count=retries,
                is_terminal=True,
            )
        self._retry_map[stage_code] = retries + 1
        return RoutingDecision(
            target_agent=specialist,
            reason=f"{stage_code}: routing to {specialist}",
            retry_count=retries + 1,
        )
```

The existing `decide_routing(kpi_id, error_message)` is kept unchanged (used by KPI generation). Medallion code path calls the new method.

### Build pass flow

```
build_medallion(workspace, repo_root, *, cfg, registry, only_layer=None, only_table=None, resume=None):
    1. Acquire lockfile state/medallion/.lock (fail WORKSPACE_BUSY if held)
    2. Re-validate manifest via WorkspaceArtifactValidator (fail on error)
    3. Check design panel — refuse if unconfirmed decisions remain (override --force-with-blockers)
    4. Select ExecutionBackend via core.execution.backend.build_execution_backend(cfg)
    5. Create run_id = timestamp + sha1(manifest_hash)[:8]
    6. Mkdir state/medallion/runs/<run_id>/
    7. Initialize run.json with started_at, target, manifest_hash
    8. For each Bronze table:
         - SQL lint pass; on fail → SQL_LINT_FAIL → Governor
         - Execute via ExecutionBackend
         - Record row_count_before, row_count_after, elapsed
    9. For each Silver table:
         - SQL lint pass
         - Execute (MERGE)
         - Execute _<table>_assertions.sql; non-zero violations → SILVER_ASSERTION_FAILED → Governor
    10. For each Gold table:
         - SQL lint pass
         - Execute (full refresh)
         - Failure → GOLD_DERIVATION_FAIL → Governor
    11. KPI regeneration:
         - Invoke SQLSpecialistIntern.regenerate_against_gold(star_schema, kpi_registry, gold_catalog)
         - Write to interns/generated/solutions/kpi_metrics_v2.sql
         - Execute v2 SQL against Gold; capture results
         - Execute legacy kpi_metrics.sql; capture results
         - Row-equality compare per KPI → mismatches → KPI_ROW_EQUALITY_FAIL → blocker
    12. Finalize run.json (finished_at, per_table_status, assertion_results, kpi_diff_summary)
    13. Release lockfile
    14. Print summary; emit MLflow run only in P2
```

## Implementation steps

### Step 1: Lockfile helper

```python
# core/medallion/run_state.py
from contextlib import contextmanager
from pathlib import Path
import time, json, os, hashlib

class WorkspaceBusy(RuntimeError):
    pass

@contextmanager
def acquire_lock(state_dir: Path, *, stale_after_seconds: int = 3600):
    lock = state_dir / ".lock"
    state_dir.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        if age < stale_after_seconds:
            try:
                holder = json.loads(lock.read_text())
            except Exception:
                holder = {}
            raise WorkspaceBusy(f"Lock held by run_id={holder.get('run_id','?')} pid={holder.get('pid','?')} for {age:.0f}s")
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}, indent=2))
    try:
        yield lock
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass

def new_run_id(manifest_hash: str) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{hashlib.sha1(manifest_hash.encode()).hexdigest()[:8]}"
```

### Step 2: SQL lint module

```python
# core/medallion/sql_lint.py
from dataclasses import dataclass
from pathlib import Path

@dataclass
class LintFinding:
    file: str
    severity: str       # "error" | "warning"
    rule: str           # "parse_failed" | "no_cartesian_join" | "partition_filter_present" | "hotspot"
    message: str

def lint_duckdb_sql(sql: str, *, file_label: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    # 1. Parse
    try:
        import duckdb
        con = duckdb.connect(":memory:")
        con.execute("PRAGMA enable_object_cache;")
        # PARSE-only by wrapping in EXPLAIN
        con.execute(f"EXPLAIN {sql}")
    except Exception as exc:
        findings.append(LintFinding(file_label, "error", "parse_failed", str(exc)))
        return findings
    # 2. Plan-property checks
    plan = con.execute(f"EXPLAIN {sql}").fetchall()
    plan_text = "\n".join(str(r) for r in plan).lower()
    if "cross_product" in plan_text or " cartesian" in plan_text:
        findings.append(LintFinding(file_label, "error", "no_cartesian_join", "Plan contains cartesian product"))
    # ... additional plan rules
    return findings

def lint_spark_sql(sql: str, *, file_label: str) -> list[LintFinding]:
    import sqlglot
    findings: list[LintFinding] = []
    try:
        sqlglot.parse_one(sql, read="spark")
    except sqlglot.errors.ParseError as exc:
        findings.append(LintFinding(file_label, "error", "parse_failed", str(exc)))
    return findings

def hotspot_check(sql: str, *, file_label: str) -> list[LintFinding]:
    """Delegates to tools/optimizer_finder.py per MEMORY.md."""
    from tools.optimizer_finder import find_hotspots  # adjust import to actual API
    hotspots = find_hotspots(sql, dialect="duckdb")
    return [LintFinding(file_label, "warning", "hotspot", str(h)) for h in hotspots]
```

The exact `tools/optimizer_finder.py` API needs verification at implementation time — adapt as needed.

### Step 3: Silver MERGE rewrite

Replace the P0 `CREATE OR REPLACE TABLE silver.X AS SELECT ...` with a true MERGE:

```sql
-- silver/patient.duckdb.sql (P1)
CREATE TABLE IF NOT EXISTS silver.patient (
    source_system VARCHAR,
    patient_id VARCHAR,
    ...
    age_at_service INTEGER,
    _silver_load_ts TIMESTAMP,
    PRIMARY KEY (source_system, patient_id)
);

INSERT INTO silver.patient
SELECT
    source_system,
    patient_id,
    ...
    date_diff('year', DOB, ServiceDate) AS age_at_service,
    current_timestamp AS _silver_load_ts
FROM (
    SELECT *, 'hospital_a' AS source_system FROM bronze.patient__hospital_a
    UNION ALL
    SELECT *, 'hospital_b' AS source_system FROM bronze.patient__hospital_b
) unioned
ON CONFLICT (source_system, patient_id) DO UPDATE SET
    age_at_service = EXCLUDED.age_at_service,
    ...
    _silver_load_ts = EXCLUDED._silver_load_ts;
```

For Spark target (P2), the equivalent is `MERGE INTO silver.patient USING (...) ON ... WHEN MATCHED THEN UPDATE SET ... WHEN NOT MATCHED THEN INSERT (...)`.

### Step 4: build-medallion CLI

Follow the `design_cli.py` pattern. New flags:

- `--target {duckdb,delta,auto}` — override manifest target (P2 makes this real).
- `--only-layer {bronze,silver,gold,kpi}` — restrict to one phase.
- `--only-table <name>` — restrict to one table within a layer.
- `--resume <run_id>` — pick up a prior partial run from `state/medallion/runs/<run_id>/`.
- `--force-with-blockers` — proceed even if design panel has unresolved entries (use carefully; logs a warning).

Exit codes added in P1: `MEDALLION_BUILD_FAIL`, `WORKSPACE_BUSY`, `SQL_LINT_FAIL` (now reachable from build).

### Step 5: KPI SQL regeneration

```python
# core/medallion/kpi_regeneration.py
def regenerate_kpi_sql(workspace: Path, manifest: Manifest, star_schema: StarSchema, kpi_registry: dict, sql_specialist) -> Path:
    gold_catalog = {g.name: g.derived_from for g in manifest.gold}
    context = {
        "star_schema": star_schema.to_dict(),
        "kpi_registry": kpi_registry,
        "gold_catalog": gold_catalog,
        "target_dialect": manifest.target,
    }
    response = sql_specialist.run(
        "Regenerate KPI SQL against the new Gold star schema.",
        context,
    )
    target_path = workspace / "interns" / "generated" / "solutions" / "kpi_metrics_v2.sql"
    target_path.write_text(response, encoding="utf-8")
    return target_path

def row_equality_check(v1_path: Path, v2_path: Path, backend) -> dict:
    """
    Execute both files; for each KPI section, hash the result set.
    Return {kpi_id: {"equal": bool, "v1_hash": str, "v2_hash": str, "row_count_v1": int, "row_count_v2": int}}.
    """
    ...
```

Splitting v1 into per-KPI sections is best done by an anchor comment convention the existing `kpi_sql_generator.py` already emits (`-- KPI: kpi_001`). If absent, treat the whole file as one comparison.

### Step 6: Per-run state file

```json
// state/medallion/runs/<run_id>/run.json
{
  "run_id": "20260516T103045Z-7bcda0fa",
  "manifest_hash": "sha256:7bcda0fa1a937b89...",
  "target_declared": "duckdb",
  "target_actual": "duckdb",
  "started_at": "2026-05-16T10:30:45Z",
  "finished_at": "2026-05-16T10:31:02Z",
  "elapsed_seconds": 17.0,
  "degraded_run": false,
  "per_table_status": {
    "bronze.patient__hospital_a": {"status": "ok", "row_count_before": 0, "row_count_after": 5000, "elapsed_s": 0.4},
    "silver.patient": {"status": "ok", "row_count_before": 0, "row_count_after": 10000, "elapsed_s": 1.2, "assertions": {"pk_unique": "pass", "no_null_pk": "pass"}},
    "gold.fact_claim": {"status": "ok", "row_count_before": 0, "row_count_after": 20000, "elapsed_s": 2.1}
  },
  "kpi_diff": {
    "kpi_001": {"equal": true},
    "kpi_002": {"equal": false, "row_count_v1": 4, "row_count_v2": 6, "delta_rows": 2}
  },
  "retry_history": [],
  "blocker_entries_added": ["kpi_diff:kpi_002"]
}
```

## Integration points

| Existing surface | What you touch | How |
|---|---|---|
| `core.orchestration.governor.Governor` | Add `MEDALLION_ROUTING` dict, `decide_medallion_routing` method | Strict addition; do not modify the existing KPI routing logic |
| `core.execution.backend.build_execution_backend` | Call as-is; pass result to build orchestrator | No backend code changes in P1 |
| `interns.sql_specialist.SQLSpecialistIntern` | Add a `regenerate_against_gold` entry path; OR extend `run()` to handle the new task class via a `task_type` context key | Pick one; document in the SQL Specialist's docstring |
| `core.onboarding.blocker_question_panel.BlockerQuestionPanelBuilder` | Add a new source `"medallion_build"`; add entries from `kpi_diff.<kpi>.equal=false` | Re-use existing panel; do not fork |
| `core.medallion.design.medallion_dirs` | Read paths; do not change | Single source of truth for medallion directory layout |

## Testing

### Unit tests

```
tests/medallion/
├── test_lockfile.py            # acquire/release; busy detection; stale lock handling
├── test_sql_lint.py            # parse fail; cartesian; hotspot
├── test_merge_emitter.py       # P0 CREATE OR REPLACE -> P1 MERGE rewrite produces correct DuckDB SQL
├── test_row_equality.py        # equal-rows / row-count-diff / value-diff
├── test_governor_routing.py    # each stage code -> correct specialist; cap-exceeded behavior
└── test_run_state.py           # serialize/deserialize round trip
```

### Integration tests

```
tests/medallion/integration/
├── test_build_e2e_duckdb.py    # full pipeline on a tiny fixture workspace
└── test_resume.py              # simulate partial failure + --resume
```

Use a small fixture workspace under `tests/fixtures/medallion-workspace/` with 2 source CSVs (multi-source), 2 KPIs, and a confirmed derived feature review. This makes the e2e test fast and deterministic.

### Acceptance criteria

The phase is done when, on `tests/fixtures/medallion-workspace/` and on the Healthcare RCM workspace:

1. `uv run design-medallion ...` followed by `uv run build-medallion ...` completes with `degraded_run=false` and `per_table_status[*].status == "ok"` for every table.
2. Modifying a Silver SQL to introduce a `not_null` violation causes the build to fail with `SILVER_ASSERTION_FAILED` routed to `medallion_architect` in `run.json:retry_history`.
3. Modifying `kpi_metrics_v2.sql` to produce different rows causes the build to surface a `kpi_diff:<kpi>` blocker entry (build itself still succeeds; the diff is a blocker, not a fatal).
4. Modifying a Bronze SQL to add an unconditional `CROSS JOIN` is rejected by SQL lint before commit (`SQL_LINT_FAIL`).
5. Running two `build-medallion` commands in parallel: the second exits `WORKSPACE_BUSY`.
6. After a successful build, `interns/state/medallion/runs/<run_id>/run.json` exists and is well-formed JSON conforming to the schema in §Step 6 above.
7. Total elapsed time on the Healthcare RCM workspace is under 60 seconds end-to-end with `--cheap`.

## Risks

| Risk | Mitigation |
|---|---|
| MERGE semantics differ subtly between DuckDB and target dialects | P1 stays on DuckDB; P2 explicitly tests the Spark MERGE equivalent against the same fixture |
| `tools/optimizer_finder.py` API may have changed since the memory note | At implementation start, read the file; adapt the lint integration; if the API is unstable, vendor a frozen helper in `sql_lint.py` |
| SQL Specialist may not handle "regenerate against new schema" cleanly | Add `task_type: "kpi_regeneration"` to context; provide a dedicated system prompt section |
| Per-KPI section splitting in `kpi_metrics.sql` is fragile | If anchor comments are absent, fall back to single-section comparison; surface a warning recommending an upgrade to `kpi_sql_generator.py` to emit anchors |
| Concurrent run lockfile race on Windows | Use atomic-write pattern (`open(lock, "x")` to claim) instead of check-then-write |
| KPI v1 and v2 may produce different ordering on equal data | Sort by KPI's natural ordering before hashing; document the comparator |

## Definition of Done

- [ ] `core/medallion/{build.py, build_cli.py, sql_lint.py, merge_emitter.py, run_state.py}` exist.
- [ ] `core/medallion/kpi_regeneration.py` exists.
- [ ] `core/orchestration/governor.py` has `MEDALLION_ROUTING` and `decide_medallion_routing`.
- [ ] `pyproject.toml` registers `build-medallion`.
- [ ] `uv run build-medallion --help` shows the full epilog (examples + exit codes).
- [ ] All seven acceptance criteria above pass.
- [ ] Unit and integration tests in `tests/medallion/` pass.
- [ ] `docs/medallion/04-phase-P1-build-and-governor.md` is updated to reflect any deviations from this plan.

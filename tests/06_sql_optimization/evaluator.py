"""
Evaluator for SQL optimization task.

Full pipeline:
  0. Run tools/optimizer_finder.py (EXPLAIN ANALYZE → hotspots.json, informational)
  1. Read execution time from analytics.duckdb
  2. Export optimized_features -> parquet
  3. Export healthcare_rules -> methodology JSON
  4. Run tools/profiler.py (Polars engine, 100% sample, methodology validation)
  5. Read matching_score from profiler output
  6. Compute primary_metric = accuracy_score + time_score

Scoring (max 10.0):
  accuracy_score = (matching_score / 100) * 5.0
  time_score     = max(0, 10 - exec_time) / 10 * 5.0   (only if matching_score == 100%)
"""
import duckdb
import json
import subprocess
import sys
from pathlib import Path

DB             = "workspaces/sql_optimization/analytics.duckdb"
SQL_TARGET     = "workspaces/sql_optimization/prescriber_features.sql"
OPTIMIZER      = "tools/optimizer_finder.py"
HOTSPOTS_OUT   = "workspaces/sql_optimization/hotspots.json"
PROFILER       = "tools/profiler.py"
PARQUET_EXPORT = "state/optimized_features_eval.parquet"
METHODOLOGY    = "state/healthcare_rules.json"
PROFILE_DIR    = "state/experiment_profile"


def _export_methodology(conn: duckdb.DuckDBPyConnection) -> bool:
    try:
        row = conn.execute(
            "SELECT columns, relationships FROM healthcare_rules"
        ).fetchone()
        if not row:
            return False
        schema = {"columns": row[0], "relationships": row[1] or []}
        Path(METHODOLOGY).parent.mkdir(parents=True, exist_ok=True)
        with open(METHODOLOGY, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2)
        return True
    except Exception as exc:
        print(f"[evaluator] methodology export failed: {exc}")
        return False


def _run_optimizer() -> None:
    """Step 0: EXPLAIN ANALYZE → hotspots.json. Informational only; never raises."""
    print("Running optimizer_finder for SQL hotspot analysis...")
    try:
        subprocess.run(
            [
                sys.executable, OPTIMIZER,
                "--target",  SQL_TARGET,
                "--db",      DB,
                "--out",     HOTSPOTS_OUT,
                "--timeout", "20",
            ],
            timeout=30,
        )
    except Exception as exc:
        print(f"[optimizer_finder] skipped: {exc}")


def evaluate():
    # ── 0. Hotspot profiling (informational, no score impact) ──────────────────
    _run_optimizer()

    conn = duckdb.connect(DB)

    # ── 1. Execution time ──────────────────────────────────────────────────────
    try:
        row = conn.execute(
            "SELECT execution_time_seconds, success FROM sql_execution_time"
        ).fetchone()
        exec_time, sql_success = (float(row[0]), bool(row[1])) if row else (100.0, False)
    except Exception:
        exec_time, sql_success = 100.0, False

    matching_score = 0.0

    if sql_success:
        # ── 2. Export optimized_features -> parquet ────────────────────────────
        print("Exporting optimized_features to parquet...")
        Path(PARQUET_EXPORT).parent.mkdir(parents=True, exist_ok=True)
        conn.execute(f"COPY optimized_features TO '{PARQUET_EXPORT}' (FORMAT PARQUET)")

        # ── 3. Export healthcare_rules -> methodology JSON ─────────────────────
        has_methodology = _export_methodology(conn)
        conn.close()

        # ── 4. Run tools/profiler.py ───────────────────────────────────────────
        print("Running profiler for methodology guardrails...")
        cmd = [
            sys.executable, PROFILER,
            "--input",  PARQUET_EXPORT,
            "--pct",    "100",
            "--strat",  "auto",
            "--out",    PROFILE_DIR,
            "--fmt",    "parquet",
        ]
        if has_methodology:
            cmd += ["--methodology", METHODOLOGY]

        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )

        if result.returncode != 0:
            print(f"[profiler] exit {result.returncode}")
            print(result.stderr[:600])
        else:
            # ── 5. Read matching_score ─────────────────────────────────────────
            score_file = Path(PROFILE_DIR) / "matching_score.csv"
            if score_file.exists():
                import csv
                with open(score_file, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        matching_score = float(row["matching_score"])
                        break
                print(f"Profiler matching_score: {matching_score}")
            else:
                print("[evaluator] matching_score.csv not found — defaulting to 0")
    else:
        conn.close()

    # ── 6. Primary metric ──────────────────────────────────────────────────────
    accuracy_score = (matching_score / 100.0) * 5.0
    time_score     = max(0.0, 10.0 - exec_time) / 10.0 * 5.0 if matching_score >= 100.0 else 0.0
    primary_metric = round(accuracy_score + time_score, 4)

    metrics = {
        "primary_metric":          primary_metric,
        "execution_time_seconds":  round(exec_time, 4),
        "matching_score":          matching_score,
    }

    print("\n---")
    for k, v in metrics.items():
        print(f"{k}:    {v}")
    print("---")


if __name__ == "__main__":
    evaluate()

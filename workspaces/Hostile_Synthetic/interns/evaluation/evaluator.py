from __future__ import annotations

from pathlib import Path

import duckdb

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = WORKSPACE_ROOT / "interns" / "state" / "analytics.duckdb"


def main() -> None:
    conn = duckdb.connect(str(DB_PATH))
    row = conn.execute("SELECT execution_time_seconds, success FROM sql_execution_time").fetchone()
    manifest_count = conn.execute("SELECT count(*) FROM kpi_baseline_manifest").fetchone()[0]
    ready_count = conn.execute(
        "SELECT count(*) FROM kpi_baseline_manifest WHERE status = 'ready'"
    ).fetchone()[0]
    conn.close()
    execution_time, success = (float(row[0]), bool(row[1])) if row else (100.0, False)
    matching_score = (
        round((ready_count / manifest_count) * 100.0, 4)
        if success and manifest_count > 0
        else 0.0
    )
    time_score = max(0.0, 10.0 - execution_time) / 10.0 * 5.0 if matching_score == 100.0 else 0.0
    primary_metric = round((matching_score / 100.0) * 5.0 + time_score, 4)
    print("---")
    print(f"primary_metric: {primary_metric}")
    print(f"execution_time_seconds: {execution_time:.4f}")
    print(f"matching_score: {matching_score}")
    print(f"kpi_count: {manifest_count}")
    print(f"ready_kpi_count: {ready_count}")
    print("---")


if __name__ == "__main__":
    main()

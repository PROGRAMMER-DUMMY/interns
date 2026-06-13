from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
INTERNS_ROOT = WORKSPACE_ROOT / "interns"
DB_PATH = INTERNS_ROOT / "state" / "analytics.duckdb"
SQL_FILE = INTERNS_ROOT / "generated" / "solutions" / "kpi_metrics.sql"
RESULT_PATH = INTERNS_ROOT / "runs" / "baseline_result.json"


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH))
    started = time.perf_counter()
    success = False
    error = ""
    try:
        conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        success = True
    except Exception as exc:
        error = str(exc)
    elapsed = time.perf_counter() - started
    conn.execute("DROP TABLE IF EXISTS sql_execution_time")
    conn.execute(
        "CREATE TABLE sql_execution_time AS SELECT ? AS execution_time_seconds, ? AS success, ? AS error",
        [elapsed, success, error],
    )
    conn.close()
    RESULT_PATH.write_text(
        json.dumps(
            {"execution_time_seconds": elapsed, "success": success, "error": error},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"execution_time_seconds: {elapsed:.4f}")
    print(f"success: {success}")
    if error:
        print(f"error: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

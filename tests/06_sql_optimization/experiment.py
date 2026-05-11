import duckdb
import time
import json

DB  = "workspaces/sql_optimization/analytics.duckdb"
SQL = "workspaces/sql_optimization/prescriber_features.sql"

print("Starting SQL optimization experiment...")
start = time.time()
success = False
error_msg = ""

try:
    sql_script = open(SQL, encoding="utf-8").read().rstrip().rstrip(";")
    conn = duckdb.connect(DB)
    conn.execute("DROP TABLE IF EXISTS optimized_features")
    conn.execute(f"CREATE TABLE optimized_features AS (\n{sql_script}\n)")
    row_count = conn.execute("SELECT COUNT(*) FROM optimized_features").fetchone()[0]
    conn.close()
    success = True
    print(f"Produced {row_count:,} rows")
except Exception as e:
    success = False
    error_msg = str(e)

elapsed = time.time() - start

conn = duckdb.connect(DB)
conn.execute("DROP TABLE IF EXISTS sql_execution_time")
conn.execute(
    "CREATE TABLE sql_execution_time AS "
    f"SELECT {elapsed} AS execution_time_seconds, {success} AS success, '{error_msg.replace(chr(39), '')}' AS error"
)
conn.close()

print(f"Executed SQL in {elapsed:.4f}s  (success={success})")
if not success:
    print(f"Error: {error_msg}")
    exit(1)

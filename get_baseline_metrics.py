import duckdb
import argparse
from pathlib import Path

DB_PATH = ":memory:"
DEFAULT_WORKSPACE = "workspaces/Healthcare-RCM-Data-Platform"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the legacy Healthcare RCM KPI baseline.")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    return parser.parse_args()


args = _parse_args()
workspace = args.workspace.rstrip("/\\")
if workspace != DEFAULT_WORKSPACE:
    raise SystemExit(
        "get_baseline_metrics.py is a legacy Healthcare-RCM-Data-Platform baseline only. "
        "For generated KPI SQL, use: uv run run-kpi-execution-harness --workspace <workspace>"
    )

SQL_FILE = str(Path(workspace) / "kpi_metrics.sql")
if not Path(SQL_FILE).exists():
    fallback = Path(workspace) / "interns" / "generated" / "solutions" / "kpi_metrics.sql"
    if fallback.exists():
        SQL_FILE = str(fallback)

conn = duckdb.connect(DB_PATH)
conn.execute("CREATE SCHEMA IF NOT EXISTS silver;")
conn.execute("CREATE SCHEMA IF NOT EXISTS gold;")
conn.execute("CREATE TABLE silver.transactions AS SELECT *, 'hos-a' as datasource, true as is_current, false as is_quarantined FROM read_csv_auto('workspaces/Healthcare-RCM-Data-Platform/datasets/EMR/trendytech-hospital-a/transactions.csv', header=True) UNION ALL SELECT *, 'hos-b' as datasource, true as is_current, false as is_quarantined FROM read_csv_auto('workspaces/Healthcare-RCM-Data-Platform/datasets/EMR/trendytech-hospital-b/transactions.csv', header=True)")
conn.execute("CREATE TABLE gold.dim_patient AS SELECT PatientID as src_patientid, 'hos-a' as datasource, Gender as gender, CAST(DOB AS DATE) as dob FROM read_csv_auto('workspaces/Healthcare-RCM-Data-Platform/datasets/EMR/trendytech-hospital-a/patients.csv', header=True) UNION ALL SELECT ID as src_patientid, 'hos-b' as datasource, Gender as gender, CAST(DOB AS DATE) as dob FROM read_csv_auto('workspaces/Healthcare-RCM-Data-Platform/datasets/EMR/trendytech-hospital-b/patients.csv', header=True)")
conn.execute("CREATE TABLE gold.dim_department AS SELECT concat(DeptID, '-hos-a') as Dept_Id, Name FROM read_csv_auto('workspaces/Healthcare-RCM-Data-Platform/datasets/EMR/trendytech-hospital-a/departments.csv', header=True) UNION ALL SELECT concat(DeptID, '-hos-b') as Dept_Id, Name FROM read_csv_auto('workspaces/Healthcare-RCM-Data-Platform/datasets/EMR/trendytech-hospital-b/departments.csv', header=True)")

conn.execute(open(SQL_FILE, encoding="utf-8").read())

print("KPI 1 Baseline:")
print(conn.execute("SELECT count(*), round(sum(Total_PaidAmount), 2) FROM kpi1_medicare_trend").fetchone())

print("KPI 2 Baseline:")
print(conn.execute("SELECT count(*), round(sum(Pct_Share_Of_Lives), 2) FROM kpi2_lives_percentage").fetchone())

print("KPI 3 Baseline:")
print(conn.execute("SELECT count(*), round(sum(Total_PaidAmount), 2) FROM kpi3_top_commercial_payers").fetchone())

conn.close()

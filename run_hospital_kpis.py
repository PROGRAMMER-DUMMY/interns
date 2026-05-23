import duckdb
import os
import glob
import sys

from core.presentation.console_tables import render_query_result_table

workspace = "workspaces/Hospital_Patient_Records"
solutions_dir = os.path.join(workspace, "interns/generated/solutions")
sql_files = sorted(glob.glob(os.path.join(solutions_dir, "kpi_*.sql")))
sql_files = [path for path in sql_files if os.path.basename(path) != "kpi_metrics.sql"]

conn = duckdb.connect(":memory:")

for sql_file in sql_files:
    kpi_id = os.path.basename(sql_file).replace(".sql", "")
    print(f"\n{'='*50}")
    print(f"Executing {kpi_id}...")
    
    with open(sql_file, "r", encoding="utf-8") as f:
        sql_script = f.read()
        
    try:
        # Split script by semicolon to handle multiple statements if necessary
        # However, DuckDB's execute can often handle multiple statements if they are just views.
        # But some might be complex. Let's try executing the whole thing.
        conn.execute(sql_script)
        
        # We need to find the final result table/view name.
        # Usually it's named something like {kpi_id}_results or features.
        # Let's try to infer it from the script or just list all views.
        views = conn.execute("SELECT table_name FROM information_schema.views WHERE table_name LIKE ?", [f"{kpi_id}%"]).fetchall()
        result_view = f"{kpi_id}_results"
        names = {row[0] for row in views}
        if result_view in names:
            print(f"Results for {result_view}:")
            cursor = conn.execute(f'SELECT * FROM "{result_view}"')
            print(render_query_result_table(cursor, limit=50))
        else:
            print(f"Error executing {kpi_id}: missing exact final result view {result_view}")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error executing {kpi_id}: {e}")
        sys.exit(1)

conn.close()

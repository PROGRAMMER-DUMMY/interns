import duckdb
import json
from pathlib import Path
import shutil

def sanitize_table_name(name):
    # DuckDB table names should not start with numbers and shouldn't have weird characters
    sanitized = name.replace(" ", "_").replace("-", "_").replace(".", "_")
    if sanitized[0].isdigit():
        sanitized = "t_" + sanitized
    return sanitized

def migrate():
    workspace_dir = Path("workspaces/sql_optimization")
    db_path = workspace_dir / "analytics.duckdb"
    
    print(f"Connecting to DuckDB at {db_path}...")
    con = duckdb.connect(str(db_path))
    
    # 1. Migrate root parquet/csv files (avoiding duplicating MUP if both csv and parquet exist)
    files_to_delete = []
    
    # Let's prefer parquet over csv for the large MUP file to save time and space
    mup_parquet = workspace_dir / "MUP_DPR_RY25_P04_V10_DY23_NPI.parquet"
    mup_csv = workspace_dir / "MUP_DPR_RY25_P04_V10_DY23_NPI.csv"
    
    if mup_parquet.exists():
        print(f"Loading {mup_parquet.name}...")
        con.execute(f"CREATE TABLE IF NOT EXISTS mup_data AS SELECT * FROM read_parquet('{mup_parquet}')")
        files_to_delete.append(mup_parquet)
        if mup_csv.exists():
            files_to_delete.append(mup_csv) # mark csv for deletion since we loaded parquet
            
    # Load other parquet files
    for pq_file in workspace_dir.glob("*.parquet"):
        if pq_file == mup_parquet: continue
        t_name = sanitize_table_name(pq_file.stem)
        print(f"Loading {pq_file.name} into table {t_name}...")
        con.execute(f"CREATE TABLE IF NOT EXISTS {t_name} AS SELECT * FROM read_parquet('{pq_file}')")
        files_to_delete.append(pq_file)
        
    # 2. Migrate JSON files
    for json_file in workspace_dir.glob("*.json"):
        t_name = sanitize_table_name(json_file.stem)
        print(f"Loading {json_file.name} into table {t_name}...")
        # DuckDB can read JSON natively
        con.execute(f"CREATE TABLE IF NOT EXISTS {t_name} AS SELECT * FROM read_json_auto('{json_file}')")
        files_to_delete.append(json_file)
        
    # 3. Migrate experiment_profile files
    profile_dir = workspace_dir / "experiment_profile"
    if profile_dir.exists():
        for f in profile_dir.glob("*"):
            if f.suffix not in [".csv", ".parquet"]: continue
            
            t_name = "profile_" + sanitize_table_name(f.stem)
            print(f"Loading {f.name} into table {t_name}...")
            
            if f.suffix == ".csv":
                con.execute(f"CREATE TABLE IF NOT EXISTS {t_name} AS SELECT * FROM read_csv_auto('{f}')")
            elif f.suffix == ".parquet":
                con.execute(f"CREATE TABLE IF NOT EXISTS {t_name} AS SELECT * FROM read_parquet('{f}')")
                
            files_to_delete.append(f)
            
    print("Migration successful. Deleting original files...")
    for f in files_to_delete:
        try:
            f.unlink()
            print(f"Deleted {f}")
        except Exception as e:
            print(f"Could not delete {f}: {e}")
            
    if profile_dir.exists() and not any(profile_dir.iterdir()):
        profile_dir.rmdir()
        print("Deleted empty experiment_profile directory.")

if __name__ == "__main__":
    migrate()

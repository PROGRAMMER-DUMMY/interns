"""Bronze ingestion: s3://amzn-workspace-rcm/ -> healthcare_rcm.bronze.*

Uses `COPY INTO` (SQL-native, idempotent-by-default batch load), not Auto
Loader. Two real, reproducible platform limitations on this workspace's
serverless-only compute ruled out Auto Loader for this proof run:
  1. The Python readStream/writeStream/toTable API hit a Spark Connect bug
     (SPARK_CONNECT_ILLEGAL_STATE.STATE_CONSISTENCY_EXECUTION_STATE_
     TRANSITION_INVALID_OPERATION_STATUS_MISMATCH) running several
     sequential streaming queries in one session.
  2. SQL-native `CREATE OR REFRESH STREAMING TABLE` is blocked outright:
     STREAMING_TABLE_OPERATION_NOT_ALLOWED.ST_NOT_ENABLED_ON_SERVERLESS_
     GENERIC_COMPUTE -- requires a feature-preview enrollment not enabled
     on this workspace.
At this file count (13) `COPY INTO` is Databricks' own documented right
answer anyway (their guidance: thousands of files -> COPY INTO, millions+ ->
Auto Loader) -- this isn't a downgrade for this proof run's actual scale.

Every column is created as STRING explicitly (not schema-inferred) --
preserves the all-strings-at-bronze design intent (avoids type inference
silently corrupting a numeric-looking-but-not identifier). Casting is
deferred to the dbt staging layer. `COPY INTO` is idempotent by default:
already-loaded files are skipped on rerun, without extra flags.
"""
from pyspark.sql import SparkSession

BUCKET = "s3://amzn-workspace-rcm"
CATALOG = "healthcare_rcm"

# (table_name, source_path, [explicit column names, in source order])
SOURCES = [
    ("hospital_a_departments", f"{BUCKET}/datasets/EMR/trendytech-hospital-a/departments.csv",
     ["DeptID", "Name"]),
    ("hospital_a_encounters", f"{BUCKET}/datasets/EMR/trendytech-hospital-a/encounters.csv",
     ["EncounterID", "PatientID", "EncounterDate", "EncounterType", "ProviderID",
      "DepartmentID", "ProcedureCode", "InsertedDate", "ModifiedDate"]),
    ("hospital_a_patients", f"{BUCKET}/datasets/EMR/trendytech-hospital-a/patients.csv",
     ["PatientID", "FirstName", "LastName", "MiddleName", "SSN", "PhoneNumber",
      "Gender", "DOB", "Address", "ModifiedDate"]),
    ("hospital_a_providers", f"{BUCKET}/datasets/EMR/trendytech-hospital-a/providers.csv",
     ["ProviderID", "FirstName", "LastName", "Specialization", "DeptID", "NPI"]),
    ("hospital_a_transactions", f"{BUCKET}/datasets/EMR/trendytech-hospital-a/transactions.csv",
     ["TransactionID", "EncounterID", "PatientID", "ProviderID", "DeptID", "VisitDate",
      "ServiceDate", "PaidDate", "VisitType", "Amount", "AmountType", "PaidAmount",
      "ClaimID", "PayorID", "ProcedureCode", "ICDCode", "LineOfBusiness", "MedicaidID",
      "MedicareID", "InsertDate", "ModifiedDate"]),
    ("hospital_b_departments", f"{BUCKET}/datasets/EMR/trendytech-hospital-b/departments.csv",
     ["DeptID", "Name"]),
    ("hospital_b_encounters", f"{BUCKET}/datasets/EMR/trendytech-hospital-b/encounters.csv",
     ["EncounterID", "PatientID", "EncounterDate", "EncounterType", "ProviderID",
      "DepartmentID", "ProcedureCode", "InsertedDate", "ModifiedDate"]),
    ("hospital_b_patients", f"{BUCKET}/datasets/EMR/trendytech-hospital-b/patients.csv",
     ["ID", "F_Name", "L_Name", "M_Name", "SSN", "PhoneNumber", "Gender", "DOB",
      "Address", "Updated_Date"]),
    ("hospital_b_providers", f"{BUCKET}/datasets/EMR/trendytech-hospital-b/providers.csv",
     ["ProviderID", "FirstName", "LastName", "Specialization", "DeptID", "NPI"]),
    ("hospital_b_transactions", f"{BUCKET}/datasets/EMR/trendytech-hospital-b/transactions.csv",
     ["TransactionID", "EncounterID", "PatientID", "ProviderID", "DeptID", "VisitDate",
      "ServiceDate", "PaidDate", "VisitType", "Amount", "AmountType", "PaidAmount",
      "ClaimID", "PayorID", "ProcedureCode", "ICDCode", "LineOfBusiness", "MedicaidID",
      "MedicareID", "InsertDate", "ModifiedDate"]),
    ("claims_hospital1", f"{BUCKET}/datasets/claims/hospital1_claim_data.csv",
     ["ClaimID", "TransactionID", "PatientID", "EncounterID", "ProviderID", "DeptID",
      "ServiceDate", "ClaimDate", "PayorID", "ClaimAmount", "PaidAmount", "ClaimStatus",
      "PayorType", "Deductible", "Coinsurance", "Copay", "InsertDate", "ModifiedDate"]),
    ("claims_hospital2", f"{BUCKET}/datasets/claims/hospital2_claim_data.csv",
     ["ClaimID", "TransactionID", "PatientID", "EncounterID", "ProviderID", "DeptID",
      "ServiceDate", "ClaimDate", "PayorID", "ClaimAmount", "PaidAmount", "ClaimStatus",
      "PayorType", "Deductible", "Coinsurance", "Copay", "InsertDate", "ModifiedDate"]),
    ("cptcodes", f"{BUCKET}/datasets/cptcodes/cptcodes.csv",
     ["Procedure Code Category", "CPT Codes", "Procedure Code Descriptions", "Code Status"]),
]
# cptcodes source header has spaces in column names. COPY INTO with
# header='true' matches columns by NAME against the target table (confirmed
# empirically -- an earlier attempt renamed the target columns to
# underscores and COPY INTO then reported a schema mismatch, since it
# matches the real header text, not position). So: keep the real header
# names (more faithful to source anyway) and enable Column Mapping just on
# this one table, rather than diverging the target schema from the source.
NEEDS_COLUMN_MAPPING = {"cptcodes"}


def ingest_one(spark: SparkSession, table_name: str, source_path: str, columns: list) -> dict:
    target_table = f"{CATALOG}.bronze.{table_name}"
    col_defs = ", ".join(f"`{c}` STRING" for c in columns)
    tbl_props = (
        " TBLPROPERTIES ('delta.columnMapping.mode' = 'name', "
        "'delta.minReaderVersion' = '2', 'delta.minWriterVersion' = '5')"
        if table_name in NEEDS_COLUMN_MAPPING else ""
    )
    spark.sql(f"CREATE TABLE IF NOT EXISTS {target_table} ({col_defs}) USING DELTA{tbl_props}")
    spark.sql(
        f"""
        COPY INTO {target_table}
        FROM '{source_path}'
        FILEFORMAT = CSV
        FORMAT_OPTIONS ('header' = 'true', 'inferSchema' = 'false')
        """
    )
    landed = spark.table(target_table).count()
    return {"table": table_name, "landed_rows": landed}


def main() -> None:
    spark = SparkSession.builder.getOrCreate()
    results = [ingest_one(spark, name, path, cols) for name, path, cols in SOURCES]
    print("=== bronze_ingest_s3 results ===")
    for r in results:
        print(r)
    print("=== rerun for idempotency check ===")
    results2 = [ingest_one(spark, name, path, cols) for name, path, cols in SOURCES]
    for r in results2:
        print(r)


if __name__ == "__main__":
    main()

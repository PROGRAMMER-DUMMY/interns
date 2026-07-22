"""Verify bronze ingestion: row counts + a second COPY INTO pass to prove
idempotency (a rerun against unchanged source files must not duplicate rows).
"""
import json
from pyspark.sql import SparkSession

CATALOG = "healthcare_rcm"
TABLES = [
    "hospital_a_departments", "hospital_a_encounters", "hospital_a_patients",
    "hospital_a_providers", "hospital_a_transactions",
    "hospital_b_departments", "hospital_b_encounters", "hospital_b_patients",
    "hospital_b_providers", "hospital_b_transactions",
    "claims_hospital1", "claims_hospital2", "cptcodes",
]


def main() -> None:
    spark = SparkSession.builder.getOrCreate()
    print("=== row counts, first check ===")
    before = {}
    for t in TABLES:
        n = spark.table(f"{CATALOG}.bronze.{t}").count()
        before[t] = n
        print(f"{t}: {n}")

    print("=== re-running COPY INTO on every table (idempotency check) ===")
    sources = {
        "hospital_a_departments": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-a/departments.csv",
        "hospital_a_encounters": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-a/encounters.csv",
        "hospital_a_patients": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-a/patients.csv",
        "hospital_a_providers": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-a/providers.csv",
        "hospital_a_transactions": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-a/transactions.csv",
        "hospital_b_departments": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-b/departments.csv",
        "hospital_b_encounters": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-b/encounters.csv",
        "hospital_b_patients": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-b/patients.csv",
        "hospital_b_providers": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-b/providers.csv",
        "hospital_b_transactions": "s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-b/transactions.csv",
        "claims_hospital1": "s3://amzn-workspace-rcm/datasets/claims/hospital1_claim_data.csv",
        "claims_hospital2": "s3://amzn-workspace-rcm/datasets/claims/hospital2_claim_data.csv",
        "cptcodes": "s3://amzn-workspace-rcm/datasets/cptcodes/cptcodes.csv",
    }
    for t, path in sources.items():
        spark.sql(
            f"""
            COPY INTO {CATALOG}.bronze.{t}
            FROM '{path}'
            FILEFORMAT = CSV
            FORMAT_OPTIONS ('header' = 'true', 'inferSchema' = 'false')
            """
        )

    print("=== row counts, after rerun ===")
    mismatches = []
    for t in TABLES:
        n = spark.table(f"{CATALOG}.bronze.{t}").count()
        status = "OK" if n == before[t] else "MISMATCH"
        if n != before[t]:
            mismatches.append(t)
        print(f"{t}: {n} (was {before[t]}) [{status}]")

    print("=== IDEMPOTENCY: PASSED ===" if not mismatches else f"=== IDEMPOTENCY: FAILED on {mismatches} ===")

    payload = json.dumps({"before": before, "after": {t: spark.table(f"{CATALOG}.bronze.{t}").count() for t in TABLES}, "mismatches": mismatches})
    try:
        dbutils.notebook.exit(payload)
    except NameError:
        print(payload)


if __name__ == "__main__":
    main()

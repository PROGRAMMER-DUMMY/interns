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

spark = SparkSession.builder.getOrCreate()
counts = {t: spark.table(f"{CATALOG}.bronze.{t}").count() for t in TABLES}

try:
    dbutils.notebook.exit(json.dumps(counts))
except NameError:
    print(json.dumps(counts))

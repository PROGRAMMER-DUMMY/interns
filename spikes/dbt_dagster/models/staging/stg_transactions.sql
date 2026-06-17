{{ config(materialized='view') }}

-- L1 staging: 1:1 cleaned mirror of bronze.transactions.
-- Allowed ops only: type casts + snake_case renames. No joins (medallion rule).
-- Governance: MedicaidID / MedicareID (PII identifiers) are intentionally NOT
-- selected -- they never enter the model, mirroring the platform's
-- pii_redaction drop and the generated SQL's pruned catalog bootstrap.
select
    "TransactionID"               as transaction_id,
    "PatientID"                   as patient_id,
    "DeptID"                      as dept_id,
    "VisitType"                   as visit_type,
    "LineOfBusiness"              as line_of_business,
    "PayorID"                     as payor_id,
    cast("ServiceDate" as date)   as service_date,
    cast("PaidDate"    as date)   as paid_date,
    cast("PaidAmount"  as double) as paid_amount
from {{ source('raw', 'transactions') }}

{{ config(materialized='view') }}

-- L1 staging: bronze.patients with PII removed.
-- Dropped (PII, never displayable): FirstName, LastName, MiddleName, SSN,
-- PhoneNumber, Address. Kept: the join key plus the two non-PII attributes the
-- KPIs need (Gender, DOB). DOB is a raw date input used only to DERIVE age
-- downstream; age itself is non-PII -- the same rule the platform applies
-- before redaction.
select
    "PatientID"          as patient_id,
    "Gender"             as gender,
    cast("DOB" as date)  as dob
from {{ source('raw', 'patients') }}

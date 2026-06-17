{{ config(materialized='view') }}

-- L2 intermediate: the conformed star. Joins ONLY along approved, executable
-- relationship edges (allowed_in_sql_generation = true in
-- relationship_contracts.json):
--   transactions -> patients     (PatientID)
--   transactions -> departments  (DeptID)
-- The advisory / failed-RI edges (encounters, providers) are deliberately
-- excluded -- the same gate the platform's conformed.py applies.
select
    t.transaction_id,
    t.patient_id,
    t.dept_id,
    t.visit_type,
    t.line_of_business,
    t.payor_id,
    t.service_date,
    t.paid_date,
    t.paid_amount,
    p.gender,
    p.dob,
    d.department_name
from {{ ref('stg_transactions') }} t
left join {{ ref('stg_patients') }}    p on t.patient_id = p.patient_id
left join {{ ref('stg_departments') }} d on t.dept_id    = d.dept_id

{{ config(materialized='view') }}

-- L1 staging: bronze.departments. Small lookup dimension; Name is a category
-- label (not PII) and is preserved as a dimension.
select
    "DeptID"  as dept_id,
    "Name"    as department_name
from {{ source('raw', 'departments') }}

-- Governance gate (dbt singular test): NO PII column may surface in any mart.
-- Returns rows (= test FAILS) if a known PII identifier leaked through staging
-- into a fct_ model. This is the dbt-native expression of the platform's
-- pii_redaction rule; validate_spike.py runs the same check offline.
select table_name, column_name
from information_schema.columns
where lower(table_name) like 'fct\_%' escape '\'
  and lower(column_name) in (
    'ssn', 'firstname', 'lastname', 'middlename',
    'phonenumber', 'address', 'medicaidid', 'medicareid'
  )

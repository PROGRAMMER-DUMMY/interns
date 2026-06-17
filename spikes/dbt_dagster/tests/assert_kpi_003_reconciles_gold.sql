-- Parity gate (dbt singular test): the dbt mart must reproduce the platform's
-- gold KPI total & row count -- the dq.py gold-reconciliation rule, expressed
-- in dbt. Pass the gold Delta path as a var when running with real dbt:
--   dbt test -s assert_kpi_003_reconciles_gold \
--     --vars '{gold_kpi_003: /abs/.../gold/kpi_003_results}'
{% set gold = var('gold_kpi_003', '') %}
{% if gold == '' %}
select 1 as failure where false   -- skipped when no gold path is provided
{% else %}
with mart as (
    select round(sum(sum_paidamount), 2) as total, count(*) as n
    from {{ ref('fct_kpi_003_top_payers') }}
),
gold as (
    select round(sum(sum_paidamount), 2) as total, count(*) as n
    from delta_scan('{{ gold }}')
)
select 'kpi_003 parity mismatch' as failure
from mart, gold
where mart.total <> gold.total or mart.n <> gold.n
{% endif %}

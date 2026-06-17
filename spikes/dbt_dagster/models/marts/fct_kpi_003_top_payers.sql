{{ config(materialized='table') }}

-- KPI-003: top-10 Commercial-LOB payers by amount paid. Ports kpi_003.sql.
-- Single-table aggregate (no join needed) -- still sourced from the conformed
-- star so the lineage graph stays uniform.
select
    line_of_business            as lineofbusiness,
    payor_id                    as payorid,
    round(sum(paid_amount), 2)  as sum_paidamount
from {{ ref('int_claims_enriched') }}
where line_of_business = 'Commercial'
group by 1, 2
order by sum_paidamount desc
limit 10

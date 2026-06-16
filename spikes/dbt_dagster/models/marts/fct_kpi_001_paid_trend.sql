{{ config(materialized='table') }}

-- KPI-001: paid-amount trend for Medicare LOB across gender & payer, for
-- patients over 50. Ports interns/generated/solutions/kpi_001.sql onto the
-- conformed star. age is derived from DOB vs the EVENT date (ServiceDate) --
-- stable, unlike a CURRENT_DATE anchor. Output column names match the gold
-- result so parity is a direct comparison.
select
    date_trunc('month', service_date)     as month,
    line_of_business                      as lineofbusiness,
    payor_id                              as payorid,
    gender,
    date_diff('year', dob, service_date)  as age,
    round(sum(paid_amount), 2)            as sum_paidamount
from {{ ref('int_claims_enriched') }}
where line_of_business = 'Medicare'
  and date_diff('year', dob, service_date) > 50
group by 1, 2, 3, 4, 5
order by 1, 2, 3, 4, 5

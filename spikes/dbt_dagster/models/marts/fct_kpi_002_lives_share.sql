{{ config(materialized='table') }}

-- KPI-002: percentage share of lives by department, visit type, gender, age
-- band. Ports kpi_002.sql INCLUDING single attribution -- each patient is
-- counted in exactly one grain cell (deterministic ROW_NUMBER order) so the
-- shares sum to 100% (the platform's human-confirmed share_attribution
-- default). NOTE: age_band is anchored to CURRENT_DATE (no event date exists in
-- this KPI's grain), so exact band values drift with the run date relative to
-- the stored gold -- a documented as_of caveat, not a logic error.
with attributed as (
    select *,
        row_number() over (
            partition by patient_id
            order by department_name, visit_type, gender,
                     cast(floor(date_diff('year', dob, current_date) / 10) as bigint) * 10
        ) as attribution_rn
    from {{ ref('int_claims_enriched') }}
)
select
    department_name,
    visit_type as visittype,
    gender,
    concat(
        cast(floor(date_diff('year', dob, current_date) / 10) as bigint) * 10, '-',
        cast(floor(date_diff('year', dob, current_date) / 10) as bigint) * 10 + 9
    ) as age_band,
    cast(count(*) as double) / nullif(sum(count(*)) over (), 0) * 100 as percentage_share
from attributed
where attribution_rn = 1
group by 1, 2, 3, 4
order by 1, 2, 3, 4

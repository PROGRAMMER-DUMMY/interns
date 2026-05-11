-- ==============================================================================
-- CMS Medicare Part D Prescriber Data - Optimized Feature Engineering v2
-- ==============================================================================
-- Changes from v1:
--   [fix] Column pruning: dropped 9 high-null / zero-importance columns
--   [fix] Bug 2: Opioid suppression — preserve NULLs, do not impute 0
--   [fix] Bug 3: Added Antpsyct_GE65_Tot_Benes (feature importance 0.482)
--   [perf] specialty_medians CTE: pre-aggregated 205 rows, safe at 9TB scale
--          replaces hardcoded constants with actual specialty-level medians
-- ==============================================================================

WITH raw_data AS (
    SELECT
        -- Identity (pruned: Prscrbr_MI 35% null/~0 importance, Prscrbr_Cntry ~0 importance)
        PRSCRBR_NPI,
        Prscrbr_Last_Org_Name,
        Prscrbr_First_Name,
        Prscrbr_Crdntls,
        Prscrbr_Ent_Cd,
        Prscrbr_St1,
        -- Prscrbr_St2 dropped: 79.7% null
        Prscrbr_City,
        Prscrbr_State_Abrvtn,
        Prscrbr_State_FIPS,
        Prscrbr_zip5,
        Prscrbr_RUCA,
        Prscrbr_RUCA_Desc,
        Prscrbr_Type,
        Prscrbr_Type_src,

        -- Core volume (always present, denominator-safe)
        COALESCE(Tot_Clms,     0)   AS Tot_Clms,
        COALESCE(Tot_Drug_Cst, 0.0) AS Tot_Drug_Cst,
        COALESCE(Tot_Benes,    0.0) AS Tot_Benes,
        Tot_30day_Fills,
        Tot_Day_Suply,

        -- Generic drug (Guardrail 1: NULL when suppressed flag present)
        Gnrc_Sprsn_Flag,
        CASE WHEN Gnrc_Sprsn_Flag IS NULL THEN Gnrc_Tot_Clms    END AS Gnrc_Tot_Clms,
        CASE WHEN Gnrc_Sprsn_Flag IS NULL THEN Gnrc_Tot_Drug_Cst END AS Gnrc_Tot_Drug_Cst,

        -- Brand drug (flag preserved for downstream suppression checks)
        Brnd_Sprsn_Flag,
        Brnd_Tot_Clms,
        Brnd_Tot_Drug_Cst,

        -- Other drug
        Othr_Sprsn_Flag,
        Othr_Tot_Clms,
        Othr_Tot_Drug_Cst,

        -- 65+ cohort
        GE65_Sprsn_Flag,
        GE65_Tot_Clms,
        GE65_Tot_Drug_Cst,
        GE65_Bene_Sprsn_Flag,
        GE65_Tot_Benes,

        -- Opioids — BUG FIX: was CASE WHEN NOT NULL THEN x ELSE 0.0 END
        -- That imputed 0 for suppressed records, violating Guardrail 1.
        -- Fix: pass raw values; NULLs flow through to KPI calc and get
        -- imputed with specialty median at the final step.
        Opioid_Tot_Clms,
        Opioid_Tot_Drug_Cst,
        Opioid_Tot_Suply,
        Opioid_Tot_Benes,
        Opioid_Prscrbr_Rate,
        Opioid_LA_Tot_Clms,
        Opioid_LA_Tot_Drug_Cst,
        Opioid_LA_Tot_Suply,
        Opioid_LA_Tot_Benes,
        -- Opioid_LA_Prscrbr_Rate dropped: 73.7% null

        -- Antibiotic
        Antbtc_Tot_Clms,
        Antbtc_Tot_Drug_Cst,
        Antbtc_Tot_Benes,

        -- Antipsychotic GE65 — NEW: feature importance 0.482 (was missing from v1)
        Antpsyct_GE65_Sprsn_Flag,
        CASE WHEN Antpsyct_GE65_Sprsn_Flag    IS NULL THEN Antpsyct_GE65_Tot_Clms    END AS Antpsyct_GE65_Tot_Clms,
        CASE WHEN Antpsyct_GE65_Sprsn_Flag    IS NULL THEN Antpsyct_GE65_Tot_Drug_Cst END AS Antpsyct_GE65_Tot_Drug_Cst,
        CASE WHEN Antpsyct_GE65_Bene_Suprsn_Flag IS NULL THEN Antpsyct_GE65_Tot_Benes END AS Antpsyct_GE65_Tot_Benes,

        -- Beneficiary demographics (pruned: Bene_Race_Othr_Cnt 70.8% null)
        Bene_Avg_Age,
        Bene_Feml_Cnt,
        Bene_Male_Cnt,
        Bene_Race_Wht_Cnt,
        Bene_Race_Black_Cnt,
        Bene_Race_Api_Cnt,
        Bene_Race_Hspnc_Cnt,
        Bene_Race_Natind_Cnt,
        Bene_Dual_Cnt,
        Bene_Ndual_Cnt,
        Bene_Avg_Risk_Scre

    FROM mup_data
),

-- Pre-aggregate specialty medians (205 rows) directly from source.
-- Parallel to raw_data scan — DuckDB pipelines both. No OOM at 9TB scale.
specialty_medians AS (
    SELECT
        Prscrbr_Type,
        APPROX_QUANTILE(
            CASE WHEN Gnrc_Sprsn_Flag IS NULL
                 THEN Gnrc_Tot_Clms / NULLIF(Tot_Clms, 0) END, 0.5
        ) AS median_gdr,
        APPROX_QUANTILE(Opioid_Tot_Clms / NULLIF(Tot_Clms, 0), 0.5) AS median_opioid_rate,
        APPROX_QUANTILE(
            Bene_Dual_Cnt / NULLIF(COALESCE(Bene_Dual_Cnt, 0) + COALESCE(Bene_Ndual_Cnt, 0), 0), 0.5
        ) AS median_dual_ratio
    FROM mup_data
    WHERE Tot_Clms > 0
    GROUP BY Prscrbr_Type
),

-- KPI calculation — all ratios guarded with NULLIF per Guardrail 3
kpi_features AS (
    SELECT
        r.PRSCRBR_NPI,
        r.Prscrbr_Type,
        r.Prscrbr_State_Abrvtn,

        r.Tot_Clms,
        r.Tot_Drug_Cst,
        r.Tot_Benes,

        r.Tot_Drug_Cst / NULLIF(r.Tot_Clms,  0) AS Cost_Per_Claim,
        r.Tot_Drug_Cst / NULLIF(r.Tot_Benes, 0) AS Cost_Per_Bene,

        r.Gnrc_Tot_Clms   / NULLIF(r.Tot_Clms, 0) AS Generic_Dispensing_Rate,
        r.Opioid_Tot_Clms / NULLIF(r.Tot_Clms, 0) AS Opioid_Prescribing_Rate,

        r.Bene_Avg_Age,
        r.Bene_Avg_Risk_Scre,

        r.Bene_Dual_Cnt / NULLIF(
            COALESCE(r.Bene_Dual_Cnt, 0) + COALESCE(r.Bene_Ndual_Cnt, 0), 0
        ) AS Dual_Eligible_Ratio,

        r.Bene_Feml_Cnt / NULLIF(
            COALESCE(r.Bene_Feml_Cnt, 0) + COALESCE(r.Bene_Male_Cnt, 0), 0
        ) AS Female_Ratio,

        r.Antpsyct_GE65_Tot_Benes,
        r.Antpsyct_GE65_Tot_Clms / NULLIF(r.Tot_Clms, 0) AS Antpsyct_GE65_Rate,

        m.median_gdr,
        m.median_opioid_rate,
        m.median_dual_ratio

    FROM raw_data r
    LEFT JOIN specialty_medians m ON r.Prscrbr_Type = m.Prscrbr_Type
    WHERE r.PRSCRBR_NPI IS NOT NULL
),

-- Imputation: actual value → specialty median → safe constant
final_features AS (
    SELECT
        PRSCRBR_NPI,
        Prscrbr_Type,
        Prscrbr_State_Abrvtn,
        Tot_Clms,
        Tot_Drug_Cst,
        Tot_Benes,

        COALESCE(Cost_Per_Claim,  0) AS Cost_Per_Claim,
        COALESCE(Cost_Per_Bene,   0) AS Cost_Per_Bene,
        COALESCE(Generic_Dispensing_Rate,  median_gdr,         0.5) AS Generic_Dispensing_Rate,
        COALESCE(Opioid_Prescribing_Rate,  median_opioid_rate, 0.0) AS Opioid_Prescribing_Rate,
        COALESCE(Bene_Avg_Risk_Scre, 1.0)                           AS Bene_Avg_Risk_Scre,
        COALESCE(Dual_Eligible_Ratio, median_dual_ratio, 0.0)       AS Dual_Eligible_Ratio,
        COALESCE(Female_Ratio, 0.5)                                  AS Female_Ratio,
        COALESCE(Antpsyct_GE65_Tot_Benes, 0)                        AS Antpsyct_GE65_Tot_Benes,
        COALESCE(Antpsyct_GE65_Rate,      0.0)                      AS Antpsyct_GE65_Rate,
        Bene_Avg_Age

    FROM kpi_features
)

SELECT * FROM final_features;

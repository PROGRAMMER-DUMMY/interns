# Healthcare Prescriber KPI Registry & Guardrails

**Domain:** Medicare Part D Prescriber Data (MUP_DPR_RY25_P04_V10_DY23_NPI)
**Roles:** Business Analyst, Product Lead, Healthcare Client

## 1. Business Objective
To transform raw Medicare Part D prescriber data into a robust, feature-rich dataset optimized for predictive modeling (e.g., identifying anomalous prescribing behavior, predicting high-cost providers, or stratifying risk).

## 2. Key Performance Indicators (KPIs)
These derived metrics will serve as the primary features for our predictive models:

| KPI Name | Definition | Business Rationale |
| :--- | :--- | :--- |
| **Generic Dispensing Rate (GDR)** | `Gnrc_Tot_Clms / Tot_Clms` | Indicates cost-consciousness. Low GDR may flag wasteful spending or brand-name pharmaceutical steering. |
| **Opioid Prescribing Rate** | `Opioid_Tot_Clms / Tot_Clms` | A critical safety and compliance metric. High rates relative to peers in the same specialty require auditing. |
| **Dual-Eligible Ratio** | `Bene_Dual_Cnt / Tot_Benes` | Captures socioeconomic vulnerability. Dual-eligible patients (Medicare + Medicaid) often have complex, high-cost health needs. |
| **High-Risk Beneficiary Average** | `Bene_Avg_Risk_Scre` | Clinical complexity of the provider's patient panel. Justifies higher costs for specific providers. |
| **Cost Per Claim** | `Tot_Drug_Cst / Tot_Clms` | Baseline efficiency metric. Identifies providers driving disproportionate financial burden. |
| **Cost Per Beneficiary**| `Tot_Drug_Cst / Tot_Benes` | Normalizes cost based on patient panel size. |

## 3. Transformation Guardrails (Non-Negotiable Requirements)
As we optimize the SQL queries for speed during our experiments, the following requirements **must not be dropped**:

1. **Suppression Flag Integrity (`*_Sprsn_Flag`)**: 
   - CMS suppresses data for privacy (e.g., cell sizes < 11). We must **never** impute `0` where a suppression flag is present (`*` or `#`). Nulls caused by suppression must be handled safely (e.g., imputed as medians or flagged as missing).
2. **NPI Uniqueness**: 
   - Every row must represent exactly one unique Prescriber NPI (`PRSCRBR_NPI`). No accidental cartesian explosions (fan-outs) during joins.
3. **Denominator Safety**: 
   - All ratio calculations (GDR, Cost per Claim) must include `NULLIF(denominator, 0)` to prevent divide-by-zero fatal errors.
4. **Specialty Preservation**: 
   - `Prscrbr_Type` must be preserved as it is the primary categorical axis for peer-to-peer comparison. An Oncologist cannot be compared to a Dentist.

## 4. Modeling Optimization Strategy
During our optimization loops:
- We will replace complex CASE WHEN structures with native array/struct operations where applicable.
- We will push down aggregations.
- We will use DuckDB/Polars native SQL dialets if testing locally, or Spark SQL syntax if testing on the cluster.
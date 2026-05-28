-- Generated baseline KPI manifest.
-- Replace manifest-only rows with executable KPI logic as mappings are approved.
CREATE OR REPLACE TABLE kpi_baseline_manifest AS
SELECT * FROM (VALUES
  (1, 'What is trend for amount paid for medicare LOB across gender and payer for patients above 50 years of age', 'sum(PaidAmount)', 'Month (ServiceDate), LineOfBusiness, PayorID, Gender, Age(DOB)', 'needs_mapping'),
  (2, 'What is percentage share of lives by gender, age , visit type, department', 'percentage of sum(distinct PatientID)     /   sum(disitnct PatientID) for departement', 'Department Name, VisitType, Gender, Age (DOB)', 'needs_mapping'),
  (3, 'What are top 10 payers in Commercial LOB w.r.t. amount paid', 'sum(PaidAmount)', 'LineOfBusiness, PayorID', 'needs_mapping')
) AS t(kpi_id, kpi_name, metric_expression, grain_or_cuts, status);

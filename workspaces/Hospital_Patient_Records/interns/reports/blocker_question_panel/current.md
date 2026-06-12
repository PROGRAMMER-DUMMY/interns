# Blocker Question Panel: KPI definition

- Workspace: `workspaces/Hospital_Patient_Records`
- Applies to KPIs: kpi_002
- Reuse scope: `kpi_specific`
- Answer type: `kpi_definition_required`

## Feature Resolution

| Feature | Resolves as | Where it lands |
| --- | --- | --- |
| Id | proven_alias | encounters.Id |
| PAYER | proven_direct | encounters.PAYER |
| START | proven_alias | encounters.START |
| TOTAL_CLAIM_COST | proven_direct | encounters.TOTAL_CLAIM_COST |
| KPI definition | blocked_missing_evidence | (no candidate) |
| kpi_001 derived metric | candidate_unconfirmed | candidate: encounters.Id |
| kpi_007 derived metric | candidate_unconfirmed | candidate: encounters.TOTAL_CLAIM_COST |
| kpi_008 derived metric | candidate_unconfirmed | candidate: patients.Id |
| over_24_hour | blocked_missing_evidence | derived from encounters.START |
| recurred_within_30_day | blocked_missing_evidence | derived from encounters.Id |

## Sample Evidence

| Feature | Column | First 5 samples |
| --- | --- | --- |
| Id | encounters.Id | 0002c38a-54e9-0788-930a-90900dce3612, 00059b24-6473-ca4a-8795-7373d4ddc7e0, 00091c5b-f3a1-ee7b-88cc-850c746f8f58, 00092210-7294-428e-1334-9d3f4b671ca4, 000cc8f4-f2c7-5cf3-419d-13be23d959da |
| START | encounters.START | 2011-01-02T09:26:36Z, 2011-01-03T05:44:39Z, 2011-01-03T14:32:11Z, 2011-01-03T16:24:45Z, 2011-01-03T17:36:53Z |
| kpi_001 derived metric | encounters.Id | 0002c38a-54e9-0788-930a-90900dce3612, 00059b24-6473-ca4a-8795-7373d4ddc7e0, 00091c5b-f3a1-ee7b-88cc-850c746f8f58, 00092210-7294-428e-1334-9d3f4b671ca4, 000cc8f4-f2c7-5cf3-419d-13be23d959da |
| TOTAL_CLAIM_COST | encounters.TOTAL_CLAIM_COST | 0.0, 85.55, 85.56, 85.57, 85.59 |
| PAYER | encounters.PAYER | 047f6ec3-6215-35eb-9608-f9dda363a44c, 42c4fca7-f8a9-3cd1-982a-dd9751bf3e2a, 4d71f845-a6a9-3c39-b242-14d25ef86a8d, 5059a55e-5d6e-34d1-b6cb-d83d16e57bcf, 6e2f1a2d-27bd-3701-8d08-dae202c58632 |
| kpi_007 derived metric | encounters.TOTAL_CLAIM_COST | 0.0, 85.55, 85.56, 85.57, 85.59 |
| kpi_008 derived metric | patients.Id | 002bc307-2fff-04ba-161b-98cce123e226, 0034fe01-207f-275f-6b4b-821f7b0af044, 00abe029-00fa-a666-34f5-258a36978f6d, 010ff9c1-564b-6e32-09f4-29cb4224bba9, 01274098-150f-8211-6150-29f2a2da266c |

## Interaction Contract

Show current.md verbatim, or render options directly from current.json. Do not summarize the blocker panel and do not use a generic answer picker that adds options outside this artifact.

- Display mode: `project_blocker_panel`
- Generic answer picker allowed: `False`

## KPI Source Truth

### kpi_002

- Business question: For each year, what percentage of all encounters belonged to each encounter class (ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)?
- Description: OBJECTIVE 1: ENCOUNTERS OVERVIEW
- Metric from source: ``
- Cuts / dimensions from source: year(START)
- Source: `workspaces/Hospital_Patient_Records/hospital_analytics_questions.sql`

Source workbook text is authoritative for KPI question, description, metric wording, cuts, filters, and continuation rows. It is not proof of joins, derived formulas, or executable grain unless those are explicit.

## Panel Contract

- Display shape: `full_kpi_truth_packet`
- Default code preference: `sql`
- Required sections: `absolute KPI source truth`, `all KPI fields`, `source cell or source artifact proof`, `recommended option`, `formula or derived logic when present`, `SQL table/column mapping with source evidence`, `sample values`, `SQL query or query sketch`, `demo result table shape`, `custom fallback option`

## Output Dialect

- Default: `SQL (default)`
- Alternatives: `polars`, `pyspark`, `databricks_sql`
- Rule: Render SQL by default. Generate other dialects only when the user explicitly selects them.

## Immutable KPI Policy

The KPI from the source workbook or registry is hard truth and must not be rewritten.

- Understanding is review context only: `True`
- Placeholder SQL is non-executable: `True`

## KPI Understanding Review

### kpi_002

- Presentation level: `full`
- Requires understanding approval: `True`
- Affected unresolved feature: `KPI definition`
- Original KPI: For each year, what percentage of all encounters belonged to each encounter class (ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)?
- Source metric: ``
- Source cuts / filters: year(START)

#### My Understanding

Answer the source KPI exactly as written: For each year, what percentage of all encounters belonged to each encounter class (ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)? Break out or filter by: year(START).

This is interpretation for review only. It does not replace or modify the source KPI.

#### Output Dialect: SQL

#### Strict Proven SQL

```sql
SELECT
  "START" AS "start"
FROM "encounters"
LIMIT 20;
```

#### Placeholder Intent SQL

```sql
-- NON-EXECUTABLE INTENT SKETCH: placeholders require user/proof confirmation.
-- KPI text is hard truth; this sketch is only to review intent.
SELECT
  <METRIC_EXPRESSION> AS metric_value,
  <year>
FROM "encounters"
GROUP BY <year>
-- unresolved blocker: <KPI definition>;
```

#### Demo Result Table

| year | metric_value | kpi_id |
| --- | --- | --- |
| <example> | <computed> | kpi_002 |

## Required User-Facing Ask

Use this section when asking the user for the blocker answer. Do not replace it with a freehand summary.

- Question: Define the metric and grain for `For each year, what percentage of all encounters belonged to each encounter class (ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)?`: which datasets, columns, filters, or derivations implement the prose definition? Matched workspace evidence is attached; confirm with apply-kpi-definition.
- Recommended option id: `custom`
- Recommended answer: Provide a concrete KPI definition before mapping features.
- Allowed option ids: `option_a`, `custom`

Do not state that another option is recommended unless `current.json` says so.

## Blocker

The KPI is defined in stakeholder prose; a concrete metric expression and grain have not been confirmed yet. The prose and matched workspace evidence are attached.

## Question

Define the metric and grain for `For each year, what percentage of all encounters belonged to each encounter class (ambulatory, outpatient, wellness, urgent care, emergency, and inpatient)?`: which datasets, columns, filters, or derivations implement the prose definition? Matched workspace evidence is attached; confirm with apply-kpi-definition.

## Instruction

Provide a concrete KPI definition before mapping features.

## Why

Executable KPI logic needs a proven metric and grain. Mapping placeholder words such as confirm, metric, or grain to columns would create invalid evidence.

## Options

### option_a: Provide KPI definition

Replace the seed KPI with a concrete metric — a rate, trend, aging, or count — with a defined business question and grain.

### custom: Restart KPI generation

Run KPI generation again with stakeholder context or a richer registry source.

## Evidence Files

- `workspaces/Hospital_Patient_Records/hospital_analytics_questions.sql` (kpi_source)

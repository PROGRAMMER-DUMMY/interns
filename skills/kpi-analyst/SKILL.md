---
name: kpi-analyst
description: Use this skill when the user uploads, shares, pastes, or describes a KPI sheet, KPI tracker, metrics document, dashboard metric list, or structured business analytics metric definitions; when asked to understand a metric, write queries for KPIs, calculate a KPI, build a dashboard from KPIs, or inspect files with columns such as Key Business Question, Metric, Dimension, Cut, Filter, Grain, or Description; also use when validating generated KPI SQL and result samples against KPI intent.
---

# KPI Analyst

Use this skill to read, interpret, validate, and operationalize KPI definition documents.

## Workflow

1. Parse every KPI row or block:
   - Business question: what decision the metric informs.
   - Metric formula: aggregation, ratio, ranking, count distinct, average, or percentage.
   - Dimensions and cuts: grouping columns and segment fields.
   - Filters: hard `WHERE` conditions.
   - Grain: one result row represents which combination of dimensions.
   - Output type: trend, snapshot, ranking, share/percentage, scorecard, or cohort.
2. Classify each KPI:
   - Trend: metric over time using `DATE_TRUNC`.
   - Ranking: top or bottom N by metric.
   - Share/percentage: part divided by a clear denominator.
   - Snapshot: point-in-time summary.
   - Scorecard: actual compared with target.
   - Cohort: segment comparison.
3. Before writing or accepting SQL, check for ambiguity:
   - Missing metric column, denominator, grain, time anchor, date field, table, join path, or SQL dialect.
   - Typos in formula keywords such as `disitnct`; treat them as formula/operator typos, not business features.
   - Unproven source columns or relationship joins.
   - Mismatch between accepted mappings and generated query aliases.
4. If ambiguity materially changes correctness, ask one targeted question. Use the repo's blocker panel flow for governed workspaces instead of freehand choices.
5. Write one query per KPI. Use CTEs for ratios, shares, joins, denominators, and derived fields.
6. Show a result table for each KPI:
   - Prefer actual execution output when available.
   - If using sample data only, label it clearly as sample or illustrative.
   - For percentage KPIs, include raw counts and percentages, and check denominator scope.
   - For ranking KPIs, include rank or top-N ordering.
7. End with assumptions and open questions only when real uncertainty remains.

## Query Rules

- Default to ANSI-style SQL unless the workspace or user specifies DuckDB, Databricks SQL, Spark SQL, BigQuery, Snowflake, or another dialect.
- Make filters explicit in `WHERE`.
- Alias all derived expressions.
- Use `COUNT(DISTINCT <id_col>)` for distinct entity counts; never map `distinct` itself as a column.
- Derive age from a confirmed DOB and anchor date. If no anchor date is given, ask or state the assumption.
- For share/percentage KPIs, make denominator scope explicit with a CTE or window partition.
- Do not generate executable SQL from column-name similarity alone in governed workspaces. Require profiles, data model evidence, accepted workspace definitions, or user confirmation.
- When reviewing generated SQL, verify that accepted physical mappings appear in the query or feature view lineage, not just in comments.

## Query Artifact Classification

Classify the requested or generated SQL before treating it as production work:

| Type | Purpose | Lifespan |
|---|---|---|
| Ad hoc | Answer a one-off question | Single use |
| Reporting query | Power a recurring report | Weeks to months |
| View / dbt model | Reusable logic for teams | Long-lived |
| Stored procedure | Automated, parameterized workflow | Permanent |

Use the classification to set quality gates:

- Ad hoc queries may run against proof views when clearly labeled.
- Reporting queries need stable filters, parameters, result grain, and basic data quality checks.
- Views/dbt models need silver/gold source readiness, tested joins, documented columns, and reproducible lineage.
- Stored procedures need parameter validation, idempotency, scheduling/error handling assumptions, and stronger operational review.

## Response Shape

Lead with the KPI, query, and result table:

```markdown
## KPI summary
| KPI | Type | Metric | Grain | Key dimensions |
|---|---|---|---|---|

## KPI 1 - <name>
**Business question:** ...
**Type:** ...
**Metric:** ...
**Grain:** ...

### Query
```sql
...
```

### Result
| ... |

### Checks
- ...
```

Keep explanations concise. Put assumptions and open questions at the end.

## Governed Workspace Rule

For this repo, prefer generated artifacts and validators:

- Read `interns/generated/contracts/kpi_registry.json` for source KPI truth.
- Read `interns/generated/contracts/kpi_feature_mapping.json` for accepted mappings.
- Read `interns/generated/contracts/source_to_target_plan.json` before SQL generation.
- Read `interns/generated/contracts/relationship_contracts.json` before using joins.
- Read `interns/reports/kpi_execution_harness.md` and generated SQL files for query/result review.
- Run `uv run validate-workspace-artifacts --workspace workspaces/<project>` after regeneration.

If a query result looks plausible but the SQL uses a placeholder, typo-derived feature, unproven join, or unmapped alias, call that out before accepting the result.

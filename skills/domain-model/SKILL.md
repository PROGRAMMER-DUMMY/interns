---
name: domain-model
description: >
  Align work with the project's domain language, KPI registry, data model, schema, relationships,
  grain, and business rules. Use before generating contracts, task configs, optimizations, or reports.
---

# Domain Model

Build a project-specific vocabulary and constraint map from available inputs.

## Inspect

- `CONTEXT.md`
- `config/tasks.json`
- `workspaces/<project>/docs/`
- KPI registry files
- data model docs or diagrams
- SQL/Polars/pipeline source files
- profiler output under `workspaces/<project>/interns/generated/profiles/`

Use Polars, not pandas, for dataframe inspection and profiling. If a dependency
forces pandas at an integration boundary, document that exception and keep the
conversion local.

## Extract

- Entities, facts, dimensions.
- Primary keys and foreign keys.
- Source-to-target dataset eligibility for KPI/query, ETL, ELT, and medallion-layer outputs.
- Output grain and valid aggregation levels.
- Join paths, cardinality expectations, and invalid joins.
- KPI formulas, tolerances, and filters.
- Temporal anchors and lifecycle-state rules.
- Required columns and forbidden transformations.
- Bronze/raw, silver/conformed, and gold/KPI layer assumptions when pipeline generation is requested.
- Business terms and synonyms.
- Candidate KPI term mappings to tables/columns with evidence source and confidence.
- Formula-derived, join-derived, taxonomy-derived, and temporal-anchor-derived features with
  required inputs and proof source.
- Missing metadata, dictionaries, catalog paths, SLA files, or contracts required to
  resolve a mapping.

## Output

```text
workspaces/<project>/interns/generated/contracts/domain_model.json
workspaces/<project>/interns/generated/contracts/domain_glossary.md
workspaces/<project>/interns/generated/contracts/kpi_column_mapping.json
workspaces/<project>/interns/reports/open_questions.md
```

If source documents disagree, record the conflict instead of guessing.
Do not mark a KPI feature ready unless it is proven by source evidence or explicitly confirmed by
the user.

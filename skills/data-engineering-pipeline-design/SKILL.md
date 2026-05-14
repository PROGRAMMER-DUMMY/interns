---
name: data-engineering-pipeline-design
description: >
  Design source-to-target SQL, Polars, PySpark, ETL/ELT, and medallion-layer workflows from KPI
  requirements, data model evidence, profiles, and accepted workspace definitions.
---

# Data Engineering Pipeline Design

Use this skill when the user asks for:

- SQL, Polars, or PySpark implementation choices;
- ETL/ELT loading logic;
- bronze/silver/gold or medallion architecture;
- source-to-target mapping;
- data-model-driven dataset selection;
- verification that generated KPI/query logic matches available datasets.

## Required Evidence Order

1. KPI/metric requirements and accepted workspace definitions.
2. Data model docs, diagrams, contracts, dictionaries, and catalog metadata.
3. Generated profile evidence under `workspaces/<project>/interns/generated/profiles/`.
4. Existing generated contracts, especially `domain_model.json` and `kpi_feature_mapping.json`.
5. Bounded samples only when profiles cannot answer a concrete mapping or quality question.

Do not infer source truth from column-name similarity alone.

## Design Rules

- Treat KPI text as business intent, not as a complete implementation spec.
- Use the data model to choose source datasets, join keys, valid grain, temporal anchors, filters,
  and target layer.
- For medallion work, separate:
  - bronze/raw: faithful ingestion and source metadata;
  - silver/conformed: typed, deduplicated, joined, and quality-checked entities;
  - gold/KPI: business aggregates, dimensions, and reporting outputs.
- Match the engine to the target:
  - SQL for warehouse-native KPI queries and Databricks SQL;
  - Polars for local file processing, profiling, and small/medium deterministic transforms;
  - PySpark for distributed medallion pipelines or enterprise Spark environments.
- Preserve semantic correctness over engine convenience.

## Blockers

Stop and ask through the blocker-panel workflow when any of these are unproven:

- source dataset for a KPI feature;
- join key or cardinality;
- output grain;
- temporal anchor;
- lifecycle status filter;
- medallion layer ownership;
- null/default handling;
- deduplication or slowly-changing-dimension policy;
- target engine or deployment environment.

## Output

When executable generation is not yet safe, write a solution brief or open question instead of code.
When generation is safe, record:

- source-to-target mapping;
- selected engine and reason;
- input datasets and join keys;
- data quality checks;
- grain and partitioning assumptions;
- medallion layer placement;
- validation query or test plan;
- remaining risks.

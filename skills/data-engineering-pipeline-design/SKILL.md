---
name: data-engineering-pipeline-design
description: >
  Design source-to-target SQL, Polars, PySpark, ETL/ELT, and medallion-layer workflows from KPI
  requirements, data model evidence, profiles, and accepted workspace definitions.
---

# Data Engineering Pipeline Design

Use this skill when the user asks for:

- external folder/source-root discovery and data lake intake;
- SQL, Polars, or PySpark implementation choices;
- ETL/ELT loading logic;
- bronze/silver/gold or medallion architecture;
- source-to-target mapping;
- data-model-driven dataset selection;
- verification that generated KPI/query logic matches available datasets.

## Required Evidence Order

1. Workspace/external-source discovery artifacts and source-selection approvals.
2. KPI/metric requirements and accepted workspace definitions.
3. Data model docs, diagrams, contracts, dictionaries, and catalog metadata.
4. Generated profile evidence under `workspaces/<project>/interns/generated/profiles/`.
5. Existing generated contracts, especially `domain_model.json` and `kpi_feature_mapping.json`.
6. Bounded samples only when profiles cannot answer a concrete mapping or quality question.

Do not infer source truth from column-name similarity alone.

## External Source Intake

When a user points to a folder such as `<external-data-root>`, do not make that folder the workspace.
Create or use a repo workspace such as `workspaces/cold-storage`, then run:

```powershell
uv run discover-external-sources --workspace workspaces/<project> --external-root <external-root>
```

Use the generated `external_source_discovery.json`, `external_source_discovery.md`, and
`docs/source_selection.generated.json` to decide what to register, copy, ignore, or ask about.

Default strategies:

- Raw CSV/JSON/Parquet with dictionaries/methodology docs: register as bronze candidates, parse docs,
  profile schemas, then design silver/gold only after relationships and grain are proven.
- Raw CSV/JSON/Parquet without docs: register/profile only; ask for dictionaries, source ownership,
  grain, and refresh cadence before silver/gold.
- Delta tables: register external table paths, inspect metadata/schema, then decide whether they are
  bronze source tables or already-silver/conformed tables.
- DuckDB/SQLite files: inspect table metadata first; export selected tables only after approval.
- Logs, sessions, `_delta_log`, system state, cache, and runtime files: exclude by default.
- Specs and Markdown/PDF documents: use as requirement/context evidence, not as datasets.

## Design Rules

- Treat KPI text as business intent, not as a complete implementation spec.
- Use the data model to choose source datasets, join keys, valid grain, temporal anchors, filters,
  and target layer.
- Before Bronze/Silver or medallion code generation, materialize the production standards contract:

```powershell
uv run prepare-bronze-silver-standards --workspace workspaces/<project> --domain <domain>
```

- Use `bronze_silver_standards.json` and `transformation_manifest.json` as the source of truth for
  layer responsibilities, allowed transformations, approved enrichment, quarantine, lineage,
  idempotency, exception handling, and engine parity.
- If a workflow drifts, use `workflow_reroute_policy.json`: stop the wrong branch, record a
  structured reroute event, run the replacement local-safe command once, and escalate if the same
  rule fails again.
- For medallion work, separate:
  - bronze/raw: source-preserving ingestion plus technical quality evidence, schema drift detection,
    duplicate evidence, and audit metadata, with no business mutation;
  - silver/conformed: typed, normalized, semantically conformed, deterministically enriched,
    quarantine-backed, lineage-complete, and quality-checked reusable entities;
  - gold/KPI: business aggregates, dimensions, and reporting outputs.
- Do not apply deduplication in Bronze. Silver may quarantine or dedup only after an approved,
  JSON-backed duplicate decision and must retain rejected-row lineage.
- Keep KPI-specific filters, aggregations, and metric formulas out of default Silver. Put those in
  Gold unless an explicit exception contract and passing compensating controls exist.
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
- unsupported rule parity for the selected SQL, Polars, or PySpark generator.

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

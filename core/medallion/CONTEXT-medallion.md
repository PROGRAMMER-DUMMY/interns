# Core Medallion Architecture Context: `core/medallion`

This document provides an exhaustive reference for all components in [`core/medallion`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion).

---

## Executive Overview & Architectural Model

The `core/medallion` package implements Medallion architecture (Bronze raw ingestion, Silver conformed transformations, Gold KPI business marts), design ratifiers, lineage parsers, incremental builders, and SQL linters.

---

## Key Modules & Responsibilities

- **Build Engine**: [`build.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/build.py#L40-L150), [`build_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/build_cli.py#L20-L60) - Executes medallion loading pipelines.
- **Design & Ratification**: [`design.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/design.py#L50-L180), [`design_ratify.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/design_ratify.py#L15-L65) - Generates medallion table design specs and validates join paths.
- **Incremental Loading**: [`incremental.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/incremental.py#L20-L80) - Generates incremental append/merge loading expressions.
- **Lineage Parsing**: [`sql_lineage_parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/sql_lineage_parser.py#L15-L50), [`spark_lineage_parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/spark_lineage_parser.py#L15-L50) - Parses SQL/Spark queries into table/column lineage graphs.
- **PII & Salt Store**: [`pii.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/pii.py#L10-L40), [`salt_store.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/salt_store.py#L15-L50) - Hashes sensitive columns using workspace salt keys. Lookup order is Databricks secret scope -> `AUTORESEARCH_WORKSPACE_SALT__<WORKSPACE>` -> `~/.config/autoresearch/secrets.toml`. `materialize_salt_if_missing` REFUSES (raises `RuntimeError`) when that toml exists but does not parse, instead of replacing it: the file is shared by every workspace on the box, and a dropped salt makes already-hashed PII unjoinable forever.
- **Emitters**: [`delta_emitter.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/delta_emitter.py#L20-L75), [`merge_emitter.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/merge_emitter.py#L15-L50) - Writes Delta Lake tables and SQL MERGE statements.

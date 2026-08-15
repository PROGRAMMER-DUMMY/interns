# Agents Architecture Context: `docs/agents`

This document provides an exhaustive reference for all components in [`docs/agents`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents).

---

## Executive Overview & Architectural Model

The `docs/agents` directory serves as the primary reference hub for AI agent behavior, operational rules, data engineering field knowledge, and Gemini CLI configuration guidelines. It bridges high-level data-engineering domain knowledge with CLI runtime policies and memory management standards required for autonomous operation within this control-plane repo.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           docs/agents                                   │
├─────────────────────────────────────┬───────────────────────────────────┤
│ DataEngineerTP.md                   │ gemini-cli-reference.md           │
│ (0.1% Data Engineer Field Manual)   │ (Gemini CLI Operational Reference)│
├─────────────────────────────────────┴───────────────────────────────────┤
│ data processing/                                                        │
│ (Subdirectory: Canonical Medallion & Schema Classification Guides)      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Subdirectory Overview: `data processing`

The [`data processing`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/data%20processing) directory contains the canonical data-engineering and schema-type reference materials for this project. It grounds the **data-understanding gate** ([`BUG-010`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L397-L463)) for classifying workspace data-quality tiers (raw, bronze, silver, gold) and schema types (star, snowflake, galaxy, 3NF, OBT) prior to onboarding.

- [`index.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/data%20processing/index.md#L1-L23): Central index and quick-routing guide for the 4-part data engineering guide set and the `schema_types_identification_guide.md`.

---

## File Details

### 1. [`DataEngineerTP.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md)

- **Exact Purpose**: Exhaustive 20-domain technical manual defining the foundational principles, storage patterns, data modeling strategies, pipeline patterns, compute internals, streaming systems, observability, and system design patterns expected of top 0.1% data engineers.
- **Key Sections & Content**:
  - [`Foundations — What Makes the 0.1%`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L32-L57): First-principles comparison of average vs. elite DEs, CAP theorem, columnar storage, compute models, networking costs, and schema evolution.
  - [`Storage Architectures`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L59-L157): Warehouses (Snowflake micro-partitions, BigQuery slot reservations), Data Lakes (Parquet, ORC, Avro), Open Table Formats comparison matrix (Delta Lake vs. Iceberg vs. Hudi vs. Paimon), and Medallion Architecture (Bronze/Silver/Gold).
  - [`Data Modelling`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L159-L239): Kimball dimensional modeling (grain declaration, SCD Types 1-6, factless facts), Inmon/3NF, Data Vault 2.0 (Hubs, Links, Satellites), and One Big Table (OBT).
  - [`Pipeline Patterns — Batch`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L240-L310): ETL vs. ELT, dbt transformation layers (staging/intermediate/marts, Jinja macros, semantic layer), and Reverse ETL (Census, Hightouch).
  - [`Pipeline Patterns — Streaming & Real-Time`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L312-L449): Event time vs. processing time, watermarks, windows, Kafka architecture & producer/consumer tuning, Log-based Change Data Capture (Debezium), Apache Flink state management, and Spark Structured Streaming.
  - [`Compute Engines`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L451-L529): Apache Spark execution model, Catalyst Optimizer, AQE (Adaptive Query Execution), memory fractions, shuffle tuning, Trino/Presto vectorization, and DuckDB embedded OLAP.
  - [`OLAP & Serving Layers`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L530-L584): OLTP vs. OLAP comparison, ClickHouse MergeTree engines, Apache Druid/Pinot real-time OLAP, and Centralized Semantic Metrics Layers (dbt MetricFlow, Cube.dev).
  - [`Orchestration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L585-L626): Apache Airflow (KubernetesExecutor, dynamic DAGs, XCom safety, dataset scheduling) vs. Prefect and Dagster asset-based orchestration.
  - [`Data Quality & Contracts`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L627-L668): Quality dimensions (completeness, uniqueness, validity, consistency, timeliness, accuracy), Great Expectations, Soda, and formal Data Contracts.
  - [`Data Observability`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L669-L695): 5 Pillars of Observability (freshness, volume, distribution, schema, lineage), Monte Carlo, Datafold, and circuit breakers.
  - [`Data Governance, Cataloguing & Lineage`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L697-L725): DataHub, Unity Catalog, OpenLineage, and ABAC/RBAC dynamic column masking.
  - [`Cloud Platforms`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L727-L788): AWS (S3, Glue, EMR, Athena, Lake Formation), GCP (BigQuery, Dataflow, Pub/Sub), and Azure / Microsoft Fabric (OneLake).
  - [`DataOps & CI/CD for Data`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L789-L834): Git version control for data, dbt CI testing pipeline, Terraform IaC, and Docker containerization.
  - [`Data Mesh & Platform Thinking`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L835-L854): 4 Principles of Data Mesh (domain ownership, data as a product, self-serve platform, federated computational governance).
  - [`Feature Engineering & ML Pipelines`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L855-L897): Online/offline Feature Stores (Feast, Tecton), Point-in-time (ASOF) joins to prevent training-serving skew, and ML batch/streaming scoring.
  - [`Performance Engineering & Cost Optimisation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L898-L938): File management (128–256MB Parquet target, Z-ordering, compaction), SQL query anti-patterns, and Cloud cost optimization (Spark right-sizing, Spot instances, auto-suspend).
  - [`Security, Privacy & Compliance`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L939-L968): PII tokenization/anonymization, GDPR right-to-erasure (Iceberg row-level deletes, Delta deletion vectors, crypto-shredding), and compliance frameworks (HIPAA, SOC 2).
  - [`System Design — Senior Patterns`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L969-L1055): Pipeline idempotency, backfill strategies, backward/forward schema evolution rules, handling late-arriving data (watermarking, Lambda/Kappa), and 2025 modern reference architecture.
  - [`Soft Skills & Tech Radar (2025–2026)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/DataEngineerTP.md#L1057-L1130): Business communication, design doc writing, tech radar (Adopt/Trial/Assess/Hold), and key numerical rules of thumb.
- **Inputs & Outputs**:
  - *Inputs*: Enterprise architectural constraints, data engineering specs, platform requirements.
  - *Outputs*: Guidelines for designing production-grade data pipelines, storage formats, and governance rules.
- **Failure Modes & Edge Cases**:
  - Highlights failure risks associated with uncompacted small files, non-idempotent re-runs, un-partitioned tables, un-governed CDC streams, and incorrect denominator scoping in KPI definitions.

---

### 2. [`gemini-cli-reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md)

- **Exact Purpose**: Repo-local operational reference manual for agents running Gemini CLI against this control plane, governing slash commands, configuration hierarchy, tool usage, policy engine rules, memory management, and Healthcare RCM workflow constraints.
- **Key Sections & Content**:
  - [`Operational Rules For This Repo`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md#L14-L29): Mandatory rule to render generated Markdown artifacts verbatim as human-facing answers, prohibiting replacement of governed panels with generic `ask_user` prompts.
  - [`Slash Commands`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md#L30-L58): Commands for reloading memory (`/memory refresh`), commands (`/commands reload`), agents (`/agents reload`), tools (`/tools desc`), and policy rules (`/policies list`), plus TOML command namespacing rules (e.g. `.gemini/commands/rcm/panel.toml` -> `/rcm:panel`).
  - [`Configuration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md#L59-L87): 7-tier configuration precedence order (defaults -> user settings -> project settings -> env vars -> CLI flags), context settings, and tool truncation thresholds.
  - [`Tools`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md#L88-L117): Built-in tools (`run_shell_command`, `read_file`, `write_file`, `replace`, `ask_user`, `activate_skill`) and parameter keys relevant for policy enforcement.
  - [`Policy Engine`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md#L118-L159): TOML rule syntax (`allow`, `deny`, `ask_user`), priority evaluation, user vs. admin policy paths, and safety policy patterns.
  - [`Memory And Context`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md#L160-L179): Hierarchical memory loading from `GEMINI.md`, `AGENTS.md`, `TOOLS.md`, and `.agents/tools.json`.
  - [`For Example RCM Workflow Notes`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/gemini-cli-reference.md#L180-L215): Hospital A isolation settings in `workspace_settings.json` (`dataset_allowlist`) and standard operator sequence (`/memory refresh` -> `/commands reload` -> `/rcm:panel` -> `/rcm:answer`).
- **Inputs & Outputs**:
  - *Inputs*: Gemini CLI invocation flags, `.gemini/settings.json`, `.gemini/commands/*.toml`, policy rules.
  - *Outputs*: Governed CLI behavior, rendered markdown panels, structured tool execution.
- **Failure Modes & Edge Cases**:
  - Documents gemini-cli issue #18186 where workspace/project policies under `.gemini/policies` are non-functional, requiring user or admin policy placement for enforceable rules.
  - Prevents agent hallucination by prohibiting `ask_user` substitution for generated blocker/review panels.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None. All documentation files in `docs/agents` are active references.
- 🔌 **Unwired Components**: None. Files are integrated into `AGENTS.md`, `GEMINI.md`, and `docs/README.md`.
- 👯 **Duplication & Overlap**: Minor topic overlap exists between `DataEngineerTP.md` section 2.4 (Medallion) and `docs/agents/data processing/data_engineering_guide_part2.md`.
- ⚠️ **Mismatches & Risks**: Policy engine documentation notes that project-level policies in `.gemini/policies` are currently ignored by Gemini CLI upstream, requiring user/admin policies (`~/.gemini/policies/*.toml`) for hard enforcement.

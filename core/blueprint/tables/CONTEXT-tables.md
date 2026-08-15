# Core Blueprint Tables Context: `core/blueprint/tables`

This document provides an exhaustive reference for all decision tables and policy specifications in `core/blueprint/tables`.

---

## Executive Overview & Architectural Model

The `core/blueprint/tables` directory houses declarative YAML decision tables used by the solution blueprint generator (`core.blueprint`). These tables codify rules for architecture and engine decisions:
1. **Transform Engine Selection** (`engine.yaml`)
2. **Compute Tier Sizing** (`compute.yaml`)
3. **Data Quality Placement & Rules** (`dq_placement.yaml`)
4. **Data Modeling Techniques** (`modeling.yaml`)
5. **Velocity Lanes & Latency** (`velocity.yaml`)

```
┌─────────────────────────────────────────────────────────┐
│              Input Workspace Parameters                 │
│      (Working set size, latency SLA, SQL complexity)    │
└──────────────────────────┬──────────────────────────────┘
                           │ Evaluates rules top-down (first-match)
                           ▼
┌─────────────────────────────────────────────────────────┐
│               Blueprint Decision Tables                 │
│  (engine.yaml, compute.yaml, modeling.yaml, etc.)       │
└──────────────────────────┬──────────────────────────────┘
                           │ Generates architecture decision
                           ▼
┌─────────────────────────────────────────────────────────┐
│               Solution Blueprint Plan                   │
│   (Target engine, cluster size, DQ checks, velocity)    │
└─────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`compute.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/compute.yaml)

- **Exact Purpose**: Defines compute tier selection rules (serverless vs. classic, single node vs. warehouse sizing) based on dataset working set size, engine choice, and cost constraints.
- **Key Sections**:
  - `rules`: Rules [`CMP-R0`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/compute.yaml#L20-L34) to [`CMP-R8`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/compute.yaml#L140-L149) matching conditions on `engine`, `serverless_allowed`, `resumability`, `budget_posture`, and `working_set_bytes`.
  - `revisit_triggers`: Triggers [`CMP-T1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/compute.yaml#L152) to [`CMP-T6`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/compute.yaml#L157) defining when compute sizing should be re-evaluated (e.g. disk spilling, queue time).
- **Inputs & Outputs**:
  - *Inputs*: Workspace parameters (`engine`, `working_set_bytes`, `serverless_allowed`, `budget_posture`).
  - *Outputs*: Selected compute tier (`single_node_host`, `serverless_jobs`, `classic_job_cluster`, `serverless_sql_2x_small`, etc.) with rationale and alternatives.
- **Failure Modes & Edge Cases**:
  - Evaluates rules top-down in `first_match` mode; missing working set bytes causes fallback to default rule `CMP-R8`.

### 2. [`dq_placement.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/dq_placement.yaml)

- **Exact Purpose**: Defines data quality check placement and severity tiers across Bronze, Silver, and Gold medallion layers.
- **Key Sections**:
  - `severity_tiers`: Definitions for `warn`, `fail`, and `quarantine` ([lines 16-19](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/dq_placement.yaml#L16-L19)).
  - `rules`: Rules [`DQ-R1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/dq_placement.yaml#L22-L37) (Bronze schema/freshness), [`DQ-R2`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/dq_placement.yaml#L38-L54) (Silver integrity), [`DQ-R3`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/dq_placement.yaml#L55-L70) (Gold business reconciliation), and [`DQ-R4`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/dq_placement.yaml#L71-L89) (Bronze regulatory retention).
  - `auto_derived_from_profiles`: List of check types derived from profile data ([lines 90-95](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/dq_placement.yaml#L90-L95)).
- **Inputs & Outputs**:
  - *Inputs*: Medallion layer (`bronze`, `silver`, `gold`), consumer classes, lineage flags.
  - *Outputs*: Collection of applicable DQ checks, severity actions, and dbt test generation hints (`mode: all`).
- **Failure Modes & Edge Cases**:
  - Operates in `mode: all` where all matching rules fire simultaneously.

### 3. [`engine.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/engine.yaml)

- **Exact Purpose**: Decision table for selecting the primary transform engine (`pyspark`, `sql_dbt_warehouse`, or `polars_single_node`).
- **Key Sections**:
  - `rules`: Top-down first-match rules [`ENG-R1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/engine.yaml#L18-L32) to [`ENG-R8`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/engine.yaml#L116-L138) checking `non_sql_logic`, `working_set_bytes` thresholds (1 TiB, 50 GiB, 10 GiB), `join_complexity`, and consumer counts.
  - `constraints`: Operational constraints [`ENG-C1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/engine.yaml#L142-L149) to [`ENG-C3`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/engine.yaml#L157-L161).
- **Inputs & Outputs**:
  - *Inputs*: `working_set_bytes`, `non_sql_logic`, `join_complexity`, `is_cloud_workspace`, `consumer_class_count`.
  - *Outputs*: Primary transform engine choice (`sql_dbt_warehouse`, `pyspark`, `polars_single_node`).
- **Failure Modes & Edge Cases**:
  - If working set bytes are unmeasured and no early rule matches, evaluation blocks rather than guessing.

### 4. [`modeling.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml)

- **Exact Purpose**: Decision table for selecting data modeling techniques (Data Vault, Star Schema, OBT, Feature Tables, Activity Schema).
- **Key Sections**:
  - `rules`: Base rules [`R1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml#L16-L38) to [`R9`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml#L110-L120) evaluating lineage requirements, consumer classes, and query shapes.
  - `modifiers`: Layered modifiers [`R7`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml#L122-L133) (SCD2 history), [`R8`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml#L135-L147) (OBT projections), and [`R10`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml#L149-L168) (Inferred member dimensions).
  - `defaults`: Standard physical modeling guidelines ([lines 170-176](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml#L170-L176)).
- **Inputs & Outputs**:
  - *Inputs*: `lineage_required`, `consumer_classes`, `query_shape`, `kpi_count`, `velocity_lane`.
  - *Outputs*: Selected modeling pattern (`conformed_star_gold`, `data_vault_silver_star_gold`, `obt_from_conformed_silver`, etc.) plus active modifiers.
- **Failure Modes & Edge Cases**:
  - Terminal fallback [`R9`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/modeling.yaml#L110-L120) defaults to `conformed_star_gold` if no prior condition matches.

### 5. [`velocity.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/velocity.yaml)

- **Exact Purpose**: Decision table for selecting processing velocity lanes (`realtime_serving`, `streaming`, `micro_batch`, or `batch`).
- **Key Sections**:
  - `rules`: Rules [`VEL-R1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/velocity.yaml#L18-L42) to [`VEL-R6`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/velocity.yaml#L106-L117) checking `target_latency` and `checkpointable_source`.
  - `constraints`: Constraints [`VEL-C1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/velocity.yaml#L119-L124) and [`VEL-C2`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/velocity.yaml#L125-L129).
- **Inputs & Outputs**:
  - *Inputs*: `target_latency`, `checkpointable_source`, `online_store_serving_edge`.
  - *Outputs*: Chosen velocity lane (`batch`, `micro_batch`, `streaming`, or explicit hard block for `realtime_serving`).
- **Failure Modes & Edge Cases**:
  - `VEL-R1` contains a `hard_block: true` flag to refuse `realtime_serving` requests when operational serving edges are unbuilt.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None. All YAML tables are parsed and evaluated by `core.blueprint`.
- 🔌 **Unwired Components**: None.
- 👯 **Logic & Code Duplication**: None. Clean separation between engine, compute, modeling, DQ, and velocity decisions.
- ⚠️ **Broken References & Mismatches**: None.

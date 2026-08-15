# Core Observability Architecture Context: `core/observability`

This document provides an exhaustive reference for all components in [`core/observability`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability).

---

## Executive Overview & Architectural Model

The `core/observability` package monitors execution timing, cost ingestion, warehouse cost calculation, log redaction, metric emission, telemetry backends, and KPI anomaly checks.

```
┌─────────────────────┐        ┌─────────────────────┐        ┌─────────────────────┐
│     events.py       ├───────►│  telemetry_backend  ├───────►│  cost_ledger.py     │
└─────────────────────┘        └─────────────────────┘        └─────────────────────┘
           │                              │                              │
           ▼                              ▼                              ▼
┌─────────────────────┐        ┌─────────────────────┐        ┌─────────────────────┐
│  log_redaction.py   │        │ kpi_anomaly_check   │        │  warehouse_cost.py  │
└─────────────────────┘        └─────────────────────┘        └─────────────────────┘
```

---

## File Details

### 1. [`cost_ingest.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/cost_ingest.py)

- **Exact Purpose**: Ingests system compute cost records, Databricks DBU usage logs, and query execution costs.
- **Key Functions / Classes**:
  - [`ingest_compute_costs(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/cost_ingest.py#L30-L90): Parses system billing logs or Unity Catalog system tables.
- **Inputs & Outputs**:
  - *Inputs*: Usage log files or system tables.
  - *Outputs*: Ingested cost metrics written to cost ledger.
- **Failure Modes & Edge Cases**:
  - Skips inaccessible remote system cost tables gracefully in local mode.

### 2. [`cost_ledger.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/cost_ledger.py)

- **Exact Purpose**: Maintains cumulative execution cost ledger per workspace, run, and KPI query.
- **Key Functions / Classes**:
  - [`CostLedger`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/cost_ledger.py#L25-L85): Records DBU usage, CPU/memory time, and token costs in `interns/state/cost_ledger.json`.
- **Inputs & Outputs**:
  - *Inputs*: Cost events, run IDs, resource usage data.
  - *Outputs*: Summarized cost reports and JSON ledger.
- **Failure Modes & Edge Cases**:
  - Handles concurrent ledger writes safely using workspace locks.

### 3. [`events.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/events.py)

- **Exact Purpose**: Structured event emitter and execution timer (`emit_event`, `time_command`).
- **Key Functions / Classes**:
  - [`emit_event(workspace_dir, event_name, payload)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/events.py#L15-L50): Writes structured JSONL event to `interns/state/events.jsonl`.
  - [`time_command(command_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/events.py#L51-L85): Context manager measuring command duration in milliseconds.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory, event name, duration.
  - *Outputs*: Appended event in `events.jsonl`.
- **Failure Modes & Edge Cases**:
  - Ensures event logging failure does not break primary workflow execution.

### 4. [`kpi_anomaly_check.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/kpi_anomaly_check.py)

- **Exact Purpose**: Detects numerical anomalies, unexpected null spikes, or extreme statistical variance in generated KPI outputs.
- **Key Functions / Classes**:
  - [`detect_kpi_anomalies(df, kpi_spec)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/kpi_anomaly_check.py#L25-L80): Calculates z-scores, null ratios, and distribution changes against baseline profiles.
- **Inputs & Outputs**:
  - *Inputs*: Polars DataFrame result, KPI specification.
  - *Outputs*: Anomaly flags and alert summary report.
- **Failure Modes & Edge Cases**:
  - Handles small or empty result sets without throwing division-by-zero errors.

### 5. [`log_redaction.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/log_redaction.py)

- **Exact Purpose**: Secret and credential sanitizer preventing API keys, Bearer tokens, passwords, and `.env` values from leaking into logs or stdout.
- **Key Functions / Classes**:
  - [`redact_sensitive_text(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/log_redaction.py#L20-L70): Regex scanner masking tokens, passwords, and sensitive keys.
- **Inputs & Outputs**:
  - *Inputs*: Log message string.
  - *Outputs*: Redacted text string with `<redacted>` tokens.
- **Failure Modes & Edge Cases**:
  - Preserves non-sensitive query text while redacting auth tokens.

### 6. [`parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/parser.py)

- **Exact Purpose**: Parses execution log output, command exit codes, and metric lines.
- **Key Functions / Classes**:
  - [`parse_log_metrics(log_content)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/parser.py#L10-L40): Extracts structured key-value metrics from raw log output.
- **Inputs & Outputs**:
  - *Inputs*: Raw log text.
  - *Outputs*: Extracted metric dictionary.
- **Failure Modes & Edge Cases**:
  - Ignores unparseable log lines.

### 7. [`telemetry_backend.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/telemetry_backend.py)

- **Exact Purpose**: Local and remote telemetry sink forwarding system metrics to observability backends.
- **Key Functions / Classes**:
  - [`TelemetryBackend`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/telemetry_backend.py#L20-L65): Dispatches aggregated performance metrics.
- **Inputs & Outputs**:
  - *Inputs*: Event objects, execution stats.
  - *Outputs*: Forwarded telemetry payloads.
- **Failure Modes & Edge Cases**:
  - Operates asynchronously to avoid slowing down query loops.

### 8. [`warehouse_cost.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/warehouse_cost.py)

- **Exact Purpose**: Calculates SQL warehouse scaling and execution cost estimates based on query duration and warehouse size.
- **Key Functions / Classes**:
  - [`calculate_warehouse_cost(duration_seconds, size_tier)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/warehouse_cost.py#L15-L50): Computes DBU consumption and estimated dollar cost.
- **Inputs & Outputs**:
  - *Inputs*: Duration, warehouse size string.
  - *Outputs*: Estimated cost numeric value.
- **Failure Modes & Edge Cases**:
  - Uses standard Databricks pricing tiers with fallback for custom enterprise rates.

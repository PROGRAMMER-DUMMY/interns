# autoresearch — Databricks Full Integration Plan
**Generated:** 2026-05-11  
**Status:** Implementing  
**PRD:** `.memory/sessions/2026-05-11/databricks-integration/PRD_databricks_integration.md`

---

## What We're Building

Databricks becomes a first-class, optional execution and telemetry backend for the autoresearch loop. When credentials are absent the system behaves exactly as today (DuckDB + SQLite). When credentials are present, experiments run on Databricks compute and results flow to MLflow 3 + Delta tables.

**Hard constraints:**
- Works with any `main_agent`: `claude-code`, `gemini-cli`, `codex`, `api`
- Works on AWS / Azure / GCP Databricks (auth via env vars only)
- `lock.toml` is human-edited only — agents never touch it
- `Workspace` (SQLite) remains local state owner — Databricks is additive
- No MCP servers — pure Python SDK, CLI-agnostic

---

## Architecture

### New Abstractions

```
ExecutionBackend (ABC)           ← mirrors LLMEngine strategy pattern
├── DuckDBBackend                ← current behaviour, default fallback
├── WarehouseBackend             ← databricks-sql-connector + SQL Warehouse
├── ConnectBackend               ← databricks-connect (local→remote Spark)
└── JobsBackend                  ← Jobs API: submit script, poll, fetch log

TelemetryBackend (ABC)
├── LocalTelemetry               ← wraps existing Workspace (SQLite)
└── DatabricksTelemetry          ← MLflow 3: tracking + tracing + evaluate

DatabricksClient                 ← thin WorkspaceClient builder + health check
DatabricksConfig (dataclass)     ← parsed from lock.toml [databricks] block
```

### MLflow 3 — Three Use Cases

| # | Use Case | MLflow API | Hook |
|---|---|---|---|
| 1 | Experiment Tracking | `mlflow.log_metric()` | After each `ExecutionBackend.execute()` |
| 2 | LLM Tracing | `mlflow.start_span()` | Wraps `InternBus.invoke()` per intern |
| 3 | GenAI Evaluation | `mlflow.evaluate()` | Wraps evaluator output per run |

### Execution Decision (D-01 resolution)

- `JobsBackend`: submits **unmodified** `experiment_cmd` script as a Databricks Job — works with frozen experiment.py
- `WarehouseBackend` / `ConnectBackend`: require new task `experiment.py` templates that use Spark SQL / databricks-connect. The frozen SQL optimization task stays on DuckDB unless switched via `execution = "jobs"`.
- Bottom line: set `execution = "jobs"` to run any existing experiment on Databricks with zero script changes.

### Data Flow

```
loop._run_one()
  telemetry.begin_run("exp_N")          ← starts MLflow run
    bus.invoke(intern) × N              ← each call → MLflow span (LLM trace)
    backend.execute(task)               ← DuckDB / Warehouse / Connect / Jobs
    evaluator output → mlflow.evaluate() ← GenAI eval score
  telemetry.end_run(metric, status)     ← log_metric + end MLflow run
  workspace.log_experiment(...)         ← SQLite always written (not replaced)
  [if Databricks] delta_write(run)      ← mirror to Delta table
```

---

## File Map

### New Files

| File | Purpose |
|---|---|
| `PLAN.md` | This file |
| `.gitignore` | Prevents `.env`, tokens entering git |
| `.env.example` | 3-var template for teams |
| `core/databricks_client.py` | WorkspaceClient builder + health check |
| `core/execution_backend.py` | ABC + all 4 backend implementations |
| `core/telemetry_backend.py` | ABC + LocalTelemetry + DatabricksTelemetry (MLflow 3) |
| `tools/databricks_setup.py` | Onboarding: validate → schema → test query |
| `Dockerfile` | Containerised autoresearch loop |
| `docker-compose.yml` | Full stack (loop + dashboard) with env injection |

### Modified Files

| File | Change |
|---|---|
| `config/lock.toml` | Add `[databricks]` block (human edit — see §4) |
| `core/config.py` | Add `DatabricksConfig` dataclass + parse from lock.toml |
| `core/loop.py` | Inject `ExecutionBackend` + `TelemetryBackend`; wire MLflow run lifecycle |
| `core/runner.py` | Fix missing `import shlex` (line 92); delegate to `ExecutionBackend` |
| `core/intern_bus.py` | Accept `TelemetryBackend`; call `log_intern_trace()` per invoke |
| `core/workspace.py` | Extend `redact_keys()` for `DATABRICKS_TOKEN` |
| `pyproject.toml` | Add `databricks-sdk`, `mlflow>=3.0`; optional: `databricks-sql-connector`, `databricks-connect` |
| `CONTEXT.md` | Add `ExecutionBackend`, `TelemetryBackend` to domain model |

### Debt Fixes (inline)

| File | Line | Fix |
|---|---|---|
| `core/runner.py` | 92 | `import shlex` missing |
| `core/parser.py` + `core/runner.py` + `tests/.../evaluator.py` | multiple | Consolidate duplicated metric parsing into `core/parser.py` only |

---

## lock.toml — Human Edit Required

Add this block to `config/lock.toml` (the file is locked to human edits only):

```toml
# ── Databricks Integration ────────────────────────────────────────────────────
# Set enabled = true and configure env vars to activate.
# When enabled = false (default), all execution falls back to DuckDB.
[databricks]
enabled            = false          # flip to true to activate
execution          = "jobs"         # jobs | connect | warehouse | duckdb
loop_on_databricks = false          # true = submit loop itself as Databricks Workflow
fallback           = "duckdb"       # duckdb | fail
host_env           = "DATABRICKS_HOST"
token_env          = "DATABRICKS_TOKEN"
http_path_env      = "DATABRICKS_HTTP_PATH"   # required for warehouse mode
catalog            = "autoresearch"
schema             = "experiments"
trace_redact       = true           # redact prompts from MLflow traces
http_timeout_sec   = 60
```

---

## Environment Variables

```bash
# Required for Databricks
DATABRICKS_HOST=https://adb-<workspace-id>.<region>.azuredatabricks.net
DATABRICKS_TOKEN=dapi<your-token>

# Required for warehouse execution mode only
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>

# Existing (unchanged)
GOOGLE_API_KEY=...
ANTHROPIC_API_KEY=...
```

Supported on: AWS Databricks, Azure Databricks (PAT or AAD), GCP Databricks, free trial.

---

## Implementation Phases

### Phase A — Foundation (no behaviour change)
- [x] `PLAN.md`
- [ ] `.gitignore` + `.env.example`
- [ ] `DatabricksConfig` in `core/config.py`
- [ ] `core/databricks_client.py`
- [ ] `core/execution_backend.py` (ABC + DuckDBBackend)
- [ ] `core/telemetry_backend.py` (ABC + LocalTelemetry)
- [ ] Wire into `core/loop.py` — DuckDB + LocalTelemetry only (no behaviour change)
- [ ] Fix `shlex` in `core/runner.py`
- [ ] Extend `redact_keys()` in `core/workspace.py`

### Phase B — Databricks Execution
- [ ] `WarehouseBackend` in `execution_backend.py`
- [ ] `JobsBackend` in `execution_backend.py`
- [ ] `ConnectBackend` in `execution_backend.py`
- [ ] Fallback: unreachable → DuckDB + warning
- [ ] Update `pyproject.toml` with new deps

### Phase C — MLflow 3 Telemetry
- [ ] `DatabricksTelemetry` in `telemetry_backend.py`
- [ ] `begin_run` / `end_run` wired into `loop.py`
- [ ] `log_intern_trace` wired into `intern_bus.py` (LLM Tracing)
- [ ] `mlflow.evaluate()` wrapper (GenAI Evaluation)
- [ ] Delta table writes for profiler CSVs + intern logs

### Phase D — Deployment & Onboarding
- [ ] `tools/databricks_setup.py`
- [ ] `Dockerfile`
- [ ] `docker-compose.yml`
- [ ] `CONTEXT.md` update

---

## Security Requirements

| Requirement | Implementation |
|---|---|
| Token never in logs | `Workspace.redact_keys()` extended for DATABRICKS_TOKEN |
| Token never in git | `.gitignore` created before any credential use |
| Token never in MLflow traces | `trace_redact = true` strips prompt bodies |
| Delta write failure | Silent fail → `telemetry_partial = true` flag, log to Workspace, loop continues |
| Auth token expiry (Azure AAD) | Catch HTTP 401 → log human-readable error → exit loop gracefully |
| Network timeout | `http_timeout_sec = 60` passed to all SDK clients |
| Loop-on-Databricks + AAD | Setup script warns: use OAuth M2M service principal for long-running jobs |

---

## Success Criteria

- `uv run loop` with no Databricks env vars = identical to today
- Setting env vars + `enabled = true` routes to configured backend with zero code changes
- All 4 backends selectable via `lock.toml` only
- Every run → MLflow entry with `primary_metric`, `token_count`, `execution_time_seconds`
- Every intern call → MLflow child span with latency, model, token count
- `mlflow.evaluate()` produces GenAI eval per run
- Profiler CSVs → `autoresearch.experiments.profiler_results` Delta table
- `uv run python tools/databricks_setup.py` completes in <60s
- `docker-compose up` starts full stack with only env vars
- `DATABRICKS_TOKEN` never appears in logs, git, or MLflow artifacts
- HTTP 401 → graceful exit, no SQLite corruption
- Works on AWS / Azure / GCP Databricks + free trial
- `main_agent` = any value → identical Databricks behaviour

---

## Team Onboarding (Quick Start)

```bash
# 1. Copy env template
cp .env.example .env

# 2. Fill in your Databricks workspace details
# DATABRICKS_HOST=https://adb-xxx.azuredatabricks.net
# DATABRICKS_TOKEN=dapi...

# 3. Edit config/lock.toml — set [databricks] enabled = true

# 4. Run setup script (validates everything, creates schema)
uv run python tools/databricks_setup.py

# 5. Run the loop
uv run loop

# OR: run via Docker
docker-compose up
```

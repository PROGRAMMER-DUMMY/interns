# Airflow CLI + Astro CLI Reference (operator-grade)

Scope: Apache Airflow **3.x** CLI (3.3.0 docs are current), the Astro CLI wrapper, and how each
command maps onto THIS platform's orchestration surface
(`core/orchestration/airflow_dag.py`, `core/orchestration/cosmos_dag.py`,
`core/orchestration/dbt_backfill.py`).

**Local state, verified on this box (2026-08-05):**

| Thing | State |
| --- | --- |
| `.venv\Scripts\airflow.exe` | absent |
| `.venv` `pip show apache-airflow` | not installed |
| `astro` on PATH | YES (`...\WinGet\Packages\Astronomer.Astro_...\astro.exe`) |
| `docker` on PATH | YES (`C:\Program Files\Docker\Docker\resources\bin\docker.exe`) |

So every command below is **docs-derived, not executed here**. `core/platform_readiness.py`
`_check_airflow()` already reports this correctly as a capability gap, not a blocker. Nothing in
this document has been run against a live scheduler in this session; the only real-infra Airflow
verification this repo has is the one recorded in `airflow_dag.py`'s module docstring (Astro
CLI/Docker, Cosmos `DbtBuildLocalOperator` reaching live Databricks).

All credentials below are placeholders (`<host>`, `<token>`). Never paste a real token into a
`connections add` command that lands in shell history.

---

## 1. Install story: what a user actually needs

Two supported routes. We recommend the first.

### Recommended: Astro CLI + Docker (isolated runtime)

```
astro dev init            # scaffold an Astro project (dags/, include/, requirements.txt, ...)
astro dev start           # build image + start scheduler/api-server/triggerer/postgres
astro dev run dags list   # any `airflow ...` command, inside the container
astro dev stop            # keep the metadata DB
astro dev kill            # hard reset, drops local metadata
```

Why this and not `pip install apache-airflow` into `.venv`:

- `airflow_dag.py`'s own docstring records the reason from the real verification run:
  pip-installing Airflow into this project's shared venv **downgrades shared deps** (it pins a
  large transitive set). The repo deliberately kept Airflow out of `.venv`, and that is still the
  right call.
- Astro pins a tested Airflow + provider set (Astro Runtime) so `astronomer-cosmos` and the
  Databricks provider resolve consistently.
- The one live failure found during that verification (a `databricks-sql-connector` Thrift vs
  warehouse-endpoint 404) was a *container dependency resolution* difference — exactly the class of
  problem that a shared venv would have smeared into the rest of this project.

Requirement: Docker (or Podman). Astro also offers `astro dev start --standalone`, which runs
Airflow directly on the machine with no Docker — useful on a locked-down box, but it re-introduces
the "Airflow's deps live next to yours" problem, so prefer containers.

### Alternative: pip install, in a SEPARATE venv

```
python -m venv .venv-airflow
.venv-airflow\Scripts\python -m pip install "apache-airflow==3.*" astronomer-cosmos
```

Only if Docker is unavailable. Must be a separate venv from `.venv` — never `uv add apache-airflow`
into this project.

### Wiring OUR generated DAGs into the runtime

`airflow_dag.py` builds its DAG at module import from `AUTORESEARCH_PIPELINE_WORKSPACE`, and every
stage is a `BashOperator` doing `cd <repo_root> && uv run <governed CLI>`. That means the container
needs the repo AND `uv` visible, not just the DAG file:

- Mount the repo into the Astro project (`volumes:` in `docker-compose.override.yml`, or develop
  with the repo as the Astro project root and `dags/` symlinked/copied).
- `dags/autoresearch_dag.py` can be a two-liner: `from core.orchestration.airflow_dag import dag`.
- Env the container needs: `AUTORESEARCH_PIPELINE_WORKSPACE=workspaces/<ws>`,
  `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` (Cosmos path refuses to even *wire* without it — see
  `cosmos_dag.build_dbt_tasks`), optional `AUTORESEARCH_ALERT_WEBHOOK_URL`. Put them in `.env`,
  which `astro dev start` loads.
- Cosmos needs the generated `workspaces/<ws>/dbt/` project **on disk at DAG-parse time**
  (`cosmos_dag.py` docstring). Run `uv run generate-dbt-project --workspace <ws>` before the
  scheduler parses, or accept the BashOperator fallback.

---

## 2. Command inventory

Role column = what it is for. Platform column = where WE use it (or "not used" with the reason).

### `airflow dags`

Airflow 3 subcommands (verbatim from the 3.3.0 CLI ref): `clear, delete, details, list,
list-import-errors, list-jobs, list-runs, next-execution, pause, report, reserialize, show,
show-dependencies, state, test, trigger, unpause`.

**Note the removal: there is NO `airflow dags backfill` in Airflow 3.** It was the 2.x spelling.
Airflow 3 moved it to a top-level `airflow backfill create`. Any doc, blog, or generated runbook of
ours that says `airflow dags backfill` is stale.

| Command | Role | Our platform |
| --- | --- | --- |
| `dags list` | enumerate parsed DAGs | smoke test that our module was picked up |
| `dags list-import-errors` | show DAGs that failed to parse | **primary parse gate** — our DAG imports the whole repo, so this is where a bad import surfaces |
| `dags report` | DagBag load report (file, parse duration, #DAGs) | parse-time budget check; Cosmos render cost shows here |
| `dags details --dag-id X` | DAG metadata | confirm schedule got the hashed cron, not `0 2 * * *` |
| `dags show / show-dependencies` | render topology | visual diff of `STAGES` vs what Airflow sees |
| `dags trigger -c '<json>'` | one manual run, with conf | **how our backfill seam is invoked** (params `event_time_start`/`event_time_end`) |
| `dags pause` / `unpause` | stop/allow scheduling | deploy safety: pause before a schema-breaking change |
| `dags test <dag_id> [logical_date]` | run one DagRun **locally, no scheduler, no DB state** | the dev loop (section 3) |
| `dags state` / `list-runs` | run status | workflow-guard "did last night's run succeed" |
| `dags clear` | clear DagRuns in a window | reprocess path (section 4) |
| `dags reserialize` | re-serialize to metadata DB | after a code change the scheduler didn't pick up |
| `dags next-execution` | next scheduled times | verify the hashed offset landed where expected |
| `dags delete` | purge all DB records for a DAG | decommissioning a workspace pipeline |

### `airflow tasks`

`test, run, state, clear, states-for-dag-run, list, render, failed-deps`.

| Command | Role | Our platform |
| --- | --- | --- |
| `tasks test <dag> <task> [date]` | run ONE task, no deps, no DB state | fastest loop when only one stage is broken (e.g. `dbt_build`) |
| `tasks run` | run a task instance for real (scheduler's own path) | not used directly; that's the scheduler's job |
| `tasks state` | one task instance's state | targeted failure triage |
| `tasks clear` | clear task instances (`--start-date/--end-date`, `-y`) | the "reprocess a stage" primitive |
| `tasks states-for-dag-run` | all task states in a run | one-shot triage of a failed nightly |
| `tasks render` | render Jinja templates | **verify the backfill command's `{{ params.event_time_start }}` actually renders** — `cosmos_dag.backfill_command()` builds a templated string; this is the only way to see the substituted result without running it |
| `tasks failed-deps` | why a task won't run | stuck-task diagnosis (pool full, upstream failed, depends_on_past) |

### `airflow backfill`

Only subcommand: `create`. Full treatment in section 4.

### `airflow db`

`migrate, reset, check, clean, downgrade, check-migrations, drop-archived, export-archived, shell`.

| Command | Our platform |
| --- | --- |
| `db check` | liveness probe for the metadata DB; cheap workflow-guard check |
| `db migrate` | first-run / upgrade (Astro does this for you) |
| `db clean --clean-before-timestamp <ts>` | metadata retention; our DAG fires nightly per workspace, so a fleet grows the metadata DB fast |
| `db reset` | destructive; only in a throwaway local env (`astro dev kill` is the Astro equivalent) |

### `airflow connections` / `variables` / `pools`

| Command | Our platform |
| --- | --- |
| `connections add/get/list/delete/export/import/test` | Databricks connection — section 5 |
| `variables set/get/list/export/import` | not used today. Our stages take config as CLI args from `pipeline_stages.command_for()`, not Airflow Variables. Don't add Variables just to have them — a Variable read at parse time is a DB hit per parse. |
| `pools set/get/list/delete/export/import` | **should be used**: a dedicated `backfill` pool with few slots is the documented control that stops a replay starving the nightly run (gap research §1, Shopify). Not wired yet. |

### `airflow providers` / `config` / `assets` / `jobs` / `info`

| Command | Our platform |
| --- | --- |
| `providers list` / `providers hooks` | confirm the Databricks provider is present in the image |
| `config get-value core dags_folder`, `config list --show-values --hide-sensitive` | debugging where the runtime is actually reading DAGs from. Always `--hide-sensitive`. |
| `assets list/details` | inspect the Airflow 3 Assets our `stage_assets()` emits as `outlets` |
| `jobs check --job-type SchedulerJob --local` | scheduler liveness — section 7 |
| `info` | environment dump for a bug report. **Careful**: can include config values; treat its output as sensitive. |
| `version` | pin verification |

### Long-running components

| Command | Role |
| --- | --- |
| `airflow scheduler` | scheduling loop |
| `airflow api-server` | Airflow 3's replacement for `airflow webserver` (UI + REST API in one). `webserver` is the 2.x name. |
| `airflow triggerer` | deferrable-operator event loop |
| `airflow dag-processor` | parses DAG files (separate process in Airflow 3) |
| `airflow standalone` | all of the above in one process + a generated admin user. Dev only. |

For us: `astro dev start` runs the whole set; we never invoke these directly.

---

## 3. The dev loop (running our generated DAG without a scheduler)

Three rungs, cheapest first.

**Rung 1 — does it even import?** Our DAG module pulls in `core.orchestration.*`, which pulls in
the KPI SQL generation stack. Import errors are the most likely failure and they are silent in the
UI unless you look.

```
astro dev parse                 # Docker, no running Airflow: parse + render check
astro dev run dags list-import-errors
```

**Rung 2 — run one task.** No dependency checks, no DB state, runs `execute()` directly:

```
airflow tasks test autoresearch_medallion_pipeline dbt_build 2026-08-05
```

Use this on `dbt_backfill` after `tasks render` to confirm the templated window substitutes.

**Rung 3 — run the whole DAG once.** `airflow dags test <dag_id> [logical_date]` executes a single
DagRun honouring task dependencies, in one process, **without registering state in the metadata
DB** and without an executor. `-e/--logical-date` defaults to now (UTC).

```
airflow dags test autoresearch_medallion_pipeline 2026-08-05
```

Python equivalent, and the one the Airflow debugging docs actually lead with:

```python
if __name__ == "__main__":
    dag.test()
```

`dag.test()` accepts `use_executor=` to exercise the real executor, and `mark_success_pattern=` to
skip tasks matching a regex — that last one is the practical way to test our topology while
short-circuiting `dbt_build` so you don't hit Databricks on every iteration.
Interactive: `python -m pdb dags/autoresearch_dag.py`.

Caveats that bite us specifically:

- `dags test` still needs the connections/env the tasks use. Our BashOperators need `uv` and the
  repo on the container's filesystem; a bare `dags test` in a container without the repo mounted
  fails at `cd <repo_root>`.
- Because `dags test` writes no run state, a green `dags test` is **not** evidence the scheduler
  will schedule it. Parse + schedule are separate failures from execution.

---

## 4. Backfill, in depth

This is the CLI side of the seam `cosmos_dag.backfill_command()` /
`airflow_dag.build_dag()`'s `params` already emit.

### 4.1 `airflow backfill create`

```
airflow backfill create \
    --dag-id autoresearch_medallion_pipeline \
    --from-date 2026-06-01 \
    --to-date 2026-06-07 \
    --reprocess-behavior failed \
    --max-active-runs 3 \
    --run-backwards \
    --dag-run-conf '{"event_time_start": "2026-06-01", "event_time_end": "2026-06-07"}'
```

Flags: `--dag-id` (required), `--from-date` / `--to-date` (both inclusive), `--dag-run-conf`,
`--dry-run`, `--reprocess-behavior {none,completed,failed}`, `--run-backwards`,
`--max-active-runs`, `--run-on-latest-version` / `--no-run-on-latest-version`.

`reprocess_behavior` semantics (quoting the docs):

- `none` — "if there's already a run for this logical date, do not create another, no matter the
  state".
- `failed` — create a new run only where the existing run failed.
- `completed` — create a new run where the existing run failed **or** completed.
- Hard rule regardless of setting: "If the latest run is still running or is queued, we do not
  create another run." A backfill can never duplicate an in-flight run.

`--max-active-runs` on the backfill "is applied independently [of] the Dag `max_active_runs`
setting". This is the throttle that keeps a replay from starving the nightly run. Combine with a
dedicated Airflow **pool** (`airflow pools set backfill 2 "bounded replay"`) and `priority_weight`
for the same effect at task granularity.

`--run-backwards` runs the most recent intervals first. Right default for us: the business wants
recent numbers correct first, history trickling.

`--run-on-latest-version` matters with Airflow 3 DAG versioning — whether replayed runs use the DAG
code as it was, or as it is now. For a *code-fix* backfill you want the latest version; for a
*data-fix* backfill of an audited period, you may not.

### 4.2 `backfill create` vs `dags trigger` vs clearing

Three different tools, and the gap research says pick the one matching the unit of work:

| Approach | What it produces | Use when |
| --- | --- | --- |
| `backfill create --from-date/--to-date` | one DagRun **per schedule interval / per partition** in the range | the DAG has a real time partition and you want partition-aligned replay |
| `dags trigger -c '{...}'` | exactly ONE run, window carried in conf | **our current seam.** `build_dag()` declares `params={"event_time_start": "", "event_time_end": ""}` and `build_backfill_task()` shells to `run-dbt-backfill` with the templated window. One run, one bounded dbt replay. |
| `dags clear` / `tasks clear` | no new runs; wipes state so existing runs re-execute | re-run a stage of runs that already exist (e.g. `publish_gold` failed but the build was fine) |

Our DAG is `catchup=False` with `schedule=None`-or-hashed-cron and a fixed `start_date(2024,1,1)`.
That is deliberate and matches the research: `catchup=True` with an old `start_date` creates
thousands of runs on first deploy. Because we are `catchup=False`, `backfill create` is the ONLY
way to get historical runs — it does not depend on catchup and is not blocked by it.

### 4.3 The honesty clause

`cosmos_dag.backfill_command()` already prefixes a `DEGRADED:` echo when no model in the generated
dbt project declares `event_time`. That mirrors the research rule: **backfill is first-class only
when the unit of work is a declared time partition.** Without one, dbt's
`--event-time-start/--event-time-end` match no batch and the invocation is a full refresh of the
selected models. Keep that echo; it is the difference between an honest degradation and a lie in
the task log.

Second bound, worth restating: backfill capability is a function of **source retention**, not
pipeline design. A 24h-retention Kafka topic cannot be replayed from 30 days ago no matter how the
DAG is written.

---

## 5. Connections, variables, secrets (Databricks)

Three ways to define a connection; all placeholders below.

```
# 1. discrete flags
airflow connections add databricks_default \
    --conn-type databricks \
    --conn-host '<workspace-host>.cloud.databricks.com' \
    --conn-extra '{"token": "<REDACTED>", "http_path": "/sql/1.0/warehouses/<id>"}'

# 2. URI (since 2.3.0)
airflow connections add databricks_default \
    --conn-uri 'databricks://<host>?token=<REDACTED>&http_path=%2Fsql%2F1.0%2Fwarehouses%2F<id>'

# 3. JSON (since 2.3.0)
airflow connections add databricks_default --conn-json '{"conn_type": "databricks", ...}'
```

URI encoding: reserved characters must be `quote_plus()`-encoded (a `/` in a password becomes
`%2F`). Deeply nested `extra` JSON goes under an `__extra__` query param. This is exactly why
`--conn-json` is the sane choice for a Databricks connection — the `http_path` is full of slashes.

**Environment-variable connections** (`AIRFLOW_CONN_{CONN_ID}` uppercased, value = URI *or* JSON):

```
export AIRFLOW_CONN_DATABRICKS_DEFAULT='{"conn_type":"databricks","host":"<host>","extra":{"token":"<REDACTED>"}}'
```

Documented gotcha: env-var connections are **not shown in the UI and not listed by
`airflow connections list`** — they resolve at runtime on the worker. Do not conclude a connection
is missing because `connections list` is empty.

Lookup order: environment variables -> configured secrets backend -> metadata DB. For anything
beyond local dev, use a secrets backend (`airflow providers secrets` lists what's available) so no
token ever lands in the metadata DB or in shell history.

`airflow connections test <conn_id>` exists but is **disabled by default** for security and must be
explicitly enabled. There is also `POST /connections/enqueue-test` for a worker-side async test.

For us: today `cosmos_dag.py` doesn't use an Airflow Connection at all — Cosmos reads the generated
`workspaces/<ws>/dbt/profiles.yml`, and the BashOperator stages read the same env our CLIs read. An
Airflow Connection becomes worth adding only when a task uses a Databricks *operator* rather than
shelling to our CLI. Ladder rung: don't add it before then.

Related: `--show-values` on `connections list` and `config list` prints secrets. Per this repo's
secret-display hard stop, always pass `--hide-sensitive` and never paste that output into a
transcript.

---

## 6. Astro CLI

Local project lifecycle:

| Command | Role |
| --- | --- |
| `astro dev init` | scaffold: `dags/`, `include/`, `tests/`, `requirements.txt`, `packages.txt`, `Dockerfile`, `airflow_settings.yaml`, `.env` |
| `astro dev start` | build image, start 4 containers (scheduler, api-server/webserver, triggerer, postgres); UI at `localhost:8080`, default `admin`/`admin` |
| `astro dev start --standalone` | run Airflow on the host, no Docker |
| `astro dev restart` | rebuild + restart (needed after `requirements.txt` / `airflow_settings.yaml` changes) |
| `astro dev stop` | stop, **keep** the metadata DB |
| `astro dev kill` | hard reset, **delete** local metadata |
| `astro dev logs` | scheduler/webserver/triggerer logs |
| `astro dev ps` | container status |
| `astro dev run <airflow args>` | run any Airflow CLI command in the container, e.g. `astro dev run dags list-import-errors` |
| `astro dev bash` | shell into a container |
| `astro dev parse` | parse/render check of all DAGs. Docker required, running Airflow not. |
| `astro dev pytest` | run `tests/` with pytest inside the image (default test asserts unique dag_ids, no cycles, imports succeed) |
| `astro dev upgrade-test` | pre-upgrade compatibility report (`--version-test`, `--dag-test`, `--deployment-id`) |
| `astro deploy` | push image + DAGs to an Astro Deployment (`astro login`, `astro deployment ...` for targets) |

DAG code edits hot-reload into a running environment; everything else needs `astro dev restart`.

Our mapping: `astro dev parse` is the DAG-parse gate, `astro dev run backfill create ...` is how a
backfill is issued against the local runtime, and `astro dev run dags test ...` is rung 3 of the dev
loop inside the container where `uv` and the repo actually exist.

---

## 7. REST API vs CLI

Rule of thumb for our platform: **the CLI needs to be co-located with (or shelled into) the Airflow
deployment; the REST API does not.** So:

| Situation | Use |
| --- | --- |
| Local dev on this box | CLI, via `astro dev run ...` |
| Our platform triggering a run on a remote/managed Airflow (Astro, MWAA, Composer) | REST API |
| Workflow-guard health polling | REST API (`/api/v2/monitor/health`) — no exec access needed |
| Anything inside a CI container that already has the image | CLI |

Airflow 3 auth is JWT, not Airflow 2's basic auth:

```
curl -X POST http://localhost:8080/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "<user>", "password": "<REDACTED>"}'
# -> {"access_token": "<JWT>"}

curl -X GET http://localhost:8080/api/v2/dags \
  -H "Authorization: Bearer <JWT>"
```

The `/auth/token` endpoint is supplied by the configured auth manager (SimpleAuthManager for dev,
FabAuthManager or a provider-specific one otherwise), so the exact credential flow is
deployment-specific. Note the `/api/v2/` prefix — Airflow 3's API is v2, served by `api-server`,
not the 2.x `webserver`.

Useful endpoints for us: `POST /api/v2/dags/{dag_id}/dagRuns` (trigger with `conf` — the REST
equivalent of our backfill seam), `GET .../dagRuns` (run history),
`GET /api/v2/monitor/health`.

Do not shell to the CLI from our Python when the API answers the same question — a subprocess to a
container we may not be inside is strictly worse than an HTTP GET.

---

## 8. Operational surface: what workflow_guard should check

Cheapest to most expensive, all read-only:

1. **Metadata DB reachable** — `airflow db check`, or the `metadatabase` field of the health JSON.
2. **Scheduler alive** — `airflow jobs check --job-type SchedulerJob --local`
   (`--allow-multiple --limit 100` for an HA scheduler pair), or:

   ```
   GET /api/v2/monitor/health
   {"metadatabase":{"status":"healthy"},
    "scheduler":{"status":"healthy","latest_scheduler_heartbeat":"..."},
    "triggerer":{"status":"healthy","latest_triggerer_heartbeat":"..."},
    "dag_processor":{"status":"healthy","latest_dag_processor_heartbeat":"..."}}
   ```

   A component is `unhealthy` if its heartbeat is older than the threshold (default 30s). There is
   also a dedicated scheduler health server on port 8974 (`[scheduler] enable_health_check = True`)
   returning 200/503.
3. **DAG parses** — `airflow dags list-import-errors` non-empty is an ERROR. This is our highest-risk
   check: our DAG module imports the repo, so any repo-side import break silently un-schedules the
   pipeline.
4. **DAG not accidentally paused** — `airflow dags details --dag-id <id> -o json` (`is_paused`). A
   paused DAG fails silently and forever; it looks identical to "no data changed".
5. **Last run succeeded** — `airflow dags list-runs --dag-id <id> --state failed`, or
   `dags state <dag_id> <run_id>`.
6. **Stuck / late** — compare `dags next-execution` against wall clock; a scheduled run that never
   appeared is a scheduler or parse problem, not a task problem.
7. **Why is it stuck** — `airflow tasks failed-deps` (pool exhausted, upstream failed,
   `depends_on_past`) and `tasks states-for-dag-run`.
8. **Metadata growth** — `airflow db clean --clean-before-timestamp <ts> --dry-run` before doing it
   for real; a per-workspace nightly DAG across a fleet grows this table fast.

The four that map to our existing guard-error vocabulary (stall / retry / unsupported-cmd /
incomplete) are 2, 3, 4, and 6. Item 4 in particular has no analogue in our current guard and is the
classic silent failure.

---

## 9. Platform integration map

Spec `2026-08-05-cloud-first-restructure-design.md` §8 phase -> exact commands.

| Spec §8 item | Our code | Airflow / Astro commands |
| --- | --- | --- |
| One DAG per workspace pipeline | `airflow_dag.build_dag(workspace=...)` over `STAGES` | `astro dev run dags list`, `dags details --dag-id autoresearch_medallion_pipeline` |
| Hashed cron offset (no 2am stampede) | `hashed_schedule()`, `HASHED_SCHEDULE` | `dags next-execution --dag-id <id>` to prove the offset |
| dbt build via Cosmos | `cosmos_dag.build_dbt_tasks()` (`DbtBuildLocalOperator`, `DBT_RUNNER`) | `uv run generate-dbt-project --workspace <ws>` first (parse-time requirement), then `astro dev run tasks test <dag> dbt_build <date>` |
| Remote-execution gate | `check_remote_approval()` inside `build_dbt_tasks` | set `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` in the **scheduler's** env (`.env` for Astro); otherwise the DAG raises `SystemExit` at parse and shows in `dags list-import-errors` |
| WAP gold swap | `build_publish_gold_task()` | `tasks states-for-dag-run` to confirm `publish_gold` ran only after `dbt_build`; `tasks clear -t publish_gold` to retry the swap alone |
| Backfill seam (Airflow 3 native) | DAG `params` + `build_backfill_task()` | `dags trigger -c '{"event_time_start":"...","event_time_end":"..."}'` for a bounded single replay; `backfill create --from-date --to-date --reprocess-behavior failed --max-active-runs 3 --run-backwards` for partition-aligned range replay |
| Backfill cost cap | not wired yet | `airflow pools set backfill 2 "bounded replay"` + backfill `--max-active-runs` |
| Backfill honest degradation | `DEGRADED:` echo in `backfill_command()` | visible in the `dbt_backfill` task log; verify the rendered command with `tasks render <dag> dbt_backfill <date>` |
| Airflow 3 asset-aware scheduling | `stage_assets()` -> `outlets` | `airflow assets list` / `assets details` |
| Failure alerting | `_notify_failure` on_failure_callback | set `AUTORESEARCH_ALERT_WEBHOOK_URL`; fires only after retries are exhausted |
| Catchup off, backfill deliberate | `catchup=False`, fixed `start_date` | never `--reprocess-behavior completed` on a range you haven't checked |
| DAG parse gate (CI) | — | `astro dev parse`, `astro dev pytest`, `astro dev run dags list-import-errors` |
| Deploy | — | `astro login`, `astro deploy` (or image push to MWAA/Composer) |
| Readiness reporting | `core/platform_readiness.py` `_check_airflow()` | `uv run check-platform-readiness`; `airflow version`, `airflow providers list` inside the runtime |

### Known gaps in our wiring (not fixed here)

1. No `backfill` pool is emitted, so a large replay can starve the nightly run. The research names
   pools + `priority_weight` as the fix.
2. `build_backfill_task()` is a leaf task on the same DAG; a true partition-aligned
   `backfill create` would need a partition-aware timetable (one run per partition), which our
   fixed daily cron does not express.
3. Any of our docs/runbooks saying `airflow dags backfill` are stale for Airflow 3.
4. Nothing checks `is_paused`, which is the quietest way for this pipeline to die.

---

## Sources

- Airflow CLI + env var reference (3.3.0):
  https://airflow.apache.org/docs/apache-airflow/stable/cli-and-env-variables-ref.html
- Backfill (core concepts):
  https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/backfill.html
- Dag Runs: https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.html
- Debugging Dags: https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/debug.html
- Managing Connections:
  https://airflow.apache.org/docs/apache-airflow/stable/howto/connection.html
- Checking Airflow Health Status:
  https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/logging-monitoring/check-health.html
- REST API auth (JWT): https://airflow.apache.org/docs/apache-airflow/stable/security/api.html
- Using the CLI: https://airflow.apache.org/docs/apache-airflow/stable/howto/usage-cli.html
- Astro CLI reference: https://www.astronomer.io/docs/astro/cli/reference/
- Run Airflow locally (Astro CLI): https://www.astronomer.io/docs/astro/cli/run-airflow-locally
- Test your Astro project locally:
  https://www.astronomer.io/docs/astro/cli/test-your-astro-project-locally
- Internal: `docs/reference/pipeline_practices_gap_research.md` §1 (backfill), §2 (Cosmos)
- Internal: `docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md` §8

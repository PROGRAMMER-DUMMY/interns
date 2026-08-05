# Databricks CLI Reference (operator-grade)

Probed for real on this machine, 2026-08-05. `databricks --version` -> **v1.7.0** (the Go-based
unified CLI, installed via WinGet). Method: `--help` on every group and on the commands that
matter, plus harmless reads (`current-user me`, `catalogs list`, `warehouses list`,
`auth describe`, a locally rendered `bundle init dbt-sql` + `bundle validate`). Nothing was
created, modified, or deleted in the workspace.

**Redaction.** Hosts, tokens, account/workspace IDs, and profile hosts are written as
`<host-redacted>` / `<token-redacted>` / `<id-redacted>` throughout. Auth state is reported as
counts and names only. Never paste `auth describe --sensitive`, `auth token`, or
`secrets get-secret` output into chat, a report, or a commit.

Primary docs: https://docs.databricks.com/aws/en/dev-tools/cli/

---

## 1. Command-group inventory

`databricks --help` lists 90+ groups. The full list is reproducible on demand; below is the
inventory that matters, with a relevance flag for this platform.

Relevance: **CORE** = the cloud-first spine depends on it; **USE** = useful operationally;
**SKIP** = out of scope today.

| Group | Role (one line) | Relevance |
|---|---|---|
| `auth` | login / logout / describe / profiles / switch / token. Owns `~/.databrickscfg` and the OAuth token cache. | CORE |
| `configure` | Non-interactive profile writer; reads a PAT from stdin, host from `--host`/`DATABRICKS_HOST`. | CORE |
| `bundle` | Declarative Automation Bundles (DABs): init/validate/plan/deploy/run/destroy/summary/generate/sync/deployment/schema/open. | CORE (see S7) |
| `sync` | One-way local dir -> workspace dir sync, incremental, `--watch`, include/exclude globs. | CORE |
| `api` | Raw REST escape hatch: `get/post/put/patch/delete/head PATH [--json @file]`. | CORE |
| `catalogs` | UC catalog CRUD (`create/get/list/update/delete`). | CORE |
| `schemas` | UC schema CRUD. | CORE |
| `volumes` | UC volume CRUD (`create/read/list/update/delete`), MANAGED or EXTERNAL. | CORE |
| `external-locations` | Bind a cloud storage URL to a storage credential; `--skip-validation` available. | CORE |
| `storage-credentials` | Cloud auth objects (IAM role / SP / SA). Has a `validate` subcommand. | CORE |
| `grants` | `get` / `get-effective` / `update` permissions on any securable. | CORE |
| `warehouses` | DBSQL warehouse CRUD + `start`/`stop` + permissions + workspace warehouse config. | CORE |
| `jobs` | Job CRUD, `run-now`, `submit` (one-time), `get-run`, `get-run-output`, `repair-run`, `list-runs`. | CORE |
| `pipelines` | Lakeflow Spark Declarative Pipelines. v1.7.0 has BOTH a project-style front end (`init/deploy/run/dry-run/logs/history/stop/destroy/generate`) and the raw API (`create/update/start-update/list-updates/list-pipeline-events`). | CORE |
| `query-history` | `list` past queries w/ `--include-metrics`, `--max-results`, `--page-token`. Cost/perf attribution. | CORE |
| `workspace` | Workspace object CRUD + `import-dir` / `export-dir` (notebooks lose extensions on import). | USE |
| `fs` | `cat/cp/ls/mkdir/rm` against DBFS and UC Volumes (`dbfs:` scheme required). | USE |
| `secrets` | Scope + secret + ACL management. Credential *references* live here. | CORE |
| `current-user` | `me` - the cheapest liveness/identity probe. | CORE |
| `system-schemas` | Enable `system.billing` / `system.query` per metastore (admin-only, one-time). | CORE |
| `repos` / `git-credentials` | Databricks Repos + git PAT registration. Alternative to `sync` for code delivery. | USE |
| `clusters` / `cluster-policies` / `instance-pools` / `libraries` | Classic compute. Only needed when a job exceeds the serverless envelope. | USE |
| `tables` / `table-constraints` / `functions` | UC object reads; `tables get` is the honest existence check. | USE |
| `permissions` | Non-UC object ACLs (jobs, warehouses, notebooks). Distinct from `grants`. | USE |
| `metastores` / `workspace-bindings` / `system-schemas` | Metastore-level admin. | USE |
| `queries` / `alerts*` / `dashboards` / `lakeview` | DBSQL saved objects + AI/BI dashboards. Our dashboard is Dash-based, so mostly SKIP. | SKIP |
| `data-quality` / `quality-monitors` | UC-native DQ monitors. Spec S7 chose dbt tests as primary, Lakeflow expectations at bronze - revisit only if that flips. | SKIP-for-now |
| `experiments` / `model-registry` / `registered-models` / `model-versions` | MLflow. Used by the existing telemetry path, not by the KPI spine. | USE |
| `serving-endpoints` / `vector-search-*` / `ai-search` / `genie` / `knowledge-assistants` | ML serving + AI. | SKIP |
| `labs` | Installer for Databricks Labs projects (UCX, dqx, etc.). Not a first-party API. | SKIP |
| `aitools` | Ships Databricks skills/plugins into coding agents. | SKIP |
| `account` | Account-scoped commands (needs `--account-id` / account host). | SKIP |
| `apps`, `clean-room*`, `marketplace*`, `delta-sharing (providers/recipients/shares)`, `postgres`/`database`/`psql`, `environments`, `tag-policies` | Feature areas the platform does not touch. | SKIP |

Global flags on every command: `--debug`, `-o/--output text|json`, `-p/--profile`,
`-t/--target` (bundle target).

---

## 2. Auth mechanics

Docs: https://docs.databricks.com/aws/en/dev-tools/auth/unified-auth
and https://docs.databricks.com/aws/en/dev-tools/auth/env-vars

### 2.1 Resolution order (unified auth, shared by CLI and Python SDK)

Per method, the tool tries: **auth type** first (PAT -> OAuth M2M -> OAuth U2M), and within each,
credentials are looked up in this order:

1. Explicit config fields passed in code / on the command line (`--host`, `--profile`,
   `WorkspaceClient(host=..., token=...)`).
2. Environment variables.
3. The `~/.databrickscfg` profile (`DEFAULT` unless `DATABRICKS_CONFIG_PROFILE` or `--profile`).

Because the CLI and the Python SDK share this chain, **a machine that works for `databricks
catalogs list` also works for `WorkspaceClient()`** - which is exactly what
`core/execution/databricks_client.py:get_client()` relies on when it falls back to
`WorkspaceClient(profile=...)`.

### 2.2 Environment variables

| Variable | Meaning |
|---|---|
| `DATABRICKS_HOST` | Workspace URL (or account URL when account-scoped) |
| `DATABRICKS_TOKEN` | PAT (or OAuth access token) |
| `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` | OAuth M2M service principal |
| `DATABRICKS_ACCOUNT_ID` | Account-level operations |
| `DATABRICKS_CONFIG_FILE` | Alternate path to `.databrickscfg` |
| `DATABRICKS_CONFIG_PROFILE` | Which profile to use |
| `DATABRICKS_AUTH_TYPE` | Force one method (`pat`, `oauth-m2m`, `databricks-cli`, ...) |
| `DATABRICKS_CLUSTER_ID`, `DATABRICKS_SERVERLESS_COMPUTE_ID` | Default compute |
| `DATABRICKS_OIDC_TOKEN_ENV`, `DATABRICKS_OIDC_TOKEN_FILEPATH` | Workload identity federation (GitHub Actions OIDC etc.) |

Our repo reads `DATABRICKS_HOST` / `DATABRICKS_TOKEN` by default but makes the names
configurable per enterprise (`core/config.py`: `block.get("host_env", "DATABRICKS_HOST")`).
That is a superset of the CLI's behaviour and is fine - but it means a workspace can be
configured to read a non-standard env var that the CLI will never see. Readiness must report
which env var it actually consulted.

### 2.3 The four auth states, and what readiness should say

| State | How to detect (read-only) | What `check-platform-readiness` should report |
|---|---|---|
| **OAuth U2M via CLI** (this machine's `DEFAULT`) | `auth describe -o json` -> `details.auth_type == "databricks-cli"`; `auth profiles` shows `valid: true` | `ready`. Note the token is cached in `~/.databricks/token-cache.json` and expires - fine on a workstation, **not** valid for Airflow workers or CI. |
| **PAT** | `auth_type == "pat"`, host+token from env or profile | `ready`, but flag expiry. Existing 401 hint in `health_check()` is correct. |
| **OAuth M2M (service principal)** | `DATABRICKS_CLIENT_ID`/`SECRET` present, `auth_type == "oauth-m2m"` | `ready` and **preferred for prod**. `databricks auth token` explicitly does NOT support M2M - don't build a token-echo path. |
| **Not configured / stale** | `auth profiles` -> `valid: false`, or `current-user me` non-zero exit | `blocked` with the exact remedy: `databricks auth login --host <host>` or set `DATABRICKS_CLIENT_ID/SECRET`. |

Observed on this machine (summary only): **2 profiles**, cloud `aws`, auth type
`databricks-cli` for both; 1 valid, 1 invalid. `current-user me` succeeds; 5 catalogs listed;
1 SQL warehouse, currently `STOPPED`.

### 2.4 A real failure mode readiness should own: ambiguous profile match

Reproduced live. With two `.databrickscfg` profiles pointing at the same host, any
bundle command that resolves auth from `workspace.host` fails:

```
Error: cannot resolve bundle auth configuration: resolve: <host-redacted>
multiple profiles matched: DEFAULT, <profile-2>: please set DATABRICKS_CONFIG_PROFILE
or provide --profile flag to specify one.
```

Exit code 1. Adding `--profile DEFAULT` makes the identical command exit 0 with
`Validation OK!`. **A stale second profile is enough to break every bundle command while
`current-user me` still passes.** `check-platform-readiness` should count profiles per host
and warn when a host is claimed by more than one, because the SDK path
(`WorkspaceClient(profile="DEFAULT")`) is immune and will happily disagree with the CLI -
which is failure mode #3 in the cloud-first spec ("readiness checks disagreeing with the
Databricks CLI").

### 2.5 Auth commands

```
databricks auth login --host <host>              # interactive OAuth U2M, writes a profile
databricks auth login --host "<host>?w=<id>"     # quote the URL: '?' is a shell metachar
databricks auth describe -o json                 # WHICH credential and WHERE it came from
databricks auth describe --sensitive             # NEVER run in an agent session
databricks auth profiles --skip-validate -o json # names only; validation costs one API call each
databricks auth switch                           # set default_profile in [__settings__]
databricks auth logout [PROFILE] [--delete]      # clears the OAuth token cache
databricks configure --host <host>               # non-interactive: reads PAT from stdin
```

---

## 3. Unity Catalog operations

Every UC create command accepts positional args for the common fields plus
`--json '{...}'` or `--json @path/to/body.json` for the full request body. The `--json` form is
the one to generate from a plan file - it is the same JSON the REST API takes, so it round-trips
with the SDK's dataclasses.

```bash
# storage credential (account admin; AWS role ARN goes in the JSON body)
databricks storage-credentials create <name> --json @cred.json
databricks storage-credentials validate --json '{"storage_credential_name":"<name>","url":"s3://<bucket>/"}'

# external location  (positional: NAME URL CREDENTIAL_NAME)
databricks external-locations create <name> s3://<bucket>/<prefix>/ <credential_name> \
  --comment "..." [--read-only] [--skip-validation]

# catalog
databricks catalogs create <catalog> [--storage-root s3://...] [--comment ...]

# schema        (positional: NAME CATALOG_NAME  -- note the order)
databricks schemas create bronze <catalog>

# volume        (positional: CATALOG SCHEMA NAME VOLUME_TYPE in {MANAGED,EXTERNAL})
databricks volumes create <catalog> bronze _checkpoints MANAGED

# grants        (positional: SECURABLE_TYPE FULL_NAME; changes go in --json)
databricks grants update catalog <catalog> --json \
  '{"changes":[{"principal":"<group>","add":["USE_CATALOG","USE_SCHEMA","SELECT"]}]}'
databricks grants get-effective catalog <catalog> --principal <group> -o json

# existence checks (exit 1 + "does not exist" when absent)
databricks catalogs get <catalog>          -o json
databricks schemas  get <catalog>.<schema> -o json
databricks volumes  read  <catalog>.<schema>.<volume> -o json
databricks external-locations get <name>   -o json
```

Mapping to `core/provisioning/plan.py` step kinds:

| Plan step `kind` | CLI equivalent | Idempotent check |
|---|---|---|
| `create_external_location` | `external-locations create NAME URL CRED` | `external-locations get NAME` |
| `create_catalog` | `catalogs create NAME` | `catalogs get NAME` |
| `create_schema` | `schemas create NAME CATALOG` | `schemas get CATALOG.NAME` |
| `create_volume` | `volumes create CAT SCH NAME MANAGED` | `volumes read CAT.SCH.NAME` |
| `grant` | `grants update catalog NAME --json '{"changes":[...]}'` | `grants get-effective` |
| `blocked_destructive` | (no command - correct: the CLI has `delete`/`update`, the planner must never reach for them) | n/a |

The plan's five kinds are a 1:1 subset of the CLI surface, which is a good sign: the additive
provisioner did not invent an abstraction the platform can't explain to an operator.

`system.billing` / `system.query` (which `_check_cost_telemetry` probes) are enabled with
`databricks system-schemas enable <metastore-id> billing` - the hint already printed by
`core/platform_readiness.py` is correct as written.

---

## 4. Jobs and Lakeflow

Docs: https://docs.databricks.com/aws/en/dev-tools/cli/ (jobs group) and
https://docs.databricks.com/aws/en/jobs/

### 4.1 Jobs

```bash
databricks jobs create --json @job.json -o json          # -> {"job_id": ...}
databricks jobs run-now <JOB_ID> [--json '{"job_parameters":{...}}'] \
    [--idempotency-token <tok>] [--no-wait] [--timeout 20m]
databricks jobs submit --json @run.json --run-name <name> \
    [--idempotency-token <tok>] [--no-wait]
databricks jobs get-run <RUN_ID> -o json
databricks jobs get-run-output <RUN_ID> -o json
databricks jobs list --limit 25 --offset 0 --expand-tasks -o json
databricks jobs list-runs --job-id <id> -o json
databricks jobs repair-run <RUN_ID> --json '{"rerun_tasks":["dbt"]}'
databricks jobs cancel-run <RUN_ID>
```

Two behaviours worth designing around:

- **`run-now` and `submit` block by default** until TERMINATED/SKIPPED, with a 20-minute
  default `--timeout`; `--no-wait` returns the run id immediately. Our
  `DatabricksClient.poll_job_run()` reimplements that wait loop in Python because it needs a
  structured `(state, log)` tuple - reasonable, keep it.
- **`--idempotency-token`** is the primitive that makes re-running a governed command safe
  after a partial failure. The repo's `record_idempotent=True` in
  `core/provisioning/cli.py` is a *local* replay guard; it does not stop a duplicate remote
  run. Any generated job trigger should carry an idempotency token derived from the workspace
  + plan hash.

A dbt task inside a job (from the real rendered `dbt-sql` template, S7.4):

```yaml
tasks:
  - task_key: dbt
    environment_key: default
    dbt_task:
      project_directory: ../
      profiles_directory: dbt_profiles/
      commands:
        - 'dbt deps --target=${bundle.target}'
        - 'dbt run  --target=${bundle.target}'
environments:
  - environment_key: default
    spec:
      environment_version: "4"
      dependencies: ["dbt-databricks>=1.8.0,<2.0.0"]
```

This is the serverless dbt task shape (`environments` + `environment_key`, no cluster block) -
it is what spec S3's "serverless-first" and S8's "dbt build (Cosmos)" would target if the
platform ever ran dbt on Databricks rather than from an Airflow worker.

### 4.2 Lakeflow pipelines

v1.7.0's `pipelines` group is two APIs in one:

- Project front end: `init`, `deploy`, `run`, `dry-run`, `stop`, `logs`, `history`,
  `generate`, `destroy`, `open`. It even takes `--var` like bundles. This is the newer
  "pipelines project" workflow.
- Raw API: `create`, `update`, `get`, `delete`, `start-update`, `get-update`,
  `list-updates`, `list-pipelines`, `list-pipeline-events`, `clone`, `apply-environment`.

```bash
databricks pipelines create --json @pipeline.json -o json
databricks pipelines start-update <PIPELINE_ID> [--full-refresh] -o json
databricks pipelines get-update <PIPELINE_ID> <UPDATE_ID> -o json
databricks pipelines list-pipeline-events <PIPELINE_ID> -o json   # DQ expectation results live here
databricks pipelines dry-run                                       # validate the graph, run nothing
```

`dry-run` is the interesting one for us: it validates the pipeline graph without executing,
which is the Lakeflow analogue of `bundle validate` and belongs in a pre-deploy gate.
`list-pipeline-events` is where Lakeflow **expectation** results surface - the spec's
"bronze boundary: Databricks-native expectations" (S7) reads its evidence from there.

### 4.3 Cost and performance attribution

```bash
databricks query-history list --include-metrics --max-results 100 -o json
databricks query-history list --page-token <token> -o json
```

The response carries a `next_page_token`; the CLI does **not** auto-paginate this command, so
a collector must loop. For attribution the spec (S6.6) enables `query_tags` on generated dbt
models; those tags come back in query history and in `system.query.history`, and warehouse
spend comes from `system.billing.usage`. The system-schema route is preferable for a
long-horizon ledger (query history has a retention window); the CLI route is preferable for
"what did the run I just triggered cost".

---

## 5. Shipping code: `sync`, `workspace`, `fs`

```bash
databricks sync ./workspaces/<ws>/dbt /Workspace/Users/<user>/<ws>/dbt \
  --exclude 'target/**' --exclude '.venv/**' [--full] [--dry-run] [--watch]

databricks workspace import-dir ./local /Workspace/path --overwrite
databricks workspace export-dir /Workspace/path ./local

databricks fs cp -r --overwrite ./ingestion dbfs:/Volumes/<cat>/bronze/code/
databricks fs ls dbfs:/Volumes/<cat>/bronze/_checkpoints
```

Differences that matter:

- `sync` is **incremental and stateful** (it keeps a local snapshot to compute deltas) and has
  `--dry-run`. It is the right tool for pushing the generated `dbt/` and `ingestion/` trees
  repeatedly during a run.
- `workspace import-dir` is a plain recursive upload and **strips notebook extensions**
  (`.py`, `.sql`, `.ipynb`...) - fine for notebooks, wrong for a dbt project where
  `models/foo.sql` must stay `foo.sql`. Prefer `sync` for dbt.
- `fs cp` requires the `dbfs:` scheme even for `/Volumes` paths, and has `--concurrency`
  (default 8). Use it for data/artifact files, not code.
- `repos create`/`update` is the third option: have the workspace pull from git instead of
  pushing bytes. Better provenance, but needs `git-credentials create` and a reachable remote.

---

## 6. `databricks api` - the escape hatch

```bash
databricks api get /api/2.1/unity-catalog/catalogs -o json
databricks api post /api/2.2/jobs/create --json @job.json
databricks api patch /api/2.1/unity-catalog/schemas/<full_name> --json '{"comment":"..."}'
databricks api get /api/2.0/preview/... --account          # account-scoped routing
```

Use it when a REST endpoint is newer than the CLI's typed surface, or is in a preview that
never got a group. It reuses the same resolved auth, so it is safe in a script that already
authenticated. Cost: no argument validation, no typed errors, and the URL path is a version
string you now own. Treat it as a documented exception, never the default.

---

## 7. Databricks Asset Bundles (DABs), in depth

Docs: https://docs.databricks.com/aws/en/dev-tools/bundles/ ,
settings https://docs.databricks.com/aws/en/dev-tools/bundles/settings ,
resources https://docs.databricks.com/aws/en/dev-tools/bundles/resources ,
deployment modes https://docs.databricks.com/aws/en/dev-tools/bundles/deployment-modes

### 7.1 Lifecycle

```bash
databricks bundle init [TEMPLATE] [--config-file params.json] [--output-dir DIR]
databricks bundle validate [--target prod] [--strict]        # syntax/schema/permissions
databricks bundle plan     [--select jobs.my_job]            # what deploy WOULD change
databricks bundle deploy   [--target prod] [--auto-approve] [--fail-on-active-runs]
                           [--select ...] [--plan plan.json] [--force-lock]
databricks bundle run <KEY> [-- --arg1 v1]                   # job / pipeline update / app
databricks bundle summary [--force-pull]                     # deployed resources + URLs
databricks bundle destroy [--target dev] [--auto-approve]
databricks bundle sync [--watch] [--dry-run]                 # files only, no resource update
databricks bundle generate job --existing-job-id <id> --key <k> [--bind]
databricks bundle deployment bind <key> <remote-id> | unbind <key>
databricks bundle schema                                     # JSON Schema for editor validation
```

Built-in templates: `default-python`, `default-sql`, `default-minimal`, `default-scala`,
**`dbt-sql`**, `mlops-stacks`, `pydabs` (resources defined in Python instead of YAML).
`TEMPLATE_PATH` also accepts a local directory or a git URL - so a platform can ship its own
template and `bundle init` it.

`bundle plan` + `deploy --plan plan.json` is a genuine plan/apply split (direct deployment
engine), which is the same shape as our `plan-provisioning` / `apply-provisioning` pair.

### 7.2 `databricks.yml` structure

Top-level mappings: `bundle` (name, uuid), `variables`, `workspace` (host, profile, root_path,
artifact_path, state_path), `artifacts`, `include` (globs pulling in `resources/*.yml`),
`resources`, `sync` (include/exclude), `targets`, `permissions`, `run_as`, `presets`, `scripts`.

`resources:` supported types now include, per the resources doc: `job`, `pipeline`, `cluster`,
`sql_warehouse`, `dashboard`, `alert`, `app`, `experiment`, `model_serving_endpoint`,
`registered_model`, `quality_monitor`, `secret_scope`, `vector_search_*`, the Lakebase/Postgres
family - **and the Unity Catalog objects: `catalog` (direct deployment engine required),
`schema`, `volume`, `external_location`.** UC resources take a `lifecycle` block controlling
deploy/destroy behaviour.

That last point is the crux of the DAB-vs-SDK question and is new relative to how the repo's
provisioner was designed: a bundle can now declare the catalog/schema/volume/external-location
set that `core/provisioning/plan.py` builds step-by-step.

### 7.3 Targets and modes

`mode: development` (verified live): prefixes non-file resources with
`[dev <short_name>]`, tags them `dev`, sets pipelines `development: true`, pauses all schedules
and triggers, allows `--cluster-id` override, disables the deployment lock. `bundle summary`
on the rendered template showed the job as `[dev <user-redacted>] probe_dbt_job` - the prefix
is real, not documentation.

`mode: production`: requires pipelines `development: false`, optionally validates the git
branch (`--force` overrides), and when not running as a service principal it validates that
root/artifact/state paths are not user-specific and that `run_as` + `permissions` are explicit.

This maps cleanly onto the spec's catalog-per-env (`<ws>_dev` / `<ws>_prod`, S8) - targets are
the natural home for that split.

### 7.4 Real rendered `dbt-sql` bundle (probed, host redacted)

`databricks bundle init dbt-sql --config-file cfg.json` produced, locally, with no remote calls:

```
databricks.yml
dbt_project.yml
profile_template.yml
dbt_profiles/profiles.yml
resources/<name>.job.yml
src/{models,seeds,snapshots,macros,tests,analyses}/
requirements-dev.txt  README.md  .vscode/  .gitignore
```

```yaml
# databricks.yml (verbatim shape, host redacted)
bundle:
  name: probe_dbt
  uuid: <uuid>
include:
  - resources/*.yml
targets:
  dev:
    mode: development
    default: true
    workspace:
      host: <host-redacted>
  prod:
    mode: production
    workspace:
      host: <host-redacted>
      root_path: /Workspace/Users/<user-redacted>/.bundle/${bundle.name}/${bundle.target}
    permissions:
      - user_name: <user-redacted>
        level: CAN_MANAGE
```

Note what the template does with dbt credentials: `dbt_profiles/profiles.yml` uses
`host: "{{ env_var('DBT_HOST') }}"` and `token: "{{ env_var('DBT_ACCESS_TOKEN') }}"`, which
Databricks injects into the dbt task at run time. Generated dbt profiles should copy that
pattern - never a literal token, which matches the repo's credential-reference rule.

Caveat found live: the template hardcodes `workspace.host` into `databricks.yml`, and with two
local profiles on the same host every bundle command fails until `--profile` is supplied
(S2.4). If the platform ever emits a `databricks.yml`, set `workspace.profile` explicitly
rather than relying on host matching.

### 7.5 Should our provisioning/orchestration emit a DAB instead of raw SDK calls?

Honest assessment, split by job:

| Platform job | DAB fit | Verdict |
|---|---|---|
| Create catalog/schema/volume/external-location once, additively, with per-step existence checks and a `blocked_destructive` gate | Possible now (`resources.catalog/schema/volume/external_location`), but DABs are **desired-state**: the value of `bundle destroy` and of drift reconciliation is exactly the destructive power spec S10 forbids. A bundle that must never delete is a bundle with its main feature disabled. | **Keep the SDK provisioner.** |
| Deploy the generated dbt job / pipeline / schedule to the workspace | This is DABs' home turf: multi-env targets, dev prefixes, paused-by-default dev schedules, `bundle plan` before apply, `bundle summary` with URLs, `bundle destroy` to clean a dev target. Hand-rolling job JSON + reset semantics against the Jobs API is strictly more code for strictly less. | **Emit a DAB** (if and when jobs run on Databricks). |
| Ship generated dbt/ingestion code to the workspace | `bundle sync` (or plain `sync`) - already the right tool either way. | CLI. |
| Trigger a run and read results back into a governed artifact | `bundle run` returns human-oriented output; the SDK returns typed run state. | **SDK** (existing `poll_job_run`). |

The complication specific to this repo: **spec D3 says Airflow/Cosmos is THE orchestrator.**
If dbt is invoked by an Airflow worker via Cosmos, there is no Databricks job to deploy, and a
DAB has almost nothing to manage - the workspace only needs a warehouse and the UC objects.
DABs become compelling only in the variant where the platform also (or instead) runs dbt as a
Databricks job, or deploys Lakeflow ingestion pipelines as managed resources. That variant is
plausible for the ingestion side (S5's Auto Loader / Lakeflow jobs are Databricks-resident by
construction), and that is where the first DAB should land.

Cost of adopting DABs anywhere: a second state system (bundle state in the workspace) beside
the repo's own contracts/evidence, a `databricks.yml` that must stay consistent with generated
artifacts, an external CLI binary in the execution path (version skew, no typed errors), and
a `destroy` verb that the safety model has to keep locked. None fatal; all real.

---

## 8. Failure modes

**Exit codes** (verified): `0` on success, `1` on any error - missing bundle root, nonexistent
catalog, unknown command group, ambiguous profile. There is no distinct code for "not found"
vs "unauthorized" vs "config error", so **any wrapper must parse stderr text to classify**,
which is brittle. This alone argues for the SDK wherever the platform needs to branch on the
failure kind (the SDK raises typed `NotFound`, `PermissionDenied`, `ResourceAlreadyExists`).

**JSON output.** `-o json` is per-command, not global-only-on-success: errors still go to
stderr as text while stdout may be empty or partial. Always check the exit code before parsing.
Verified shapes: `catalogs list -o json` -> a bare JSON **array** (not `{"catalogs":[...]}`),
each element carrying `name`, `full_name`, `owner`, `catalog_type`, `metastore_id`,
`isolation_mode`, `securable_type`, `browse_only`, timestamps. `warehouses list -o json` ->
array with `state`. `bundle validate -o json` -> `{bundle, include, resources, workspace}`.
Do not assume an envelope; probe the shape per command.

**Pagination.** Inconsistent by design:
- `jobs list` uses `--limit` / `--offset`.
- `query-history list` uses `--max-results` / `--page-token` and returns `next_page_token`;
  the CLI does not loop for you.
- `catalogs list` / `schemas list` returned complete arrays here, but on a large metastore they
  page too - do not assume a single call is complete just because it looked complete on a
  5-catalog workspace.

**Rate limits.** The platform returns HTTP 429 with `Retry-After`; the Go CLI and the Python
SDK both retry with backoff internally, so an occasional 429 is invisible. What is *not*
handled for you: a tight loop over per-object commands (e.g. one `tables get` per table across
thousands of tables) will hit limits and serialize badly. Batch through
`information_schema` on a warehouse instead - the same lesson the cardinality profiler already
learned.

**Timeouts.** `jobs run-now` / `jobs submit` default to a 20-minute wait; `auth token` to 1
hour. Long-running deploys hold a **deployment lock** (`bundle deploy --force-lock` breaks a
stale one) - an interrupted CI deploy can block the next one.

**Warehouse cold start.** `warehouses list` showed the only warehouse `STOPPED`. Any
CLI/SDK statement will auto-start it and block for the start latency; readiness should report
warehouse state so a 60-second first query is not mistaken for a hang.

---

## 9. CLI vs Python SDK: which seam for which platform job

Both share `~/.databrickscfg`, the same env vars, and the same auth chain, so this is purely a
question of which is the better *tool*, not which credentials are available.

| Job | Seam | Why |
|---|---|---|
| Liveness / identity probe | **SDK** (`current_user.me()`) - already in `health_check()` | Typed exception; no subprocess; one dependency fewer in the hot path. |
| Enumerate catalogs/schemas/tables for discovery | **SDK** | Iterators handle pagination; objects, not JSON dicts. |
| Additive UC provisioning (catalog/schema/volume/external location/grant) | **SDK** (current `core/provisioning/apply.py`) | Needs typed "already exists" vs "permission denied" branching that exit code 1 cannot express; needs per-step evidence records. |
| Destructive-op detection | **SDK** | Same reason; plus the CLI's `delete` verbs are exactly what we must never call. |
| Statement execution / reading results | **SDK** (`statement_execution`, already wrapped) | The CLI has no first-class "run this SQL and give me rows" command. |
| Multi-env deployment of jobs/pipelines/dashboards | **CLI (`bundle`)** | Desired-state diffing, dev/prod presets, plan-before-apply, resource URLs - all free. |
| Pushing generated code trees to the workspace | **CLI (`sync`)** | Incremental with a local snapshot; the SDK has no equivalent. |
| Bundle validation as a pre-deploy gate | **CLI (`bundle validate --strict`, `pipelines dry-run`)** | No SDK equivalent exists. |
| Interactive human auth setup | **CLI (`auth login`)** | Opens a browser; the SDK cannot. |
| One-off preview/new REST endpoint | **CLI (`api`)** or SDK `ApiClient.do()` | Whichever the surrounding code already uses. |
| Readiness reporting | **Both** - SDK for the probe, and read `auth profiles` for CLI-visible state | Section 2.4: SDK-only readiness will miss the ambiguous-profile failure that breaks every CLI command. |

Rule of thumb: **SDK for anything whose result becomes a governed artifact** (needs typed
errors and structured evidence). **CLI for anything whose result is a deployment**
(needs desired-state diffing) **or a file transfer**.

---

## 10. Platform integration map

Spec: `docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md`.

| Spec phase | Repo command | Databricks call today | Best seam / change |
|---|---|---|---|
| Pre-flight | `uv run check-platform-readiness` (`core/platform_readiness.py`) | SDK `current_user.me()`, `catalogs.list()`, `jobs.list()`, `warehouses.list()`; SQL probes of `system.billing.usage` / `system.query.history` | Keep SDK. **Add**: profile-ambiguity check (S2.4), warehouse `state` (S8 cold start), and which env var name was actually read (S2.2). Remedy strings should name `databricks auth login` / `system-schemas enable`. |
| P0 Measure - source declaration | `discover-external-sources`, `prepare/apply-external-source-intake` | Cloud-side listing (S3/ADLS/GCS) | Unchanged. UC-side existence facts feeding `discovery.existing_objects` should come from SDK `catalogs.get` / `external_locations.list`, not a re-scan. |
| P4 Blueprint | `prepare-blueprint`, `confirm-blueprint` | none (local artifact) | Correct - no remote call belongs before the single confirmation. |
| P5 Provision | `plan-provisioning` -> `apply-provisioning` | SDK: `catalogs.create`, `schemas.create`, `volumes.create(VolumeType.MANAGED)`, `external_locations.create`, `grants.update(PermissionsChange)` | **Keep SDK.** CLI equivalents documented in S3 for operator debugging only. Do not migrate to `resources.catalog` in a bundle: `bundle destroy` is a deletion path S10 forbids. |
| P5 Ingestion codegen | `generate-ingestion` | none (emits Auto Loader / COPY INTO / JDBC / Kafka code) | Unchanged. **Delivery** of that code is the gap: today it lands in `workspaces/<ws>/ingestion/` and nothing ships it. Add `databricks sync <ws>/ingestion /Workspace/.../ingestion`, or make it a DAB `resources.pipelines` entry if it runs as Lakeflow. |
| P5 dbt project | `core/onboarding/kpi/dbt_project_generator.py` | none | Ship with `databricks sync` (not `workspace import-dir` - it strips `.sql`). Emit profiles using `env_var('DBT_HOST')` / `env_var('DBT_ACCESS_TOKEN')` like the first-party `dbt-sql` template. |
| P5 Orchestration | `core/orchestration/{airflow_dag,cosmos_dag}.py` | Cosmos invokes dbt from the Airflow worker | No Databricks job exists in this design, so no DAB is needed. **If** a Databricks-resident variant is added, that is the first place a DAB earns its keep: `bundle deploy --target dev|prod` + `bundle run <key>`. |
| P5 Execute / results | `core/execution/backend.py`, `DatabricksClient.execute_query` | SDK `statement_execution.execute_statement` + poll | Keep. `_extract_warehouse_id()` parses `DATABRICKS_HTTP_PATH`; `warehouses list -o json` is the operator's way to find the right id. |
| Cost attribution | `reconcile-warehouse-cost`, `core/observability/cost_ledger.py` | SQL against `system.billing.usage` / `system.query.history` | Keep SQL for the durable ledger. `databricks query-history list --include-metrics` is the ad-hoc "what did this run cost" path; remember to follow `next_page_token`. |
| Deploy gates | `check-remote-execution-gate` (`deploy_gates.py` G1-G5) | env + local artifacts only | Correct. If a DAB is ever introduced, `bundle validate --strict` and `pipelines dry-run` become natural G0-style pre-deploy gates - both are read-only. |

---

## 11. Verdict: DAB vs SDK

**Do not replace the SDK provisioner with a bundle.** DABs are a desired-state deployer whose
core value - diffing, reconciling, and destroying to match a declaration - is precisely the
capability spec S10 gates. Additive-only provisioning with typed existence checks and
`blocked_destructive` records is a better fit for the SDK, and the current
`core/provisioning/` design already mirrors the CLI's own object model 1:1.

**Do reach for DABs the moment anything runs *on* Databricks rather than *against* it** -
a dbt job, a Lakeflow ingestion pipeline, a scheduled refresh. There, hand-rolled Jobs-API
JSON plus bespoke dev/prod naming is strictly more code than `targets: {dev, prod}` plus
`bundle deploy`, and `bundle plan` gives the same reviewable plan/apply split the platform
already believes in. Under the current Airflow/Cosmos design that moment has not arrived.

**Use the CLI unconditionally for**: interactive auth (`auth login`), code delivery (`sync`),
and validation gates with no SDK equivalent (`bundle validate --strict`, `pipelines dry-run`).

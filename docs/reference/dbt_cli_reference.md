# dbt CLI — Operator Reference

Ground truth for the dbt command line as this platform drives it. Everything marked
`[verified]` was run against the repo venv (`.venv/Scripts/dbt.exe`) on 2026-08-05; everything
else is cited to `docs.getdbt.com` (which serves Markdown when you append `.md` to a page URL,
e.g. `https://docs.getdbt.com/reference/node-selection/defer.md`).

Installed here `[verified: dbt --version]`:

```
dbt-core     1.11.12   (latest 1.12.0)
databricks   1.12.2
duckdb       1.10.1
spark        1.10.3
```

Version skew is real: dbt-databricks 1.12.2 runs on dbt-core 1.11.12 and the CLI warns
`At least one plugin is out of date with dbt-core`. It works, but adapter docs written for
1.12 core (e.g. the `selector:` selection method, `--hints-enabled`) do **not** all apply —
see §2 and §7.

Companion doc: `dbt_agentic_cli_reference.md` (how an agent uses dbt as a tool). This file is
the command/flag/artifact surface itself.

---

## 1. Command inventory

`[verified: dbt --help` and `dbt <cmd> --help` for all 17 subcommands + 3 sub-subcommands]

| Command | What it does | When OUR platform uses it |
|---|---|---|
| `dbt deps` | Installs packages from `packages.yml`; `--lock` writes `package-lock.yml`, `--add-package`, `--upgrade`, `--source [hub\|git\|local]` | Generation-time validation and every Cosmos run (`install_deps=True` in `cosmos_dag.build_dbt_tasks`). Our `packages.yml` is currently `packages: []`, so it is a fast no-op — keep the call anyway, dbt-expectations is planned (spec §7) |
| `dbt parse` | Parses the project, writes `manifest.json` + `perf_info.json`, executes nothing | The cheapest post-generation gate. Catches bad `ref()`, bad Jinja, bad configs with **no warehouse connection**. Should run inside `generate-dbt-project` |
| `dbt list` / `dbt ls` | Lists resources matching a selection; `--output [json\|name\|path\|selector]`, `--output-keys` | Machine-readable graph inspection without a manifest parse of our own. `dbt ls --output json --output-keys name resource_type depends_on` is the honest input for ghost-table reconcile and lineage |
| `dbt compile` | Renders executable SQL to `target/compiled/`; `--inline`, `--output [json\|text]`, `--no-introspect` | Showing the generated SQL in a KPI packet without executing it. `--no-introspect` keeps it warehouse-free when no model uses `run_query`/`statement` |
| `dbt run` | Builds models only | Not our default — see `build` |
| `dbt test` | Runs data tests only; `--store-failures` | Only when we deliberately want to re-test without rebuilding (post-WAP audit) |
| `dbt seed` | Loads `seeds/*.csv` | Unused today (no seeds generated) |
| `dbt snapshot` | Runs snapshots (SCD2) | The dbt-native SCD2 path if/when Phase 2 says "as it was then = yes" and we stop hand-emitting SCD2 |
| `dbt build` | seeds → models → snapshots → tests, **in DAG order, per node** | The production verb. A model's tests run right after that model, so a failing test SKIPs its children instead of letting bad data propagate. Already what `cosmos_dag`/`pipeline_stages`/`dbt_backfill` invoke |
| `dbt retry` | Re-executes the last invocation from its point of failure, reading `target/run_results.json` | Airflow task retry. `pipeline_stages.py` already emits `dbt retry ... \|\| dbt build ...` |
| `dbt clone` | Creates clones (Delta shallow clone on Databricks) of nodes from a `--state` manifest | Blue-green / WAP alternative: clone live gold into a staging schema instead of rebuilding. Requires `--state` |
| `dbt docs generate` | Writes `catalog.json` (+ `index.html`, `static_index.html` with `--static`); `--no-compile`, `--empty-catalog` | Lineage artifact for the blueprint and for the dashboard exposure graph. **Queries `information_schema`** — not free, not offline |
| `dbt docs serve` | Local docs webserver (`--host`, `--port`, `--no-browser`) | Dev only. Never in a DAG |
| `dbt source freshness` | Runs freshness checks, writes `target/sources.json` | Bronze-boundary freshness gate (spec §7) and the `source_status:fresher+` selector |
| `dbt run-operation <macro>` | Runs one macro; `--args YAML` | The WAP publish step: `dbt run-operation publish_gold` (`cosmos_dag.publish_gold_command`) |
| `dbt debug` | Config + dependency + connection check; `--connection` (connection only), `--config-dir` | Readiness check for a cloud workspace. The only command in this doc that may touch a live warehouse during exploration |
| `dbt init [PROJECT_NAME]` | Scaffolds a project (`-s/--skip-profile-setup`) | Never — we generate the project ourselves (`dbt_project_generator.py`) |
| `dbt clean` | Deletes `clean-targets`; `--clean-project-files-only/--no-...` | Only to force a cold parse. Note it deletes `target/`, which destroys `run_results.json` and therefore `dbt retry` |

### Flags that exist on every command (global)

`--project-dir`, `--profiles-dir`, `--target/-t`, `--profile`, `--vars YAML`, `--target-path`,
`--threads`, `--log-format [text|debug|json|default]` (+ `--log-format-file`), `--log-level`
(+ `--log-level-file`), `--log-path`, `--quiet/-q`, `--no-print`, `--printer-width`,
`--partial-parse/--no-partial-parse`, `--static-parser`, `--use-experimental-parser`,
`--populate-cache`, `--cache-selected-only`, `--fail-fast/-x`, `--version-check`,
`--warn-error`, `--warn-error-options`, `--write-json/--no-write-json`, `--send-anonymous-usage-stats`,
`--state`, `--defer`, `--defer-state`, `--favor-state`, `--indirect-selection`,
`--record-timing-info/-r`, `--show-all-deprecations`, `--upload-to-artifacts-ingest-api`.

Note the ordering rule: global flags go **before** the subcommand
(`dbt --warn-error ls ...`), command flags after (`dbt ls --select ...`). Several appear in
both positions; `--select`/`--exclude`/`--vars` etc. are command-level only.

### Command-specific flags worth knowing

| Flag | Commands | Note |
|---|---|---|
| `--select/-s/-m`, `--exclude`, `--selector` | build run test seed snapshot compile show ls clone docs generate source freshness | See §2 |
| `--full-refresh/-f` | build run seed compile show clone retry | Drops and rebuilds incremental models |
| `--event-time-start` / `--event-time-end` | **build, run only** | Microbatch backfill window. Mutually necessary. UTC assumed |
| `--empty` | build run snapshot compile | Limits refs/sources to zero rows — schema-only "does it compile against real relations" smoke test |
| `--sample SAMPLE` | build run compile? (build/run) | Sample-window mode for dev loops |
| `--store-failures` | build test | Persists failing rows to a table |
| `--resource-type` / `--exclude-resource-type` | build test ls clone | `model`, `test`, `unit_test`, `source`, `snapshot`, `seed`, `exposure`, `metric`, `semantic_model`, `saved_query`, `analysis`, `function`, `default`, `all` |
| `--output json`, `--output-keys` | ls | Machine-readable graph |
| `--inline TEXT`, `--introspect/--no-introspect` | compile show | `--inline` compiles ad-hoc SQL against the project context |
| `--limit N` | show | Row cap on the preview |
| `--compile/--no-compile`, `--static`, `--empty-catalog` | docs generate | `--no-compile` reuses the existing manifest; `--empty-catalog` skips the metadata queries |
| `-o/--output FILE` | source freshness | **Deprecated** (`CustomOutputPathInSourceFreshnessDeprecation`) `[verified]`. Default and expected path is `target/sources.json` — using `-o` means `source_status:` selectors find nothing |
| `--args YAML` | run-operation | Macro kwargs |
| `--lock`, `--upgrade`, `--add-package`, `--source` | deps | |
| `--connection`, `--config-dir` | debug | |

---

## 2. Node selection

Docs: <https://docs.getdbt.com/reference/node-selection/methods.md>,
<https://docs.getdbt.com/reference/node-selection/graph-operators.md>,
<https://docs.getdbt.com/reference/node-selection/defer.md>

### Set and graph operators `[verified]`

| Syntax | Meaning |
|---|---|
| `--select "a b"` (space) | union |
| `--select "a,b"` (comma) | intersection |
| `+model` | model and all ancestors |
| `model+` | model and all descendants |
| `2+model+3` | bounded degrees of ancestors/descendants |
| `@model` | model, its ancestors, **and the ancestors of its descendants** (what you need to build a model's downstream in an empty environment) |
| `path/to/dir`, `models/marts` | path selector |
| `pkg.subdir.name` | fqn selector (this is `dbt ls`'s default output form) |
| `*`, `?`, `[abc]`, `[a-z]` | wildcards inside a method value |

Verified on the probe project: `--select "@stg_orders"` returned the exposure, both models, the
seed and all three tests; `--select "+fct_orders"` returned the upstream chain.

### Methods (the ones we will actually use)

- `state:new`, `state:modified`, `state:old`, `state:unmodified` and the subselectors
  `state:modified.body|.configs|.relation|.persisted_descriptions|.macros|.contract`.
- `result:error`, `result:fail`, `result:warn`, `result:success` — read from the **`run_results.json`
  in `--state`**, not the manifest. `result:fail+` does not expand (tests have no children); the
  documented idiom for "rebuild what the failed tests were testing" is `1+result:fail` and
  `1+result:fail+`.
- `source_status:fresher+` — read from `sources.json` in `--state`.
- `config.materialized:incremental`, `config.<any>:<value>` (works on dicts and lists:
  `config.unique_key:column_a`, `config.meta.contains_pii:true`).
- `tag:`, `path:`, `fqn:`, `package:` (+ `package:this`), `resource_type:`, `exposure:`,
  `metric:`, `source:`, `test_name:`, `test_type:[unit|data|generic|singular]`, `group:`,
  `access:`, `version:[latest|prerelease|old|none]`, `unit_test:`, `semantic_model:`,
  `saved_query:`, `file:`.
- `selector:my_selector` inside a `--select` string is **1.12+ only** — not available on our
  1.11.12. On 1.11, `--selector` is a standalone flag and silently **overrides** `--select`/`--exclude`.

### Slim CI: `state:modified` + `--defer` + `--state`

```bash
dbt build --select "state:modified+" --defer --state path/to/prod/artifacts
```

- `--state DIR` must contain the prior `manifest.json` (and `run_results.json` for `result:`,
  `sources.json` for `source_status:`).
- `--defer` makes dbt resolve `ref()` for unselected nodes against the state manifest — but only
  if the node is (1) unselected **and** (2) not present in the current database. `--favor-state`
  drops condition (2) and always prefers the state relation.
- `--defer-state` lets you compare logical state against one manifest while deferring to another.
- Both flags are required together; env vars work too (see §4).

`[verified]` end-to-end on the probe project: with a saved `prod_state/manifest.json`, editing
one staging model made `dbt ls --select state:modified` return exactly that model and
`state:modified+` return the model, its child mart, the child's three tests, and the exposure.

`[verified]` `dbt ls --select "result:fail" --state target` after a failed `dbt build` returned
exactly the failed test node. dbt emits
`Warning: The state and target directories are the same: 'target'. This could lead to missing
changes due to overwritten state including non-idempotent retries.` — for `result:` selectors
that warning is expected; for `state:modified` it means your comparison is worthless.

### Source freshness → selection

```bash
dbt source freshness                                   # writes target/sources.json
dbt build --select "source_status:fresher+" --state path/to/prod/artifacts
```

`[verified]`: freshness runs standalone with no models built. Exit code is **1** when a source
breaches `error_after`; the JSON carries `max_loaded_at`, `snapshotted_at`,
`max_loaded_at_time_ago_in_s`, `status`, and the evaluated `criteria`. Also `[verified]`:
`source_status:fresher+` raises `Internal Error: No previous state comparison freshness results
in sources.json` when the state dir has no `sources.json` — it is not a soft no-op.

---

## 3. What our generated pipeline drives, per spec phase

Spec: `docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md` §§4, 6, 8.
Code: `core/onboarding/kpi/dbt_project_generator.py`, `core/orchestration/cosmos_dag.py`,
`core/orchestration/dbt_backfill.py`, `core/orchestration/pipeline_stages.py`.

### Generation-time validation (Phase 5 "dbt project + dbt tests")

```bash
dbt deps  --project-dir <ws>/dbt --profiles-dir <ws>/dbt
dbt parse --project-dir <ws>/dbt --profiles-dir <ws>/dbt
```

`dbt parse` is the honest generator gate: it is offline, it catches every dangling `ref()`,
malformed `config()` and Jinja error, and it exits **2** on a compilation error `[verified]`.
`validate_generated_project()` in `dbt_project_generator.py` checks emitted-code *semantics*
(merge-without-unique_key, missing `on_schema_change`, microbatch without `event_time`/`lookback`,
`select *` in a position-inserting strategy, >4 cluster keys, non-deterministic surrogate keys,
missing `query_tags`) — dbt cannot see any of those. The two are complementary; run both.

Add `dbt compile --no-introspect` only when a human wants to read the SQL. It is not needed for
validation and it can hit the warehouse if introspection is on.

### `dbt build` vs `dbt run` + `dbt test`

Use `build`. `run` then `test` builds the **entire** model graph before any test runs, so a
broken silver model publishes bad rows into every downstream mart before the first assertion
fires. `build` interleaves per node, and a failed test marks the node's children SKIP. That is
what makes the WAP flow in §8 of the spec real: `build` into `<gold>__staging`, and
`publish_gold` never runs when `build` exits nonzero.

One caveat for our WAP: because marts materialize into the staging gold schema and the swap is a
separate `run-operation`, a *test-only* failure still leaves a fully-built staging table. That is
intended (auditable), but a re-run of `publish_gold` by hand would publish it — the publish must
stay downstream of the build task's success, as `cosmos_dag` wires it.

### Backfills

```bash
dbt build --select <models> --event-time-start 2026-01-01 --event-time-end 2026-02-01
```

- Only meaningful for `incremental_strategy='microbatch'` models with `event_time`, `begin`,
  `batch_size`, and (for us, mandatory) explicit `lookback`.
- `--event-time-start` and `--event-time-end` are mutually necessary; end is **exclusive**,
  start inclusive; both are interpreted as **UTC**.
- `[verified]`: passing them to a project with no microbatch models is silently accepted and
  changes nothing — dbt does not warn. This is exactly why
  `cosmos_dag.backfill_command()` prefixes a `DEGRADED:` echo when
  `project_declares_event_time()` is false. Keep that; dbt will not tell you.
- Upstream parents that do not declare `event_time` are **full-scanned once per batch**
  (docs, "How microbatch works"). `validate_generated_project` already flags this.
- On Databricks, microbatch is implemented as `replace_where`, which **inserts by position**
  (docs, databricks-configs "The `replace_where` strategy"): a column reorder silently writes
  values into the wrong columns. Our `select *` ban is not paranoia.
- dbt-databricks 1.11.0+ requires **DBR 12.2 LTS or higher** for incremental models
  (`INSERT BY NAME` fix). Our adapter is 1.12.2, so this is a hard runtime floor for any
  workspace we generate incremental models for.
- Concurrency: `USE_CONCURRENT_MICROBATCH` behavior flag, default **False** in dbt-databricks
  1.12.2 `[verified in adapter source: dbt/adapters/databricks/impl.py]`. Batches run
  sequentially unless we opt in.

### Retry

```bash
dbt retry --project-dir <ws>/dbt --profiles-dir <ws>/dbt || dbt build ...
```

- Reads `run_results.json` from `--state` (default: the target dir) and resumes at the failure.
- Supported after `build`, `run`, `test`, `seed`, `snapshot`, `compile`, `clone`,
  `docs generate`, `run-operation`.
- On core 1.x it **cannot** take new `--select`/`--exclude` — it reuses the prior selection.
  Accepted overrides `[verified from --help]`: `--project-dir`, `--profiles-dir`, `--vars`,
  `--target-path`, `--threads`, `--full-refresh`, `--target`, `--profile`, `--state`.
- `[verified]`: retry after a failed build re-ran only the one failed test and exited **1**.
- If nothing ran before the failure (connection/permission error), retry has nothing to resume
  and does nothing — the `|| dbt build` fallback in `pipeline_stages.py` is the right shape.
- Retry resumes **failed microbatch batches** individually, not the whole model.
- Hard dependency: a persistent `target/`. Any step that runs `dbt clean`, or a container that
  regenerates the project into a fresh dir, destroys retry. `pipeline_stages.py` already notes this.

### `dbt clone` for WAP / blue-green

```bash
dbt clone --select <marts> --state path/to/prod/artifacts
```

Creates clones of the state manifest's relations in the current target. On Databricks this is a
Delta `CREATE TABLE ... SHALLOW CLONE` where supported, which is why it is the cheaper
blue-green primitive than our `CREATE OR REPLACE TABLE ... AS SELECT *` swap in
`macros/publish_gold.sql`. `[verified]` it runs and reports per-node results (on duckdb it
degrades to a create-or-replace; adapter-dependent). Two caveats before adopting it:

1. It needs a state manifest describing the *source* relations — i.e. we would have to keep a
   published-gold manifest, which we currently do not.
2. A shallow clone shares files with its source; dropping the source under a shallow clone is a
   destructive op and belongs behind the same gate as ghost-table deletion.

The current explicit-mart-list `publish_gold` macro is safer (it can only ever touch marts this
generator wrote) and is the right default until we keep a published manifest.

### `docs generate` for lineage

```bash
dbt docs generate --no-compile     # reuse existing manifest, still queries the catalog
dbt docs generate --empty-catalog  # manifest-only, zero warehouse metadata queries
```

`[verified]`: writes `catalog.json`, `manifest.json`, `graph_summary.json`, `graph.gpickle`,
`index.html`, `semantic_manifest.json`. Our exposures (the dashboard is registered via
`_write_exposures`) show up in `manifest.json['exposures']` and in `parent_map`/`child_map`,
which is the cheapest lineage source for the blueprint renderer — no HTML parsing needed.

### `run-operation` for the gold publish

```bash
dbt run-operation publish_gold --project-dir <ws>/dbt --profiles-dir <ws>/dbt
```

`[verified]` on the probe project: the macro executed and `{{ log(..., info=True) }}` reached
stdout; exit 0. `--args '{...}'` passes kwargs. `run-operation` still parses the whole project
first, so it is not a cheap shell — but it is transactionally the right place for the swap
because it runs with the project's `var()` context (`catalog`, `gold_schema`,
`gold_staging_schema`).

---

## 4. profiles.yml / dbt_project.yml / flags precedence

Docs: <https://docs.getdbt.com/reference/global-configs/about-global-configs.md>

### Precedence (highest wins)

1. CLI flag on the invocation (`--target prod`, `--threads 8`)
2. Environment variable (`DBT_ENGINE_*`, falling back to legacy `DBT_*`)
3. `flags:` block in `dbt_project.yml` (only for flags marked "in project")
4. dbt's built-in default

Some flags are project-only (`source_freshness_run_project_hooks`); some are CLI/env only
(`defer`, `state`, `empty`, `event_time_start/end`, `sample`, `quiet`, `target`, `profile`,
`profiles_dir`, `project_dir`, `target_path`, `log_path`).

### The `DBT_ENGINE_` rename — verified behavior on 1.11.12

Docs say 1.10-and-earlier use `DBT_*` and 1.11+ use `DBT_ENGINE_*`. The installed CLI supports
**both**, with `DBT_ENGINE_*` preferred `[verified in dbt/cli/params.py]`:

```python
# every option's envvar is registered as [DBT_ENGINE_<NAME>, DBT_<NAME>]
# "Order matters, the first envvar in the list is preferred"
```

`[verified]`: `DBT_STATE=prod_state dbt ls --select state:modified` and
`DBT_ENGINE_STATE=prod_state dbt ls --select state:modified` both worked and returned identical
node sets. Write `DBT_ENGINE_*` in anything new; the legacy names are an alias, not a guarantee.

Relevant vars for us: `DBT_ENGINE_STATE`, `DBT_ENGINE_DEFER`, `DBT_ENGINE_DEFER_STATE`,
`DBT_ENGINE_TARGET`, `DBT_ENGINE_PROJECT_DIR`, `DBT_ENGINE_PROFILES_DIR`,
`DBT_ENGINE_TARGET_PATH`, `DBT_ENGINE_EVENT_TIME_START/END`, `DBT_ENGINE_WARN_ERROR_OPTIONS`,
`DBT_ENGINE_PARTIAL_PARSE`, `DBT_ENGINE_WRITE_JSON`, `DBT_ENGINE_FAIL_FAST`.

Note our own `DBT_STATE_PATH` (in `core/orchestration/dbt_backfill.py`) is a **platform** env
var, not a dbt one — it is resolved and then passed as an explicit `--state`. Keep the distinct
name; do not let it collide with dbt's `DBT_STATE`.

### profiles.yml and targets — our catalog-per-env model

What we generate today (`_write_project_files`) is a **single** `prod` target:

```yaml
<project>:
  target: prod
  outputs:
    prod:
      type: databricks
      catalog: <catalog>
      schema: <schema>
      host: "{{ env_var('DATABRICKS_HOST') }}"
      http_path: "{{ env_var('DATABRICKS_HTTP_PATH') }}"
      token: "{{ env_var('DATABRICKS_TOKEN') }}"
      threads: 4
      query_tags: "{\"project_name\":\"...\",\"env\":\"prod\",\"run_id\":\"{{ env_var('AUTORESEARCH_RUN_ID','') }}\"}"
```

For spec §8's `<ws>_dev` / `<ws>_prod` catalog-per-env, the change is a second target, not a
second profile:

```yaml
<project>:
  target: dev              # safe default; CI/DAG passes --target prod explicitly
  outputs:
    dev:  {type: databricks, catalog: <ws>_dev,  schema: ..., http_path: "{{ env_var('DATABRICKS_HTTP_PATH_DEV') }}",  ...}
    prod: {type: databricks, catalog: <ws>_prod, schema: ..., http_path: "{{ env_var('DATABRICKS_HTTP_PATH') }}", ...}
```

Three real constraints found in the code and adapter, worth carrying forward:

- `profiles.yml` Jinja has a **limited** context — `env_var()` works, `{{ target.name }}` does
  **not** (only model/macro compilation has `target` in scope). So the `env` query tag must be a
  literal per target, not `{{ target.name }}`. The generator's comment says exactly this.
- `query_tags` is a **JSON-encoded string**, not a YAML mapping — the adapter types it
  `Optional[str]` and parses with `json.loads`
  `[verified: dbt/adapters/databricks/connections.py, utils.py QueryTagsUtils]`. A nested dict
  fails schema validation.
- Per-model compute (a second warehouse for heavy marts) is a `compute:` block **inside each
  target**, referenced from a model as `databricks_compute: Compute1`. Each target must define
  the same compute names.

`generate_schema_name` override: dbt's default **concatenates** `target.schema` with a model's
`+schema:` (giving `bronze_silver`). We override it to use the custom schema verbatim, which is
the documented `generate_schema_name_for_env` pattern. Any new target must not reintroduce the
concatenation.

---

## 5. Artifacts our platform should parse

All land in `target/` (or `--target-path`) unless `--no-write-json`.
Docs index: <https://docs.getdbt.com/reference/artifacts/dbt-artifacts.md>

### `manifest.json` `[verified keys]`

Top level: `metadata`, `nodes`, `sources`, `macros`, `docs`, `exposures`, `metrics`,
`semantic_models`, `saved_queries`, `functions`, `groups`, `group_map`, `selectors`, `disabled`,
`parent_map`, `child_map`, `unit_tests`.

Per node: `unique_id`, `name`, `alias`, `resource_type`, `database`, `schema`, `fqn`, `config`,
`unrendered_config`, `depends_on`, `columns`, `checksum`, `tags`, `relation_name`, `raw_code`,
`original_file_path`, `patch_path`, `description`, `meta`, `docs`, `group`, `build_path`.

Use for:
- **Ghost-table reconcile** — `_manifest_model_names()` already reads
  `nodes[*].alias or .name where resource_type == 'model'`. Better: use `relation_name`, which is
  the fully-qualified `catalog.schema.table` dbt actually wrote, so the diff against
  `information_schema.tables` compares like with like instead of bare names.
- **Lineage for the blueprint** — `parent_map` / `child_map` / `exposures`, no HTML.
- **Slim CI state** — this file *is* the state manifest.

### `run_results.json` `[verified keys]`

Top: `metadata`, `args`, `results`, `elapsed_time`.
`metadata`: `dbt_schema_version`, `dbt_version`, `generated_at`, `invocation_id`,
`invocation_started_at`, `env`.
Per result: `unique_id`, `status`, `execution_time`, `message`, `failures`, `adapter_response`,
`timing` (per-phase `compile`/`execute` with started/completed), `thread_id`, `relation_name`,
`compiled`, `compiled_code`, `batch_results`.

Use for:
- **Run telemetry** — `invocation_id` is the join key; `timing` gives per-node compile-vs-execute
  split, which is the honest input to "is this model slow or is the warehouse cold".
- **Cost attribution** — pair `invocation_id` with our `AUTORESEARCH_RUN_ID` query tag. dbt does
  not record warehouse cost; `system.query_history` does. The join is
  `query_tags['run_id'] = <our run id>` and `query_tags['@@dbt_model_name'] = <model>`.
- **Retry** — this is the file `dbt retry` reads.
- **Microbatch** — `batch_results` is where per-batch success/failure lives.
- **DQ reporting** — `status ∈ {success, error, fail, warn, skipped, pass, no-op}`, `failures`
  is the failing row count for a test.

### `sources.json` `[verified keys]`

`metadata`, `results`, `elapsed_time`; per result `unique_id`, `max_loaded_at`,
`snapshotted_at`, `max_loaded_at_time_ago_in_s`, `status`, `criteria`, `adapter_response`,
`timing`. Written **only** to `target/sources.json` — do not use the deprecated `-o`.

### `catalog.json`

Written by `docs generate` only. Column-level types and stats **as the warehouse reports them** —
the one artifact that reflects reality rather than our declaration, so it is the right source for
"did the emitted contract match what got created".

### Also present

`graph_summary.json` (compact node/edge lists), `perf_info.json` (parse timings),
`partial_parse.msgpack` (see §7), `semantic_manifest.json`, `compiled/` and `run/` SQL trees.

---

## 6. dbt-databricks specifics

Docs: <https://docs.getdbt.com/reference/resource-configs/databricks-configs.md>
Verified against the installed adapter source where noted.

### Incremental strategies

`[verified: DatabricksAdapter.valid_incremental_strategies()]`
`append`, `merge` (default), `insert_overwrite`, `replace_where`, `delete+insert`, `microbatch`.

`[verified: macros/materializations/incremental/validate.sql]` the adapter raises a compiler
error for:
- `merge` with `file_format` not in `delta`/`hudi`
- `replace_where` / `microbatch` / `delete+insert` with `file_format != delta`
- `delete+insert` without `unique_key`

...and only **logs a warning** for an unrecognized strategy. Critically it does **not** error on
`merge` without `unique_key` — that silently degrades to append-with-duplicates, which is why
`validate_generated_project()` has to catch it.

Adapter-level traps:
- `replace_where` (and therefore `microbatch`) inserts **by position**, not by name.
- `insert_overwrite` without `partition_by`/`liquid_clustered_by` replaces the **entire table**.
- `insert_overwrite` is rejected when connected via a SQL warehouse ("Use `merge` or
  `replace_where` instead") `[verified in validate.sql message]`.

### Clustering / table configs

`liquid_clustered_by` (≤4 keys), `auto_liquid_cluster: true` (1.10+; never combine with
`liquid_clustered_by`), legacy `clustered_by` + `buckets`, `partition_by`, `location_root`,
`include_full_name_in_path`, `tblproperties`, `file_format`, `table_format: iceberg`,
`skip_optimize` (1.12.2+). With `liquid_clustered_by` the adapter issues an `OPTIMIZE` after
every run; disable globally with `dbt run --vars "{'databricks_skip_optimize': true}"` or
`DATABRICKS_SKIP_OPTIMIZE=true`, or per-model with `skip_optimize: true`.

### Merge options

`merge_update_columns` / `merge_exclude_columns` (mutually exclusive),
`merge_with_schema_evolution`, `target_alias` / `source_alias` (default `DBT_INTERNAL_DEST` /
`DBT_INTERNAL_SOURCE`), `matched_condition`, `not_matched_condition`,
`not_matched_by_source_action`, `skip_matched_step`, `incremental_predicates`.

### Query tags — free cost attribution

`[verified: connections.py QueryConfigUtils.get_merged_query_tags, utils.py QueryTagsUtils]`
The adapter injects these automatically on every query:

```
@@dbt_core_version, @@dbt_databricks_version, @@dbt_model_name, @@dbt_materialized
```

Merge order: defaults < connection-level `query_tags` (profiles.yml, JSON string) <
model-level `query_tags` override. Reserved `@@`-prefixed keys cannot be overridden. This lands
in `system.query_history.query_tags`, which is what makes per-model and per-run spend
attributable — our custom `project_name`/`env`/`run_id` tags ride alongside.

### Python models

`submission_method` `[verified: DatabricksAdapter.python_submission_helpers]`:
`all_purpose_cluster`, `job_cluster`, `serverless_cluster`, `workflow_job`.

Limits worth knowing before Phase 3 routes anything to a Python model:
- `create_notebook: false` (Command API path) supports **only** `timeout` and
  `cluster_id`/`http_path` — no packages, no job config.
- `job_cluster_config` applies only to `job_cluster` and `workflow_job`.
- `workflow_job` creates a **persistent** Databricks workflow that users can run outside dbt —
  an artifact our destructive-op gate does not currently know about.
- `tblproperties` on a Python model is annotation-only (no PySpark API to set them at creation).
- `http_path` per-model is `all_purpose_cluster` only; `cluster_id` also works for `workflow_job`.

### Behavior flags (dbt-databricks 1.12.2) `[verified: impl.py]`

| Flag | Default | Why we care |
|---|---|---|
| `use_user_folder_for_python` | True | Python notebooks upload to the user's home folder |
| `use_materialization_v2` | **False** | Split create/insert; better column comments |
| `use_replace_on_for_insert_overwrite` | True | New `INSERT REPLACE ON` syntax |
| `use_managed_iceberg` | False | Otherwise UniForm |
| `use_concurrent_microbatch` | **False** | Batches are sequential unless enabled |
| `use_describe_as_json_for_relation_metadata` | False | Otherwise `information_schema` queries |

Set them under `flags:` in `dbt_project.yml`.

### Per-model compute

`databricks_compute: <name>` on a model, with a matching `compute:` block in **every** target of
the profile. Unnamed work (metadata gathering, catalog) always uses the target's top-level
`http_path`.

---

## 7. Failure modes

### Exit codes `[verified]`

| Code | Meaning | Seen in probe |
|---|---|---|
| 0 | Completed without error | `deps`, `parse`, `build` (all pass), `run-operation`, `clone`, `ls` with **no matching nodes** |
| 1 | Completed with handled errors — a model errored, a test failed, a source breached freshness | `dbt build` with a failing `accepted_values` test; `dbt retry` on the same; `dbt source freshness` past `error_after` |
| 2 | Unhandled error — compilation error, bad config, ctrl-c, network | `dbt parse` with a dangling `ref()`; `dbt --warn-error ls --select nonexistent` |

The trap: **an empty selection exits 0**. `dbt ls --select nonexistent_model` printed
`The selection criterion 'nonexistent_model' does not match any enabled nodes` / `No nodes
selected!` and returned **0** `[verified]`. A CI step that builds `state:modified+` and matches
nothing looks identical to a successful build. Any wrapper we write must treat
`NoNodesForSelectionCriteria` as a decision point, not a pass.

### `--warn-error` / `--warn-error-options`

Docs: <https://docs.getdbt.com/reference/global-configs/warnings.md>

`--warn-error` promotes **all** warnings to errors — including future warnings a dbt upgrade
introduces, which is how a green pipeline starts failing after a patch bump. `[verified]`
`dbt --warn-error ls --select nonexistent_model` exited **2**.

Prefer the targeted form. It is mutually exclusive with `--warn-error`:

```bash
dbt build --warn-error-options '{"error": ["NoNodesForSelectionCriteria", "MicrobatchModelNoEventTimeInputs"]}'
```

Names come from dbt-core's `events/types.py` (any class inheriting `WarnLevel`) or from
`--log-format json` output. Keys are `error` / `warn` / `silence` (the old `include`/`exclude`
are deprecated); values must be **arrays** (or the string `"all"` / `"*"` for `error`).

Recommended for our generated pipelines — these three are silent-wrong-data warnings:

```yaml
# dbt_project.yml
flags:
  warn_error_options:
    error:
      - NoNodesForSelectionCriteria          # empty selection is a bug, not a pass
      - MicrobatchModelNoEventTimeInputs     # every batch full-scans the parent
      - InvalidConcurrentBatchesConfig
```

### Partial parsing gotchas

`--partial-parse` is on by default and caches to `target/partial_parse.msgpack`.

- Changing an **environment variable** used in a model/config does not necessarily invalidate the
  cache — dbt cannot see through `env_var()` for `state:modified` either (docs, state-comparison
  caveats "Vars"). Our `query_tags` `run_id` is an `env_var()`; that is fine (it is read at
  connect time, not parse time), but any *config* driven by an env var is a partial-parse hazard.
- Some deprecation warnings only appear on a full parse — dbt says so itself: `You may also need
  to run with --no-partial-parse as some deprecations are only encountered during parsing`
  `[verified in output]`.
- Renaming a group with partial parsing on can leave broken downstream refs undetected until
  runtime.
- Rule: any **validation** invocation (the generator's gate, CI) should pass `--no-partial-parse`.
  Any **execution** invocation should not — it is a real speedup on large projects.

### State comparison caveats

- Seeds ≥1 MiB are compared by **path only**, not contents.
- `var`/`env_var` changes are invisible to `state:modified` (a model only shows as modified if
  the resulting config text differs).
- `dbt test -s state:modified` picks up both new tests and tests on modified models; a
  `relationships` test with one modified and one unmodified parent will query **across two
  environments** under `--defer`.
- Running with `--state target` (same dir dbt is writing to) is self-defeating: dbt warns
  `WarnStateTargetEqual` and the comparison silently degrades `[verified]`. State must be a
  copy from another run — which is exactly why `dbt_backfill._resolve_state_path()` refuses a
  path inside the repo.
- `state_modified_compare_more_unrendered_values: true` (behavior flag, 1.9+) reduces false
  positives from env-aware logic. Worth setting once we have two targets.

### Other operational traps

- `dbt clean` deletes `target/` → kills `dbt retry` and any `--state target` workflow.
- `dbt docs generate` queries the catalog; on a large Unity Catalog schema that is not free.
  Use `--empty-catalog` when only lineage is wanted.
- `--fail-fast/-x` stops at the first failure but leaves partially-built state; with WAP that is
  safe (nothing published), without it is not.
- `--version-check` compares against `require-dbt-version` in `dbt_project.yml`. We do not emit
  that key; adding it would catch the core/adapter skew noted at the top of this file.
- `--threads` overrides `profiles.yml`; on a shared SQL warehouse it is a concurrency knob that
  affects other tenants, not just us.

---

## 8. Platform integration map

Spec phase → exact commands. `<ws>` = `workspaces/<name>`, all invocations carry
`--project-dir <ws>/dbt --profiles-dir <ws>/dbt`.

| Spec phase | Purpose | Exact dbt commands |
|---|---|---|
| P0 Measure | none | dbt is not involved; discovery is connector-side |
| P2 Model | validate the emitted project offline | `dbt deps` → `dbt parse --no-partial-parse` → `validate_generated_project()` (ours) |
| P3 Choose | show the SQL a KPI will run, no execution | `dbt compile --select fct_<kpi> --no-introspect` |
| P4 Blueprint | lineage + node inventory for the rendered blueprint | `dbt ls --output json --output-keys name resource_type depends_on config` ; optionally `dbt docs generate --empty-catalog` then read `manifest.json` `parent_map`/`exposures` |
| P5 Provision | verify the target is reachable before anything writes | `dbt debug --connection --target <env>` |
| P5 Ingest → DQ gate | bronze freshness before the transform runs | `dbt source freshness` (exit 1 = stale; artifact `target/sources.json`) |
| P5 dbt build (Cosmos `dbt_build` task) | build models + tests into staging gold | `dbt build --target prod` (Cosmos `DbtBuildLocalOperator`, `InvocationMode.DBT_RUNNER`) |
| P5 task retry | resume the failed node only | `dbt retry \|\| dbt build` (`pipeline_stages.py`) |
| P5 WAP publish | staging gold → live gold, only after build succeeds | `dbt run-operation publish_gold` |
| P5 Dashboard refresh | none | reads the published gold tables |
| Backfill (manual trigger) | bounded replay | `uv run run-dbt-backfill --workspace <ws> --event-time-start <d> --event-time-end <d>` → `dbt build --event-time-start ... --event-time-end ...` (+ `--defer --select state:modified+ --state $DBT_STATE_PATH`) |
| Regeneration | ghost-table detection | `dbt parse` (refresh manifest) → read `manifest.json` `relation_name`s → diff vs `information_schema.tables` → dry-run report only |
| Slim CI (spec §8, at flip time) | build only what changed | `dbt build --select "state:modified+" --defer --state $DBT_STATE_PATH --target ci --warn-error-options '{"error":["NoNodesForSelectionCriteria"]}'` |
| CI re-test after a fix | rebuild what the failed tests covered | `dbt build --select "1+result:fail+" --state target` |
| Fresh-source-only run | skip untouched sources | `dbt source freshness` → `dbt build --select "source_status:fresher+" --state $DBT_STATE_PATH` |

### Gaps between this map and the code today

1. `dbt deps` / `dbt parse` are **not** run by `generate-dbt-project`. The generator writes files
   and runs its own semantic validator; a syntactically broken project is only discovered when
   Airflow runs `dbt build`. Adding `dbt parse --no-partial-parse` to the generator is the single
   highest-value change in this doc.
2. `profiles.yml` emits one `prod` target. Catalog-per-env (spec §8) needs a second target plus a
   target-aware `env` query tag (literal per target, not `{{ target.name }}`).
3. Nothing writes or reads a `--state` manifest outside `dbt_backfill.py`. Slim CI, `result:`
   selectors, and `dbt clone`-based blue-green all depend on publishing `target/` to object
   storage after every prod run. That upload step does not exist.
4. `packages.yml` is empty; spec §7 wants dbt-expectations for range/statistical checks.
5. No `require-dbt-version` in the generated `dbt_project.yml`, so the core/adapter skew this
   repo currently has (1.11.12 core + 1.12.2 databricks) is invisible to the pipeline.
6. No `flags:` block in the generated `dbt_project.yml` — the `warn_error_options` and behavior
   flags in §6/§7 are all unset.

---

## Sources

- `dbt --help` / `dbt <cmd> --help`, dbt-core 1.11.12, repo venv — 2026-08-05
- Live probe project (dbt-duckdb 1.10.1): deps, parse, ls, build, test failure, retry,
  result/state/source_status selectors, defer, clone, docs generate, source freshness,
  run-operation, debug, exit codes
- `dbt/adapters/databricks/{impl,connections,utils}.py` and
  `dbt/include/databricks/macros/materializations/incremental/validate.sql`, dbt-databricks 1.12.2
- <https://docs.getdbt.com/reference/node-selection/methods.md>
- <https://docs.getdbt.com/reference/node-selection/defer.md>
- <https://docs.getdbt.com/reference/node-selection/state-comparison-caveats.md>
- <https://docs.getdbt.com/reference/global-configs/about-global-configs.md>
- <https://docs.getdbt.com/reference/global-configs/warnings.md>
- <https://docs.getdbt.com/reference/exit-codes.md>
- <https://docs.getdbt.com/reference/commands/retry.md>
- <https://docs.getdbt.com/docs/build/incremental-microbatch.md>
- <https://docs.getdbt.com/reference/resource-configs/databricks-configs.md>
- `docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md` §§4, 6, 8
- `core/onboarding/kpi/dbt_project_generator.py`, `core/orchestration/{cosmos_dag,dbt_backfill,pipeline_stages}.py`

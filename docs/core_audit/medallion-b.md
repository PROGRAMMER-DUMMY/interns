# medallion-B (lineage/emit/deploy) — audit

## Purpose
The lineage/emit/deploy half of the Medallion Architect. It parses generated SQL and PySpark
files into column-level lineage, emits the executable Bronze/Silver/Gold artifacts (DuckDB SQL +
PySpark/Delta), rewrites P0 Silver SQL to MERGE semantics, tags builds in MLflow, manages PII
hashing salts, lints emitted SQL, tracks per-run state, and runs the dry-run-first,
remote-approval-gated Databricks deployment boundary (plan -> 5 gates -> approval artifact ->
deployer that re-verifies). The `medallion` CLI is the single front door.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| lineage.py | 104 | Column-level lineage graph + backward source trace | `LineageNode`, `LineageEdge`, `Lineage.trace_to_sources` |
| lineage_cli.py | 91 | `medallion lineage trace` CLI | `main`, `_trace` |
| sql_lineage_parser.py | 123 | sqlglot SELECT projection -> edges | `extract_edges_from_sql`, `_resolve_projection`, `_collect_source_tables` |
| spark_lineage_parser.py | 154 | AST walker for `*.spark.py` -> edges | `extract_edges_from_spark_py`, `_handle_with_column/_select/_merge`, `_extract_col_refs` |
| delta_emitter.py | 212 | Emit PySpark/Delta files for B/S/G + DuckDB Gold | `emit_bronze_spark`, `emit_silver_spark`, `emit_gold_spark`, `emit_gold_duckdb`, `uc_schema_for` |
| merge_emitter.py | 94 | Rewrite P0 Silver SQL to P1 DELETE+INSERT merge | `emit_silver_merge`, `_extract_select_body` |
| mlflow_emit.py | 115 | MLflow run tagging/metrics/artifacts (best-effort) | `start_run`, `finalize_run`, `write_lineage_with_runtime` |
| manifest.py | 257 | Manifest schema, inputs hash, hand-rolled YAML emitter | `Manifest`, `Bronze/Silver/GoldTable`, `compute_inputs_hash`, `manifest_to_yaml` |
| deploy_plan.py | 631 | `medallion plan-deploy` — deterministic UC/Jobs plan + validator | `build_deploy_plan`, `validate_deploy_plan`, `_table_mappings`, `_job_tasks`, `_sensitivity` |
| apply_deploy.py | 120 | `medallion apply-deploy` — eval 5 gates, write approval | `main`, `_write_approval`, `_verdict_table` |
| databricks_target.py | 71 | Downsize-then-DuckDB-fallback on compute failure | `execute_with_downsize_then_fallback`, `CompromiseHistory` |
| run_state.py | 124 | Per-run state file + filesystem lock | `RunState`, `TableRunStatus`, `acquire_lock`, `new_run_id` |
| salt_store.py | 114 | Workspace PII salt lookup/materialize + init CLI | `get_workspace_salt`, `materialize_salt_if_missing`, `_init_salt_cli`, `SaltMissing` |
| pii.py | 64 | PII hash helpers (Python/DuckDB/Spark) | `pii_hash_value`, `pii_hash_sql_duckdb`, `pii_hash_spark_expr`, `register_spark_salt_udf` |
| sql_lint.py | 105 | Parse + cartesian-plan + hotspot lint passes | `lint_sql`, `_lint_parse`, `_lint_plan`, `_lint_hotspot` |
| medallion_cli.py | 41 | Subcommand dispatcher | `main`, `SUBCOMMANDS` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | delta_emitter.py:101,106-110 | Derived-column `spark_expr` and `pii_hash_spark_expr(col)` are interpolated into `F.expr("{...}")` inside a generated `.spark.py` file with no escaping. A `"` or `\` in a formula template or column name breaks the generated Python string and produces a syntax error or, with a crafted contract, arbitrary embedded code. The contract is workspace-owned input, so this is a code-injection surface into emitted scripts. | Validate/escape `spark_expr` and column identifiers before interpolation; reject quotes/backslashes; ideally emit via `repr()` of the expression string and validate column names against an identifier regex. |
| [BUG] | delta_emitter.py:84-86,146-149; merge_emitter.py:40,52 | MERGE/PK conditions are built by raw f-string concatenation of `primary_key` column names (`tgt.{col} = src.{col}`, `({pk_tuple}) IN (...)`). No identifier validation. A malformed/hostile PK name injects SQL into the emitted Silver merge. Same class as above but in SQL. | Validate every PK/column against an identifier allowlist (reuse `_IDENT_RE`-style check) before emitting; fail the build on non-identifier names. |
| [NOT-PROD] | salt_store.py:62-96 | No salt rotation exists anywhere in the repo (grep confirms only init/materialize). PII salts are write-once; there is no re-hash/rotation path, and `materialize_salt_if_missing` silently no-ops if a salt is present. For a PHI/HIPAA control plane a compromised salt cannot be rotated without manual, undocumented surgery. | Add a `rotate-salt` command that versions the salt and records which hashes were produced under which salt version; document the rehash implications. |
| [NOT-PROD] | salt_store.py:73-94 | `secrets.toml` write is not atomic and not concurrency-safe: read-modify-write of the shared TOML with no lock; two concurrent `init-salt` for different workspaces can clobber each other. `chmod 0o600` is applied to the file but the parent `~/.config/autoresearch` dir is created with default perms (not 0o700), and chmod is a silent no-op on Windows (the dev platform). | Write to a temp file + atomic `os.replace`; create the dir with mode 0o700; on Windows fall back to documented DPAPI/secret-store guidance rather than a plaintext TOML. |
| [BUG] | run_state.py:21-51 | `acquire_lock` stale check uses `st_mtime` for age but the `WorkspaceBusy` message claims to read the holder; more importantly the stale-then-reacquire path (unlink + open 'x') is racy: two processes can both see the lock stale, both unlink, both create. PID liveness is never checked, so a crashed run holds the lock for the full `stale_after_seconds=3600`. | Check PID liveness; make stale reclaim atomic (e.g. rename-based or O_EXCL on a freshly-named file); the lock JSON has `pid`/`ts` but neither is used for the decision. |
| [BUG] | sql_lint.py:86-92 | Hotspot pass writes the SQL to a `NamedTemporaryFile(delete=False)` and only unlinks inside the `try`; if `SQLOptimizer(...).analyze` raises, the outer `except` swallows it but the temp `.sql` file is leaked (unlink line never reached). | Wrap creation in try/finally so the temp file is always unlinked. |
| [BUG] | sql_lineage_parser.py:65-73 | `_collect_source_tables` uses `find_all(exp.Table)` across the whole SELECT including subqueries, and keys by alias-or-name with no scope awareness; later `_resolve_projection` maps a column's `c.table` to a possibly-wrong source when aliases collide or are reused in nested scopes. Lineage edges can point to the wrong source table. Acceptable for an advisory graph but not "auditable where-did-this-column-come-from" as the docstring claims. | Use sqlglot's `optimizer.scope`/qualify to resolve columns per-scope rather than a flat alias map; or downgrade the docstring's correctness claim. |
| [BUG] | mlflow_emit.py:71 | `assertions_pass_rate` divides `passed / max(total, 1)` so a table with zero assertions reports a 0.0 pass rate (looks like a failure) rather than N/A; also `log_metric` keys derive from table names via `.replace(".", "_")` which can collide (`a.b` and `a_b`). | Skip the metric when `total == 0`; sanitize/namespace metric keys to avoid collision. |
| [NOT-PROD] | delta_emitter.py:55-58 | Bronze emitter hardcodes `option("header", True).option("inferSchema", True).csv(...)` — schema inference per run is non-deterministic and unsafe for governed Bronze (type drift across loads). Only CSV is supported; parquet/json sources silently won't work. | Emit an explicit schema (available from profiles) and switch reader by source file extension; do not infer schema for governed loads. |
| [NOT-PROD] | delta_emitter.py:36-38 | DBFS source path is `dbfs:/mnt/data/<rel>` with a comment "users must map this to their mount" — generated Bronze files will not run as-emitted on Databricks; the volume mapping in deploy_plan.py (`/Volumes/...`) is inconsistent with the emitted `dbfs:/mnt/...` path. | Generate the path from the deploy plan's UC volume layout so emitted code and the plan agree. |
| [MISSING] | deploy_plan.py:257-304 | `_job_tasks` has no cycle detection on Gold->Gold deps; the validator checks layer rank but a Gold table depending on another Gold table (same rank) is allowed and could form a cycle that Lakeflow rejects at deploy. | Add a topological-sort/cycle check across same-layer Gold dependencies in the validator. |
| [BUG] | databricks_target.py:68-69 | `fallback_reason` reports `exit_codes = attempts[:-1]` after the DuckDB attempt is appended, so it includes the primary+downsize codes — correct — but `history.degraded=True` is set even if the DuckDB fallback itself failed (exit!=0); a degraded-but-still-broken run is indistinguishable from a degraded-but-recovered run in `degraded_run`. | Set `degraded=True` only when the fallback succeeded; otherwise record a hard-failure status. |
| [DEAD] | pii.py:13-18,44-48,39-41 | `pii_hash_value`, `register_spark_salt_udf`, and `pii_columns_for_silver_table` are not referenced outside this module/tests (grep). The generated Silver script defines its own inline `_workspace_salt`/UDF (delta_emitter.py:122-130) rather than calling `register_spark_salt_udf`. | Either route the emitter through these helpers or remove the unused Python-side hashing path to avoid two divergent salt-injection mechanisms. |
| [DUP] | delta_emitter.py:122-130 vs pii.py:44-48 | The salt UDF is registered two ways: an inline `_workspace_salt` reading `dbutils.secrets` in emitted code, and `register_spark_salt_udf` capturing the salt in a Python closure. Different security postures (secret-scope lookup vs salt materialized into the driver process) for the same logical operation. | Pick one mechanism (prefer the secret-scope lookup that never materializes the salt) and delete the other. |
| [INTEGRATION] | salt_store.py:38,28; delta_emitter.py:124 | Salt lookup keys differ across the stack: env var uppercases and replaces `-`->`_` (`AUTORESEARCH_WORKSPACE_SALT__<UPPER>`), but the Databricks secret key and the emitted UDF use the raw `workspace` name (`medallion_salt__{workspace}`). A workspace name with `-`/mixed case resolves to different keys on env vs secret-scope paths. | Normalize the workspace key once in a shared helper and use it for all three lookup paths and the emitter. |

## Cross-package coupling
- **build.py** (sibling/design half) is the primary consumer: `emit_silver_merge`, `lint_sql`,
  `execute_with_downsize_then_fallback`, `write_lineage_with_runtime`. **design.py** consumes both
  lineage parsers (`extract_edges_from_sql`, `extract_edges_from_spark_py`) and the delta emitters.
- **Deploy boundary is well-formed and verified end-to-end**: `apply_deploy.py` -> `deploy_gates`
  (5 pure gates, no short-circuit) -> writes `deploy_approval.json` -> `medallion deploy`
  (`core/onboarding/databricks/workspace_deployer.py`) which calls `verify_deploy_approval`
  (re-checks artifact_type/version/consumed/provenance/plan-binding and **re-runs G4 freshness at
  apply time**) and `_require_remote_approval` (refuses without `confirm_remote_mutation` +
  human-set `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`). This is genuinely dry-run-first and
  remote-approval-gated. Strong: provenance source must be `human` at both write and consume.
- `deploy_plan.py` -> `core.governance.phi_gate` (PHI/PCI sensitivity blockers),
  `core.governance.provenance.decision_source`, `core.medallion.incremental` (refresh manifest),
  `core.storage.workspace_layout`. `databricks_target.py` -> `core.execution.backend`.
- `sql_lint.py` -> `tools.optimizer_finder.SQLOptimizer` + DuckDB EXPLAIN.
- All symbols in scope are reachable (no fully-dead files); only the `pii.py` Python-side helpers
  are unused in production paths.

## Verdict
The **deploy half is production-grade**: the plan/gate/approval/apply chain is dry-run-first,
re-verifies freshness and human provenance at apply time, refuses without explicit remote approval,
and the validator is thorough (identifier checks, layer-rank dep checks, repo-relative path
enforcement, PHI blocker enforcement). The **emit half is not yet production-ready**: the highest
risk is unescaped interpolation of workspace-controlled formula templates, PII expressions, and
PK/column identifiers into generated PySpark `F.expr(...)` and SQL MERGE conditions (code/SQL
injection into emitted artifacts). The **salt store** is the other gap — no rotation, non-atomic
non-locked TOML writes, and `chmod 0o600` that silently no-ops on the Windows dev box, with
inconsistent key normalization across env/secret/UDF paths. Lineage parsers are fit for an advisory
graph but oversell their "auditable" correctness (flat alias map, no scope qualification). Fix the
emitter injection surface and harden the salt store before any PHI deployment.

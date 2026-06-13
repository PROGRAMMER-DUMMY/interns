# execution — audit

## Purpose
`core/execution/` owns the `ExecutionBackend` strategy: it decides *where* one
experiment iteration runs and runs it. DuckDB (local subprocess) is the default
and the universal fallback; Databricks Jobs / SQL Warehouse / Connect are the
remote variants. The package also holds `DatabricksClient`, a thin lazy wrapper
over the `databricks-sdk` `WorkspaceClient` (health check, job submit/poll,
Delta write, MLflow experiment setup, capability discovery).

The governance contract enforced here is the local-safe-by-default rule:
remote execution is gated behind `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`, an
unhealthy Databricks connection falls back to DuckDB unless strict
(`AUTORESEARCH_DATABRICKS_STRICT=1` or `fallback=fail`), and a PHI/PCI gate can
block remote backends defensively. All Databricks SDK imports are lazy so the
module costs nothing when Databricks is disabled.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 23 | Public re-exports for the package | `DuckDBBackend`, `ExecutionBackend`, `ExecutionResult`, `StrictJobsBackend`, `StrictWarehouseBackend`, `build_execution_backend`, `normalize_command`, `DatabricksClient` |
| `backend.py` | 652 | Strategy interface, all backends, factory + helper gates | `ExecutionBackend(ABC)`, `ExecutionResult`, `DuckDBBackend`, `IsolatedDuckDBBackend`, `JobsBackend`, `WarehouseBackend`, `ConnectBackend`, `StrictJobsBackend`, `StrictWarehouseBackend`, `build_execution_backend`, `normalize_command`, `_phi_gate_failure_for_task`, `_resource_decision_for_task`, `_strict_databricks` |
| `databricks_client.py` | 237 | Lazy `WorkspaceClient` wrapper + health/jobs/Delta/MLflow ops | `DatabricksClient` (`get_client`, `health_check`, `create_mlflow_experiment`, `ensure_delta_schema`, `discover_capabilities`, `write_delta`, `submit_job_run`, `poll_job_run`, `_extract_warehouse_id`), `_is_remote_databricks_path` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | backend.py:290, 431 | `exit_code = 0 if result_state == "SUCCESS" else 1`. `poll_job_run` returns `str(state.result_state)`, which for the SDK enum renders as e.g. `RunResultState.SUCCESS` (or `"TIMEDOUT"`/`"FAILED"` literals), never the bare `"SUCCESS"`. A genuinely successful Databricks job is therefore almost always recorded as `exit_code=1`. The StrictJobs metadata `result_state` then also disagrees with `exit_code`. | Compare against `RunResultState.SUCCESS` (or `.name == "SUCCESS"` / `str(...).endswith("SUCCESS")`) and have `poll_job_run` return a normalized string. Add a test with the real SDK enum, not a `"SUCCESS"` stub. |
| [BUG] | databricks_client.py:142-161 | `write_delta` builds an `INSERT ... from_json('{rows_json}', ...)` by f-string interpolation of arbitrary record JSON straight into SQL. Any single quote or SQL metacharacter in a record value breaks the statement or enables SQL injection into the warehouse. Telemetry payloads (user names, error messages, KPI text) are exactly the kind of free text that contains quotes. | Use parameterized statements / SDK bind parameters, or at minimum escape via `json.dumps` + a properly quoted string literal and validate. Treat telemetry text as untrusted. |
| [BUG] | backend.py:384-387 | `ConnectBackend` calls `backend._run_subprocess.__func__(backend, ..., open(log_path, "w"))`. The opened file handle is never closed (leak), `elapsed_seconds` is hardcoded to `0`, and `__func__` indirection is fragile. It also never validates that `databricks-connect` actually wired anything — it just runs the local subprocess with env vars set. | Open the log via `with`, pass `elapsed = time.time()-start`, call `self._run_subprocess(...)` normally (no `__func__`). |
| [NOT-PROD] | databricks_client.py:32-37 | `get_client()` constructs `WorkspaceClient(host=..., token=...)` but never applies `cfg.http_timeout_sec`, retry policy, or connection pooling. Long-poll job loops (`poll_job_run`, 3s sleep, no max-retry on transient `get_run` errors) can hang or crash on a single transient 5xx. | Pass timeout/retry config to the SDK `Config`; wrap `get_run` in bounded retry; honor `http_timeout_sec`. |
| [NOT-PROD] | backend.py:377-380 | `ConnectBackend` copies `self.cfg.token` into a child-process env (`DATABRICKS_TOKEN`). The token is then visible to the subprocess env and any process listing / crash dump of the child. No redaction, no scrubbing on exit. | Prefer OAuth/SP or a short-lived scoped token; document the exposure; avoid putting the PAT in the experiment subprocess env where possible. |
| [BUG] | databricks_client.py:59-77 | `create_mlflow_experiment(self, name)` ignores the `name` argument entirely and hardcodes `/Users/<email>/autoresearch`. Caller in `tools/databricks_setup.py:184` and `telemetry_backend.py:121` pass meaningful names that are silently discarded — all experiments collide into one path. | Use `name` (sanitized) to build the experiment path, or remove the parameter and rename to reflect the fixed behavior. |
| [NOT-PROD] | backend.py:570-588 | `_phi_gate_failure_for_task` swallows every exception (`except Exception: return None`) — a crash inside the PHI/PCI gate import or layout means the gate silently passes and remote execution proceeds. A security gate that fails open is the wrong direction. | Fail closed on gate-internal errors in strict mode (return a blocking failure), and log the swallowed exception rather than discarding it. |
| [NOT-PROD] | backend.py:192-193 | DuckDB hard-timeout kill on Windows uses `proc.terminate()` only (no process-group kill, unlike POSIX `killpg`). Child/grandchild processes spawned by the experiment can survive the timeout, leaking workers. | Use a Job Object or `taskkill /T /F /PID` on Windows to kill the whole tree. |
| [MISSING] | backend.py:144-177 | `DuckDBBackend._run_subprocess` polls `proc.wait(timeout=1.0)` against `hard_timeout` but ignores `time_budget` entirely except as an env var. The "soft" `time_budget` is never enforced as a deadline; only the hard timeout (124) applies. If `time_budget < hard_timeout` the soft budget is advisory only. | Document that `time_budget` is advisory, or enforce a soft signal at the budget boundary before the hard kill. |
| [DEAD] | backend.py:205-256 | `IsolatedDuckDBBackend` is defined, marked "Experimental", and referenced nowhere in the repo (not exported in `__init__.py`, not built by the factory, no tests). Its `_run_subprocess` also lacks the start-up `[backend]` env logging and never honors `hard_timeout` via `_kill` correctly on its strip-env path. | Either wire it into `build_execution_backend` behind a mode/flag with tests, or remove it to cut dead surface. |
| [BUG] | backend.py:141 | `failure=self.fallback_failure if exit_code == 0 else None`. The fallback failure (e.g. "remote denied, fell back to DuckDB") is only attached when the local run *succeeds*. If the local fallback run fails, the original remote-denial context is dropped from the result. The logic reads inverted. | Attach `fallback_failure` regardless of local exit code (it describes why we are local, not the local outcome), or clarify intent. |
| [NOT-PROD] | databricks_client.py:228-232 | `_extract_warehouse_id` blindly takes the last `/`-split segment of `http_path` with no validation. An empty or malformed `http_path` yields `""`, which is then passed to `execute_statement(warehouse_id="")` and fails deep in the SDK with an opaque error. | Validate `http_path` shape; raise a `validation_blocker` with a clear message when warehouse id cannot be derived. |
| [NOT-PROD] | databricks_client.py:142-161 | `write_delta` casts every column to `map<string,string>` and `SELECT *` into the target table — schema/type fidelity is lost and column order must match exactly or it silently mis-maps. Docstring says "fails silently — caller marks telemetry_partial", so bad writes are invisible. | Use typed schema / explicit column list; surface write failures to the caller instead of relying on silent partial telemetry. |
| [NOT-PROD] | backend.py:329, 477 | Warehouse `wait_timeout=f"{min(time_budget, 50)}s"` caps the synchronous wait at 50s but there is no async/polling continuation — a statement that needs >50s returns a non-terminal state treated as failure/fallback even though it may still be running on the warehouse. | Poll the statement to completion (like `poll_job_run`) instead of capping at one 50s synchronous wait. |
| [DUP] | backend.py:280-297 vs 415-439 / 314-354 vs 463-524 | `JobsBackend.execute` / `StrictJobsBackend.execute` and `WarehouseBackend.execute` / `StrictWarehouseBackend.execute` duplicate nearly all submit/poll/parse logic. The non-strict base classes are only ever reached via `super().execute()` from the strict subclasses; the factory always builds the Strict* variants. | Collapse into one parametrized implementation (`strict: bool`) or have base classes own the shared body and strict override only the fallback branch. |

## Cross-package coupling
- **Consumed by** `core/orchestration/loop.py` (builds backend at line 92 via `build_execution_backend`, calls `.execute(...)` at 417). This is the sole runtime construction path.
- **Config**: depends on `core.config.DatabricksConfig` (`is_active`, `execution`, `fallback`, `host`, `token`, `http_path`, `http_timeout_sec`, `phi_covered`, `pci_covered`). Note `http_timeout_sec` is defined in config but never read by this package — [INTEGRATION] gap.
- **Failures**: imports `core.failures` (`StructuredFailure`, `internal_bug`, `remote_denied`, `remote_unavailable`, `validation_blocker`) — well integrated; surfaced back into the loop's structured-failure print.
- **Resource gating**: `core.resource.manager.ResourceManager/ResourceDecision` — local preflight blocker.
- **PHI/PCI gate**: `core.governance.phi_gate.enforce_remote_sensitive_gate` + `core.storage.workspace_layout.WorkspaceLayout` (lazy, defensive).
- **Parsing**: `core.observability.parser.RegexLogParser` (re-instantiated inline in 4 places rather than reusing a passed parser — minor [DUP]).
- **DatabricksClient consumers**: `tools/databricks_setup.py`, `core/observability/telemetry_backend.py` (`write_delta`, `create_mlflow_experiment`), `core/onboarding/databricks/workspace_deployer.py` (`ensure_delta_schema`, reads `cfg.http_timeout_sec` itself), `core/medallion/*`.

## Verdict
The approval-gating skeleton is correct and the right shape: remote needs an
explicit env opt-in, strict mode fails closed, non-strict falls back to DuckDB,
and a defensive PHI/PCI gate sits in front of both remote backends. The lazy
import discipline is clean and credential values are never printed (only
redacted/status). That core safety story holds.

However the package is **not production-ready as-is**. The most serious issue is
the Databricks job success check (`result_state == "SUCCESS"`) which almost
certainly misclassifies real successes as failures — a correctness bug that
would silently poison governance/promotion decisions. The `write_delta` f-string
SQL is an injection/robustness hole for untrusted telemetry text. The PHI gate
fails *open* on internal error, which contradicts its defensive purpose. Add to
that the timeout config never reaching the SDK, the unbounded poll loop, the
ConnectBackend file-handle leak + token-in-subprocess-env, and a sizable amount
of dead/duplicated backend code. These are fixable without redesign, but the job
success bug and the fail-open PHI gate should block any remote rollout until
addressed.

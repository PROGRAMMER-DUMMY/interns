# storage — audit

## Purpose
`core/storage/` owns all persistent and serialized workspace state for the control plane:

- **`workspace.py`** — `Workspace`: a single SQLite datastore (`workspace.db`) that replaced
  scattered state files (results.tsv, run.log, ideas.md, loop_status.json, intern_log.jsonl), plus
  a thin shell-out wrapper over `git` for history/commit/diff used by the experiment loop.
- **`metadata_store.py`** — pluggable structured-metadata backend (`local` JSON-per-document,
  `delta` Delta tables, `mongo`) with `build_metadata_store` selection and local fallback on
  remote-write failure.
- **`workspace_layout.py`** — `WorkspaceLayout`: pure, frozen path-derivation for the
  `workspaces/<project>/interns/**` tree; also the settings loader and dataset-allowlist gate.
- **`workspace_lock.py`** — cross-platform (msvcrt/fcntl) re-entrant per-workspace file mutex with
  stale-PID reclamation.
- **`external_data.py`** — policy helpers for bounded scanning and allowlisting of large external
  data roots that live outside the repo.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 24 | Package re-exports | (re-exports only) |
| `external_data.py` | 177 | External-root policy, bounded scan, allowlist matching | `ExternalDataPolicy`, `load_external_data_policy`, `is_external_path`, `bounded_external_files`, `path_allowed_by_entries`, `external_allowlist_paths`, `allowlist_entries`, `_is_relative_to` |
| `metadata_store.py` | 296 | Pluggable structured metadata store + fallback | `MetadataStore`, `LocalMetadataStore`, `DeltaMetadataStore`, `MongoMetadataStore`, `MetadataWriteResult`, `build_metadata_store`, `_write_delta_row`, `_envelope`, `_safe_name` |
| `workspace.py` | 447 | SQLite state DB + git operator | `Workspace`, `RunRecord`, `_init_db`, `log_experiment`, `log_optimization_memory`, `log_governance_decision`, `redact_keys`, git wrappers |
| `workspace_layout.py` | 212 | Workspace path layout / settings / allowlist | `WorkspaceLayout` (frozen dataclass) |
| `workspace_lock.py` | 317 | Cross-platform re-entrant workspace mutex | `workspace_lock`, `WorkspaceLockTimeout`, `_pid_alive`, `_try_lock`/`_unlock`, `_write_metadata` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | workspace.py:36 | `sqlite3.connect` uses default `check_same_thread=True` and a single shared `self.conn`. The experiment loop, telemetry backend, and dashboard read/write the same `Workspace`; any cross-thread access raises `ProgrammingError`. No `timeout=`/WAL set either, so concurrent processes get immediate `database is locked`. | Pass `check_same_thread=False` + `timeout=30`, enable `PRAGMA journal_mode=WAL`, and guard `self.conn` with a `threading.Lock`, or open per-thread connections. |
| [BUG] | workspace_lock.py:264-283 | Stale-reclaim does `unlink()` then reopen, but on POSIX `flock` is tied to the open file description, not the path. Two waiters can both decide the holder is dead, both unlink (one removing the *other's* freshly created file), and both then acquire locks on different unlinked inodes — a double-acquire race. The whole reclaim is also unsynchronized across processes. | Reclaim under an OS-level lock on a stable sentinel (lock the parent dir, or `O_EXCL` create a `.reclaim` marker); never `unlink` a POSIX flock target while contending. Prefer truncate-and-rewrite over delete. |
| [BUG] | workspace_lock.py:303-313 | The lock file is `unlink()`ed in `finally` on every release. On POSIX another waiter blocked in `flock` on the old inode keeps a lock that no longer protects anything once a third party recreates the path — classic flock-with-unlink hazard. Combined with the reclaim path this makes the mutex unsound under real contention. | Do not delete the lock file on release; keep a persistent lock file and only release the flock. Deletion should be a rare, separately-locked GC step. |
| [BUG] | metadata_store.py:294 | Delta write mode is `append` whenever `_delta_log` exists, so `upsert` never replaces — every call appends a new row for the same `document_id`. Readers would see stale duplicates; this is an insert store mislabeled as upsert. | Implement real upsert (deltalake `merge` on `document_id`+`workspace`) or document/rename as append-only and have readers take latest `updated_at`. |
| [NOT-PROD] | workspace.py:399-446 | All git operations shell out with `capture_output=True` and ignore return codes; `commit`/`revert_file` swallow failures silently (e.g. nothing-to-commit, detached HEAD, merge conflict). `diff_file`/`get_diff` pass an unsanitized `editable_file` straight into argv. | Check `returncode`, log/raise on failure, and validate `editable_file` is a repo-relative path. Add `timeout=` to the long-running git calls (only `get_recent_log_stat` has one). |
| [NOT-PROD] | workspace_layout.py:183-185, external_data.py:166-168 | `load_settings` and `_read_json` catch broad `Exception`/`OSError` and silently return `{}`. A malformed `workspace_settings.json` silently disables Delta backend selection and dataset-allowlist entries (security-relevant: a corrupt allowlist falls back to "project-root only", which is safe, but a corrupt settings file silently changes backend). | Narrow to `json.JSONDecodeError`/`OSError`, and log a warning so a corrupt settings file is visible rather than silently ignored. |
| [NOT-PROD] | metadata_store.py:107, 170 | Both Delta and Mongo `upsert` catch bare `Exception` and fall back to local. Correct for resilience, but the embedded `warning=f"...:{exc}"` can leak a Mongo connection string / credentials present in the exception text into the returned result and any logged audit. | Keep fallback, but sanitize the warning (use `type(exc).__name__` only, or run through `redact_keys`); do not embed `{exc}` for the Mongo URI path. |
| [NOT-PROD] | workspace.py:130-151 | `redact_keys` only masks exact-substring matches of currently-configured keys. A rotated/old token, a token from a different field, or a partially-quoted token in JSON evidence is not redacted before being persisted to SQLite. | Add a regex pass for known token shapes (sk-, dapi, AIza, ghp_) in addition to exact-match. |
| [DEAD] | workspace.py:171-177 (`write_session_report`), 204-212 (`load_loop_status`), 415-446 (`get_history`, `get_diff`, `get_recent_log_stat`) | Zero callers anywhere in `core/`, `tools/`, `tests/`, or repo root. `load_loop_status` is the read-side of `save_loop_status` but nothing reads it; the three git-read helpers and `write_session_report` are orphaned (likely intended for a dashboard that does not call them). | Remove, or wire into the dashboard. If kept for a planned UI, mark clearly. |
| [DEAD] | metadata_store.py:248-249 | The `backend != "mongo"` branch is unreachable as written: by that point `backend` can only be `"mongo"` (env/settings/`delta_enabled`/default already routed `local` and `delta`; default is `local`). The line is a redundant guard that can never fire — any non-recognized backend string already routed elsewhere is impossible to reach here. | Replace the dead `if backend != "mongo"` guard with an explicit `else: raise ValueError(f"unknown backend {backend}")` so an invalid `AUTORESEARCH_METADATA_BACKEND` fails loudly instead of silently becoming Delta. |
| [INTEGRATION] | metadata_store.py:133-202 | `MongoMetadataStore` is only reachable through `build_metadata_store`, which only selects Mongo when `AUTORESEARCH_METADATA_BACKEND=mongo` AND `AUTORESEARCH_MONGO_URI` is set. No production caller, config, or test exercises it; CONTEXT.md lists only local/delta/Databricks. Effectively unexercised code. | Confirm Mongo is a real target; if not, drop it. If yes, add a smoke test (mongomock) so the backend isn't silently broken. |
| [INTEGRATION] | workspace.py:331-370 | `get_recent_governance_decisions` / `get_recent_alerts` have no `core/` caller — only `tests/test_enterprise_optimization.py`. They are written (`log_governance_decision`) by the loop but never read in product code. | Wire into the dashboard/reporting layer or document as test-only read APIs. |
| [MISSING] | metadata_store.py:205 | `build_metadata_store(layout, *, repo_root=...)` accepts `repo_root` but never uses it; four call sites pass it. Dead parameter that misleads callers into thinking root resolution happens here. | Remove the unused `repo_root` param (and the call-site args) or actually use it. |
| [MISSING] | workspace_lock.py:124-140 / 292 | `_write_metadata` on Windows writes JSON into the *leading* bytes while the lock is held on a sentinel byte at 1 GiB — but the file is opened `O_CREAT` without sparse handling, so on NTFS the file's logical size jumps to ~1 GiB on first `msvcrt.locking` at that offset region only if written; here only byte at offset is locked (not written), so size stays small — acceptable, but undocumented reliance on lock-without-write. Also no validation that `os.write` wrote all bytes. | Document the sentinel-byte invariant; check the `os.write` return value. |
| [DUP] | workspace_lock.py:38-45 vs external_data.py:171-176 / workspace_layout — path-normalization | `_lock_key` resolves+normcases, `external_data._is_relative_to` and layout resolution each re-implement resolve/relative logic. Minor duplication of resolve/normcase patterns across the package. | Consider a shared `core.paths` helper (already exists) for normcase/resolve. |

## Cross-package coupling
- **`core.paths.PROJECT_ROOT`** — `workspace.py` imports it as the default `db_path`/`work_dir` base.
- **`core.config.load`** — lazily imported inside `Workspace.redact_keys` to fetch
  `google_api_key`/`anthropic_api_key`/`databricks.token`. A runtime cross-package dependency hidden
  inside a method (acceptable to avoid import cycle, but undeclared at module top).
- **`metadata_store.build_metadata_store`** consumes a `layout` duck-typed object needing
  `state_dir` and `load_settings()` — satisfied by `WorkspaceLayout`. Callers:
  `onboarding/workspace/{bootstrap,kickstart,onboarding}.py`, `onboarding/kpi/feature_resolver.py`,
  `optimization/engine_evolution.py`.
- **`WorkspaceLayout`** is the most widely consumed symbol (95 files reference `core.storage`),
  feeding nearly every onboarding/kpi/medallion/dashboard module its paths.
- **`workspace_lock`** consumed by `dev/green_gate.py`, `onboarding/workspace/{onboarding,cli_runner}.py`,
  `onboarding/kpi/{blocker_cli,panel_preview_executor}.py`.
- **`external_data`** consumed by `onboarding/sources/{catalog,external_discovery}.py` and
  `onboarding/workspace/onboarding.py`; `WorkspaceLayout` also wraps it (`is_dataset_allowed`).
- Optional deps: `deltalake`+`pyarrow` (Delta), `pymongo` (Mongo) — both lazily imported with
  `RuntimeError` if missing. Good for zero-cost-when-disabled.

## Verdict
**Not production-ready as-is for concurrent use.** The path-layout (`workspace_layout.py`),
`external_data.py`, and the metadata-store *abstraction* are clean, well-typed, and correctly
fallback-safe. The two highest-risk components are exactly the ones flagged in the brief:

1. **`workspace_lock.py`** has a genuine POSIX `flock`-with-`unlink` soundness hole (delete on every
   release + unsynchronized stale reclaim) that permits double-acquire under real multi-process
   contention. The Windows path and re-entrancy registry are well-reasoned; the POSIX delete/reclaim
   path is the defect. This must be fixed before relying on it as a real cross-process mutex.
2. **`workspace.py`** uses a single non-thread-safe SQLite connection with no WAL/timeout, while the
   architecture explicitly runs telemetry + loop + dashboard against the same DB. Transaction
   blocks (`with self.conn`) are correct per-call, but concurrency is unhandled.
3. **`metadata_store.py`** Delta backend is append-not-upsert (silent data duplication) and has a
   dead/silent backend-selection branch that turns an invalid backend string into Delta.

Lower priority: several orphaned read methods ([DEAD]), an unused `repo_root` param, an unexercised
Mongo backend, and credential-leak risk in fallback warning strings. None of these block, but they
indicate the package grew faster than its callers.

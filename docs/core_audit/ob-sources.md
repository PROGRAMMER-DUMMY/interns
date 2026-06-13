# ob-sources — audit

## Purpose
`core/onboarding/sources/` owns two distinct source-onboarding surfaces:

1. **Source catalog** (`catalog.py`): turns an approved `docs/source_selection.json`
   into a dry-run plan or applied ingestion. Handles three source families — `api`
   (async paged/streamed HTTP fetch with rate-limiting, retry, quarantine,
   checkpointing), `local` (copy or register-by-allowlist), and `databricks_uc`
   (metadata export, remote-approval gated). Also profiles/drift-checks materialized
   datasets (Polars), and indexes/matches large external data catalogs (e.g.
   data.gov-style JSON) to draft selections.
2. **External data intake** (`external_discovery.py`, `external_intake_workflow.py`,
   `external_intake_cli.py`): metadata-only discovery of an external root (paths +
   sizes, never content), classification into datasets/docs/delta/db/logs, grouping,
   strategy recommendation, and a panel-driven routing workflow (create workspace /
   attach existing / custom) with team-preference memory and an approval gate.

The catalog feeds profiling/relationships via two routes: `process` writes
`*.profile.json` + drift reports under `evidence/source_catalog/profiles/`, and
`register` mode appends to `dataset_allowlist` in `workspace_settings.json`
(consumed by `WorkspaceLayout.is_dataset_path_allowed` / `external_dataset_allowlist_paths`).

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 1 | Package docstring only. | — |
| `catalog.py` | 2110 | Governed source catalog plan/ingest/preflight/index/match/draft/finalize/process/validate. | `SourceCatalogManager`, `ApiFetchPolicy`, `HostRateLimiter`, `CatalogResult`, async fetch/stream helpers, `_process_dataset`, `_build_drift_report`, `main`/`plan_main`/`ingest_main` |
| `external_discovery.py` | 447 | Metadata-only classify/group an external root; emit discovery JSON/MD + draft selection. | `ExternalSourceDiscoverer`, `ExternalFileClass`, `ExternalGroup`, `_strategy_for`, `_delta_roots`, `_group_for`, `main` |
| `external_intake_workflow.py` | 694 | Panel-driven multi-stage intake routing + memory. | `ExternalSourceIntakeWorkflow`, `ExternalSourceIntakeResult`, `_route_panel`/`_outcome_panel`/etc, `_workspace_for_route`, `_resolve_option` |
| `external_intake_cli.py` | 119 | CLI for prepare/apply intake panel; wires lock/idempotency via `run_workspace_command`. | `prepare_main`, `apply_main`, `_workflow_workspace_hint` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | external_discovery.py:284-288 (`_validate`) | **External-root allowlist NOT enforced.** `ExternalSourceDiscoverer` validates only `external_root.exists()`/`is_dir()`. It never calls `load_external_data_policy`/`is_external_path` to confirm the path is one of the configured `external_data_roots`. Any absolute path (`C:\Windows`, `/etc`, another user's home) can be scanned and have its full file tree (paths + sizes) written into workspace artifacts and a draft selection. The module imports the policy helpers nowhere; only `catalog.py` imports them, and even there only to gate the *workspace*. This is the primary path-traversal/over-reach gap for this package. | Load `load_external_data_policy(repo_root)` and require the resolved `external_root` to be relative-to one of `policy.configured_roots` (when any are configured); otherwise refuse with a clear error. Apply the same check in `ExternalSourceIntakeWorkflow.__init__`/`prepare`. |
| [BUG] | catalog.py:607-622, 646-659 (`_plan_local_source`/`_apply_local_source`) | **Local source path is unbounded.** A `local` source's `path` is `expanduser()`-ed and accepted as any absolute path on the host. `register` mode adds that arbitrary absolute path to `dataset_allowlist`; copy mode `shutil.copy2` reads any file the process can read into the workspace. No allowlist/policy check against `configured_roots`. Combined with the discovery gap, an operator approving a generated selection can pull arbitrary host files into a governed workspace. | Validate `source_path` against `load_external_data_policy` allowlist (reuse `is_external_path` semantics: must be inside repo OR inside a configured external root). Block and warn otherwise. |
| [BUG] | external_discovery.py:357-360 (`_path_size`) | Symlink / reparse-point handling: `bounded_external_files` (storage) follows dirs via `iterdir`/`is_dir` without `is_symlink` guard, and `_path_size` does `rglob("*")` summing sizes. A symlink loop or a symlink pointing outside the (unenforced) root can cause traversal outside the intended tree and unbounded walk on the size path (only the file *listing* is time/count-bounded; `_path_size` rglob is not). | Skip symlinks in the storage walk (`not child.is_symlink()`); bound or skip `rglob` in `_path_size` for directory (delta) entries, or use the already-listed file set. |
| [NOT-PROD] | catalog.py:1148-1184, 1187-1223 (`_fetch_bytes_with_retry`/`_stream_to_path`) | **No SSRF / host allowlist on API fetch.** Approved `api` sources fetch arbitrary user-supplied URLs with `aiohttp` and no scheme/host restriction, no block on private/loopback/metadata IPs (e.g. `169.254.169.254`, `localhost`). Approval is the only gate. For a governed control plane this is a server-side request forgery vector. | Add an egress allowlist/denylist (block link-local, loopback, RFC1918 unless explicitly permitted) and optionally a configured host allowlist; surface in preflight. |
| [BUG] | external_intake_workflow.py:676-677 (`_has_workspace_files`) | `rglob("*")` over the proposed workspace with no bound or symlink guard; on a large/attached existing workspace this can be slow and could traverse symlinks. Runs during `__init__` (constructor side effect: filesystem walk). | Use a bounded `any(... )` that stops at first file via `os.scandir` recursion with a depth/time cap; avoid heavy work in `__init__`. |
| [BUG] | catalog.py:1028-1032 | `json.loads` failures (`UnicodeDecodeError`, `JSONDecodeError`) silently set `parsed=None`, which then routes the page to the *streaming-file* branch (line 1047 `else`) — a non-JSON 200 response on a `.json`/`.csv` target is silently written as a raw file and marked `fetched` rather than quarantined. Masks malformed/HTML-error responses as success. | On parse failure when rows were expected (response_mode rows / data suffix), quarantine the page instead of treating bytes as a file download. |
| [BUG] | catalog.py:1262 (`_read_existing_rows`), 1529, 1530 (`_discover_doc_links`) | Broad `except Exception: return []` swallows all errors (corrupt checkpoint resume, unreadable doc-link payloads). Resume after partial fetch silently restarts from empty rows, risking duplicate/lost rows. | Narrow excepts; log/warn on resume-read failure and include in result warnings. |
| [BUG] | catalog.py:692-705 (`_register_external_allowlist`) | Read-modify-write of `workspace_settings.json` with no lock; concurrent `local-stage` of multiple sources (or the async path) can lose allowlist entries (last-writer-wins). Also writes to `state_dir/workspace_settings.json` only, ignoring `durable_workspace_settings` that `load_settings` also reads. | Serialize via the workspace command lock; or merge against a re-read under lock. Document which settings file is authoritative. |
| [INTEGRATION] | external_intake_workflow.py (whole) | The intake workflow's terminal stage only writes discovery + draft selection + memory. It never invokes `SourceCatalogManager` to actually register/profile selected groups; the "approve_later"/"review_only" gate is the only outcome. The bridge from external-intake `selected_groups` to a finalized `source_selection.json` consumed by `catalog.py` is manual (operator edits `docs/source_selection.generated.json`). Confirm this hand-off is intended and documented, else datasets discovered here never reach profiling/relationships automatically. | Document or wire the generated draft → `finalize-selection` → `process` path; or add an explicit next-step command in the terminal panel. |
| [BUG] | external_intake_workflow.py:45, 680-682; external_discovery.py:78-83 | `external_root.name` is used to derive the proposed workspace slug; a root like `/` or a drive root (`C:\`) yields empty `.name` → slug falls back to `external-source`, and the discoverer would still scan a drive root (no allowlist). Combined with the missing allowlist this allows whole-drive metadata enumeration. | Reject filesystem/drive roots explicitly; enforce allowlist (see first finding). |
| [NOT-PROD] | catalog.py:849-858 (`_validate_workspace`) | The repo-containment check is duplicated three ways (policy `is_external_path`, equality, `is_relative_to`) but the *selection* path containment relies on `_resolve_selection_path` which, for a relative path not under workspace, silently re-roots it under the workspace (line 847) instead of erroring — a confusing fallback that can mask a wrong `--selection`. | Make the re-root explicit/logged or error on out-of-workspace selection. |
| [DUP] | external_discovery.py:363-381 & external_intake_workflow.py:680-694 & catalog.py:1962-1986 | `_safe_id`/`_safe_slug`/`_safe_name`, `_rel`, `_is_relative_to`, `_now` are re-implemented in each module with subtly different rules (`_safe_id` lowercases + 100-char cap; `_safe_name` uses isalnum; `_safe_slug` uses `-`). Risk of divergent sanitization. | Consolidate path/id helpers into one shared util (e.g. `core/storage` or a `sources/_util.py`). |
| [BUG] | external_discovery.py:20 (`LOG_SUFFIXES`) | `.jsonl` is in both `DATA_SUFFIXES`-adjacent intent and `LOG_SUFFIXES`. In `_classify`, `.jsonl` matches `DATA_SUFFIXES` first (line 161) and is classed as a **dataset**, so `LOG_SUFFIXES` `.jsonl` (line 167) is dead for that suffix — only `.log` ever reaches it. NDJSON log files are misclassified as ingestable datasets (classification false positive). | Decide precedence explicitly; if path is under a `system/sessions/state/logs` dir treat `.jsonl` as log regardless of suffix (the SYSTEM_DIR_NAMES check at 159 partially covers this but not arbitrary `logs/` dirs). |
| [BUG] | external_discovery.py:159 (`_relative_parts`) | `SYSTEM_DIR_NAMES` excludes `system/sessions/state/__pycache__` but not common `logs`, `tmp`, `.git`, `node_modules`, venvs — those get profiled as data/doc candidates, inflating the draft selection and risking ingestion of junk. | Broaden the exclude set; allow config-driven excludes. |
| [DEAD] | catalog.py:218-248 (`discover_docs`) + `_discover_doc_links` | `discover_docs` only inspects `materialized_path` of `api` actions for embedded doc links. Reached only via the `discover-docs` subcommand / `run`. Functional but the doc links are written to an evidence artifact that nothing else in the package consumes — verify a downstream reader exists, else it is informational-only. | Confirm a consumer; otherwise label informational. |
| [MISSING] | catalog.py:1237-1248 (`_validate_expected_columns`) | Schema validation only checks **presence** of required columns, not types or row-level constraints. `process`/`_process_dataset` profiles but does not enforce the declared `schema`. Drift detection exists only after a baseline. No first-ingest schema gate. | Add type/required validation in `process` against `schema`; fail or warn on mismatch. |
| [NOT-PROD] | catalog.py:1432 | `lf.collect(engine="streaming").write_parquet` stages a full Parquet copy for every non-parquet dataset with only the disk-budget check at the API layer — `process` itself has no resource gate before staging potentially large CSVs. | Add a disk-budget check in `_process_dataset` before staging. |
| [BUG] | external_intake_cli.py:71-87 (`apply_main`) | When no workspace context is supplied, `apply_answer` runs with `workspace=None`; `_load_active_session` will only find a session under the derived `proposed_workspace` slug. If `prepare` was run with a workspace and `apply` without one (or vice versa), the session lookup silently fails with FileNotFoundError. The lock/idempotency path is also skipped in the no-context branch, so concurrent applies are unguarded. | Require consistent workspace context across prepare/apply, or persist a pointer to the active session location. |

## Cross-package coupling
- `core.storage.external_data`: `bounded_external_files`, `is_external_path`,
  `load_external_data_policy` — the policy/allowlist source of truth. **Under-used:**
  discovery/intake never consult it (see top findings).
- `core.storage.workspace_layout.WorkspaceLayout`: runtime dirs, `requirements_dir`,
  `reports_dir`, `evidence_dir`, `state_dir`, `memory_dir`, `load_settings`,
  `workspace_settings`. Register mode writes `dataset_allowlist` here, which is the
  real feed into downstream profiling/relationship gating.
- `core.resource.manager.ResourceManager`: disk-budget and API-concurrency decisions
  (preflight, api ingestion, catalog index).
- `core.paths.PROJECT_ROOT`: used by intake workflow to resolve a relative discovery path.
- `core.onboarding.workspace.cli_runner.run_workspace_command`: lock + event +
  idempotency wrapper for intake CLI.
- `aiohttp`, `polars`, `csv`, `hashlib`: API fetch, dataset profiling, provenance.
- Tests present: `tests/test_source_catalog.py`, `tests/test_external_source_discovery.py`,
  `tests/test_external_source_intake.py`, `tests/test_external_data_guard.py`.
- Entry points (pyproject): `source-catalog`, `prepare-source-catalog`,
  `ingest-source-catalog`, `discover-external-sources`,
  `prepare-external-source-intake`, `apply-external-source-intake`. All wired.

## Verdict
**Not production-ready as a security boundary; functionally mature otherwise.**
The catalog ingestion machinery (paged/streamed API fetch, retry/backoff, quarantine,
checkpoint-resume, provenance hashing, drift detection, large-catalog streaming index)
is well-engineered and clearly battle-tested. The blocking issue is governance: the
external-root **allowlist defined in `core.storage.external_data` is never enforced** by
either the discovery scanner or the local-source ingestion/registration path. Any
absolute host path can be enumerated and any host file can be copied/registered into a
governed workspace once a selection is approved, and API sources have no SSRF egress
control. These contradict the platform's "external roots are governed" contract. Fix the
three path/host-allowlist gaps (external_discovery `_validate`, catalog `_plan/_apply_local_source`,
API egress) plus the silent-success-on-parse-failure and symlink-walk issues before
exposing this to untrusted selections. Classification logic is reasonable but has
false-positive gaps (`.jsonl` as dataset, narrow system-dir excludes) that should be
tightened.

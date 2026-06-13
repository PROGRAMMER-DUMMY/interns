# ob-workspace-B (onboarding/validation/bootstrap/kickstart) — audit

## Purpose
This slice is the fresh-workspace ingestion + governance gate for the KPI/query control plane.
`WorkspaceOnboarder` scans `workspaces/<project>` user inputs (datasets, KPI registries, data-model
docs/images/PDFs), profiles datasets, and emits the normalized contract set under `interns/`
(`kpi_registry.json`, `domain_model.json`, `semantic_contract.json`, `profile_index.json`,
baseline SQL, evaluator/experiment scripts, open questions, lexicon-filled KPIs). `AutoBootstrap`
fingerprints inputs and decides reuse-vs-regenerate for the orchestration loop. `WorkspaceKickstarter`
is the enterprise bridge that discovers/classifies docs, runs bootstrap + feature resolution, and
writes the hybrid `config/tasks.json` entry. `WorkspaceArtifactValidator`
(`validate-workspace-artifacts`) is the read-only gate AGENTS.md requires to pass; it validates
artifact shape + several anti-fabrication invariants.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| onboarding.py | 2430 | Scan inputs, profile datasets, build normalized KPI/contract artifacts, incremental re-run | `WorkspaceOnboarder`, `KpiDefinition`, `OnboardingResult`, `_run_locked`, `_fill_kpi_gaps_with_lexicon/_with_derivation`, `_extract_tabular_kpis`, `_read_markdown_kpis`, `_dedupe_kpis_by_name`, `_clear_onboarding_artifacts` |
| validation.py | 1220 | Read-only artifact validator + anti-tamper gates | `WorkspaceArtifactValidator`, `_validate_*` per artifact, `_verify_harness_against_execution`, `_collect_pii_columns_from_sc` |
| bootstrap.py | 240 | Fingerprint-driven reuse/regenerate decision for the loop | `AutoBootstrap.ensure_ready`, `compute_fingerprint`, `_is_current`, `required_artifacts`, `check_databricks_readiness`, `DatabricksReadiness`, `BootstrapResult` |
| kickstart.py | 519 | Enterprise discovery + task-config upsert wrapper around bootstrap | `WorkspaceKickstarter.run/discover`, `_task_from_bootstrap`, `_upsert_task_config`, `_classify_doc`, `accepted_defaults` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | validation.py:1070-1083, 1186-1196 + onboarding.py:1236-1239 | Medallion PII check (`_collect_pii_columns_from_sc`) reads `sc["datasets"][].columns[].pii` / `pii_columns`, but the onboarder-generated semantic_contract has NO `datasets` key — it stores sensitivity as a flat `columns.<name>.is_sensitive` map. The PII-in-Silver invariant (check 6) therefore silently collects zero PII and can never fire on an onboarded workspace. | Make `_collect_pii_columns_from_sc` also read the flat `columns.<name>.is_sensitive` shape the onboarder actually writes, or have onboarding emit `datasets[].columns[].pii`. Add a test with the real onboarder contract shape. |
| [BUG] | medallion/design.py:575 vs onboarding.py:1246-1275 | Same shape divergence downstream: `_pii_lookup_from_semantic` / `_build_bronze_tables` read `semantic.get("datasets")` (expected dict), which is absent from the onboarder contract, so medallion bronze PII tagging gets nothing from the semantic contract. | Standardize the semantic_contract schema (one shape) and key both the SQL-mask path (`columns.is_sensitive`) and medallion off the same map. |
| [NOT-PROD] | bootstrap.py:169-182 vs onboarding.py:1664-1683 | Two divergent staleness mechanisms. `AutoBootstrap.compute_fingerprint` only hashes `docs/`+`datasets/`+`editable/sql_file`; it explicitly excludes everything under `interns/`. The onboarder's own incremental path additionally fingerprints durable decision stores (`decisions/kpi_definitions.json`, `workspace_feature_definitions.json`, `accepted_candidates.json`, `kpi_feature_mapping.json`). When the loop gates onboarding through bootstrap, accepting a KPI/feature definition does NOT bust the bootstrap manifest, so the loop reuses stale contracts that ignore the new human decision. | Fold the onboarder's `_decision_input_files()` into `AutoBootstrap._fingerprint_inputs()` (or have bootstrap always defer the currency decision to the onboarder's incremental manifest). |
| [BUG] | onboarding.py:1537, 2355 | Generated `onboarding_report.md` and `next_command` still emit the deprecated `uv run resolve-kpi-features ...`. AGENTS.md states `resolve-kpi-features` is deprecated and redirects to `prepare-kpi-blocker-panel`; the report hands the user the wrong command. | Emit `uv run prepare-kpi-blocker-panel --workspace <ws> --domain <domain>` as the next command. |
| [MISSING] | validation.py:213-217 | `_validate_feature_mapping` warning text references `resolve-kpi-features` (deprecated) and the validator never checks `semantic_contract.json` content at all beyond what onboarding wrote — there is no `_validate_semantic_contract`. Drift in the semantic contract (e.g. missing `columns`/`rules`) is uncaught by the gate. | Add a `_validate_semantic_contract` with an artifact contract; update the deprecated command string. |
| [MISSING] | validation.py:90-110 | The validator never checks `workspace_lexicon.json` or `kpi_format/current.json` (both written by onboarding) and does not assert KPI provenance fields (`metric_provenance`/`cuts_provenance`) that the resolver relies on to keep machine-guessed metrics out of `ready_for_sql`. A lexicon-inferred metric promoted to ready is not caught here. | Add provenance assertions to `_validate_kpi_registry` (warn when `metric_provenance != authored` and KPI reaches ready downstream). |
| [NOT-PROD] | onboarding.py:730-754 | `kpi_registry.json` is written twice per run via `_write_json` (line 730 pre-lexicon, line 751 post-fill). The first write is the documented two-phase seed for `build_workspace_lexicon`; acceptable, but each `_write_json` also upserts the metadata store, so the metadata `contracts/kpi_registry` doc is written twice (the first with un-filled cells). Harmless but wasteful and momentarily inconsistent if read between writes. | Seed the first phase with `_write_text` only (skip metadata upsert) and upsert once after the fill. |
| [NOT-PROD] | onboarding.py:755-757 | `workspace_lexicon` artifact path is recorded with a bare `str(...)` rather than through `_write_json`, so it is excluded from the metadata store and from `_metadata_collection_for_path` routing even though it sits in `contracts/`. `artifacts_exist` still file-checks it, so the skip path is safe, but metadata coverage is inconsistent with the other contracts. | Either record it through the writer path or document why it is metadata-exempt. |
| [NOT-PROD] | onboarding.py:248-257, 256-257 | Bare `except Exception: pass` when reading OCR sidecars swallows all errors silently (no `[~]` warning), unlike the surrounding code which surfaces degradations. A corrupt sidecar is invisible. | Append a `[~] sidecar_read_failed` warning instead of `pass`. |
| [NOT-PROD] | onboarding.py:1029-1030 | `_load_column_glosses` filters interns by substring `"/interns/" in path.as_posix()`. A workspace literally named with `interns` in a parent path, or a non-posix edge, is fragile vs the layout-based `interns_dir in parents` check used elsewhere (e.g. line 284). | Use `self.layout.interns_dir.resolve() in path.resolve().parents` for consistency. |
| [BUG] | bootstrap.py:138-167 | `check_databricks_readiness` runs a live `DatabricksClient(...).health_check()` during `ensure_ready` whenever `cfg.databricks.is_active()`, i.e. on every loop bootstrap. This is a network call inside what AGENTS.md calls a "local-safe" gate, runs before any remote-execution approval, and adds latency/failure surface to onboarding currency checks. | Gate the health check behind the same remote-approval flag the execution backend uses, or make it lazy/cached. |
| [NOT-PROD] | kickstart.py:265-289 | `_upsert_task_config` rewrites the human-owned `config/tasks.json` and, on a JSON-decode error, overwrites it with a fresh skeleton after backing up to `.json.invalid`. A malformed tasks.json (e.g. mid-edit) silently loses all prior task entries on the next kickstart. | On decode error, raise rather than reset; let the user fix the file. |
| [NOT-PROD] | validation.py:816-872 | `_verify_harness_against_execution` re-executes generated SQL via DuckDB inside the "read-only" validator. Good anti-fabrication intent, but it is not actually read-only (creates/queries a DuckDB db, runs the harness) and `except Exception: return` makes any execution problem an invisible skip — a tampered manifest in an environment where execution fails for unrelated reasons passes the gate silently. | Document that the validator executes SQL; emit a warning (not silent skip) when verification cannot run so reviewers know the anti-tamper check was bypassed. |
| [DUP] | onboarding.py:1879-1916, 2230-2315 | Several thin pass-through wrappers (`_read_excel_kpis`, `_read_tabular_kpis`, `_first_existing`, `_first_index`, `_cell_at`, `_clean_cell`, `_infer_metric_and_cuts`, `_is_template_kpi_row`) just re-export `core.onboarding.kpi.text_parser` functions; `_is_template_kpi_row` is also re-implemented independently in validation.py:1130. Two copies of the template-row predicate can drift. | Import the single text_parser predicate in validation.py instead of re-implementing. |
| [NOT-PROD] | onboarding.py:1358-1381 | `_write_baseline_sql` builds SQL via f-string `_sql_escape` (single-quote doubling only). Values are workspace KPI text; not an injection risk in this trusted-input context, but newlines/backslashes in KPI names are not escaped and could break the generated DuckDB `VALUES` block. | Use a parameter table or escape control chars; or note KPI text is sanitized upstream. |

## Cross-package coupling
- `bootstrap.AutoBootstrap` is the loop's onboarding gate (`core/orchestration/loop.py:76`) and is also
  driven by `kickstart.WorkspaceKickstarter.run` and `flow.py`. It instantiates `WorkspaceOnboarder`
  directly — bypassing the governed CLI envelope (`cli_runner`), so loop-triggered onboarding does not
  get the lock/idempotency/event wrapper that the `apply-*` commands get (onboarding takes its own
  `workspace_lock` in `run()`, so the lock is covered, but not idempotency replay).
- `onboarding` pulls in a wide surface: `kpi.text_parser`, `kpi.kpi_format_detector`,
  `kpi.workbook_structure`, `kpi.metric_derivation`, `kpi.kpi_definition`, `lexicon.builder`,
  `incremental`, `profiling.data_model_profiler`, `resource.manager`, `governance.data_policy`,
  `governance.phi_gate`, `documents.*`, `data_model.image_parser`, `tools.methodology_parser`,
  `tools.list_workspace_files`, `storage.metadata_store/workspace_layout/workspace_lock/external_data`,
  `contracts.versioning`. Most optional integrations are import-guarded and degrade to `[~]` warnings.
- `validation` couples to `artifact_contracts.*` (shape contracts), `kpi.execution_harness`
  (re-execution + intent-block markers), `documents.dictionary_reconciliation`, `medallion.manifest`,
  and `workspace.bugs` (`WorkspaceBugDetector`, also run inside kickstart and onboarding paths).
- Semantic-contract shape is the load-bearing coupling defect: onboarding writes
  `{columns: {<name>: {is_sensitive}}}` while validation (medallion check 6) and `medallion.design`
  read `{datasets: [...]}`. The SQL generator reads the onboarder shape; medallion reads the other.

## Verdict
The onboarding/incremental machinery is the most mature part of this slice: deterministic input
ordering (sorted), a documented two-phase lexicon fill, a byte-identical nothing-changed replay,
per-dataset profile reuse, removed-dataset cleanup, and consistently import-guarded optional
extractors that degrade to warnings rather than hard-failing. The validator is genuinely strong on
anti-fabrication (harness re-execution, hash-staleness, dictionary-conflict tainting, silent-dead-end
detection). It is close to production-ready, with these blockers: (1) the semantic_contract shape
divergence that makes the medallion PII-in-Silver invariant a dead check on real onboarded
workspaces; (2) the two-headed staleness model where `AutoBootstrap` ignores the human-decision
stores that the onboarder's own incremental path tracks, so accepted KPI/feature definitions can be
reused-over by the loop; (3) deprecated `resolve-kpi-features` guidance baked into the generated
report and next-command; and (4) a network health check + SQL re-execution living inside paths
labeled "local-safe" / "read-only". None are workspace-specific (workspace-agnosticism holds — no
domain vocabulary in the hot paths), but the semantic-contract and bootstrap-staleness items are
correctness gaps that the validator does not catch.

# ob-kpi-C (blocker panel / preview) — audit

## Purpose

This slice builds the governed KPI **blocker question panel** — the only sanctioned Ask-User
surface for unresolved KPI feature mappings (CLAUDE.md forbids freehand prompts). The panel
carries JSON-backed options (physical-column, derived-formula, dictionary-conflict,
no-supporting-evidence, kpi-definition, intent-facet, base-source, and CLI-agent-proposal shapes),
attaches a DuckDB-executed sample **preview** per option (cache-first), and routes answers through
an idempotent `apply-kpi-panel-answer` CLI plus a two-step CLI-agent confirm flow. It is the
contract boundary between the deterministic Python resolver and human/agent judgment.

## Files

| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `blocker_question_panel.py` | 2872 | Build panel JSON+MD from `kpi_feature_mapping.json`; pick `current` blocker; emit option payloads with proof packets; compose preview sections. | `BlockerQuestionPanelBuilder`, `_build_questions`, `_question_for_cluster`, `_derived_option_payload`, `_physical_option_payload`, `_cli_agent_evidence_pack`, `_render_markdown`, `_attach_preview_sections`, `_execute_option_preview`, `_empty_panel` |
| `blocker_cli.py` | 211 | CLI entrypoints `prepare_main`/`apply_main`; workspace-lock + trajectory recording + idempotency. | `prepare_main`, `apply_main` |
| `blocker_workflow.py` | 432 | Deterministic wrappers: onboard→resolve→derived-md→panel→validate; answer resolution + option application + wiki note. | `prepare_kpi_blocker_panel`, `apply_kpi_panel_answer`, `_apply_option`, `_resolve_answer`, `_one_option` |
| `kpi_confirmation_panel.py` | 187 | Pure renderer for a detected KPI-file format confirmation panel (separate stage from blocker). | `build_kpi_confirmation_panel`, `render_kpi_confirmation_markdown` |
| `panel_preview_cache.py` | 158 | stdlib-only on-disk cache of executed previews keyed on SQL+dataset mtime; atomic writes; eviction. | `compute_preview_cache_key`, `load_cached_preview`, `save_cached_preview`, `evict_stale_entries` |
| `panel_preview_executor.py` | 236 | LIMIT-bounded DuckDB `:memory:` SQL execution with wall-clock budget via daemon thread; never raises. | `PreviewResult`, `execute_preview`, `_run_query`, `_json_safe` |
| `cli_agent_confirm_cli.py` | 232 | Step-2 of CLI-agent flow: flip `cli_agent_proposed` → `user_confirmed`/`cli_agent_rejected`; rewrite mapping+definitions. | `confirm_cli_agent_proposal`, `_find_proposed_entry`, `main` |

## Findings

| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | panel_preview_executor.py:147-208 | `execute_preview` does a process-global `os.chdir(repo_root)` to resolve relative `read_csv_auto` paths, restoring cwd in `finally`. This is NOT thread-safe: the panel runs many option previews and the wider control plane is multi-threaded; a concurrent thread sees the wrong cwd during the ~3s window, and the worker is a *daemon* thread that can outlive the chdir restore on timeout (query still runs against changed cwd / future cwd flips mid-query). | Pass absolute CSV paths into the SQL (rewrite `read_csv_auto('rel')` → absolute) or use DuckDB `SET file_search_path`; avoid `os.chdir` entirely. |
| [BUG] | cli_agent_confirm_cli.py:130-174 | The confirm/reject path loads, mutates, and `write_text`s `kpi_feature_mapping.json` and `workspace_feature_definitions.json` with **no `workspace_lock`** (unlike `blocker_cli.apply_main`). Concurrent `apply-kpi-panel-answer` or `prepare` can interleave and lose writes. | Wrap the mutation in `workspace_lock(workspace_path)` as the other CLIs do. |
| [BUG] | cli_agent_confirm_cli.py:138-147 (confirm branch) | On `confirm`, the row state is forced to `CONFIRMED_STATE` but `apply_workspace_definitions_to_mapping` + `recompute_mapping_status` are then re-run from definitions; if the definition entry's resolution did not re-resolve the feature (e.g. formula no longer matches columns) the row can be silently overwritten back to blocked while `previous_state`/`new_state` in the returned packet still report success. No post-recompute assertion that the feature actually reached a READY state. | After recompute, assert the feature row is in a ready state on confirm; surface a warning/error in `ConfirmResult` otherwise. |
| [NOT-PROD] | blocker_question_panel.py:171-176 | Preview composition wrapped in a blanket `except Exception` that silently drops ALL previews and only stashes `preview_compose_error` on `current`. A systemic failure (e.g. DuckDB import error) means every panel renders with zero previews and no operator-visible signal beyond one buried string. | Narrow the except, and log/emit a validator warning when `preview_compose_error` is set so the degradation is visible. |
| [NOT-PROD] | blocker_question_panel.py:1058-1096 | `_excel_cell_trace` opens the workbook with `read_only=False` (full load) on every panel build for cell-coordinate tracing, and matches KPI rows by `str(first cell) == name` — fragile for multi-line/whitespace-differing questions and slow for large workbooks. | Use `read_only=True`, normalize whitespace on compare, and cache the trace. |
| [MISSING] | blocker_workflow.py:267-298 (custom option) | The `custom` apply path classifies a definition as a derived formula purely by regex `[()<>=]|exists|case`. A custom plain-text rule containing parentheses (e.g. "revenue (net)") is misclassified as a formula with bogus `input_columns` from quoted-identifier extraction. No validation that extracted `formula_inputs` are real columns. | Validate extracted inputs against the profile/feature columns; fall back to `custom_business_definition` when they don't resolve. |
| [MISSING] | validation.py:367-395 (cross-pkg gate) | The panel-option schema gate only deep-validates options that set `json_backed: true`. A `json_backed:false` option with neither `expected_answer_shape` nor `custom` id passes (only `_is_unappliable_placeholder_option` flags non-custom placeholders *that have* a shape). A prose-only option lacking both could slip through as a non-recommended option. The builder never emits such a shape today, but the contract does not forbid it. | Add a validator rule: every non-`custom` option must be `json_backed` OR carry `expected_answer_shape`; reject bare prose options. |
| [BUG] | blocker_cli.py:118-145 vs blocker_workflow.py:177-182 | Idempotency `op_id` is computed only from CLI args (answer/custom/evidence). The same `--answer option_a` applied against a *different current panel* (after the panel rotated to a new feature) returns the cached prior result without applying to the new feature — a stale-replay hazard. The `op_id` does not include the current panel's `feature`/`question_id`. | Fold the current panel `feature`+`question_id` (or panel content hash) into `compute_op_id`. |
| [INTEGRATION] | panel_preview_cache.py:30-49 | Cache key hashes SQL + dataset `(path, mtime_ns)`, but the executed preview also passes through `redact_rows`/`neutralize_rows` using `_workspace_redaction_patterns` derived from the workspace `data_policy.json`. The cache key does NOT include the policy/patterns, so editing `data_policy.json` to widen redaction yields a stale cached (under-redacted) preview until the dataset mtime changes. | Include a hash of the active redaction patterns in `compute_preview_cache_key`. |
| [INTEGRATION] | blocker_question_panel.py:118-139, 2030-2042 | Three additive routing imports (`intent_contract`, `base_source_selector`, `delegation.routing_for`) each swallow all exceptions to "never break panel emission". Correct for resilience, but a real wiring regression (renamed symbol) goes completely silent — no hard-blocking intent facet / base-source question would ever surface. | Keep the fallback but emit a one-line `panel["routing_degraded"]` marker (or validator warning) when an import/derivation fails. |
| [DUP] | blocker_question_panel.py:2507-2519 & blocker_workflow.py:411-419 | `_norm`/`_rel` helpers re-implemented in both modules (and `_normalize_feature` again in cli_agent_confirm_cli.py:55). Minor drift risk (`_rel` uses `.as_posix()` in panel vs `.replace("\\","/")` in workflow). | Promote to a shared `core.onboarding.kpi` util. |
| [NOT-PROD] | cli_agent_confirm_cli.py:177 | `ConfirmResult.workspace=str(workspace)` returns the raw input arg (may be relative/abs inconsistently) whereas mapping/definitions paths are absolute `str(...)` — inconsistent with the rel-path convention used everywhere else in this slice. | Normalize to `_rel(workspace_path, root)`. |
| [DEAD] | blocker_question_panel.py:1940-1981 | `_blocked_kpi_details` is only consumed by `_empty_panel` (blocked-without-question branch). Fine, but `kpi_confirmation_panel.py` (`build_kpi_confirmation_panel`) is a *separate stage* unrelated to the blocker panel — included in scope but only wired into onboarding/green_gate, not the blocker flow. Not dead, but note the naming overlap can mislead. | No code change; documented for clarity. |

## Cross-package coupling

- **Resolver / mapping**: panel is a pure read of `interns/generated/contracts/kpi_feature_mapping.json`
  plus `kpi_registry.json`, `profile_index.json`, `workspace_feature_definitions.json`,
  `data_dictionary/index.json`. `prepare_kpi_blocker_panel` orchestrates
  onboard → `RelationshipContractBuilder` → `KPIFeatureResolver` → `DerivedFeatureMarkdownConverter`
  → builder → `WorkspaceArtifactValidator` (hard gate; raises `WorkflowBlockedError` on failure).
- **Apply path**: `apply_kpi_panel_answer` re-runs the validator *before* applying (good — a bad panel
  can't be applied), resolves the option, calls `feature_resolver.apply_workspace_definition` or
  `intent_contract.record_intent_answer`, writes a wiki note, then re-prepares the panel (which
  re-validates). Idempotency via `core.onboarding.workspace.idempotency` (op recorded only on success).
- **Governance/PII**: previews go through `pii_redaction.redact_rows` then
  `injection_guard.neutralize_rows`; `data_policy.json` can only widen redaction. Contract registered
  via `contracts.versioning.register_contract` and enforced by `validation.BLOCKER_QUESTION_PANEL_CONTRACT`.
- **Execution**: previews use a self-contained DuckDB `:memory:` executor (no shared ExecutionBackend),
  distinct from the production KPI execution_harness.
- **CLI-agent flow**: `_cli_agent_evidence_pack`/`_cli_agent_task` (panel) →
  `apply-kpi-panel-answer --via-cli-agent` (state `cli_agent_proposed`) →
  `confirm-cli-agent-proposal` (cli_agent_confirm_cli.py) using `memory.workspace_definitions`.
- Entry points confirmed in `pyproject.toml:69,86-88`.

## Verdict

The slice is **largely production-ready and well-governed**: option schemas are rich and JSON-backed,
the validator hard-enforces derived/physical option fields and blocks unappliable-placeholder
recommendations, prose-only options are mostly designed out, the preview executor is robust
(never raises, daemon-thread timeout), and the cache uses atomic writes with corrupt-entry
tolerance. The apply path is genuinely idempotent and lock-guarded.

Blocking before prod: **(1)** the `os.chdir` in the preview executor is a real thread-safety bug in a
multi-threaded control plane; **(2)** `confirm-cli-agent-proposal` mutates governed contracts with no
workspace lock; **(3)** idempotency `op_id` omits the current panel feature, risking stale replay onto
a rotated panel. Secondary: the redaction-pattern cache-key gap (stale under-redacted previews after
policy widening) and the custom-option formula misclassification. None are silent-data-corruption of
KPI numbers, but the chdir and unlocked-confirm issues are concurrency hazards that should be fixed
before multi-user/Databricks operation.

# onboarding-root (cross-cutting contracts + pipeline) — audit

## Purpose
These twelve root-level modules of `core/onboarding/` provide the cross-cutting
*contracts* and *pipeline scaffolding* layered between the workspace onboarding
sub-packages (kpi/, workspace/, relationships/, harness/, ...). They cover:
- versioned artifact contract definitions (`artifact_contracts.py`) consumed by the
  workspace validator;
- medallion layer governance standards (`bronze_silver_standards.py`);
- the raw-source catalog contract (`catalog_contract.py`);
- soft-deprecation/redirect helpers for stage CLIs (`cli_deprecation.py`);
- the data-quality + duplicate-review harness/panel/decision trio (`data_quality.py`);
- the provenance DAG builder + query/health (`evidence_graph.py`);
- the canonical decision-panel shape (`panel_contract.py`);
- the route -> plan -> SQL -> deployment pipeline chain
  (`pipeline_plan.py`, `pipeline_sql_generator.py`, `pipeline_deployment_plan.py`);
- source-family schema-drift contracts (`source_family_contracts.py`).

They are local-safe, deterministic, file-writing builders driven by argparse `main()`
entry points and wrapped by `workspace/cli_runner.run_workspace_command`. All are wired
into `pyproject.toml` console scripts, the validator, the green gate, and the harness.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 33 | Re-exports onboarding sub-package public API (bootstrap, databricks, kpi, workspace). Does NOT export any root-module symbol. | (re-export list) |
| `artifact_contracts.py` | 146 | Frozen `ArtifactContract` + 16 contract constants (artifact_type / version / generated_by / regenerate_command). Consumed by `workspace/validation.py`. | `ArtifactContract.validate`, 16 `*_CONTRACT` constants |
| `bronze_silver_standards.py` | 153 | Writes bronze/silver/gold allowed/forbidden transformation standards, transformation manifest, and workflow reroute policy. | `BronzeSilverStandardsBuilder.build` |
| `catalog_contract.py` | 120 | Builds `catalog_contract.json` from profile_index: one object per profiled dataset with local_file + duckdb_view bindings. | `CatalogContractBuilder.build` |
| `cli_deprecation.py` | 87 | Stderr-only soft-deprecation + redirect hints for stage CLIs; opt-out via env vars. | `warn_soft_deprecated_cli`, `announce_deprecated_cli_redirect`, `is_internal_cli_call` |
| `data_quality.py` | 423 | Generic duplicate-PK detection across profiled datasets; harness + review panel + decision recorder. Redacts samples. | `DataQualityHarness`, `DuplicateReviewPanel`, `DuplicateDecisionRecorder`, `_detect_duplicate_pk_candidates` |
| `evidence_graph.py` | 716 | Provenance DAG over workspace artifacts (datasets, columns, KPIs, features, terms, SQL, trajectory, harness findings) + health + query. | `WorkspaceEvidenceGraphBuilder`, `WorkspaceEvidenceGraphQuery`, `_query_term`, `_query_impact` |
| `panel_contract.py` | 111 | Canonical decision-panel normalize/validate; non-destructive setdefault fill + roster footer via `routing_for`. | `normalize_decision_panel`, `validate_decision_panel` |
| `pipeline_deployment_plan.py` | 80 | Dry-run-first deployment plan; gates `apply` on remote-approval env for external/warehouse targets. | `PipelineDeploymentPlanner.build` |
| `pipeline_plan.py` | 431 | Route selection (medallion/kpi_only) -> pipeline plan with blockers; format panel + denominator/grain/base-source decision recorders. | `DataEngineeringRoutePlanner`, `PipelinePlanner`, `PipelineDecisionRecorder`, `PipelineFormatPanel`, `_select_route` |
| `pipeline_sql_generator.py` | 131 | Emits catalog-bootstrap + bronze/silver/gold view SQL (DuckDB) from catalog + plan; refuses when plan has blockers. | `PipelineSQLGenerator.generate`, `_reader_for`, `_object_stem` |
| `source_family_contracts.py` | 176 | Groups profiles into source families; computes common/drift columns, type drift, release-token years. | `SourceFamilyContractBuilder.build`, `_family_name`, `_extract_data_year` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | pipeline_sql_generator.py:56 | Silver view is emitted as `SELECT DISTINCT *` — an unconditional, un-gated deduplication. This directly contradicts the governing pipeline plan, whose silver layer declares `deduplication: {application: approval_gated}` (pipeline_plan.py:252) and the bronze/silver standards which list `deduplication_application` as a forbidden bronze transform and require approval. The generator applies dedup silently regardless of whether a `duplicate_decisions.json` approval exists. | Honor the duplicate-decision contract: emit plain `SELECT *` for silver unless `duplicate_decisions[].approved_for_sql_mutation` is true (it is hardcoded `False` today), or move dedup to gold per the approval gate. |
| [BUG] | pipeline_deployment_plan.py:37 | Remote-apply gate only triggers for `target in {"external","warehouse"}`. A `databricks`/`remote`/`uc` target string (or any other remote name) in `apply` mode bypasses the `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` check entirely and reports `planned_apply`. The allow-list is brittle for a security-relevant gate. | Invert to a local allow-list: require the env unless `target == "local"` (and `mode == "apply"`). Fail closed on unknown targets. |
| [NOT-PROD] | data_quality.py:336-353 | `_count_duplicate_values` loads entire CSV columns into Python sets row-by-row via `csv.DictReader`; on large datasets this is O(n) memory and slow, and silently returns 0 on any `OSError`. The harness is "Polars-first" per CONTEXT but uses stdlib csv. | Use Polars `scan_csv`/group-by for duplicate counts; log (not swallow) read failures. |
| [BUG] | data_quality.py:281,352,330 | Three `except` clauses swallow errors silently: `_profiled_datasets` catches bare `Exception` -> `[]`; `_count_duplicate_values` and `_infer_pk_candidates_from_csv_header` catch `OSError` -> `0`/`[]`. A malformed profile_index or unreadable dataset makes the DQ harness report `ok=True` (no findings) — a false pass on a governance gate. | Narrow excepts, surface a finding/warning when a candidate dataset cannot be read instead of treating it as clean. |
| [BUG] | evidence_graph.py:360 | `sql_file.read_text(..., errors="ignore")` then regex-extracts every `"quoted"` identifier as a `term` node. SQL string literals using double-quotes, or quoted aliases, become spurious provenance terms; `errors="ignore"` can also corrupt multibyte identifiers. Provenance "introduced_term" data is noisy/unreliable. | Parse identifiers with a real tokenizer or restrict to `FROM`/`JOIN`/column contexts; drop `errors="ignore"` or log decode issues. |
| [INTEGRATION] | bronze_silver_standards.py:101-106 | Reroute policy `replacement_command`s reference `uv run harness data-quality` and `uv run harness layered-pipeline`. Confirm these subcommands exist under the current `harness` CLI; the rest of the package uses `run-data-quality-harness` / `prepare-pipeline-plan` naming, so these may be stale strings. | Grep the harness CLI for `data-quality`/`layered-pipeline` subcommands; align the reroute strings with real commands. |
| [INTEGRATION] | bronze_silver_standards.py / source_family_contracts.py / catalog_contract.py | These three builders do NOT call `register_contract` at import, unlike `pipeline_plan.py:16`. Their artifact_types (`bronze_silver_standards.json`, `source_family_contracts.json`, `catalog_contract.json`) are therefore absent from the versioning registry, so `migrate()` passes them through unchanged and future schema bumps have no migration path. `catalog_contract.json` IS validated via `CATALOG_CONTRACT_CONTRACT`, but is not registered for migration. | Add `register_contract(...)` calls for each writer, consistent with pipeline_plan. |
| [MISSING] | source_family_contracts.py:115-120 | `source_family_contracts.json` payload omits `version` and `generated_by`, unlike every sibling contract. It has no `ArtifactContract` in `artifact_contracts.py` and is not checked by the validator, so it is unversioned and unguarded. | Add `version`/`generated_by` keys and a `SOURCE_FAMILY_CONTRACTS_CONTRACT`. |
| [NOT-PROD] | catalog_contract.py:81 / source_family_contracts.py:154 / pipeline_*.py `_load_json` | These `_load_json` helpers call `json.loads` WITHOUT a try/except (unlike `evidence_graph._load_json` which guards `JSONDecodeError`). A corrupt upstream JSON aborts the whole builder with an uncaught exception. | Wrap in `try/except json.JSONDecodeError` returning `{}` (or surface a clear error), consistent with evidence_graph. |
| [BUG] | source_family_contracts.py:98 | `bronze_plan.partition_columns` is hardcoded to `["report_year"]` for every family regardless of whether that column exists in the schema. This is not workspace-agnostic and will produce an invalid partition spec for datasets lacking `report_year`. | Derive partition columns from observed schema/release tokens, or omit when not present. |
| [NOT-PROD] | data_quality.py:84,135 / pipeline_plan.py:213 / catalog_contract.py:30 | Builders `mkdir`/`ensure_runtime_dirs` and write files as a side-effect of "prepare"/"build". `DataQualityHarness.run()` is invoked transitively by routing (pipeline_plan.py:142), so a read-style `prepare-data-engineering-route` mutates DQ artifacts. Acceptable here but note the read/write coupling. | Document the side effects; consider a read-only mode for route preview. |
| [DUP] | (root modules) `_rel`, `_load_json`, `_safe_name`, `_now` | `_rel` is re-defined in 8 of these files; `_load_json` in 6; `_safe_name` duplicated between catalog_contract and pipeline_sql_generator with identical bodies; `_now` in two. Divergence already visible (`_load_json` guards JSONDecodeError only in evidence_graph). | Extract to a shared `core/onboarding/_pathutils.py` (or `core/storage`) so the JSON-decode guard is uniform. |
| [DEAD] | pipeline_plan.py:144-147 | `source_family_summary` is only populated when `selected == "medallion" and not kpis`. The `SourceFamilyContractBuilder` is therefore reachable only on the no-KPI medallion branch; on every KPI-bearing medallion route it stays `{family_count: 0}` even though families are meaningful there. Likely under-integrated rather than dead. | Build source-family contract for all medallion routes, not just KPI-empty ones. |

## Cross-package coupling
- `artifact_contracts.py` -> consumed centrally by `core/onboarding/workspace/validation.py`
  (15 of 16 constants wired, line refs above). `DUPLICATE_DECISIONS_CONTRACT` and the rest
  all validated. This is the contract authority for the validator gate.
- `evidence_graph.py` -> `core.onboarding.harness.trajectory_recorder.load_trajectory`,
  `core.presentation.console_tables.render_markdown_table`, `core.storage.workspace_layout`.
  Consumed by `dashboard.py` / `core/dashboard_services.py`, the reliability/project harness,
  and tests.
- `panel_contract.py` -> `core.onboarding.workspace.delegation.routing_for` (lazy import).
  Consumed by kpi/blocker_question_panel, kpi/generation_workflow,
  data_model/generation_workflow, workspace/flow.
- `cli_deprecation.py` -> consumed by kpi/feature_resolver, kpi/blocker_question_panel,
  features/derived_markdown, green_gate, CI. Both `warn_*` and `announce_*` are live (no dead shim).
- pipeline chain: `pipeline_plan.py` imports CatalogContractBuilder, DataQualityHarness,
  SourceFamilyContractBuilder (intra-root) + `core.contracts.versioning.register_contract`.
  `pipeline_sql_generator.py` output (`pipeline_layers.sql`) consumed by
  `harness/pipeline_execution_harness.py`. `pipeline_deployment_plan.py` is standalone, wired
  to pyproject + green_gate + tests.
- All builders depend on `core.storage.workspace_layout.WorkspaceLayout` and `core.paths.PROJECT_ROOT`.
- `__init__.py` re-exports ONLY sub-package symbols; none of these 11 root modules are re-exported
  there (callers import them by full module path). Not a bug, but worth noting for discoverability.

## Verdict
Largely production-shaped: every module is consumed (no truly dead code), contract coverage by
the validator is broad, the deployment plan is dry-run-first, and the SQL generator correctly
refuses to emit when the plan carries blockers and keeps raw paths confined to a bootstrap block.

Two correctness/governance bugs should block a clean bill of health:
(1) the silver `SELECT DISTINCT *` performs un-gated deduplication that contradicts the
approval-gated dedup policy the plan and standards advertise, and
(2) the deployment remote-approval gate uses a fragile target allow-list that a non-`external`/
`warehouse` remote target bypasses. Both are small fixes but undermine the governance story.

Secondary: several silent `except` paths in the data-quality harness can turn read/parse failures
into false "ok" passes; three contracts skip `register_contract`/versioning; `source_family_contracts`
is unversioned/unvalidated and hardcodes `report_year` partitioning; and `_rel`/`_load_json`
duplication has already diverged (only evidence_graph guards JSON decode errors). Recommend fixing
the two BUGs and the silent-except DQ paths before relying on these as production governance gates;
the rest are hardening/consistency items.

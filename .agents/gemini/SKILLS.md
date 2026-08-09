# Gemini Skill Adapter

This file is generated from canonical repo skills. Do not hand-edit it.

## Routing Rules

- Treat `skills/*/SKILL.md` as the source of truth.
- If the user explicitly names `$skill-name` or `skill-name`, load that skill.
- Otherwise match the request to skill descriptions and load the smallest relevant skill set.
- If multiple skills match, order them by dependency and keep context minimal.
- If local file access is available, open the listed `SKILL.md` before applying a skill.
- If local file access is unavailable, use embedded bodies only when this adapter was generated with full embedding.
- Hard stop: before choosing any project workflow route or next command, read `.agents/tools.json` or this adapter in the active session; if that did not happen, stop, reread it, and restart route selection.
- Hard stop: for external/profiled workspaces with no KPIs, do not run KPI feature resolution before `build-source-family-contracts`; source-family/schema-drift planning comes first.

## Project Tool Registry

- Source: `.agents/tools.json`
- Before using project tools, honor each registered command's `safety` guidance.
- Secret display safety: Never print or paste .env, .databrickscfg, private keys, tokens, connection strings, bearer headers, cookies, or shell environment dumps. Report only existence/status or redacted key names such as OPENAI_API_KEY=<redacted>.
- Dataset access safety: Use generated profile artifacts before raw data: read interns/generated/profiles/profile_index.json and the relevant *.profile.json first, and never paste raw dataset contents into prompts. Bounded samples only when profiles are insufficient, and state why.

### Registered Tools

- `apply-blueprint-answer`
  - Command: `uv run apply-blueprint-answer --workspace <workspace>`
  - Use when: The solution blueprint: what we will do, end to end, before we do any of it.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-data-model-answer`
  - Command: `uv run apply-data-model-answer --workspace <workspace>`
  - Use when: user answered the current data-model generation panel; draft data-model artifacts should be produced under interns
  - Outputs: interns/generated/requirements/data_model_draft.json; interns/reports/data_model_generation
  - Safety: local_safe_validated_write
  - Required skills: domain-model; stakeholder-memory; grill-requirements
- `apply-data-model-blocker-answer`
  - Command: `uv run apply-data-model-blocker-answer --workspace <workspace>`
  - Use when: user answered the current data-model blocker panel; accepted option should apply a structured data-model operation; next blocker panel should be generated
  - Outputs: updated interns/generated/requirements/data_model_draft.json; next interns/reports/data_model_blocker_panel/current.json; next interns/reports/data_model_blocker_panel/current.md
  - Safety: local_safe_validated_write
  - Required skills: domain-model; stakeholder-memory; grill-requirements
- `apply-data-quality-answer`
  - Command: `uv run apply-data-quality-answer --workspace <workspace>`
  - Use when: Data-quality rules authored the same way KPI features already are: ask,
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-data-source-answer`
  - Command: `uv run apply-data-source-answer --workspace <workspace>`
  - Use when: Workspace-level data-source panel: an explicit, human-confirmed, once-asked
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-design-panel-answer`
  - Command: `uv run apply-design-panel-answer --workspace <workspace>`
  - Use when: Medallion design-panel ratification (minimal slice of Priority 3.6).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-document-candidate`
  - Command: `uv run apply-document-candidate --workspace <workspace>`
  - Use when: Governed document candidate promotion (Phase 3: promote path, step 2).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-drift-answer`
  - Command: `uv run apply-drift-answer --workspace <workspace>`
  - Use when: Governed CLI entry points for schema evolution.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-duplicate-review-answer`
  - Command: `uv run apply-duplicate-review-answer --workspace <workspace>`
  - Use when: user answered the current duplicate review panel; accepted duplicate decision should be resolved against current.json; decision should be recorded without executing deduplication or quarantine SQL
  - Outputs: interns/generated/contracts/duplicate_decisions.json; interns/reports/duplicate_review/current.json; interns/reports/duplicate_review/current.md
  - Safety: local_safe_validated_decision_write_no_auto_dedup_or_quarantine_sql_mutation_milestone_1
  - Required skills: workspace-governance; stakeholder-memory; data-engineering-pipeline-design
- `apply-external-source-intake`
  - Command: `uv run apply-external-source-intake --workspace <workspace>`
  - Use when: user answered the external source intake panel; metadata-only discovery should run after route selection; saved external-source defaults or change reasons should be recorded
  - Outputs: external_source_intake_session.json; external_source_discovery.json; external_source_discovery.md; docs/source_selection.generated.json; state/team_memory/external_source_intake_preferences.json
  - Safety: local_safe_metadata_only_until_review_gate
  - Required skills: workspace-governance; data-engineering-pipeline-design; stakeholder-memory
- `apply-intake-answer`
  - Command: `uv run apply-intake-answer --workspace <workspace>`
  - Use when: Governed CLI entry points for Phase 0/1.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-kpi-definition`
  - Command: `uv run apply-kpi-definition --workspace <workspace>`
  - Use when: Apply a human-confirmed KPI definition (metric / grain / filters).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-kpi-generation-answer`
  - Command: `uv run apply-kpi-generation-answer --workspace <workspace>`
  - Use when: user answered the current KPI generation panel; KPI generation interview needs to advance; draft KPI registry and evidence proof should be produced
  - Outputs: updated kpi_generation_session.json; next kpi_generation/current.json; interns/generated/requirements/kpi_registry_draft.json
  - Safety: local_safe_validated_write
  - Required skills: grill-requirements; stakeholder-memory; workspace-kpi-query-optimizer
- `apply-kpi-panel-answer`
  - Command: `uv run apply-kpi-panel-answer --workspace <workspace>`
  - Use when: user answered the current blocker question; accepted option should be applied without inventing unsupported resolver flags; friendly answer must be resolved against current.json
  - Outputs: interns/generated/contracts/workspace_feature_definitions.json; updated kpi_feature_mapping.json; next validated blocker_question_panel/current.json
  - Safety: local_safe_validated_write
  - Required skills: domain-model; stakeholder-memory; workspace-kpi-query-optimizer
- `apply-phi-review-answer`
  - Command: `uv run apply-phi-review-answer --workspace <workspace>`
  - Use when: PHI/PII review-and-consent panel (Priority 1.1).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-pipeline-decision`
  - Command: `uv run apply-pipeline-decision --workspace <workspace>`
  - Use when: user approved a percentage or ratio KPI denominator scope; pipeline_plan.json is blocked by percentage_denominator_scope_unresolved; approved pipeline decisions should be recorded before regenerating the plan
  - Outputs: interns/generated/contracts/pipeline_decisions.json
  - Safety: local_safe_validated_decision_write_requires_user_approval
  - Required skills: data-engineering-pipeline-design; stakeholder-memory
- `apply-pipeline-format-answer`
  - Command: `uv run apply-pipeline-format-answer --workspace <workspace>`
  - Use when: user answered the pipeline format panel; accepted storage format should be recorded before pipeline planning; medallion plan should use the approved table/file format
  - Outputs: interns/generated/contracts/pipeline_decisions.json
  - Safety: local_safe_validated_decision_write_requires_user_approval
  - Required skills: data-engineering-pipeline-design; stakeholder-memory
- `apply-provisioning`
  - Command: `uv run apply-provisioning --workspace <workspace>`
  - Use when: Governed CLI entry points for provisioning + ingestion generation.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `apply-relationship-answer`
  - Command: `uv run apply-relationship-answer --workspace <workspace>`
  - Use when: user approved or rejected a relationship contract; profile_validated relationship needs governed promotion; agent must not hand-edit relationship_contracts.json
  - Outputs: updated interns/generated/contracts/relationship_contracts.json; recomputed executable and candidate relationship counts; decision_history entry with source apply-relationship-answer
  - Safety: local_safe_validated_decision_write_prevents_manual_json_edits
  - Required skills: workspace-governance; domain-model; data-engineering-pipeline-design
- `apply-uc-intake`
  - Command: `uv run apply-uc-intake --workspace <workspace>`
  - Use when: Execute an approved solution blueprint against Unity Catalog.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `assess-workspace-phi`
  - Command: `uv run assess-workspace-phi --workspace <workspace>`
  - Use when: CLI: assess-workspace-phi --workspace <ws>.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `blocker-question-panel`
  - Command: `uv run blocker-question-panel --workspace <workspace>`
  - Use when: agent needs to ask any KPI blocker question; direct mapping or source-of-truth choice needs stakeholder answer; non-technical panel is preferred over terminal option UI
  - Outputs: interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; interns/reports/blocker_question_panel/index.json
  - Safety: local_safe
  - Required skills: grill-requirements; workspace-kpi-query-optimizer
- `build-catalog-contract`
  - Command: `uv run build-catalog-contract --workspace <workspace>`
  - Use when: profile-backed source datasets need a stable logical catalog interface; pipeline and KPI code generation must avoid direct raw path dependencies; route or pipeline planning needs catalog objects before executable work
  - Outputs: interns/generated/contracts/catalog_contract.json; interns/reports/catalog_contract.md
  - Safety: local_safe_profile_backed_contract_write_no_remote_mutation
  - Required skills: workspace-governance; domain-model; data-engineering-pipeline-design
- `build-intent-contract`
  - Command: `uv run build-intent-contract --workspace <workspace>`
  - Use when: Console entry point: build-intent-contract --workspace <ws>
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `build-relationship-contracts`
  - Command: `uv run build-relationship-contracts --workspace <workspace>`
  - Use when: multi-dataset executable generation needs FK/relationship proof; source-to-target joins need production-grade relationship contracts; profile-only joins need approval gating
  - Outputs: interns/generated/contracts/relationship_contracts.json; interns/reports/relationship_contracts.md
  - Safety: local_safe_governed_contract_write
  - Required skills: domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `build-source-family-contracts`
  - Command: `uv run build-source-family-contracts --workspace <workspace>`
  - Use when: external raw folder has repeated dated CSV releases; schema drift must be understood before ETL or medallion planning; workspace has profiles but no KPI registry or no KPI-first workflow; agent must group source files without reading raw datasets or duplicating full profile payloads
  - Outputs: interns/generated/contracts/source_family_contracts.json; interns/reports/source_family_contracts.md
  - Safety: local_safe_profile_index_only_no_raw_data_reads
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
- `build-tool-index`
  - Command: `uv run build-tool-index --workspace <workspace>`
  - Use when: Regenerate .agents/tools.json from the CLI registry.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `build-workspace-evidence-graph`
  - Command: `uv run build-workspace-evidence-graph --workspace <workspace>`
  - Use when: KPI, mapping, SQL, trajectory, and harness artifacts need one traceability graph; agent needs impact analysis before changing a feature mapping; reviewer asks where a term, column, blocker answer, or SQL dependency came from
  - Outputs: interns/generated/evidence_graph/graph.json; interns/reports/evidence_graph/current.md
  - Safety: local_safe_existing_artifacts_only_no_raw_data_reads
  - Required skills: workspace-governance; domain-model; workspace-kpi-query-optimizer
- `check-kpi-anomalies`
  - Command: `uv run check-kpi-anomalies --workspace <workspace>`
  - Use when: Post-results DAG task entrypoint: read this run's KPI results + this
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `check-platform-readiness`
  - Command: `uv run check-platform-readiness --workspace <workspace>`
  - Use when: Platform readiness: is Databricks/dbt/Airflow actually usable right now.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `check-remote-execution-gate`
  - Command: `uv run check-remote-execution-gate`
  - Use when: Standalone CLI: is `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` set right now?
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `cleanup-workspace-references`
  - Command: `uv run cleanup-workspace-references --workspace <workspace>`
  - Use when: fresh workspace restart is requested; stale generated references need removal
  - Outputs: dry-run cleanup plan; optional deletion of interns and repo runtime references with --apply --confirm-delete <workspace>
  - Safety: hard_permission_block_for_any_delete
  - Required skills: workspace-governance
- `confirm-blueprint`
  - Command: `uv run confirm-blueprint --workspace <workspace>`
  - Use when: CLI entry points for the blueprint slice.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `confirm-cli-agent-proposal`
  - Command: `uv run confirm-cli-agent-proposal --workspace <workspace>`
  - Use when: CLI for the second step of the CLI-agent proposal flow.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `context-router`
  - Command: `uv run context-router`
  - Use when: task needs bounded context instead of whole artifact loading; agent needs context wiki/page index; large workspace artifacts must be routed by budget
  - Outputs: interns/generated/context/context_index.json; interns/generated/context/context_pages.jsonl; interns/generated/context/manifests/<task>_<budget>.json; interns/reports/context/<task>_<budget>.md
  - Safety: local_safe_derived_context_only
  - Required skills: workspace-governance; domain-model
- `cost-ledger-ingest`
  - Command: `uv run cost-ledger-ingest --workspace <workspace>`
  - Use when: core/observability/cost_ingest.py -- Phase 1a.2b: fill agent token spend from
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `dashboard-verify`
  - Command: `uv run dashboard-verify`
  - Use when: Interactive browser verification gate for dashboards.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `dataops`
  - Command: `uv run dataops --workspace <workspace>`
  - Use when: ``dataops``: data-engineering system-design knowledge, decisions, reviews and
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `dbt-index`
  - Command: `uv run dbt-index --workspace <workspace>`
  - Use when: Local dbt model lineage and blast radius -- no warehouse, no dbt install.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `declare-source`
  - Command: `uv run declare-source --workspace <workspace>`
  - Use when: Governed CLI entry points for Phase 0/1.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `deploy-databricks-workspace`
  - Command: `uv run deploy-databricks-workspace --workspace <workspace>`
  - Use when: Dry-run and guarded deployment for Databricks workspace specs.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `derived-feature-markdown`
  - Command: `uv run derived-feature-markdown --workspace <workspace>`
  - Use when: stakeholders need readable derived-feature blocker reviews; strict derived_feature_options JSON needs Markdown rendering
  - Outputs: interns/reports/derived_feature_reviews/md/<kpi_id>_<feature>.md; interns/reports/derived_feature_reviews/json/<kpi_id>_<feature>.json; interns/reports/derived_feature_reviews/index.md
  - Safety: local_safe
  - Required skills: feature-derivation-library; workspace-kpi-query-optimizer
- `discover-external-sources`
  - Command: `uv run discover-external-sources --workspace <workspace>`
  - Use when: user points to a large external folder; external source root needs dataset/doc/log/delta/database classification; draft source selection from cold storage without making it the workspace
  - Outputs: interns/generated/requirements/external_source_discovery.json; interns/reports/external_source_discovery.md; docs/source_selection.generated.json
  - Safety: local_safe_metadata_path_only_review_gated
  - Required skills: workspace-governance; data-engineering-pipeline-design; task-onboarding
- `discover-source`
  - Command: `uv run discover-source --workspace <workspace>`
  - Use when: Governed CLI entry points for Phase 0/1.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `doctor`
  - Command: `uv run doctor --workspace <workspace>`
  - Use when: doctor: one command that answers "is my local setup actually ready to use this
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `export-data-model-diagram`
  - Command: `uv run export-data-model-diagram --workspace <workspace>`
  - Use when: stakeholders need a presentable data model diagram; finalized or draft data model should be rendered as static SVG; Mermaid ERD should be exported for review
  - Outputs: interns/reports/presentation/data-model.svg; interns/reports/presentation/data-model.mermaid.md; interns/reports/presentation/presentation_manifest.json
  - Safety: local_safe_presentation_export
  - Required skills: workspace-governance; domain-model
- `export-kpi-registry-excel`
  - Command: `uv run export-kpi-registry-excel --workspace <workspace>`
  - Use when: stakeholders need a KPI Excel workbook; draft or finalized KPI registry should be exported for review; KPI blockers, evidence, and proof should be visible in spreadsheet form
  - Outputs: interns/reports/presentation/kpi_registry.xlsx; interns/reports/presentation/presentation_manifest.json
  - Safety: local_safe_presentation_export
  - Required skills: workspace-governance; workspace-kpi-query-optimizer
- `export-workspace-presentation`
  - Command: `uv run export-workspace-presentation --workspace <workspace>`
  - Use when: workspace needs a stakeholder-ready presentation bundle; data model diagram and KPI Excel should be generated together
  - Outputs: interns/reports/presentation/data-model.svg; interns/reports/presentation/data-model.mermaid.md; interns/reports/presentation/kpi_registry.xlsx; interns/reports/presentation/presentation_manifest.json
  - Safety: local_safe_presentation_export
  - Required skills: workspace-governance; domain-model; workspace-kpi-query-optimizer
- `fetch-source-documents`
  - Command: `uv run fetch-source-documents --workspace <workspace>`
  - Use when: Copy the declared source's documents into the workspace.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `finalize-data-model-generation`
  - Command: `uv run finalize-data-model-generation --workspace <workspace>`
  - Use when: draft data model preview was shown and explicitly approved; write user-facing data model docs and finalized model contract
  - Outputs: docs/data-model.md; docs/erd.md; docs/relationships.md; interns/generated/contracts/data_model_contract.json
  - Safety: requires_explicit_final_preview_approval
  - Required skills: workspace-governance; domain-model; to-solution-brief
- `finalize-kpi-generation`
  - Command: `uv run finalize-kpi-generation --workspace <workspace>`
  - Use when: final KPI draft preview was shown and explicitly approved; write user-facing KPI registry after KPI generation
  - Outputs: workspace docs KPI registry JSON; interns/generated/requirements/kpi_generation_production_proof.json; workspace and team memory
  - Safety: requires_explicit_final_preview_approval
  - Required skills: workspace-governance; stakeholder-memory; to-solution-brief; workspace-kpi-query-optimizer
- `generate-dbt-project`
  - Command: `uv run generate-dbt-project --workspace <workspace>`
  - Use when: Generate a real, git-tracked dbt project from the same confirmed contracts
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `generate-ingestion`
  - Command: `uv run generate-ingestion --workspace <workspace>`
  - Use when: Governed CLI entry points for provisioning + ingestion generation.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `generate-kpi-engines`
  - Command: `uv run generate-kpi-engines --workspace <workspace>`
  - Use when: Per-KPI multi-engine code generation.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `generate-kpi-sql`
  - Command: `uv run generate-kpi-sql --workspace <workspace>`
  - Use when: KPI features are proven or user-confirmed
  - Outputs: interns/generated/solutions
  - Safety: local_safe
  - Required skills: domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `generate-pipeline-sql`
  - Command: `uv run generate-pipeline-sql --workspace <workspace>`
  - Use when: catalog and pipeline contracts are ready; local DuckDB bronze/silver/gold layer SQL scaffold should be generated from pipeline_plan.json; raw paths must remain limited to catalog bootstrap
  - Outputs: interns/generated/pipeline/pipeline_layers.sql
  - Safety: local_safe_code_generation_no_execution_no_remote_writes
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
- `generate-skill-adapters`
  - Command: `uv run generate-skill-adapters`
  - Use when: skills or adapter routing changed
  - Outputs: .agents/skills_index.json; .agents/<tool>/SKILLS.md
  - Safety: local_safe
  - Required skills: workspace-governance
- `green-gate`
  - Command: `uv run green-gate`
  - Use when: green-gate: the project's portable test gate.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `harness`
  - Command: `uv run harness`
  - Use when: One front door for every validation/harness suite (Phase 1 consolidation).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `ingest-source-catalog`
  - Command: `uv run ingest-source-catalog --workspace <workspace>`
  - Use when: Governed source catalog planning and ingestion.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `kickstart-workspace`
  - Command: `uv run kickstart-workspace --workspace <workspace>`
  - Use when: new governed workspace; enterprise discovery and task config need refresh
  - Outputs: config/tasks.json; interns/generated/requirements; interns/generated/contracts
  - Safety: local_safe_config_write
  - Required skills: workspace-governance; task-onboarding; grill-requirements; domain-model
- `kpi-local-warehouse`
  - Command: `uv run kpi-local-warehouse --workspace <workspace>`
  - Use when: Local DuckDB warehouse — mirrors Databricks SQL Warehouse + Unity Catalog locally.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `kpi-proof-packet`
  - Command: `uv run kpi-proof-packet --workspace <workspace>`
  - Use when: stakeholders need one all-KPI recommendation packet; operators need source row traceability, mapping recommendations, reliability gates, generated SQL, execution output previews, and sample values in one report; bulk KPI mapping review should happen before apply-safe or execution modes
  - Outputs: interns/reports/kpi_proof_packet/current.md; interns/reports/kpi_proof_packet/current.json; interns/generated/evidence/kpi_proof_packet/current.json
  - Safety: local_safe_read_only_recommend_mode
  - Required skills: workspace-governance; domain-model; workspace-kpi-query-optimizer; to-solution-brief
- `list-workspace-files`
  - Command: `uv run list-workspace-files --workspace <workspace>`
  - Use when: workspace selection request; set current workspace; bounded startup file inventory
  - Outputs: all file paths up to cap; possible KPI files; possible data model files; dataset roots; docs; interns state
  - Safety: local_safe_file_paths_only_no_content_reads
  - Required skills: workspace-governance
- `loop`
  - Command: `uv run loop`
  - Use when: core/orchestration/loop.py — autonomous experiment loop.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `medallion`
  - Command: `uv run medallion`
  - Use when: One front door for medallion operations (Phase 1 consolidation).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `onboard-workspace`
  - Command: `uv run onboard-workspace --workspace <workspace>`
  - Use when: workspace artifacts are missing; profiles/contracts/evaluation scaffolding need refresh
  - Outputs: interns/generated/profiles; interns/generated/contracts; interns/reports
  - Safety: local_safe
  - Required skills: workspace-governance; task-onboarding; domain-model; workspace-kpi-query-optimizer
- `parse-data-model-images`
  - Command: `uv run parse-data-model-images --workspace <workspace>`
  - Use when: workspace contains image-only data model evidence; ERD, star-schema, or medallion diagram sidecars need review; image-derived relationships must stay non-executable until proof or approval; local OCR can be auto-installed with --auto-install-ocr when missing
  - Outputs: interns/generated/data_model_images/<image>.model.json; interns/reports/data_model_images/<image>.model.md; interns/reports/data_model_images/current.json; interns/reports/data_model_images/current.md
  - Safety: local_safe_review_gated_no_remote_vision_without_explicit_sensitive_upload_confirmation
  - Required skills: workspace-governance; domain-model; data-engineering-pipeline-design
- `pipeline-run`
  - Command: `uv run pipeline-run --workspace <workspace>`
  - Use when: One-command, dependency-ordered run of the whole pipeline (no Dagster
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `plan-kpi-completion`
  - Command: `uv run plan-kpi-completion --workspace <workspace>`
  - Use when: Dependency-aware parallel KPI completion planning.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `plan-provisioning`
  - Command: `uv run plan-provisioning --workspace <workspace>`
  - Use when: Governed CLI entry points for provisioning + ingestion generation.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `plan-source-to-target`
  - Command: `uv run plan-source-to-target --workspace <workspace>`
  - Use when: SQL, Polars, PySpark, ETL, or medallion implementation needs a data-model-backed source-to-target plan; agent must verify source datasets, joins, grain, temporal anchors, and target layer before code generation
  - Outputs: interns/generated/contracts/source_to_target_plan.json; interns/reports/source_to_target_plan.md; interns/generated/context/manifests/plan-source-to-target_<budget>.json
  - Safety: local_safe
  - Required skills: domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `prepare-agent-benchmark`
  - Command: `uv run prepare-agent-benchmark --workspace <workspace>`
  - Use when: workspace needs a readiness proof; release gates should be checked before SQL, ETL, medallion, autopilot, or production promotion; owners need an artifact-backed benchmark scorecard
  - Outputs: interns/generated/contracts/agent_benchmark_scorecard.json; interns/generated/contracts/release_gate_status.json; interns/reports/benchmarks/current.json; interns/reports/benchmarks/current.md
  - Safety: local_safe_project_native_scorecard_no_external_benchmark_execution
  - Required skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- `prepare-blueprint`
  - Command: `uv run prepare-blueprint --workspace <workspace>`
  - Use when: CLI entry points for the blueprint slice.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-bronze-silver-standards`
  - Command: `uv run prepare-bronze-silver-standards --workspace <workspace>`
  - Use when: Bronze/Silver production standards should be materialized; runtime-neutral transformation manifest is needed before SQL, Polars, or PySpark generation; workflow reroute policy should stop wrong-route actions and auto-reroute once
  - Outputs: interns/generated/contracts/bronze_silver_standards.json; interns/generated/contracts/transformation_manifest.json; interns/generated/contracts/workflow_reroute_policy.json; interns/reports/bronze_silver_standards.md
  - Safety: local_safe_contract_write_no_execution
  - Required skills: workspace-governance; data-engineering-pipeline-design
- `prepare-data-engineering-route`
  - Command: `uv run prepare-data-engineering-route --workspace <workspace>`
  - Use when: workspace needs a governed route before pipeline planning; agent must choose between KPI-only, ETL, ELT, medallion, OLTP ingestion, or existing-gold validation; trusted existing layers and local-first remote policy should be recorded
  - Outputs: interns/generated/contracts/data_engineering_route.json; interns/reports/data_engineering_route.md; interns/generated/contracts/catalog_contract.json
  - Safety: local_safe_route_contract_write_no_execution
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
- `prepare-data-model-blocker-panel`
  - Command: `uv run prepare-data-model-blocker-panel --workspace <workspace>`
  - Use when: data model draft exists; agent needs the next JSON-backed data-model blocker question; grain, primary key, relationship, temporal anchor, or SCD decisions need deterministic resolution
  - Outputs: interns/reports/data_model_blocker_panel/current.json; interns/reports/data_model_blocker_panel/current.md; updated data_model_generation_session.json
  - Safety: local_safe
  - Required skills: domain-model; grill-requirements
- `prepare-data-model-generation`
  - Command: `uv run prepare-data-model-generation --workspace <workspace>`
  - Use when: data model docs are missing, weak, or image-only; workspace needs governed data model creation or parsing; relationship proof should be reviewed before executable SQL
  - Outputs: interns/reports/data_model_generation/current.json; interns/reports/data_model_generation/current.md
  - Safety: local_safe
  - Required skills: workspace-governance; domain-model; grill-requirements
- `prepare-data-quality-panel`
  - Command: `uv run prepare-data-quality-panel --workspace <workspace>`
  - Use when: Data-quality rules authored the same way KPI features already are: ask,
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-data-source-panel`
  - Command: `uv run prepare-data-source-panel --workspace <workspace>`
  - Use when: Workspace-level data-source panel: an explicit, human-confirmed, once-asked
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-databricks-assets`
  - Command: `uv run prepare-databricks-assets --workspace <workspace>`
  - Use when: Databricks asset manifest generation for workspace datasets.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-document-candidate-review`
  - Command: `uv run prepare-document-candidate-review --workspace <workspace>`
  - Use when: Governed document candidate review panel (Phase 3: promote path, step 1).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-drift-panel`
  - Command: `uv run prepare-drift-panel --workspace <workspace>`
  - Use when: Governed CLI entry points for schema evolution.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-duplicate-review-panel`
  - Command: `uv run prepare-duplicate-review-panel --workspace <workspace>`
  - Use when: duplicate evidence from the data quality harness needs stakeholder review; duplicate handling options should be generated from bounded profile/catalog/pipeline contract evidence; agent needs a JSON-backed duplicate review panel before applying any decision
  - Outputs: interns/reports/duplicate_review/current.json; interns/reports/duplicate_review/current.md; interns/generated/contracts/data_quality_contract.json
  - Safety: local_safe_profile_catalog_pipeline_contract_driven_bounded_duplicate_evidence_redacted_samples_no_auto_dedup_or_quarantine_sql_mutation
  - Required skills: workspace-governance; data-engineering-pipeline-design; grill-requirements
- `prepare-external-source-intake`
  - Command: `uv run prepare-external-source-intake --workspace <workspace>`
  - Use when: user provides an external path and has not chosen existing versus new workspace; external-source route preference should be reused or challenged; agent needs a deterministic panel instead of asking freehand
  - Outputs: interns/generated/requirements/external_source_intake_session.json; interns/reports/external_source_intake/current.json; interns/reports/external_source_intake/current.md
  - Safety: local_safe_panel_write_no_source_reads
  - Required skills: workspace-governance; data-engineering-pipeline-design; grill-requirements
- `prepare-genie-workspace`
  - Command: `uv run prepare-genie-workspace --workspace <workspace>`
  - Use when: Generate reviewable Databricks Genie workspace setup specs.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-intake-panel`
  - Command: `uv run prepare-intake-panel --workspace <workspace>`
  - Use when: Governed CLI entry points for Phase 0/1.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-kpi-blocker-panel`
  - Command: `uv run prepare-kpi-blocker-panel --workspace <workspace>`
  - Use when: agent needs the next validated KPI blocker question; avoid hand-chaining onboarding, resolver, markdown, panel, and validation commands; fresh or existing KPI workspace needs deterministic blocker preparation
  - Outputs: interns/generated/contracts/kpi_feature_mapping.json; interns/reports/derived_feature_reviews; interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; validation summary
  - Safety: local_safe
  - Required skills: domain-model; feature-derivation-library; workspace-kpi-query-optimizer; grill-requirements
- `prepare-kpi-generation`
  - Command: `uv run prepare-kpi-generation --workspace <workspace>`
  - Use when: workspace confirmation completed; user may create, revise, challenge, or score KPIs; show two-path KPI generation versus usual workflow prompt
  - Outputs: interns/generated/requirements/kpi_generation_session.json; interns/reports/kpi_generation/current.json; interns/reports/kpi_generation/current.md; KPI quality/readiness score
  - Safety: local_safe
  - Required skills: workspace-governance; grill-requirements; stakeholder-memory; workspace-kpi-query-optimizer
- `prepare-phi-review-panel`
  - Command: `uv run prepare-phi-review-panel --workspace <workspace>`
  - Use when: PHI/PII review-and-consent panel (Priority 1.1).
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-pipeline-deployment-plan`
  - Command: `uv run prepare-pipeline-deployment-plan --workspace <workspace>`
  - Use when: generated pipeline outputs need a deployment dry-run contract; remote deployment target needs approval evidence before any external or warehouse apply; operators need proof that no remote mutation occurred
  - Outputs: interns/generated/contracts/pipeline_deployment_plan.json; interns/reports/pipeline_deployment_plan.md
  - Safety: dry_run_default_no_remote_mutation_external_and_warehouse_apply_require_AUTORESEARCH_ALLOW_REMOTE_EXECUTION
  - Required skills: workspace-governance; data-engineering-pipeline-design; databricks-access-gates
- `prepare-pipeline-format-panel`
  - Command: `uv run prepare-pipeline-format-panel --workspace <workspace>`
  - Use when: ETL, ELT, medallion, or ingestion pipeline needs target table/file format selection; agent must ask whether to store outputs as Delta, Parquet, Iceberg, CSV, warehouse-native, or another approved format; prepare-pipeline-plan is blocked by pipeline_table_format_unresolved
  - Outputs: interns/reports/pipeline_format/current.json; interns/reports/pipeline_format/current.md
  - Safety: local_safe_json_backed_user_choice_panel
  - Required skills: data-engineering-pipeline-design; grill-requirements
- `prepare-pipeline-plan`
  - Command: `uv run prepare-pipeline-plan --workspace <workspace>`
  - Use when: SQL, Polars, PySpark, ETL, ELT, medallion, or existing-layer validation needs a governed pipeline contract before code generation; layer definitions, quality gates, approval-gated transformations, and blockers must be recorded; source-to-target blockers should stop executable generation
  - Outputs: interns/generated/contracts/pipeline_plan.json; interns/reports/pipeline_plan.md; interns/generated/contracts/data_engineering_route.json; interns/generated/contracts/catalog_contract.json
  - Safety: local_safe_pipeline_plan_write_no_execution_remote_writes_approval_gated
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
- `prepare-solution-blueprint`
  - Command: `uv run prepare-solution-blueprint --workspace <workspace>`
  - Use when: The solution blueprint: what we will do, end to end, before we do any of it.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-source-catalog`
  - Command: `uv run prepare-source-catalog --workspace <workspace>`
  - Use when: Governed source catalog planning and ingestion.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `prepare-wiki-memory`
  - Command: `uv run prepare-wiki-memory --workspace <workspace>`
  - Use when: repeated KPI terms or data-model decisions should be found; shared team wiki memory should suggest safe reuse; autopilot needs governed draft prefill candidates
  - Outputs: state/team_memory/wiki_memory_index.json; interns/generated/memory/wiki_memory_candidates.json; interns/reports/wiki_memory/current.json; interns/reports/wiki_memory/current.md
  - Safety: local_safe_structured_artifacts_only_draft_prefill_blocks_execution
  - Required skills: stakeholder-memory; evolution; workspace-kpi-query-optimizer
- `prepare-workspace-bug-report`
  - Command: `uv run prepare-workspace-bug-report --workspace <workspace>`
  - Use when: workspace selection and onboarding evidence disagree; fresh workspace flow generated empty artifacts; blocking product bugs need a JSON and Markdown report
  - Outputs: interns/generated/evidence/bug_report.json; interns/reports/bugs/current.md
  - Safety: local_safe_governed_report_write
  - Required skills: workspace-governance; evolution
- `prepare-workspace-selection`
  - Command: `uv run prepare-workspace-selection --workspace <workspace>`
  - Use when: workspace selection request; set current workspace; workspace may be empty or missing; external raw data should stay outside workspace
  - Outputs: guarded workspace selection status; active workspace listing summary; available workspace choices; empty or missing workspace blocker; external dataset_allowlist setup template
  - Safety: local_safe_file_paths_only_no_content_reads
  - Required skills: workspace-governance; grill-requirements
- `prepare-workspace-workflow`
  - Command: `uv run prepare-workspace-workflow --workspace <workspace>`
  - Use when: workspace confirmation completed; user wants one checkpoint for KPI, data-model, blocker, validation, and presentation workflow; agent needs deterministic next commands and autopilot boundaries
  - Outputs: interns/reports/workflow/current.json; interns/reports/workflow/current.md; local-safe generated artifacts when mode is local-safe or autopilot
  - Safety: local_safe_checkpoint_autopilot_stops_before_final_delete_remote_codegen
  - Required skills: workspace-governance; task-onboarding; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `query-workspace-evidence-graph`
  - Command: `uv run query-workspace-evidence-graph --workspace <workspace>`
  - Use when: agent or reviewer asks why a term exists; feature or column impact needs to be listed from the evidence graph; stale or invented term origin needs traceability
  - Outputs: JSON query result with matched nodes, introducers, users, and impact edges
  - Safety: local_safe_existing_graph_or_rebuild_no_raw_data_reads
  - Required skills: workspace-governance; domain-model; workspace-kpi-query-optimizer
- `recommend-kpi-engine`
  - Command: `uv run recommend-kpi-engine --workspace <workspace>`
  - Use when: Complexity- and size-aware engine recommendation.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `reconcile-warehouse-cost`
  - Command: `uv run reconcile-warehouse-cost --workspace <workspace>`
  - Use when: Read warehouse spend back out of Databricks and attribute it to one run.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `record-engine-evolution`
  - Command: `uv run record-engine-evolution --workspace <workspace>`
  - Use when: Structured engine-routing memory for SQL/Polars/PySpark decisions.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `record-workspace-trajectory`
  - Command: `uv run record-workspace-trajectory --workspace <workspace>`
  - Use when: agent or CLI workflow steps should be replayable; commands, validations, decisions, retries, and artifacts need workspace-scoped audit events; workflow guardrails should validate the step-by-step trajectory
  - Outputs: interns/state/trajectory.jsonl; interns/reports/trajectory/current.json; interns/reports/trajectory/current.md; interns/generated/evidence/trajectory/current.json
  - Safety: local_safe_append_only_secret_redacted_workspace_log
  - Required skills: workspace-governance; evolution
- `resolve-kpi-features`
  - Command: `uv run resolve-kpi-features --workspace <workspace>`
  - Use when: KPI features need mapping; blockers need clustering; accepted definitions need applying
  - Outputs: interns/generated/contracts/kpi_feature_mapping.json; strict derived_feature_options with formula/input/observed_values/value_profile/semantic_meaning_sources/reason/example/evidence_sources/derivation_reasoning/evidence_state/confidence; semantically mismatched candidates rejected; interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; interns/reports/open_questions.md
  - Safety: local_safe
  - Required skills: domain-model; feature-derivation-library; workspace-kpi-query-optimizer; grill-requirements
- `resolver-accuracy`
  - Command: `uv run resolver-accuracy`
  - Use when: Score the feature resolver against human-confirmed mappings.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `resource-preflight`
  - Command: `uv run resource-preflight --workspace <workspace>`
  - Use when: CLI for local hardware/resource preflight.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `retrieve-docs`
  - Command: `uv run retrieve-docs`
  - Use when: CLI for bounded internal-doc retrieval.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `run-dbt-backfill`
  - Command: `uv run run-dbt-backfill --workspace <workspace>`
  - Use when: Bounded, dry-run-capable backfill for a workspace's dbt project.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `run-ingestion`
  - Command: `uv run run-ingestion --workspace <workspace>`
  - Use when: Governed CLI entry points for provisioning + ingestion generation.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `run-kpi-execution-harness`
  - Command: `uv run run-kpi-execution-harness --workspace <workspace>`
  - Use when: Execute generated KPI SQL and prove that final result views exist.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `run-kpi-pipeline`
  - Command: `uv run run-kpi-pipeline --workspace <workspace>`
  - Use when: Entry point for ``run-kpi-pipeline``.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `scan-document`
  - Command: `uv run scan-document --workspace <workspace>`
  - Use when: Governed PDF ingestion: scan_document() + Java preflight + review-gated sidecar.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `session-snapshot`
  - Command: `uv run session-snapshot --workspace <workspace>`
  - Use when: operator wants exact end-user conversation transcript; cross-CLI session monitoring; audit conversation turns, commands, file changes, decisions, and intent verification
  - Outputs: .agents/sessions/<session>/compact.md; .agents/sessions/<session>/intent_verification.md; .agents/sessions/<session>/intent_verification.json; .agents/sessions/<session>/events.jsonl; .agents/sessions/<session>/transcript.md; .agents/sessions/<session>/commands.md; .agents/sessions/<session>/file_changes.md; .agents/sessions/<session>/decisions.md; .agents/sessions/<session>/snapshot.json
  - Safety: local_safe_redacts_common_secret_patterns_gitignored
  - Required skills: workspace-governance; evolution; stakeholder-memory
- `source-catalog`
  - Command: `uv run source-catalog --workspace <workspace>`
  - Use when: Governed source catalog planning and ingestion.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `suggest-kpi-improvements`
  - Command: `uv run suggest-kpi-improvements --workspace <workspace>`
  - Use when: Per-KPI improvement suggestions, emitted ONLY when the readings warrant one.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `sync-workspace-code`
  - Command: `uv run sync-workspace-code --workspace <workspace>`
  - Use when: Governed CLI entry points for provisioning + ingestion generation.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `token-report`
  - Command: `uv run token-report --workspace <workspace>`
  - Use when: Token-cost reporter for development tracing.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `understand-data`
  - Command: `uv run understand-data --workspace <workspace>`
  - Use when: classify a workspace data-quality tier and schema type before KPI/SQL generation; surface tier-scoped data-processing options from generated profiles; inspect the BUG-010 data-understanding gate standalone
  - Outputs: interns/reports/data_understanding/current.json; interns/reports/data_understanding/current.md
  - Safety: local_safe_reads_generated_profiles_only_no_raw_dataset_reads
  - Required skills: workspace-governance; data-engineering-pipeline-design
- `validate-engine-generation`
  - Command: `uv run validate-engine-generation --workspace <workspace>`
  - Use when: Validate the generated multi-engine KPI outputs for coherence.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `validate-git-hygiene`
  - Command: `uv run validate-git-hygiene`
  - Use when: Validate staged or working-tree files before committing project artifacts.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `validate-kpi-intent-coverage`
  - Command: `uv run validate-kpi-intent-coverage --workspace <workspace>`
  - Use when: KPI intent-coverage harness.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `validate-memory-health`
  - Command: `uv run validate-memory-health --workspace <workspace>`
  - Use when: workspace memory needs confidence-scored lifecycle health; shared team memory should be checked before reuse; memory entries need status, confidence, verification, expiration, and evidence normalization
  - Outputs: interns/reports/memory_health/current.json; interns/reports/memory_health/current.md; interns/generated/evidence/memory_health/current.json
  - Safety: local_safe_memory_artifact_scan_no_raw_data_reads
  - Required skills: stakeholder-memory; evolution
- `validate-project-harness`
  - Command: `uv run validate-project-harness --workspace <workspace>`
  - Use when: workspace needs one top-level score before completion is claimed; all local-safe harnesses should run together; release readiness needs artifact validation, workflow guardrails, trajectory health, KPI execution, benchmark, and git hygiene proof
  - Outputs: interns/generated/evidence/project_harness.json; interns/reports/project_harness.md; score, blockers, warnings, and next commands
  - Safety: local_safe_project_harness_no_remote_execution
  - Required skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- `validate-workspace-artifacts`
  - Command: `uv run validate-workspace-artifacts --workspace <workspace>`
  - Use when: generated workspace artifacts need schema checks; agent is about to rely on KPI registry, feature mapping, derived reviews, or blocker panel; detect manual edits to generated contracts
  - Outputs: validation summary JSON with checked_files, errors, and warnings
  - Safety: local_safe_read_only
  - Required skills: workspace-governance; workspace-kpi-query-optimizer
- `verify-audit-chain`
  - Command: `uv run verify-audit-chain --workspace <workspace>`
  - Use when: ``verify-audit-chain`` console script.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `verify-dbt-project`
  - Command: `uv run verify-dbt-project --workspace <workspace>`
  - Use when: Verify a generated dbt project actually runs, with `dbt show`.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `verify-kpi-output`
  - Command: `uv run verify-kpi-output --workspace <workspace>`
  - Use when: Self-grill gate: prove generated KPI output is executable AND aligned with intent.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `verify-orchestration`
  - Command: `uv run verify-orchestration`
  - Use when: Offline verification of the orchestration graph.
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `workspace-dashboard`
  - Command: `uv run workspace-dashboard --workspace <workspace>`
  - Use when: user wants to view or verify the workspace BI dashboard; KPI completion dashboard needs a visual screening pass
  - Outputs: dashboard/exports/index.html; interns/reports/dashboard_screener/current.json; interns/reports/dashboard_screener/current.md; interns/reports/dashboard_screener/shots (agent vision review)
  - Safety: local_safe
  - Required skills: workspace-governance
- `workspace-dashboard-deck`
  - Command: `uv run workspace-dashboard-deck --workspace <workspace>`
  - Use when: ``workspace-dashboard-deck``: export a workspace's live MinusAnalyst dashboard
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `workspace-dashboard-pdf`
  - Command: `uv run workspace-dashboard-pdf --workspace <workspace>`
  - Use when: ``workspace-dashboard-pdf``: export a workspace's live MinusAnalyst dashboard
  - Safety: uncurated -- follow AGENTS.md safety rules for this command
- `workspace-flow`
  - Command: `uv run workspace-flow --workspace <workspace>`
  - Use when: agent-led workspace workflow should run quietly in the backend; user asks for KPI generation by interview; user asks to generate SQL and show KPI results; main chat should show only compact questions or results
  - Outputs: interns/state/workflow_sessions/<session-id>/session.json; interns/state/workflow_sessions/<session-id>/current.json; interns/state/workflow_sessions/<session-id>/current.md; interns/reports/kpi_results/current.md; interns/generated/evidence/kpi_results/current.json
  - Safety: local_safe_session_orchestrator_hides_lower_level_command_noise
  - Required skills: workspace-governance; task-onboarding; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `workspace-state-health`
  - Command: `uv run workspace-state-health --workspace <workspace>`
  - Use when: Read-only health report over a workspace's own state/audit files
  - Safety: uncurated -- follow AGENTS.md safety rules for this command

## Available Subagents

These subagents are generated from `skills/*/agents/*.yaml`; do not hand-edit adapter output.
Use the narrowest role that fits the task and keep write access limited to implementer-style roles.

### dashboard-engineer

- Display name: Dashboard Engineer
- Description: Designs, customizes, debugs, and verifies per-workspace BI dashboards (JSON spec contract, Dash renderer, static HTML export, PPTX/PDF export, live callback tests). Use when the user says dashboard, chart, graph, tile, page, slicer, filter, drill-down, legend, "the chart looks wrong", "add a view", "export the deck", or when a dashboard must pass the screener and vision-review gate before it is called done.
- Skills: workspace-governance; dashboard-design; grill-requirements; domain-model; kpi-analyst; evolution
- Safety: governed_dashboard_spec_local_safe_by_default
- Source: `skills/dashboard-design/agents/dashboard-team.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: {"default_tier": "standard", "escalate_to_deep_for": ["cross-workspace dashboard schema migrations", "production embedding decisions", "performance investigations with large result sets"], "use_light_for": ["spec inventory", "render-tree inspection", "simple user_overrides edits", "static HTML export"], "use_standard_for": ["chart-type inference rule additions", "callback graph design", "dialect dispatch decisions", "regression test authoring"]}
- Default prompt: Act as the dashboard-engineering role. Own everything under `workspaces/<ws>/dashboard/` and the platform modules at `core/dashboard/*`, `tools/workspace_dashboard.py`, `tests/test_dashboard_*.py`. Honor the two-section spec contract: rewrite `machine_defaults` on every regeneration; preserve `user_overrides` verbatim. Use live SQL re-execution via DuckDB — never read stale snapshots. Render blocked KPIs as recovery cards, not by hiding them. CHART-QUALITY DEFAULTS (produce a correct, readable chart by default, not just valid markup): no duplicate titles (card header carries the title; pass title='' to inline figures); trend/line charts aggregate the measure by the date axis (one value per period), never scatter raw rows; share/percentage charts aggregate by (x,color) so each stack is a true 0-100% and cap dense categoricals to a readable top-N with an 'Other' bucket; ranked/top-N charts rank by the highest-cardinality NON-constant categorical column, never a column pinned to one value by a filter; responsive sizing + margins so charts never clip; apply the shared corporate theme at one seam, do not hand-style. VISUAL VERIFICATION IS A MANDATORY GATE before showing any dashboard: structure/spec/headline checks are NOT verification. Run `uv run dashboard-verify --url <file:// or http://127.0.0.1:port> --screenshot <png>` which drives a real browser (agent-browser) and FAILS on overflow / blank charts / missing legend; a non-zero exit is a blocker — fix and re-run. THEN read the captured screenshot and judge it visually. Only claim 'looks professional' after the gate passes AND you have seen it. For the live Dash app, boot it and verify the URL; use agent-browser click/hover to confirm legend-toggle and panel select/deselect actually work. For any chart-type addition or schema change, ship a regression test that asserts the quality property (aggregated / percent 0-100% / ranked by non-constant column), not merely that a figure was produced. Debug callbacks via `/_dash-dependencies` and `/_dash-layout` before reaching for browser tests. Do not edit upstream contracts (`kpi_registry.json`, `relationship_contracts.json`, `source_to_target_plan.json`) — escalate to `data-engineer` or `kpi-analyst` when the chart is wrong because the data is wrong. LATENCY AND LINEAGE — know the real causes before guessing: the local read path in `core/dashboard/model/layers.py` full-scans bronze/silver Delta into Polars on every callback (no column/filter pushdown, no cache) and the Databricks gold path re-fetches with no TTL or Delta-version check, so 'the dashboard is slow' is usually THIS, not the KPI SQL; name the read path and hand tuning to `performance-optimizer` (it owns `config/optimization_playbook.yaml` and cites rule ids) rather than inventing a threshold. There is no per-chart definition/grain/lineage panel and no semantic layer (`metrics.yml` / UC metric views) — a number on a chart cannot be traced to its KPI definition inside the dashboard, so when a stakeholder asks 'where does this come from', answer from `kpi_registry.json` and the generated SQL and state that the in-dashboard lineage surface does not exist yet rather than implying it does. JUDGMENT SKILLS (bounded firing): run `grill-requirements` at the START only when the request is genuinely ambiguous (which KPI / dimension / chart type / fit-to-screen vs detail) — never on a clearly-specified request; run `grill-requirements` at the END to emit a short audit of the assumptions you made (e.g. why log scale, which dimensions you dropped) BEFORE presenting. grill-requirements is ADVISORY: it surfaces confidence/assumptions to the user but NEVER overrides the deterministic `dashboard-verify` gate, which remains the sole pass/fail authority. See skills/dashboard-design/SKILL.md for the full quality + verification procedure. HANDBACK CHECKLIST (all must hold): the screener ran and returned ok:true; every staged image under `interns/reports/dashboard_screener/shots/` was opened and judged (each page, each sub-tab, each per-chart crop); formats and aggregations derive from the metric function, never from a per-workspace default; `user_overrides` survived the regeneration byte-for-byte; the vision review is recorded with `--reviewed-by` and real findings. REPORTING RULE: describe only what you saw in the captured screenshots and the screener output - never claim a chart 'looks professional', a defect count, or a render time you did not observe (BUG-015).

### business-analyst

- Display name: Business Analyst
- Description: Converts stakeholder intent into KPI definitions, acceptance criteria, grains, filters, and governed open questions. Use when the request is about what a metric MEANS rather than how to compute it - "what does this KPI mean", "which denominator", "as of when", ambiguous or conflicting KPI definitions, acceptance criteria, approval owner, the intake interview (prepare-intake-panel / apply-intake-answer), the understanding playback gate, or when a blocker panel needs a reusable workspace-level business definition.
- Skills: grill-requirements; domain-model; stakeholder-memory; to-solution-brief; workspace-kpi-query-optimizer
- Safety: read_only_requirements_and_decision_capture
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: {"default_tier": "light", "escalate_to_deep_for": ["high-risk business semantics that would change production metrics"], "escalate_to_standard_for": ["conflicting KPI definitions", "unclear metric ownership", "cross-stakeholder tradeoffs"], "use_light_for": ["file-set summaries", "requirements extraction", "panel wording", "decision recording"]}
- Default prompt: Act as the business-analysis role for data work. Clarify stakeholder goals, KPI formulas, denominator rules, temporal anchors, lifecycle states, acceptance criteria, and approval owners. Do not generate executable SQL or pipeline code.

### data-analyst

- Display name: Data Analyst
- Description: Profiles, interprets, and validates data evidence for KPI readiness, anomalies, trends, and business-facing result explanations. Use when the question is what the DATA actually contains or whether a result is believable - "what is in this column", "why is this number wrong", "are these results plausible", zero-row or all-blank result review, distribution and null checks, candidate-mapping triage, derived-feature evidence, and discovery-scan interpretation (formats, sizes, arrival pattern) for a newly declared source.
- Skills: workspace-governance; domain-model; feature-derivation-library; workspace-kpi-query-optimizer; evolution
- Safety: profile_first_analysis_no_raw_dataset_overread
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: {"default_tier": "light", "escalate_to_deep_for": ["production-impacting metric interpretation disputes"], "escalate_to_standard_for": ["KPI plausibility analysis", "conflicting data evidence", "multi-table analytical reasoning"], "use_light_for": ["profile review", "anomaly summaries", "candidate mapping triage"]}
- Default prompt: Act as the data-analysis role. Use profile-first evidence to inspect distributions, nulls, categories, candidate mappings, anomalies, and KPI result plausibility. Explain what the data can and cannot support, and raise blocker-panel questions for unproven business meanings.

### data-engineer

- Display name: Data Engineer
- Description: Designs governed source-to-target, data-quality, medallion, ETL/ELT, orchestration, and deployment-safe data pipelines. Use for source declaration and discovery (declare-source, discover-source), pipeline blueprint review, velocity lane choice (batch, micro-batch, streaming, realtime), ingestion generation (Auto Loader, COPY INTO, JDBC, Kafka), schema-drift decisions (prepare-drift-panel), bronze/silver/gold layer design, dbt model and Airflow DAG structure, backfill and idempotency, and "build me a pipeline" or "land this data" requests.
- Skills: workspace-governance; domain-model; data-engineering-pipeline-design; databricks-access-gates; evolution
- Safety: governed_pipeline_design_local_safe_by_default
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: {"default_tier": "standard", "escalate_to_deep_for": ["production architecture tradeoffs", "ambiguous relationship or grain proof", "remote deployment risk decisions"], "use_light_for": ["contract inventory", "route classification", "checklist validation"], "use_standard_for": ["source-to-target planning", "medallion layer design", "data-quality gate design"]}
- Default prompt: Act as the data-engineering role. Build or review Bronze, Silver, and Gold plans from source contracts, profile evidence, relationship contracts, and accepted decisions. Produce plans and contracts before executable logic; do not run remote mutation without explicit approval.

### sql-polars-pyspark-specialist

- Display name: SQL Polars PySpark Specialist
- Description: Chooses and implements the correct query/runtime engine, preserving parity across SQL, Polars, and PySpark when required. Use when the question is which engine or how to express the logic - "SQL or Polars or PySpark", engine selection for a workspace transform DAG, velocity-lane implications for the engine, window/join/grain expression, cross-engine parity or drift, a derived_formula that only exists in SQL, or query optimization and rewrite.
- Skills: workspace-governance; domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- Safety: executable_generation_requires_source_to_target_and_relationship_proof
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: {"default_tier": "standard", "escalate_to_deep_for": ["cross-engine semantic parity", "complex joins or windowing", "high-risk KPI formula correctness"], "use_light_for": ["syntax rewrites", "simple engine selection", "formatting and lint fixes"], "use_standard_for": ["implementation from approved contracts", "test generation", "query optimization"]}
- Default prompt: Act as the SQL, Polars, and PySpark implementation specialist. Select the narrowest supported engine for the requested runtime and generate only that engine unless parity is explicitly required.

### performance-optimizer

- Display name: Performance Optimizer
- Description: Owns `config/optimization_playbook.yaml` - turns a measured performance or cost symptom into the cheapest-first remedy, cited by rule id. Use when something is slow, expensive, spilling, skewed, queued, or growing - "this query is slow", "the run got more expensive", "spill to disk", "shuffle skew", "the warehouse is queueing", "should we OPTIMIZE / VACUUM / recluster", cluster-key choice, partition-vs-liquid-clustering, small-file and write-floor problems, dashboard latency, and "should we change engine" (answer: consult the playbook first, engine change is last resort).
- Skills: workspace-governance; data-engineering-pipeline-design; workspace-kpi-query-optimizer; databricks-access-gates
- Safety: read_only_measurement_and_playbook_citation_no_remote_mutation
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: {"default_tier": "light", "escalate_to_deep_for": ["engine-change decisions", "production cost regressions with conflicting evidence"], "use_light_for": ["playbook lookup by symptom", "rule citation and remedy ordering", "artifact inventory"], "use_standard_for": ["multi-symptom diagnosis", "cost-vs-latency tradeoffs", "cluster-key and layout review"]}
- Default prompt: Act as the performance and cost optimization role. You do NOT choose the engine

### databricks-engineer

- Display name: Databricks Engineer
- Description: Plans and reviews Databricks-specific Unity Catalog, Delta, Lakeflow, jobs, permissions, costs, and production deployment gates. Use for provisioning (plan-provisioning, apply-provisioning), "create a catalog or schema", external locations, storage credentials and volumes, Delta table properties and liquid clustering, serverless vs classic compute and warehouse sizing, job and Lakeflow deployment, grants and permissions, query-history cost attribution, and any remote Databricks execution approval.
- Skills: workspace-governance; data-engineering-pipeline-design; databricks-access-gates
- Safety: databricks_remote_mutation_requires_explicit_approval
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: {"default_tier": "standard", "escalate_to_deep_for": ["production permission architecture", "failure recovery design", "remote apply risk assessment"], "use_light_for": ["access checklist review", "object naming review", "dry-run plan summaries"], "use_standard_for": ["Databricks deployment planning", "Delta and Unity Catalog design", "cost and compute control review"]}
- Default prompt: Act as the Databricks engineering role. Review or design Databricks-specific deployment choices - Unity Catalog objects, Delta tables, Lakeflow or job orchestration, SQL warehouses, clusters, permissions, data quality expectations, lineage, cost controls, and remote execution approvals.

### kpi-analyst

- Display name: KPI Analyst
- Description: Interpret KPI sheets and validate KPI queries. Use when a KPI must be read, translated into a query, or checked for correctness - "does this SQL answer the question", KPI review gates, result-packet interpretation, metric/cut/filter mismatches, share denominators and attribution, and any result that could mislead a stakeholder even though it executed cleanly.
- Skills: kpi-analyst
- Safety: follows_skill_policy
- Source: `skills/kpi-analyst/agents/openai.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Use KPI analyst to parse KPI definitions, classify metric intent, write or review one query per KPI, show result tables, and surface only correctness-relevant assumptions.

### workspace-flow-orchestrator

- Display name: Workspace Flow Orchestrator
- Description: Drives plan, local-safe, and bounded-autopilot workspace orchestration while keeping main-chat output concise. Use when the user sets or switches a workspace, says "run the pipeline", "what is next", "continue", "resume", asks for status, or when a panel must be rendered and its answer applied - it owns command sequencing, panel fidelity, and the required-specialist and suggested-skill roster attached to every panel.
- Skills: workspace-governance; workspace-kpi-query-optimizer; task-onboarding
- Safety: local_safe_workflow_only_no_remote_execution
- Source: `skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Use workspace-flow and prepare-workspace-workflow as the canonical orchestration APIs. Always let the user choose plan, local-safe, or bounded autopilot; default to local-safe. After onboarding, prepare Bronze/Silver standards, the runtime-neutral transformation manifest, workflow_reroute_policy, data-quality gate, and layer route before KPI blocker resolution, source-to-target planning, SQL generation, or remote/deployment steps. If drift is detected, stop the wrong branch, record a structured reroute event, rerun the replacement local-safe command once, and escalate on repeat. For blocker, approval, KPI-generation, data-model, duplicate-review, or pipeline-format panels, post the generated current.md verbatim as the human card; do not replace it with a tool-native generic question box or a summary. Use current.json only to render buttons/options and to apply the exact selected answer. Record deterministic next commands. Required-specialist + suggested-skills enforcement (hard rule): every panel JSON may carry summary.required_specialists, summary.suggested_skills, and summary.delegations. These are not advisory. Before answering or rendering a panel, activate every skill in suggested_skills, render every delegation verdict inline, and either invoke each agent in required_specialists or include in your reply why you are choosing not to. Never strip these fields from a panel before showing it to the user. Confirmation rules (hard): after workspace confirmation, AUTO-CHAIN deterministic next-steps (onboard-workspace, prepare-*-panel commands, validate-workspace-artifacts, build-relationship-contracts, workspace-flow status/diff/artifacts/gc-without-apply) without asking. Only prompt the user before apply-* commands, gc --apply, remote/Databricks execution, and any --force flag. Render rule for panels: show summary.preamble first; show only the top 3 options plus Custom by default and mention overflow count; bold the recommended_option_id with its concrete reasoning; render evidence files with their purpose annotation. CLOUD-FIRST SPINE ORDER (only when the workspace has a `source_declaration` in workspace_settings.json): declare-source -> discover-source -> prepare-intake-panel and apply-intake-answer (including the understanding-playback confirmation) -> prepare-blueprint -> confirm-blueprint (the single human gate) -> plan-provisioning and apply-provisioning -> generate-ingestion -> dbt/DAG generation -> KPI results and dashboard; schema drift is handled out-of-band by prepare-drift-panel and apply-drift-answer. Workspaces without a source declaration keep the existing local flow unchanged. PERFORMANCE AND COST ROUTING: slowness, spend, spill, skew, queueing, small files, clustering or OPTIMIZE questions are not orchestration decisions — route them to `performance-optimizer`, which owns `config/optimization_playbook.yaml` and answers with a cited rule id. Never restate a tuning threshold yourself, and never let an engine change through as a quiet swap: it needs a `revisit_*` rule plus `sql-polars-pyspark-specialist`. HANDBACK CHECKLIST (all must hold): the panel markdown was posted verbatim; required_specialists were invoked or the reason for not invoking each was stated; the next deterministic command was named; no apply-*, gc --apply, --force or remote execution ran without an explicit user yes; every human gate recorded with --confirmed-by. REPORTING RULE: report only artifact paths that exist and values printed by the commands you ran; never reconstruct SQL, result tables or counts from memory (BUG-015).

### source-to-target-reviewer

- Display name: Source-to-Target Reviewer
- Description: Reviews KPI, relationship, profile, and source-to-target proof before SQL or pipeline generation. Use as the gate before anything executable is emitted - "is this ready to generate", source-to-target plan review, pipeline blueprint review, per-KPI model mapping, join and grain proof, schema-drift impact on downstream models, and any request to sign off that the mapping from source columns to target metric is actually proven.
- Skills: workspace-governance; domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- Safety: read_only_review_blocks_unproven_generation
- Source: `skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Inspect generated contracts and reports before executable generation. Block on unproven source columns, joins, grain, temporal anchors, relationship contracts, missing engine parity, or unapproved Silver semantic mappings.

### validation-gatekeeper

- Display name: Validation Gatekeeper
- Description: Runs and interprets local-safe validation, workflow guardrail, project harness, and reliability checks. Use when the user asks "is it valid", "run the gates", "why is this blocked", for green-gate and regression sweeps, workspace artifact validation, workflow guard errors, the dashboard screener gate, the destructive-op boundary on provisioning, and before any promotion or deployment is called done.
- Skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- Safety: local_safe_validation_no_generated_contract_edits
- Source: `skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Run local-safe validation gates only. Treat validation errors as blockers, summarize artifact paths, and never hand-edit generated contracts to clear failures.

## Available Skills

### context-map-sync

- Path: `skills/context-map-sync/SKILL.md`
- Description: Keep every CONTEXT-<folder>.md true to the code beside it. Use whenever a file under a mapped directory is created, deleted, renamed, or has a signature/flag/constant change -- and ALWAYS before staging a commit that touches core/, tools/, tests/, config/, docs/, skills/ or vendor/. CONTEXT-MAP.md rule 3 makes this atomic with the code change, not a follow-up.

### dashboard-agent

- Path: `skills/dashboard-agent/SKILL.md`
- Description: Named conversational DashboardAgent. Triggered by "DashboardAgent, <request>", it turns a natural-language plot request into a per-workspace dashboard spec edit + verify loop. An advisor + editor that knows the spec contract (machine_defaults vs user_overrides), the renderer chart types and axes, the live KPI result columns, and display redaction/governance. Wraps the dashboard-engineer subagent and the dashboard-design skill. Use whenever the user names "DashboardAgent" or asks in plain language to add, change, or remove a plot/panel on a workspace dashboard.

### dashboard-design

- Path: `skills/dashboard-design/SKILL.md`
- Description: Design, customize, debug, and verify per-workspace BI dashboards. Owns the dashboard/ directory in any workspace: JSON spec contracts (machine_defaults + user_overrides), chart-type inference, Dash renderer, static HTML export, dialect dispatch, and live callback testing. Use whenever the user wants a chart, a layout change, a new filter, a customization, or a dashboard bug investigated.

### data-engineering-pipeline-design

- Path: `skills/data-engineering-pipeline-design/SKILL.md`
- Description: Design source-to-target SQL, Polars, PySpark, ETL/ELT, and medallion-layer workflows from KPI requirements, data model evidence, profiles, and accepted workspace definitions.

### data-model-creation

- Path: `skills/data-model-creation/SKILL.md`
- Description: Create a data model WITH the user through conversation, not by guessing from column names. Interview for grain, entities, keys, facts/dimensions, relationships, cardinality, temporal anchors, and SCD policy; score how well the model is understood; then produce a governed model + ERD/SVG. Use when a workspace needs a data model created, refined, or proven before SQL/pipeline generation, or when relationship detection is uncertain. Pairs with [[grill-requirements]], [[grill-requirements]], [[domain-model]], [[stakeholder-memory]], and [[dashboard-design]] (for the diagram export).

### databricks-access-gates

- Path: `skills/databricks-access-gates/SKILL.md`
- Description: Use when Databricks work hits or may hit missing permissions, token scopes, Unity Catalog grants, workspace API access, SQL warehouse paths, storage policies, compute policies, Genie/dashboard/job creation permissions, data registration approvals, or any remote mutation gate. Ask the user for the exact missing access or approval before retrying remote Databricks actions.

### domain-model

- Path: `skills/domain-model/SKILL.md`
- Description: Align work with the project's domain language, KPI registry, data model, schema, relationships, grain, and business rules. Use before generating contracts, task configs, optimizations, or reports.

### evolution

- Path: `skills/evolution/SKILL.md`
- Description: Learn from stakeholder interviews, user corrections, accepted decisions, rejected assumptions, optimization outcomes, and failed attempts. Use after meaningful project work, after user feedback, after governance decisions, or when patterns should improve future onboarding and optimization.

### feature-derivation-library

- Path: `skills/feature-derivation-library/SKILL.md`
- Description: Use when KPI/query work needs reusable derived-feature patterns, candidate formulas, temporal anchors, join-derived features, taxonomy-derived features, or SQL/Polars derivation templates. This skill helps propose derivations while preserving the rule that candidates are not proof.

### green-gate

- Path: `skills/green-gate/SKILL.md`
- Description: Run the project's portable green gate -- the curated CI suite plus the enterprise suite, the same way ci.yml does -- and report pass/fail with any failures. Use before claiming work is done, before commit, or when the user asks to "run the tests", "check it's green", or "run the green gate". With a sweep, also classify broader blast-radius failures as new vs. known-baseline.

### grill-requirements

- Path: `skills/grill-requirements/SKILL.md`
- Description: Interview stakeholders to understand what they want optimized, what must not change, how success is measured, and what preferences or constraints should shape the solution. Use for new workspace onboarding, KPI/data model discovery, product scoping, or when business/data/platform requirements are incomplete.

### handoff

- Path: `skills/handoff/SKILL.md`
- Description: Compact the current conversation into a handoff document for another agent to pick up. Save to the temporary directory of the user's OS — not the current workspace. Reference existing artifacts (PRDs, plans, ADRs, issues, commits, diffs) by path; do not re-paste them. Redact secrets and PII. Include a "suggested skills" section.

### kpi-analyst

- Path: `skills/kpi-analyst/SKILL.md`
- Description: Use this skill when the user uploads, shares, pastes, or describes a KPI sheet, KPI tracker, metrics document, dashboard metric list, or structured business analytics metric definitions; when asked to understand a metric, write queries for KPIs, calculate a KPI, build a dashboard from KPIs, or inspect files with columns such as Key Business Question, Metric, Dimension, Cut, Filter, Grain, or Description; also use when validating generated KPI SQL and result samples against KPI intent.

### kpi-clarification

- Path: `skills/kpi-clarification/SKILL.md`
- Description: Converts ambiguous or loosely written KPI descriptions into precise, unambiguous business metric definitions. Use this skill whenever a user mentions a KPI, metric, business measure, or indicator that needs to be defined, clarified, structured, or documented — even if they don't use the word "KPI" explicitly. Trigger examples: "define this metric", "what does this KPI mean", "help me document our conversion rate", "clarify this measure for our BI team", "we track X, can you write it up properly", "our dashboard shows Y, not sure what it means", "turn this into a proper metric definition". Always use this skill when the user presents any business performance metric, OKR component, or analytics measure that needs structured decomposition.

### stakeholder-memory

- Path: `skills/stakeholder-memory/SKILL.md`
- Description: Capture durable user, team, and stakeholder preferences discovered during interviews or corrections. Use when the user states how they prefer decisions, reviews, risk handling, output style, naming, governance, or optimization tradeoffs.

### task-onboarding

- Path: `skills/task-onboarding/SKILL.md`
- Description: Turn a workspace project with data, KPI registry, data model, and source artifacts into a runnable optimization task. Use when adding a new project under workspaces/ or refreshing task config, contracts, profiles, and baseline setup.

### to-solution-brief

- Path: `skills/to-solution-brief/SKILL.md`
- Description: Convert stakeholder interviews, KPI registry details, data model facts, and preferences into a concrete solution brief for a governed optimization task. Use after grill-requirements and domain-model have enough information.

### workspace-governance

- Path: `skills/workspace-governance/SKILL.md`
- Description: Enforce workspace safety: keep project outputs under workspaces/<project>/interns/, avoid pushing raw data or generated artifacts, prevent secret leakage, and check staged files before commit/push. Use before git add/commit/push and whenever workspace files are modified.

### workspace-kpi-query-optimizer

- Path: `skills/workspace-kpi-query-optimizer/SKILL.md`
- Description: Build, validate, and optimize query logic for any workspace that contains data, a KPI/metric registry, and a data model. Use for SQL, Polars, or hybrid KPI/query optimization tasks where generated outputs must live under workspaces/<project>/interns/.

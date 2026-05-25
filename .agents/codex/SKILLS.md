# Codex Skill Adapter

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
- Secret display safety: Hard stop: never print env files, credential files, tokens, private keys, shell environment dumps, connection strings, bearer headers, cookies, or config/AST/tree dumps containing secret values. Report only existence/status or redacted key names.
- Dataset access safety: Do not read full raw datasets when profile artifacts or bounded samples are enough.

### Registered Tools

- `list-workspace-files`
  - Command: `uv run list-workspace-files --workspace <workspace>`
  - Use when: workspace selection request; set current workspace; bounded startup file inventory
  - Outputs: all file paths up to cap; possible KPI files; possible data model files; dataset roots; docs; interns state
  - Safety: local_safe_file_paths_only_no_content_reads
  - Required skills: workspace-governance
- `prepare-workspace-selection`
  - Command: `uv run prepare-workspace-selection --workspace <workspace>`
  - Use when: workspace selection request; set current workspace; workspace may be empty or missing; external raw data should stay outside workspace
  - Outputs: guarded workspace selection status; active workspace listing summary; available workspace choices; empty or missing workspace blocker; external dataset_allowlist setup template
  - Safety: local_safe_file_paths_only_no_content_reads
  - Required skills: workspace-governance; clarify-ambiguity
- `session-snapshot`
  - Command: `uv run session-snapshot <start|append|command|file-change|decision|verify|finish>`
  - Use when: operator wants exact end-user conversation transcript; cross-CLI session monitoring; audit conversation turns, commands, file changes, decisions, and intent verification
  - Outputs: .agents/sessions/<session>/compact.md; .agents/sessions/<session>/intent_verification.md; .agents/sessions/<session>/intent_verification.json; .agents/sessions/<session>/events.jsonl; .agents/sessions/<session>/transcript.md; .agents/sessions/<session>/commands.md; .agents/sessions/<session>/file_changes.md; .agents/sessions/<session>/decisions.md; .agents/sessions/<session>/snapshot.json
  - Safety: local_safe_redacts_common_secret_patterns_gitignored
  - Required skills: workspace-governance; evolution; stakeholder-memory
- `onboard-workspace`
  - Command: `uv run onboard-workspace --workspace <workspace>`
  - Use when: workspace artifacts are missing; profiles/contracts/evaluation scaffolding need refresh
  - Outputs: interns/generated/profiles; interns/generated/contracts; interns/reports
  - Safety: local_safe
  - Required skills: workspace-governance; task-onboarding; domain-model; workspace-kpi-query-optimizer
- `kickstart-workspace`
  - Command: `uv run kickstart-workspace --workspace <workspace> --domain <domain>`
  - Use when: new governed workspace; enterprise discovery and task config need refresh
  - Outputs: config/tasks.json; interns/generated/requirements; interns/generated/contracts
  - Safety: local_safe_config_write
  - Required skills: workspace-governance; task-onboarding; grill-requirements; domain-model
- `prepare-kpi-generation`
  - Command: `uv run prepare-kpi-generation --workspace <workspace>`
  - Use when: workspace confirmation completed; user may create, revise, challenge, or score KPIs; show two-path KPI generation versus usual workflow prompt
  - Outputs: interns/generated/requirements/kpi_generation_session.json; interns/reports/kpi_generation/current.json; interns/reports/kpi_generation/current.md; KPI quality/readiness score
  - Safety: local_safe
  - Required skills: workspace-governance; grill-requirements; stakeholder-memory; workspace-kpi-query-optimizer
- `apply-kpi-generation-answer`
  - Command: `uv run apply-kpi-generation-answer --workspace <workspace> --answer <option_id_or_label>`
  - Use when: user answered the current KPI generation panel; KPI generation interview needs to advance; draft KPI registry and evidence proof should be produced
  - Outputs: updated kpi_generation_session.json; next kpi_generation/current.json; interns/generated/requirements/kpi_registry_draft.json
  - Safety: local_safe_validated_write
  - Required skills: grill-requirements; stakeholder-memory; workspace-kpi-query-optimizer
- `finalize-kpi-generation`
  - Command: `uv run finalize-kpi-generation --workspace <workspace> --approve-final-preview`
  - Use when: final KPI draft preview was shown and explicitly approved; write user-facing KPI registry after KPI generation
  - Outputs: workspace docs KPI registry JSON; interns/generated/requirements/kpi_generation_production_proof.json; workspace and team memory
  - Safety: requires_explicit_final_preview_approval
  - Required skills: workspace-governance; stakeholder-memory; to-solution-brief; workspace-kpi-query-optimizer
- `prepare-workspace-workflow`
  - Command: `uv run prepare-workspace-workflow --workspace <workspace> --mode <plan|local-safe|autopilot> --domain <domain>`
  - Use when: workspace confirmation completed; user wants one checkpoint for KPI, data-model, blocker, validation, and presentation workflow; agent needs deterministic next commands and autopilot boundaries
  - Outputs: interns/reports/workflow/current.json; interns/reports/workflow/current.md; local-safe generated artifacts when mode is local-safe or autopilot
  - Safety: local_safe_checkpoint_autopilot_stops_before_final_delete_remote_codegen
  - Required skills: workspace-governance; task-onboarding; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `prepare-bronze-silver-standards`
  - Command: `uv run prepare-bronze-silver-standards --workspace <workspace> --domain <domain>`
  - Use when: Bronze/Silver production standards should be materialized; runtime-neutral transformation manifest is needed before SQL, Polars, or PySpark generation; workflow reroute policy should stop wrong-route actions and auto-reroute once
  - Outputs: interns/generated/contracts/bronze_silver_standards.json; interns/generated/contracts/transformation_manifest.json; interns/generated/contracts/workflow_reroute_policy.json; interns/reports/bronze_silver_standards.md
  - Safety: local_safe_contract_write_no_execution
  - Required skills: workspace-governance; data-engineering-pipeline-design
- `workspace-flow`
  - Command: `uv run workspace-flow <start|status|answer|results>`
  - Use when: agent-led workspace workflow should run quietly in the backend; user asks for KPI generation by interview; user asks to generate SQL and show KPI results; main chat should show only compact questions or results
  - Outputs: interns/state/workflow_sessions/<session-id>/session.json; interns/state/workflow_sessions/<session-id>/current.json; interns/state/workflow_sessions/<session-id>/current.md; interns/reports/kpi_results/current.md; interns/generated/evidence/kpi_results/current.json
  - Safety: local_safe_session_orchestrator_hides_lower_level_command_noise
  - Required skills: workspace-governance; task-onboarding; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `prepare-wiki-memory`
  - Command: `uv run prepare-wiki-memory --workspace <workspace> --domain <domain>`
  - Use when: repeated KPI terms or data-model decisions should be found; shared team wiki memory should suggest safe reuse; autopilot needs governed draft prefill candidates
  - Outputs: state/team_memory/wiki_memory_index.json; interns/generated/memory/wiki_memory_candidates.json; interns/reports/wiki_memory/current.json; interns/reports/wiki_memory/current.md
  - Safety: local_safe_structured_artifacts_only_draft_prefill_blocks_execution
  - Required skills: stakeholder-memory; evolution; workspace-kpi-query-optimizer
- `validate-memory-health`
  - Command: `uv run validate-memory-health --workspace <workspace>`
  - Use when: workspace memory needs confidence-scored lifecycle health; shared team memory should be checked before reuse; memory entries need status, confidence, verification, expiration, and evidence normalization
  - Outputs: interns/reports/memory_health/current.json; interns/reports/memory_health/current.md; interns/generated/evidence/memory_health/current.json
  - Safety: local_safe_memory_artifact_scan_no_raw_data_reads
  - Required skills: stakeholder-memory; evolution
- `prepare-agent-benchmark`
  - Command: `uv run prepare-agent-benchmark --workspace <workspace> --domain <domain>`
  - Use when: workspace needs a readiness proof; release gates should be checked before SQL, ETL, medallion, autopilot, or production promotion; owners need an artifact-backed benchmark scorecard
  - Outputs: interns/generated/contracts/agent_benchmark_scorecard.json; interns/generated/contracts/release_gate_status.json; interns/reports/benchmarks/current.json; interns/reports/benchmarks/current.md
  - Safety: local_safe_project_native_scorecard_no_external_benchmark_execution
  - Required skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- `validate-project-harness`
  - Command: `uv run validate-project-harness --workspace <workspace> --domain <domain>`
  - Use when: workspace needs one top-level score before completion is claimed; all local-safe harnesses should run together; release readiness needs artifact validation, workflow guardrails, trajectory health, KPI execution, benchmark, and git hygiene proof
  - Outputs: interns/generated/evidence/project_harness.json; interns/reports/project_harness.md; score, blockers, warnings, and next commands
  - Safety: local_safe_project_harness_no_remote_execution
  - Required skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- `run-reliability-suite`
  - Command: `uv run run-reliability-suite --workspace <workspace> --domain <domain>`
  - Use when: scheduled local-safe reliability checks should run; workspace workflow guardrails and evidence graph should be refreshed together; project harness should run only when required generated artifacts are available
  - Outputs: interns/reports/reliability_suite/current.json; interns/reports/reliability_suite/current.md; interns/generated/evidence/reliability_suite/current.json
  - Safety: local_safe_reliability_runner_no_shell_execution
  - Required skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- `run-ai-app-harness`
  - Command: `uv run run-ai-app-harness --workspace <workspace> --dataset <workspace>/interns/ai_harness/datasets/<suite>.jsonl`
  - Use when: workspace needs dependency-free AI app tests; JSONL prompt cases should be evaluated with exact/schema/keyword or KPI/SQL-specific checks; KPI mapping, SQL semantic, result-table, or adversarial AI behavior should be regression tested; local stub or explicitly approved raw HTTP AI boundary should be tested; baseline per-case and result-signature regressions should block CI
  - Outputs: interns/ai_harness/runs/<run_id>/outputs.jsonl; interns/ai_harness/runs/<run_id>/report.json; interns/reports/ai_app_harness/current.json; interns/reports/ai_app_harness/current.md; interns/generated/evidence/ai_app_harness/current.json
  - Safety: local_safe_by_default_remote_ai_requires_allow_flag
  - Required skills: workspace-governance; workspace-kpi-query-optimizer
- `run-ai-cli-harness`
  - Command: `uv run run-ai-cli-harness --workspace <workspace> --dataset <workspace>/interns/ai_cli_harness/datasets/<suite>.jsonl`
  - Use when: CLI agents such as Claude, Gemini, Codex, or custom tools need governed workflow regression tests; command transcripts, project-tool usage, artifact outputs, JSON fields, and workflow guardrails should be evaluated; real CLI subprocess execution should remain blocked unless explicitly approved
  - Outputs: interns/ai_cli_harness/runs/<run_id>/outputs.jsonl; interns/ai_cli_harness/runs/<run_id>/report.json; interns/reports/ai_cli_harness/current.json; interns/reports/ai_cli_harness/current.md; interns/generated/evidence/ai_cli_harness/current.json
  - Safety: local_safe_stub_by_default_real_cli_requires_allow_flag
  - Required skills: workspace-governance; workspace-kpi-query-optimizer
- `validate-workflow-guardrails`
  - Command: `uv run validate-workflow-guardrails --workspace <workspace>`
  - Use when: workflow itself needs a reliability gate; blocker panels may contain invented or non-source-backed features; failed shell commands or raw-data reads should be audited; non-portable commands should be caught before retrying
  - Outputs: interns/reports/workflow_guard_harness/current.json; interns/reports/workflow_guard_harness/current.md; interns/generated/evidence/workflow_guard_harness/current.json
  - Safety: local_safe_read_only_validation_report
  - Required skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- `run-layered-pipeline-harness`
  - Command: `uv run run-layered-pipeline-harness --workspace <workspace>`
  - Use when: catalog, route, and pipeline contracts need a layered data-engineering validation gate; ETL, ELT, medallion, existing-layer validation, or KPI-only pipeline plans should be checked before code generation or production proof; deduplication, grain, raw path, and remote mutation policies need enforcement
  - Outputs: interns/reports/layered_pipeline_harness/current.json; interns/reports/layered_pipeline_harness/current.md; interns/generated/evidence/layered_pipeline_harness/current.json
  - Safety: local_safe_read_only_contract_harness_no_raw_data_reads
  - Required skills: workspace-governance; data-engineering-pipeline-design
- `run-pipeline-execution-harness`
  - Command: `uv run run-pipeline-execution-harness --workspace <workspace>`
  - Use when: generated pipeline_layers.sql should be executed locally before proof or promotion; bronze, silver, and gold layer views need row-count and column evidence; pipeline sample output must be redacted while preserving execution proof
  - Outputs: interns/reports/pipeline_execution_harness/current.json; interns/reports/pipeline_execution_harness/current.md; interns/generated/evidence/pipeline_execution_harness/current.json
  - Safety: local_safe_duckdb_execution_redacted_samples_no_remote_writes
  - Required skills: workspace-governance; data-engineering-pipeline-design
- `run-data-quality-harness`
  - Command: `uv run run-data-quality-harness --workspace <workspace>`
  - Use when: profile, catalog, and pipeline contracts need a local-safe duplicate and data-quality review; bounded duplicate evidence should be prepared before stakeholder duplicate decisions; data quality findings must be written without automatic deduplication, quarantine SQL, or remote mutation
  - Outputs: interns/generated/contracts/data_quality_contract.json; interns/generated/evidence/data_quality_harness/current.json; interns/reports/data_quality/current.json; interns/reports/data_quality/current.md
  - Safety: local_safe_profile_catalog_pipeline_contract_driven_bounded_duplicate_evidence_redacted_samples_no_auto_dedup_or_quarantine_sql_mutation
  - Required skills: workspace-governance; data-engineering-pipeline-design
- `prepare-duplicate-review-panel`
  - Command: `uv run prepare-duplicate-review-panel --workspace <workspace>`
  - Use when: duplicate evidence from the data quality harness needs stakeholder review; duplicate handling options should be generated from bounded profile/catalog/pipeline contract evidence; agent needs a JSON-backed duplicate review panel before applying any decision
  - Outputs: interns/reports/duplicate_review/current.json; interns/reports/duplicate_review/current.md; interns/generated/contracts/data_quality_contract.json
  - Safety: local_safe_profile_catalog_pipeline_contract_driven_bounded_duplicate_evidence_redacted_samples_no_auto_dedup_or_quarantine_sql_mutation
  - Required skills: workspace-governance; data-engineering-pipeline-design; clarify-ambiguity
- `apply-duplicate-review-answer`
  - Command: `uv run apply-duplicate-review-answer --workspace <workspace> --answer <option_id_or_label>`
  - Use when: user answered the current duplicate review panel; accepted duplicate decision should be resolved against current.json; decision should be recorded without executing deduplication or quarantine SQL
  - Outputs: interns/generated/contracts/duplicate_decisions.json; interns/reports/duplicate_review/current.json; interns/reports/duplicate_review/current.md
  - Safety: local_safe_validated_decision_write_no_auto_dedup_or_quarantine_sql_mutation_milestone_1
  - Required skills: workspace-governance; stakeholder-memory; data-engineering-pipeline-design
- `record-workspace-trajectory`
  - Command: `uv run record-workspace-trajectory --workspace <workspace> --event-type <type> --status <status> --summary <summary>`
  - Use when: agent or CLI workflow steps should be replayable; commands, validations, decisions, retries, and artifacts need workspace-scoped audit events; workflow guardrails should validate the step-by-step trajectory
  - Outputs: interns/state/trajectory.jsonl; interns/reports/trajectory/current.json; interns/reports/trajectory/current.md; interns/generated/evidence/trajectory/current.json
  - Safety: local_safe_append_only_secret_redacted_workspace_log
  - Required skills: workspace-governance; evolution
- `build-workspace-evidence-graph`
  - Command: `uv run build-workspace-evidence-graph --workspace <workspace>`
  - Use when: KPI, mapping, SQL, trajectory, and harness artifacts need one traceability graph; agent needs impact analysis before changing a feature mapping; reviewer asks where a term, column, blocker answer, or SQL dependency came from
  - Outputs: interns/generated/evidence_graph/graph.json; interns/reports/evidence_graph/current.md
  - Safety: local_safe_existing_artifacts_only_no_raw_data_reads
  - Required skills: workspace-governance; domain-model; workspace-kpi-query-optimizer
- `query-workspace-evidence-graph`
  - Command: `uv run query-workspace-evidence-graph --workspace <workspace> --term <term>`
  - Use when: agent or reviewer asks why a term exists; feature or column impact needs to be listed from the evidence graph; stale or invented term origin needs traceability
  - Outputs: JSON query result with matched nodes, introducers, users, and impact edges
  - Safety: local_safe_existing_graph_or_rebuild_no_raw_data_reads
  - Required skills: workspace-governance; domain-model; workspace-kpi-query-optimizer
- `kpi-proof-packet`
  - Command: `uv run kpi-proof-packet --workspace <workspace> --domain <domain>`
  - Use when: stakeholders need one all-KPI recommendation packet; operators need source row traceability, mapping recommendations, reliability gates, generated SQL, execution output previews, and sample values in one report; bulk KPI mapping review should happen before apply-safe or execution modes
  - Outputs: interns/reports/kpi_proof_packet/current.md; interns/reports/kpi_proof_packet/current.json; interns/generated/evidence/kpi_proof_packet/current.json
  - Safety: local_safe_read_only_recommend_mode
  - Required skills: workspace-governance; domain-model; workspace-kpi-query-optimizer; to-solution-brief
- `discover-external-sources`
  - Command: `uv run discover-external-sources --workspace <workspace> --external-root <external_root>`
  - Use when: user points to a large external folder; external source root needs dataset/doc/log/delta/database classification; draft source selection from cold storage without making it the workspace
  - Outputs: interns/generated/requirements/external_source_discovery.json; interns/reports/external_source_discovery.md; docs/source_selection.generated.json
  - Safety: local_safe_metadata_path_only_review_gated
  - Required skills: workspace-governance; data-engineering-pipeline-design; task-onboarding
- `prepare-external-source-intake`
  - Command: `uv run prepare-external-source-intake --external-root <external_root> --proposed-workspace <workspace>`
  - Use when: user provides an external path and has not chosen existing versus new workspace; external-source route preference should be reused or challenged; agent needs a deterministic panel instead of asking freehand
  - Outputs: interns/generated/requirements/external_source_intake_session.json; interns/reports/external_source_intake/current.json; interns/reports/external_source_intake/current.md
  - Safety: local_safe_panel_write_no_source_reads
  - Required skills: workspace-governance; data-engineering-pipeline-design; grill-requirements
- `apply-external-source-intake`
  - Command: `uv run apply-external-source-intake --external-root <external_root> --proposed-workspace <workspace> --answer <option>`
  - Use when: user answered the external source intake panel; metadata-only discovery should run after route selection; saved external-source defaults or change reasons should be recorded
  - Outputs: external_source_intake_session.json; external_source_discovery.json; external_source_discovery.md; docs/source_selection.generated.json; state/team_memory/external_source_intake_preferences.json
  - Safety: local_safe_metadata_only_until_review_gate
  - Required skills: workspace-governance; data-engineering-pipeline-design; stakeholder-memory
- `prepare-data-model-generation`
  - Command: `uv run prepare-data-model-generation --workspace <workspace>`
  - Use when: data model docs are missing, weak, or image-only; workspace needs governed data model creation or parsing; relationship proof should be reviewed before executable SQL
  - Outputs: interns/reports/data_model_generation/current.json; interns/reports/data_model_generation/current.md
  - Safety: local_safe
  - Required skills: workspace-governance; domain-model; grill-requirements
- `apply-data-model-answer`
  - Command: `uv run apply-data-model-answer --workspace <workspace> --answer <option_id_or_label>`
  - Use when: user answered the current data-model generation panel; draft data-model artifacts should be produced under interns
  - Outputs: interns/generated/requirements/data_model_draft.json; interns/reports/data_model_generation
  - Safety: local_safe_validated_write
  - Required skills: domain-model; stakeholder-memory; grill-requirements
- `finalize-data-model-generation`
  - Command: `uv run finalize-data-model-generation --workspace <workspace> --approve-final-preview`
  - Use when: draft data model preview was shown and explicitly approved; write user-facing data model docs and finalized model contract
  - Outputs: docs/data-model.md; docs/erd.md; docs/relationships.md; interns/generated/contracts/data_model_contract.json
  - Safety: requires_explicit_final_preview_approval
  - Required skills: workspace-governance; domain-model; to-solution-brief
- `prepare-data-model-blocker-panel`
  - Command: `uv run prepare-data-model-blocker-panel --workspace <workspace>`
  - Use when: data model draft exists; agent needs the next JSON-backed data-model blocker question; grain, primary key, relationship, temporal anchor, or SCD decisions need deterministic resolution
  - Outputs: interns/reports/data_model_blocker_panel/current.json; interns/reports/data_model_blocker_panel/current.md; updated data_model_generation_session.json
  - Safety: local_safe
  - Required skills: domain-model; clarify-ambiguity; grill-requirements
- `apply-data-model-blocker-answer`
  - Command: `uv run apply-data-model-blocker-answer --workspace <workspace> --answer <option_id_or_label>`
  - Use when: user answered the current data-model blocker panel; accepted option should apply a structured data-model operation; next blocker panel should be generated
  - Outputs: updated interns/generated/requirements/data_model_draft.json; next interns/reports/data_model_blocker_panel/current.json; next interns/reports/data_model_blocker_panel/current.md
  - Safety: local_safe_validated_write
  - Required skills: domain-model; stakeholder-memory; grill-requirements
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
- `resolve-kpi-features`
  - Command: `uv run resolve-kpi-features --workspace <workspace> --domain <domain> --include-candidates`
  - Use when: KPI features need mapping; blockers need clustering; accepted definitions need applying
  - Outputs: interns/generated/contracts/kpi_feature_mapping.json; strict derived_feature_options with formula/input/observed_values/value_profile/semantic_meaning_sources/reason/example/evidence_sources/derivation_reasoning/evidence_state/confidence; semantically mismatched candidates rejected; interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; interns/reports/open_questions.md
  - Safety: local_safe
  - Required skills: domain-model; feature-derivation-library; workspace-kpi-query-optimizer; clarify-ambiguity
- `apply-workspace-definition`
  - Command: `uv run resolve-kpi-features --workspace <workspace> --apply-workspace-definition --feature <feature> --definition <definition> --evidence-note <note>`
  - Use when: one accepted feature definition applies across multiple KPIs
  - Outputs: interns/generated/contracts/workspace_feature_definitions.json; interns/generated/contracts/kpi_feature_mapping.json
  - Safety: local_safe
  - Required skills: domain-model; stakeholder-memory; workspace-kpi-query-optimizer
- `prepare-kpi-blocker-panel`
  - Command: `uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>`
  - Use when: agent needs the next validated KPI blocker question; avoid hand-chaining onboarding, resolver, markdown, panel, and validation commands; fresh or existing KPI workspace needs deterministic blocker preparation
  - Outputs: interns/generated/contracts/kpi_feature_mapping.json; interns/reports/derived_feature_reviews; interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; validation summary
  - Safety: local_safe
  - Required skills: domain-model; feature-derivation-library; workspace-kpi-query-optimizer; clarify-ambiguity
- `apply-kpi-panel-answer`
  - Command: `uv run apply-kpi-panel-answer --workspace <workspace> --domain <domain> --answer <option_id_or_label>`
  - Use when: user answered the current blocker question; accepted option should be applied without inventing unsupported resolver flags; friendly answer must be resolved against current.json
  - Outputs: interns/generated/contracts/workspace_feature_definitions.json; updated kpi_feature_mapping.json; next validated blocker_question_panel/current.json
  - Safety: local_safe_validated_write
  - Required skills: domain-model; stakeholder-memory; workspace-kpi-query-optimizer
- `generate-kpi-sql`
  - Command: `uv run generate-kpi-sql --workspace <workspace> --kpi-id <kpi_id>`
  - Use when: KPI features are proven or user-confirmed
  - Outputs: interns/generated/solutions
  - Safety: local_safe
  - Required skills: domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `plan-source-to-target`
  - Command: `uv run plan-source-to-target --workspace <workspace> --target-engine <sql|polars|pyspark|hybrid>`
  - Use when: SQL, Polars, PySpark, ETL, or medallion implementation needs a data-model-backed source-to-target plan; agent must verify source datasets, joins, grain, temporal anchors, and target layer before code generation
  - Outputs: interns/generated/contracts/source_to_target_plan.json; interns/reports/source_to_target_plan.md; interns/generated/context/manifests/plan-source-to-target_<budget>.json
  - Safety: local_safe
  - Required skills: domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `build-catalog-contract`
  - Command: `uv run build-catalog-contract --workspace <workspace>`
  - Use when: profile-backed source datasets need a stable logical catalog interface; pipeline and KPI code generation must avoid direct raw path dependencies; route or pipeline planning needs catalog objects before executable work
  - Outputs: interns/generated/contracts/catalog_contract.json; interns/reports/catalog_contract.md
  - Safety: local_safe_profile_backed_contract_write_no_remote_mutation
  - Required skills: workspace-governance; domain-model; data-engineering-pipeline-design
- `build-source-family-contracts`
  - Command: `uv run build-source-family-contracts --workspace <workspace>`
  - Use when: external raw folder has repeated dated CSV releases; schema drift must be understood before ETL or medallion planning; workspace has profiles but no KPI registry or no KPI-first workflow; agent must group source files without reading raw datasets or duplicating full profile payloads
  - Outputs: interns/generated/contracts/source_family_contracts.json; interns/reports/source_family_contracts.md
  - Safety: local_safe_profile_index_only_no_raw_data_reads
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
  - Recovery: If an agent selected resolve-kpi-features for an external profiled no-KPI workspace, stop, reread .agents/tools.json or .agents/<tool>/SKILLS.md, then run build-source-family-contracts first.
- `prepare-data-engineering-route`
  - Command: `uv run prepare-data-engineering-route --workspace <workspace> --track <auto|kpi_only|etl|elt|medallion|oltp_ingestion|existing_gold_validation> --target-engine <sql|polars|pyspark|hybrid>`
  - Use when: workspace needs a governed route before pipeline planning; agent must choose between KPI-only, ETL, ELT, medallion, OLTP ingestion, or existing-gold validation; trusted existing layers and local-first remote policy should be recorded
  - Outputs: interns/generated/contracts/data_engineering_route.json; interns/reports/data_engineering_route.md; interns/generated/contracts/catalog_contract.json
  - Safety: local_safe_route_contract_write_no_execution
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
- `prepare-pipeline-plan`
  - Command: `uv run prepare-pipeline-plan --workspace <workspace> --track <auto|kpi_only|etl|elt|medallion|oltp_ingestion|existing_gold_validation> --target-engine <sql|polars|pyspark|hybrid> --table-format <auto|delta|iceberg|local_parquet|warehouse_native|duckdb_view|csv>`
  - Use when: SQL, Polars, PySpark, ETL, ELT, medallion, or existing-layer validation needs a governed pipeline contract before code generation; layer definitions, quality gates, approval-gated transformations, and blockers must be recorded; source-to-target blockers should stop executable generation
  - Outputs: interns/generated/contracts/pipeline_plan.json; interns/reports/pipeline_plan.md; interns/generated/contracts/data_engineering_route.json; interns/generated/contracts/catalog_contract.json
  - Safety: local_safe_pipeline_plan_write_no_execution_remote_writes_approval_gated
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
- `prepare-pipeline-format-panel`
  - Command: `uv run prepare-pipeline-format-panel --workspace <workspace>`
  - Use when: ETL, ELT, medallion, or ingestion pipeline needs target table/file format selection; agent must ask whether to store outputs as Delta, Parquet, Iceberg, CSV, warehouse-native, or another approved format; prepare-pipeline-plan is blocked by pipeline_table_format_unresolved
  - Outputs: interns/reports/pipeline_format/current.json; interns/reports/pipeline_format/current.md
  - Safety: local_safe_json_backed_user_choice_panel
  - Required skills: data-engineering-pipeline-design; clarify-ambiguity
- `apply-pipeline-format-answer`
  - Command: `uv run apply-pipeline-format-answer --workspace <workspace> --answer <option_id_or_label_or_format>`
  - Use when: user answered the pipeline format panel; accepted storage format should be recorded before pipeline planning; medallion plan should use the approved table/file format
  - Outputs: interns/generated/contracts/pipeline_decisions.json
  - Safety: local_safe_validated_decision_write_requires_user_approval
  - Required skills: data-engineering-pipeline-design; stakeholder-memory
- `prepare-pipeline-deployment-plan`
  - Command: `uv run prepare-pipeline-deployment-plan --workspace <workspace> --target <local|external|warehouse> --mode <dry-run|apply>`
  - Use when: generated pipeline outputs need a deployment dry-run contract; remote deployment target needs approval evidence before any external or warehouse apply; operators need proof that no remote mutation occurred
  - Outputs: interns/generated/contracts/pipeline_deployment_plan.json; interns/reports/pipeline_deployment_plan.md
  - Safety: dry_run_default_no_remote_mutation_external_and_warehouse_apply_require_AUTORESEARCH_ALLOW_REMOTE_EXECUTION
  - Required skills: workspace-governance; data-engineering-pipeline-design; databricks-access-gates
- `apply-pipeline-decision`
  - Command: `uv run apply-pipeline-decision --workspace <workspace> --kpi-id <kpi_id> --percentage-denominator-scope <global_total|within_department|within_gender|within_visit_type|selected_population>`
  - Use when: user approved a percentage or ratio KPI denominator scope; pipeline_plan.json is blocked by percentage_denominator_scope_unresolved; approved pipeline decisions should be recorded before regenerating the plan
  - Outputs: interns/generated/contracts/pipeline_decisions.json
  - Safety: local_safe_validated_decision_write_requires_user_approval
  - Required skills: data-engineering-pipeline-design; stakeholder-memory
- `generate-pipeline-sql`
  - Command: `uv run generate-pipeline-sql --workspace <workspace>`
  - Use when: catalog and pipeline contracts are ready; local DuckDB bronze/silver/gold layer SQL scaffold should be generated from pipeline_plan.json; raw paths must remain limited to catalog bootstrap
  - Outputs: interns/generated/pipeline/pipeline_layers.sql
  - Safety: local_safe_code_generation_no_execution_no_remote_writes
  - Required skills: workspace-governance; data-engineering-pipeline-design; domain-model
- `context-router`
  - Command: `uv run context-router build --workspace <workspace> --task <task> --budget <small|standard|deep>`
  - Use when: task needs bounded context instead of whole artifact loading; agent needs context wiki/page index; large workspace artifacts must be routed by budget
  - Outputs: interns/generated/context/context_index.json; interns/generated/context/context_pages.jsonl; interns/generated/context/manifests/<task>_<budget>.json; interns/reports/context/<task>_<budget>.md
  - Safety: local_safe_derived_context_only
  - Required skills: workspace-governance; domain-model
- `build-relationship-contracts`
  - Command: `uv run build-relationship-contracts --workspace <workspace>`
  - Use when: multi-dataset executable generation needs FK/relationship proof; source-to-target joins need production-grade relationship contracts; profile-only joins need approval gating
  - Outputs: interns/generated/contracts/relationship_contracts.json; interns/reports/relationship_contracts.md
  - Safety: local_safe_governed_contract_write
  - Required skills: domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- `derived-feature-markdown`
  - Command: `uv run derived-feature-markdown --workspace <workspace>`
  - Use when: stakeholders need readable derived-feature blocker reviews; strict derived_feature_options JSON needs Markdown rendering
  - Outputs: interns/reports/derived_feature_reviews/md/<kpi_id>_<feature>.md; interns/reports/derived_feature_reviews/json/<kpi_id>_<feature>.json; interns/reports/derived_feature_reviews/index.md
  - Safety: local_safe
  - Required skills: feature-derivation-library; workspace-kpi-query-optimizer
- `blocker-question-panel`
  - Command: `uv run blocker-question-panel --workspace <workspace>`
  - Use when: agent needs to ask any KPI blocker question; direct mapping or source-of-truth choice needs stakeholder answer; non-technical panel is preferred over terminal option UI
  - Outputs: interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; interns/reports/blocker_question_panel/index.json
  - Safety: local_safe
  - Required skills: clarify-ambiguity; grill-requirements; workspace-kpi-query-optimizer
- `validate-workspace-artifacts`
  - Command: `uv run validate-workspace-artifacts --workspace <workspace>`
  - Use when: generated workspace artifacts need schema checks; agent is about to rely on KPI registry, feature mapping, derived reviews, or blocker panel; detect manual edits to generated contracts
  - Outputs: validation summary JSON with checked_files, errors, and warnings
  - Safety: local_safe_read_only
  - Required skills: workspace-governance; workspace-kpi-query-optimizer
- `prepare-workspace-bug-report`
  - Command: `uv run prepare-workspace-bug-report --workspace <workspace>`
  - Use when: workspace selection and onboarding evidence disagree; fresh workspace flow generated empty artifacts; blocking product bugs need a JSON and Markdown report
  - Outputs: interns/generated/evidence/bug_report.json; interns/reports/bugs/current.md
  - Safety: local_safe_governed_report_write
  - Required skills: workspace-governance; evolution
- `cleanup-workspace-references`
  - Command: `uv run cleanup-workspace-references --workspace <workspace> --all-references`
  - Use when: fresh workspace restart is requested; stale generated references need removal
  - Outputs: dry-run cleanup plan; optional deletion of interns and repo runtime references with --apply --confirm-delete <workspace>
  - Safety: hard_permission_block_for_any_delete
  - Required skills: workspace-governance
- `profiler`
  - Command: `uv run python tools/profiler.py --input <path> --pct <pct> --engine auto --out <dir>`
  - Use when: profile artifacts are missing; bounded sample or representation evidence is required
  - Outputs: profile CSVs; sample outputs; representation reports
  - Safety: local_or_configured_data_access
  - Required skills: workspace-governance; domain-model; task-onboarding
- `optimizer-finder`
  - Command: `uv run python tools/optimizer_finder.py --target <path> --mode auto`
  - Use when: SQL/Python artifact is slow; hotspot evidence is needed
  - Outputs: state/hotspots.json
  - Safety: local_safe
  - Required skills: workspace-kpi-query-optimizer; evolution
- `methodology-parser`
  - Command: `uv run python tools/methodology_parser.py --doc <path> --out <schema.json>`
  - Use when: methodology, dictionary, or contract document needs semantic schema extraction
  - Outputs: semantic schema JSON
  - Safety: may_use_llm_intern
  - Required skills: domain-model; task-onboarding
- `databricks-setup`
  - Command: `uv run python tools/databricks_setup.py`
  - Use when: user explicitly asks to validate Databricks configuration
  - Outputs: connection and capability report
  - Safety: remote_sensitive_explicit_approval
  - Required skills: databricks-access-gates; workspace-governance
- `generate-skill-adapters`
  - Command: `uv run generate-skill-adapters`
  - Use when: skills or adapter routing changed
  - Outputs: .agents/skills_index.json; .agents/<tool>/SKILLS.md
  - Safety: local_safe
  - Required skills: workspace-governance

## Available Subagents

These subagents are generated from `skills/*/agents/*.yaml`; do not hand-edit adapter output.
Use the narrowest role that fits the task and keep write access limited to implementer-style roles.

### business-analyst

- Display name: Business Analyst
- Description: Converts stakeholder intent into KPI definitions, acceptance criteria, grains, filters, and governed open questions.
- Skills: grill-requirements; domain-model; stakeholder-memory; to-solution-brief; workspace-kpi-query-optimizer
- Safety: read_only_requirements_and_decision_capture
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `workspace-write`
- Model policy: {"default_tier": "light", "escalate_to_deep_for": ["high-risk business semantics that would change production metrics"], "escalate_to_standard_for": ["conflicting KPI definitions", "unclear metric ownership", "cross-stakeholder tradeoffs"], "use_light_for": ["file-set summaries", "requirements extraction", "panel wording", "decision recording"]}
- Default prompt: Act as the business-analysis role for data work. Clarify stakeholder goals, KPI formulas, denominator rules, temporal anchors, lifecycle states, acceptance criteria, and approval owners. Use existing files and generated panels before asking. Record accepted decisions and rejected options under the active workspace interns artifacts. Do not generate executable SQL or pipeline code.

### data-analyst

- Display name: Data Analyst
- Description: Profiles, interprets, and validates data evidence for KPI readiness, anomalies, trends, and business-facing result explanations.
- Skills: workspace-governance; domain-model; feature-derivation-library; workspace-kpi-query-optimizer; evolution
- Safety: profile_first_analysis_no_raw_dataset_overread
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `read-only`
- Model policy: {"default_tier": "light", "escalate_to_deep_for": ["production-impacting metric interpretation disputes"], "escalate_to_standard_for": ["KPI plausibility analysis", "conflicting data evidence", "multi-table analytical reasoning"], "use_light_for": ["profile review", "anomaly summaries", "candidate mapping triage"]}
- Default prompt: Act as the data-analysis role. Use profile-first evidence to inspect distributions, nulls, categories, candidate mappings, anomalies, and KPI result plausibility. Prefer generated profile artifacts and bounded samples only when profiles cannot answer a concrete question. Explain what the data can and cannot support, and raise blocker-panel questions for unproven business meanings.

### data-engineer

- Display name: Data Engineer
- Description: Designs governed source-to-target, data-quality, medallion, ETL/ELT, orchestration, and deployment-safe data pipelines.
- Skills: workspace-governance; domain-model; data-engineering-pipeline-design; databricks-access-gates; evolution
- Safety: governed_pipeline_design_local_safe_by_default
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `workspace-write`
- Model policy: {"default_tier": "standard", "escalate_to_deep_for": ["production architecture tradeoffs", "ambiguous relationship or grain proof", "remote deployment risk decisions"], "use_light_for": ["contract inventory", "route classification", "checklist validation"], "use_standard_for": ["source-to-target planning", "medallion layer design", "data-quality gate design"]}
- Default prompt: Act as the data-engineering role. Build or review Bronze, Silver, and Gold plans from source contracts, profile evidence, relationship contracts, and accepted decisions. Enforce data quality, lineage, idempotency, quarantine, schema drift, reroute policy, and deployment approval gates. Produce plans and contracts before executable logic; do not run remote mutation without explicit approval.

### sql-polars-pyspark-specialist

- Display name: SQL Polars PySpark Specialist
- Description: Chooses and implements the correct query/runtime engine, preserving parity across SQL, Polars, and PySpark when required.
- Skills: workspace-governance; domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- Safety: executable_generation_requires_source_to_target_and_relationship_proof
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `workspace-write`
- Model policy: {"default_tier": "standard", "escalate_to_deep_for": ["cross-engine semantic parity", "complex joins or windowing", "high-risk KPI formula correctness"], "use_light_for": ["syntax rewrites", "simple engine selection", "formatting and lint fixes"], "use_standard_for": ["implementation from approved contracts", "test generation", "query optimization"]}
- Default prompt: Act as the SQL, Polars, and PySpark implementation specialist. Select the narrowest supported engine for the requested runtime: SQL for warehouse-native queries, Polars for local file processing and deterministic profiling/transforms, and PySpark for distributed Spark or Databricks pipelines. Generate only the requested engine unless parity is explicitly required. Block executable generation when source, join, grain, temporal anchor, or engine parity proof is missing.

### databricks-engineer

- Display name: Databricks Engineer
- Description: Plans and reviews Databricks-specific Unity Catalog, Delta, Lakeflow, jobs, permissions, costs, and production deployment gates.
- Skills: workspace-governance; data-engineering-pipeline-design; databricks-access-gates
- Safety: databricks_remote_mutation_requires_explicit_approval
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `read-only`
- Model policy: {"default_tier": "standard", "escalate_to_deep_for": ["production permission architecture", "failure recovery design", "remote apply risk assessment"], "use_light_for": ["access checklist review", "object naming review", "dry-run plan summaries"], "use_standard_for": ["Databricks deployment planning", "Delta and Unity Catalog design", "cost and compute control review"]}
- Default prompt: Act as the Databricks engineering role. Review or design Databricks-specific deployment choices: Unity Catalog objects, Delta tables, Lakeflow or job orchestration, SQL warehouses, clusters, permissions, data quality expectations, lineage, cost controls, and remote execution approvals. Use databricks-access-gates before any remote action and keep local dry-runs as the default.

### agent-advisor-router

- Display name: Agent Advisor Router
- Description: Advises which specialist agent, skill chain, sandbox, and model tier should handle a task before expensive work starts.
- Skills: workspace-governance; clarify-ambiguity; evolution
- Safety: read_only_routing_and_cost_control_advice
- Source: `skills/data-engineering-pipeline-design/agents/specialized-data-team.yaml`
- Target model: `default`
- Target sandbox/permission: `read-only`
- Model policy: {"default_tier": "light", "escalate_to_deep_for": ["only when route selection itself affects production safety"], "escalate_to_standard_for": ["conflicting routes", "missing active workspace context"], "use_light_for": ["task classification", "agent selection", "model tier recommendation"]}
- Default prompt: Act as the advisor/router. Classify the user's request, choose the narrowest specialist role, choose the cheapest sufficient model tier, and name the required skills and tool route. Escalate only when ambiguity, production risk, security, or semantic correctness requires it. Do not perform the downstream implementation yourself unless no specialist route fits.

### databricks-access-gates

- Display name: Databricks Access Gates
- Description: Ask for missing Databricks access gates
- Skills: databricks-access-gates
- Safety: follows_skill_policy
- Source: `skills/databricks-access-gates/agents/openai.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Use Databricks access gates to identify missing scopes, grants, policies, approvals, warehouse paths, and workspace permissions before retrying Databricks remote actions.

### feature-derivation-library

- Display name: Feature Derivation Library
- Description: Use reusable KPI feature derivation patterns safely.
- Skills: feature-derivation-library
- Safety: follows_skill_policy
- Source: `skills/feature-derivation-library/agents/openai.yaml`
- Target model: `default`
- Target sandbox/permission: `role_defined`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Use reusable derivation patterns to propose evidence-backed KPI feature mappings without treating candidates as proof.

### workspace-flow-orchestrator

- Display name: Workspace Flow Orchestrator
- Description: Drives plan, local-safe, and bounded-autopilot workspace orchestration while keeping main-chat output concise.
- Skills: workspace-governance; workspace-kpi-query-optimizer; task-onboarding
- Safety: local_safe_workflow_only_no_remote_execution
- Source: `skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml`
- Target model: `default`
- Target sandbox/permission: `workspace-write`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Use workspace-flow and prepare-workspace-workflow as the canonical orchestration APIs. Always let the user choose plan, local-safe, or bounded autopilot; default to local-safe. After onboarding, prepare Bronze/Silver standards, the runtime-neutral transformation manifest, workflow_reroute_policy, data-quality gate, and layer route before KPI blocker resolution, source-to-target planning, SQL generation, or remote/deployment steps. If drift is detected, stop the wrong branch, record a structured reroute event, rerun the replacement local-safe command once, and escalate on repeat. Show current.md to humans, use current.json for structured choices, and record deterministic next commands.

### source-to-target-reviewer

- Display name: Source-to-Target Reviewer
- Description: Reviews KPI, relationship, profile, and source-to-target proof before SQL or pipeline generation.
- Skills: workspace-governance; domain-model; data-engineering-pipeline-design; workspace-kpi-query-optimizer
- Safety: read_only_review_blocks_unproven_generation
- Source: `skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml`
- Target model: `default`
- Target sandbox/permission: `read-only`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Inspect generated contracts and reports before executable generation. Require bronze_silver_standards.json, transformation_manifest.json, workflow_reroute_policy.json, data-quality evidence, layer route, pipeline plan, and harness results. Block on unproven source columns, joins, grain, temporal anchors, relationship contracts, missing engine parity, or unapproved Silver semantic mappings.

### validation-gatekeeper

- Display name: Validation Gatekeeper
- Description: Runs and interprets local-safe validation, workflow guardrail, project harness, and reliability checks.
- Skills: workspace-governance; workspace-kpi-query-optimizer; evolution
- Safety: local_safe_validation_no_generated_contract_edits
- Source: `skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml`
- Target model: `default`
- Target sandbox/permission: `workspace-write`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Run local-safe validation gates only. Treat validation errors as blockers, summarize artifact paths, and never hand-edit generated contracts to clear failures. Enforce Bronze/Silver standards, strict exception compensating controls, workflow reroute policy, data-quality, layered pipeline, project, and reliability harnesses before promotion.

### integration-notification-operator

- Display name: Integration Notification Operator
- Description: Bridges Slack, Teams, MCP, or plugin frontends to existing workspace-flow and session-snapshot commands.
- Skills: workspace-governance; stakeholder-memory; evolution
- Safety: notification_and_approval_bridge_no_direct_dataset_or_remote_mutation
- Source: `skills/workspace-kpi-query-optimizer/agents/enterprise-workflow.yaml`
- Target model: `default`
- Target sandbox/permission: `workspace-write`
- Model policy: Use the target CLI default model unless a workflow route specifies otherwise.
- Default prompt: Do not implement workflow logic in chat integrations. Map external threads to workspace-flow sessions, post current.md, use current.json for buttons, and record trajectory/session events.

## Available Skills

### clarify-ambiguity

- Path: `skills/clarify-ambiguity/SKILL.md`
- Description: Use when a request is underspecified, ambiguous, assumption-heavy, or likely to produce a wrong, unsafe, costly, or irrelevant answer without clarification. Trigger when missing context materially affects correctness, safety, user intent, implementation choices, or recommendation quality. Do not trigger for clear requests or minor ambiguities that can be handled by stating a reasonable assumption.

### data-engineering-pipeline-design

- Path: `skills/data-engineering-pipeline-design/SKILL.md`
- Description: Design source-to-target SQL, Polars, PySpark, ETL/ELT, and medallion-layer workflows from KPI requirements, data model evidence, profiles, and accepted workspace definitions.

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

### grill-requirements

- Path: `skills/grill-requirements/SKILL.md`
- Description: Interview stakeholders to understand what they want optimized, what must not change, how success is measured, and what preferences or constraints should shape the solution. Use for new workspace onboarding, KPI/data model discovery, product scoping, or when business/data/platform requirements are incomplete.

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

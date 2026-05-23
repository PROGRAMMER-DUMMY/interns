# Gemini Skill Adapter

This file is generated from canonical repo skills. Do not hand-edit it.

## Routing Rules

- Treat `skills/*/SKILL.md` as the source of truth.
- If the user explicitly names `$skill-name` or `skill-name`, load that skill.
- Otherwise match the request to skill descriptions and load the smallest relevant skill set.
- If multiple skills match, order them by dependency and keep context minimal.
- If local file access is available, open the listed `SKILL.md` before applying a skill.
- If local file access is unavailable, use embedded bodies only when this adapter was generated with full embedding.

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
- `session-snapshot`
  - Command: `uv run session-snapshot <start|append|command|file-change|decision|verify|finish>`
  - Use when: operator wants exact end-user conversation transcript; cross-CLI session monitoring; audit conversation turns, commands, file changes, decisions, and intent verification
  - Outputs: .agents/sessions/<session>/compact.md; .agents/sessions/<session>/intent_verification.md; .agents/sessions/<session>/intent_verification.json; .agents/sessions/<session>/events.jsonl; .agents/sessions/<session>/transcript.md; .agents/sessions/<session>/commands.md; .agents/sessions/<session>/file_changes.md; .agents/sessions/<session>/decisions.md; .agents/sessions/<session>/snapshot.json
  - Safety: local_safe_redacts_common_secret_patterns_gitignored
- `onboard-workspace`
  - Command: `uv run onboard-workspace --workspace <workspace>`
  - Use when: workspace artifacts are missing; profiles/contracts/evaluation scaffolding need refresh
  - Outputs: interns/generated/profiles; interns/generated/contracts; interns/reports
  - Safety: local_safe
- `kickstart-workspace`
  - Command: `uv run kickstart-workspace --workspace <workspace> --domain <domain>`
  - Use when: new governed workspace; enterprise discovery and task config need refresh
  - Outputs: config/tasks.json; interns/generated/requirements; interns/generated/contracts
  - Safety: local_safe_config_write
- `prepare-kpi-generation`
  - Command: `uv run prepare-kpi-generation --workspace <workspace>`
  - Use when: workspace confirmation completed; user may create, revise, challenge, or score KPIs; show two-path KPI generation versus usual workflow prompt
  - Outputs: interns/generated/requirements/kpi_generation_session.json; interns/reports/kpi_generation/current.json; interns/reports/kpi_generation/current.md; KPI quality/readiness score
  - Safety: local_safe
- `apply-kpi-generation-answer`
  - Command: `uv run apply-kpi-generation-answer --workspace <workspace> --answer <option_id_or_label>`
  - Use when: user answered the current KPI generation panel; KPI generation interview needs to advance; draft KPI registry and evidence proof should be produced
  - Outputs: updated kpi_generation_session.json; next kpi_generation/current.json; interns/generated/requirements/kpi_registry_draft.json
  - Safety: local_safe_validated_write
- `finalize-kpi-generation`
  - Command: `uv run finalize-kpi-generation --workspace <workspace> --approve-final-preview`
  - Use when: final KPI draft preview was shown and explicitly approved; write user-facing KPI registry after KPI generation
  - Outputs: workspace docs KPI registry JSON; interns/generated/requirements/kpi_generation_production_proof.json; workspace and team memory
  - Safety: requires_explicit_final_preview_approval
- `prepare-workspace-workflow`
  - Command: `uv run prepare-workspace-workflow --workspace <workspace> --mode <plan|local-safe|autopilot> --domain <domain>`
  - Use when: workspace confirmation completed; user wants one checkpoint for KPI, data-model, blocker, validation, and presentation workflow; agent needs deterministic next commands and autopilot boundaries
  - Outputs: interns/reports/workflow/current.json; interns/reports/workflow/current.md; local-safe generated artifacts when mode is local-safe or autopilot
  - Safety: local_safe_checkpoint_autopilot_stops_before_final_delete_remote_codegen
- `workspace-flow`
  - Command: `uv run workspace-flow <start|status|answer|results>`
  - Use when: agent-led workspace workflow should run quietly in the backend; user asks for KPI generation by interview; user asks to generate SQL and show KPI results; main chat should show only compact questions or results
  - Outputs: interns/state/workflow_sessions/<session-id>/session.json; interns/state/workflow_sessions/<session-id>/current.json; interns/state/workflow_sessions/<session-id>/current.md; interns/reports/kpi_results/current.md; interns/generated/evidence/kpi_results/current.json
  - Safety: local_safe_session_orchestrator_hides_lower_level_command_noise
- `prepare-wiki-memory`
  - Command: `uv run prepare-wiki-memory --workspace <workspace> --domain <domain>`
  - Use when: repeated KPI terms or data-model decisions should be found; shared team wiki memory should suggest safe reuse; autopilot needs governed draft prefill candidates
  - Outputs: state/team_memory/wiki_memory_index.json; interns/generated/memory/wiki_memory_candidates.json; interns/reports/wiki_memory/current.json; interns/reports/wiki_memory/current.md
  - Safety: local_safe_structured_artifacts_only_draft_prefill_blocks_execution
- `prepare-agent-benchmark`
  - Command: `uv run prepare-agent-benchmark --workspace <workspace> --domain <domain>`
  - Use when: workspace needs a readiness proof; release gates should be checked before SQL, ETL, medallion, autopilot, or production promotion; owners need an artifact-backed benchmark scorecard
  - Outputs: interns/generated/contracts/agent_benchmark_scorecard.json; interns/generated/contracts/release_gate_status.json; interns/reports/benchmarks/current.json; interns/reports/benchmarks/current.md
  - Safety: local_safe_project_native_scorecard_no_external_benchmark_execution
- `validate-project-harness`
  - Command: `uv run validate-project-harness --workspace <workspace> --domain <domain>`
  - Use when: workspace needs one top-level score before completion is claimed; all local-safe harnesses should run together; release readiness needs artifact validation, workflow guardrails, trajectory health, KPI execution, benchmark, and git hygiene proof
  - Outputs: interns/generated/evidence/project_harness.json; interns/reports/project_harness.md; score, blockers, warnings, and next commands
  - Safety: local_safe_project_harness_no_remote_execution
- `run-ai-app-harness`
  - Command: `uv run run-ai-app-harness --workspace <workspace> --dataset <workspace>/interns/ai_harness/datasets/<suite>.jsonl`
  - Use when: workspace needs dependency-free AI app tests; JSONL prompt cases should be evaluated with exact/schema/keyword or KPI/SQL-specific checks; KPI mapping, SQL semantic, result-table, or adversarial AI behavior should be regression tested; local stub or explicitly approved raw HTTP AI boundary should be tested; baseline per-case and result-signature regressions should block CI
  - Outputs: interns/ai_harness/runs/<run_id>/outputs.jsonl; interns/ai_harness/runs/<run_id>/report.json; interns/reports/ai_app_harness/current.json; interns/reports/ai_app_harness/current.md; interns/generated/evidence/ai_app_harness/current.json
  - Safety: local_safe_by_default_remote_ai_requires_allow_flag
- `run-ai-cli-harness`
  - Command: `uv run run-ai-cli-harness --workspace <workspace> --dataset <workspace>/interns/ai_cli_harness/datasets/<suite>.jsonl`
  - Use when: CLI agents such as Claude, Gemini, Codex, or custom tools need governed workflow regression tests; command transcripts, project-tool usage, artifact outputs, JSON fields, and workflow guardrails should be evaluated; real CLI subprocess execution should remain blocked unless explicitly approved
  - Outputs: interns/ai_cli_harness/runs/<run_id>/outputs.jsonl; interns/ai_cli_harness/runs/<run_id>/report.json; interns/reports/ai_cli_harness/current.json; interns/reports/ai_cli_harness/current.md; interns/generated/evidence/ai_cli_harness/current.json
  - Safety: local_safe_stub_by_default_real_cli_requires_allow_flag
- `validate-workflow-guardrails`
  - Command: `uv run validate-workflow-guardrails --workspace <workspace>`
  - Use when: workflow itself needs a reliability gate; blocker panels may contain invented or non-source-backed features; failed shell commands or raw-data reads should be audited; non-portable commands should be caught before retrying
  - Outputs: interns/reports/workflow_guard_harness/current.json; interns/reports/workflow_guard_harness/current.md; interns/generated/evidence/workflow_guard_harness/current.json
  - Safety: local_safe_read_only_validation_report
- `record-workspace-trajectory`
  - Command: `uv run record-workspace-trajectory --workspace <workspace> --event-type <type> --status <status> --summary <summary>`
  - Use when: agent or CLI workflow steps should be replayable; commands, validations, decisions, retries, and artifacts need workspace-scoped audit events; workflow guardrails should validate the step-by-step trajectory
  - Outputs: interns/state/trajectory.jsonl; interns/reports/trajectory/current.json; interns/reports/trajectory/current.md; interns/generated/evidence/trajectory/current.json
  - Safety: local_safe_append_only_secret_redacted_workspace_log
- `build-workspace-evidence-graph`
  - Command: `uv run build-workspace-evidence-graph --workspace <workspace>`
  - Use when: KPI, mapping, SQL, trajectory, and harness artifacts need one traceability graph; agent needs impact analysis before changing a feature mapping; reviewer asks where a term, column, blocker answer, or SQL dependency came from
  - Outputs: interns/generated/evidence_graph/graph.json; interns/reports/evidence_graph/current.md
  - Safety: local_safe_existing_artifacts_only_no_raw_data_reads
- `kpi-proof-packet`
  - Command: `uv run kpi-proof-packet --workspace <workspace> --domain <domain>`
  - Use when: stakeholders need one all-KPI recommendation packet; operators need source row traceability, mapping recommendations, reliability gates, generated SQL, execution output previews, and sample values in one report; bulk KPI mapping review should happen before apply-safe or execution modes
  - Outputs: interns/reports/kpi_proof_packet/current.md; interns/reports/kpi_proof_packet/current.json; interns/generated/evidence/kpi_proof_packet/current.json
  - Safety: local_safe_read_only_recommend_mode
- `discover-external-sources`
  - Command: `uv run discover-external-sources --workspace <workspace> --external-root <external_root>`
  - Use when: user points to a large external folder; external source root needs dataset/doc/log/delta/database classification; draft source selection from cold storage without making it the workspace
  - Outputs: interns/generated/requirements/external_source_discovery.json; interns/reports/external_source_discovery.md; docs/source_selection.generated.json
  - Safety: local_safe_metadata_path_only_review_gated
- `prepare-external-source-intake`
  - Command: `uv run prepare-external-source-intake --external-root <external_root> --proposed-workspace <workspace>`
  - Use when: user provides an external path and has not chosen existing versus new workspace; external-source route preference should be reused or challenged; agent needs a deterministic panel instead of asking freehand
  - Outputs: interns/generated/requirements/external_source_intake_session.json; interns/reports/external_source_intake/current.json; interns/reports/external_source_intake/current.md
  - Safety: local_safe_panel_write_no_source_reads
- `apply-external-source-intake`
  - Command: `uv run apply-external-source-intake --external-root <external_root> --proposed-workspace <workspace> --answer <option>`
  - Use when: user answered the external source intake panel; metadata-only discovery should run after route selection; saved external-source defaults or change reasons should be recorded
  - Outputs: external_source_intake_session.json; external_source_discovery.json; external_source_discovery.md; docs/source_selection.generated.json; state/team_memory/external_source_intake_preferences.json
  - Safety: local_safe_metadata_only_until_review_gate
- `prepare-data-model-generation`
  - Command: `uv run prepare-data-model-generation --workspace <workspace>`
  - Use when: data model docs are missing, weak, or image-only; workspace needs governed data model creation or parsing; relationship proof should be reviewed before executable SQL
  - Outputs: interns/reports/data_model_generation/current.json; interns/reports/data_model_generation/current.md
  - Safety: local_safe
- `apply-data-model-answer`
  - Command: `uv run apply-data-model-answer --workspace <workspace> --answer <option_id_or_label>`
  - Use when: user answered the current data-model generation panel; draft data-model artifacts should be produced under interns
  - Outputs: interns/generated/requirements/data_model_draft.json; interns/reports/data_model_generation
  - Safety: local_safe_validated_write
- `finalize-data-model-generation`
  - Command: `uv run finalize-data-model-generation --workspace <workspace> --approve-final-preview`
  - Use when: draft data model preview was shown and explicitly approved; write user-facing data model docs and finalized model contract
  - Outputs: docs/data-model.md; docs/erd.md; docs/relationships.md; interns/generated/contracts/data_model_contract.json
  - Safety: requires_explicit_final_preview_approval
- `prepare-data-model-blocker-panel`
  - Command: `uv run prepare-data-model-blocker-panel --workspace <workspace>`
  - Use when: data model draft exists; agent needs the next JSON-backed data-model blocker question; grain, primary key, relationship, temporal anchor, or SCD decisions need deterministic resolution
  - Outputs: interns/reports/data_model_blocker_panel/current.json; interns/reports/data_model_blocker_panel/current.md; updated data_model_generation_session.json
  - Safety: local_safe
- `apply-data-model-blocker-answer`
  - Command: `uv run apply-data-model-blocker-answer --workspace <workspace> --answer <option_id_or_label>`
  - Use when: user answered the current data-model blocker panel; accepted option should apply a structured data-model operation; next blocker panel should be generated
  - Outputs: updated interns/generated/requirements/data_model_draft.json; next interns/reports/data_model_blocker_panel/current.json; next interns/reports/data_model_blocker_panel/current.md
  - Safety: local_safe_validated_write
- `export-data-model-diagram`
  - Command: `uv run export-data-model-diagram --workspace <workspace>`
  - Use when: stakeholders need a presentable data model diagram; finalized or draft data model should be rendered as static SVG; Mermaid ERD should be exported for review
  - Outputs: interns/reports/presentation/data-model.svg; interns/reports/presentation/data-model.mermaid.md; interns/reports/presentation/presentation_manifest.json
  - Safety: local_safe_presentation_export
- `export-kpi-registry-excel`
  - Command: `uv run export-kpi-registry-excel --workspace <workspace>`
  - Use when: stakeholders need a KPI Excel workbook; draft or finalized KPI registry should be exported for review; KPI blockers, evidence, and proof should be visible in spreadsheet form
  - Outputs: interns/reports/presentation/kpi_registry.xlsx; interns/reports/presentation/presentation_manifest.json
  - Safety: local_safe_presentation_export
- `export-workspace-presentation`
  - Command: `uv run export-workspace-presentation --workspace <workspace>`
  - Use when: workspace needs a stakeholder-ready presentation bundle; data model diagram and KPI Excel should be generated together
  - Outputs: interns/reports/presentation/data-model.svg; interns/reports/presentation/data-model.mermaid.md; interns/reports/presentation/kpi_registry.xlsx; interns/reports/presentation/presentation_manifest.json
  - Safety: local_safe_presentation_export
- `resolve-kpi-features`
  - Command: `uv run resolve-kpi-features --workspace <workspace> --domain <domain> --include-candidates`
  - Use when: KPI features need mapping; blockers need clustering; accepted definitions need applying
  - Outputs: interns/generated/contracts/kpi_feature_mapping.json; strict derived_feature_options with formula/input/observed_values/value_profile/semantic_meaning_sources/reason/example/evidence_sources/derivation_reasoning/evidence_state/confidence; semantically mismatched candidates rejected; interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; interns/reports/open_questions.md
  - Safety: local_safe
- `apply-workspace-definition`
  - Command: `uv run resolve-kpi-features --workspace <workspace> --apply-workspace-definition --feature <feature> --definition <definition> --evidence-note <note>`
  - Use when: one accepted feature definition applies across multiple KPIs
  - Outputs: interns/generated/contracts/workspace_feature_definitions.json; interns/generated/contracts/kpi_feature_mapping.json
  - Safety: local_safe
- `prepare-kpi-blocker-panel`
  - Command: `uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>`
  - Use when: agent needs the next validated KPI blocker question; avoid hand-chaining onboarding, resolver, markdown, panel, and validation commands; fresh or existing KPI workspace needs deterministic blocker preparation
  - Outputs: interns/generated/contracts/kpi_feature_mapping.json; interns/reports/derived_feature_reviews; interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; validation summary
  - Safety: local_safe
- `apply-kpi-panel-answer`
  - Command: `uv run apply-kpi-panel-answer --workspace <workspace> --domain <domain> --answer <option_id_or_label>`
  - Use when: user answered the current blocker question; accepted option should be applied without inventing unsupported resolver flags; friendly answer must be resolved against current.json
  - Outputs: interns/generated/contracts/workspace_feature_definitions.json; updated kpi_feature_mapping.json; next validated blocker_question_panel/current.json
  - Safety: local_safe_validated_write
- `generate-kpi-sql`
  - Command: `uv run generate-kpi-sql --workspace <workspace> --kpi-id <kpi_id>`
  - Use when: KPI features are proven or user-confirmed
  - Outputs: interns/generated/solutions
  - Safety: local_safe
- `plan-source-to-target`
  - Command: `uv run plan-source-to-target --workspace <workspace> --target-engine <sql|polars|pyspark|hybrid>`
  - Use when: SQL, Polars, PySpark, ETL, or medallion implementation needs a data-model-backed source-to-target plan; agent must verify source datasets, joins, grain, temporal anchors, and target layer before code generation
  - Outputs: interns/generated/contracts/source_to_target_plan.json; interns/reports/source_to_target_plan.md; interns/generated/context/manifests/plan-source-to-target_<budget>.json
  - Safety: local_safe
- `context-router`
  - Command: `uv run context-router build --workspace <workspace> --task <task> --budget <small|standard|deep>`
  - Use when: task needs bounded context instead of whole artifact loading; agent needs context wiki/page index; large workspace artifacts must be routed by budget
  - Outputs: interns/generated/context/context_index.json; interns/generated/context/context_pages.jsonl; interns/generated/context/manifests/<task>_<budget>.json; interns/reports/context/<task>_<budget>.md
  - Safety: local_safe_derived_context_only
- `build-relationship-contracts`
  - Command: `uv run build-relationship-contracts --workspace <workspace>`
  - Use when: multi-dataset executable generation needs FK/relationship proof; source-to-target joins need production-grade relationship contracts; profile-only joins need approval gating
  - Outputs: interns/generated/contracts/relationship_contracts.json; interns/reports/relationship_contracts.md
  - Safety: local_safe_governed_contract_write
- `derived-feature-markdown`
  - Command: `uv run derived-feature-markdown --workspace <workspace>`
  - Use when: stakeholders need readable derived-feature blocker reviews; strict derived_feature_options JSON needs Markdown rendering
  - Outputs: interns/reports/derived_feature_reviews/md/<kpi_id>_<feature>.md; interns/reports/derived_feature_reviews/json/<kpi_id>_<feature>.json; interns/reports/derived_feature_reviews/index.md
  - Safety: local_safe
- `blocker-question-panel`
  - Command: `uv run blocker-question-panel --workspace <workspace>`
  - Use when: agent needs to ask any KPI blocker question; direct mapping or source-of-truth choice needs stakeholder answer; non-technical panel is preferred over terminal option UI
  - Outputs: interns/reports/blocker_question_panel/current.json; interns/reports/blocker_question_panel/current.md; interns/reports/blocker_question_panel/index.json
  - Safety: local_safe
- `validate-workspace-artifacts`
  - Command: `uv run validate-workspace-artifacts --workspace <workspace>`
  - Use when: generated workspace artifacts need schema checks; agent is about to rely on KPI registry, feature mapping, derived reviews, or blocker panel; detect manual edits to generated contracts
  - Outputs: validation summary JSON with checked_files, errors, and warnings
  - Safety: local_safe_read_only
- `prepare-workspace-bug-report`
  - Command: `uv run prepare-workspace-bug-report --workspace <workspace>`
  - Use when: workspace selection and onboarding evidence disagree; fresh workspace flow generated empty artifacts; blocking product bugs need a JSON and Markdown report
  - Outputs: interns/generated/evidence/bug_report.json; interns/reports/bugs/current.md
  - Safety: local_safe_governed_report_write
- `cleanup-workspace-references`
  - Command: `uv run cleanup-workspace-references --workspace <workspace> --all-references`
  - Use when: fresh workspace restart is requested; stale generated references need removal
  - Outputs: dry-run cleanup plan; optional deletion of interns and repo runtime references with --apply --confirm-delete <workspace>
  - Safety: hard_permission_block_for_any_delete
- `profiler`
  - Command: `uv run python tools/profiler.py --input <path> --pct <pct> --engine auto --out <dir>`
  - Use when: profile artifacts are missing; bounded sample or representation evidence is required
  - Outputs: profile CSVs; sample outputs; representation reports
  - Safety: local_or_configured_data_access
- `optimizer-finder`
  - Command: `uv run python tools/optimizer_finder.py --target <path> --mode auto`
  - Use when: SQL/Python artifact is slow; hotspot evidence is needed
  - Outputs: state/hotspots.json
  - Safety: local_safe
- `methodology-parser`
  - Command: `uv run python tools/methodology_parser.py --doc <path> --out <schema.json>`
  - Use when: methodology, dictionary, or contract document needs semantic schema extraction
  - Outputs: semantic schema JSON
  - Safety: may_use_llm_intern
- `databricks-setup`
  - Command: `uv run python tools/databricks_setup.py`
  - Use when: user explicitly asks to validate Databricks configuration
  - Outputs: connection and capability report
  - Safety: remote_sensitive_explicit_approval
- `generate-skill-adapters`
  - Command: `uv run generate-skill-adapters`
  - Use when: skills or adapter routing changed
  - Outputs: .agents/skills_index.json; .agents/<tool>/SKILLS.md
  - Safety: local_safe

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

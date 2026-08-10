# Core KPI Onboarding System (`core/onboarding/kpi`)

## Executive Overview

The `core/onboarding/kpi` module is the platform's core engine for taking raw client inputs (Excel workbooks, CSV files, raw SQL dumps, natural language requirement documents), structuring them into formal KPI definitions, resolving business terms against physical database schemas, generating multi-engine analytical code (DuckDB SQL, Polars, PySpark, dbt projects), and executing self-grill verification with privacy redaction and auditability proof packets.

The system is designed around five primary architectural sub-pipelines:

1. **Intake & Format Detection**: Detects layout orientation, merged cell spans, and column roles in Excel/CSV files (`kpi_format_detector.py`, `workbook_structure.py`, `registry_loader.py`, `text_parser.py`), converting raw inputs into structured `KPI` domain models (`kpi_definition.py`, `kpi_intent.py`).
2. **Semantic Resolution & Interactive Blocker Panels**: Maps requested metrics, cuts, and filters against physical column profiles and domain dictionaries (`feature_resolver.py`, `metric_derivation.py`). Unresolved ambiguities trigger deterministic, score-ranked interactive blocker question panels (`blocker_workflow.py`, `blocker_question_panel.py`, `data_quality_panel.py`, `phi_review_panel.py`, `kpi_confirmation_panel.py`, `cli_agent_confirm_cli.py`) with cached preview execution (`panel_preview_executor.py`, `panel_preview_cache.py`).
3. **Requirements Discovery & Interactive Interview Workflow**: Provides an end-to-end interactive interviewing loop (`generation_workflow.py`, `generation_cli.py`) that evaluates KPI draft quality across clarity and measurability (`generation_quality.py`).
4. **Multi-Engine Code & Project Generation**: Translates resolved KPI intents into production DuckDB SQL CTEs (`sql_generator.py`, `result_view_builder.py`), Polars DataFrames (`polars_generator.py`), PySpark scripts (`pyspark_generator.py`), or complete governed dbt projects (`dbt_project_generator.py`). Directs execution strategy using complexity signals (`engine_recommender.py`, `generate_kpi_engines.py`, `parallel_completion.py`, `local_warehouse.py`, `intent_contract.py`, `intent_coverage.py`).
5. **Execution, Parity, Proof & Privacy Redaction**: Executes generated code against DuckDB or remote warehouses (`execution_harness.py`), verifies cross-engine parity (`engine_parity.py`), audits execution sanity and non-emptiness (`verify_kpi_output.py`), redacts PII/PHI (`pii_redaction.py`, `sensitive_masking.py`), and compiles complete auditability proof packets (`proof_packet.py`).

## ASCII Architectural Model

```text
                     ┌─────────────────────────────────────────────────────────┐
                     │   Raw Customer Input (Excel / CSV / JSON / SQL / Prose) │
                     └────────────────────────────┬────────────────────────────┘
                                                  │
                                                  ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Stage 1: INTAKE & FORMAT DETECTION                                                                     │
│   workbook_structure.py ──► kpi_format_detector.py ──► registry_loader.py ──► text_parser.py        │
│   ──► kpi_definition.py & kpi_intent.py (Structured KPI Intent & Metadata)                             │
└─────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Stage 2: FEATURE RESOLUTION & BLOCKER PANELS                                                           │
│   feature_resolver.py ──► metric_derivation.py ──► intent_contract.py ──► intent_coverage.py           │
│                                         │                                                              │
│                            [ Are Features Blocked? ]                                                   │
│                                   │          │                                                         │
│                                  YES         NO                                                        │
│                                   │          │                                                         │
│                                   ▼          │                                                         │
│   ┌──────────────────────────────────────┐   │                                                         │
│   │ INTERACTIVE BLOCKER PANELS           │   │                                                         │
│   │  blocker_workflow.py                 │   │                                                         │
│   │  blocker_question_panel.py           │   │                                                         │
│   │  data_quality_panel.py               │   │                                                         │
│   │  phi_review_panel.py                 │   │                                                         │
│   │  cli_agent_confirm_cli.py            │   │                                                         │
│   │  (Preview: panel_preview_executor)   │   │                                                         │
│   └──────────────────┬───────────────────┘   │                                                         │
│                      │                       │                                                         │
│           (Answers Applied via CLI)          │                                                         │
│                      │                       │                                                         │
│                      └───────────────────────┼─────────────────────────────────────────────────────────┘
                                               │
                                               ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Stage 3: REQUIREMENTS GENERATION & QUALITY SCORING                                                     │
│   generation_workflow.py ◄──► generation_quality.py ◄──► generation_cli.py                           │
└─────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Stage 4: MULTI-ENGINE CODE & PROJECT GENERATION                                                        │
│   engine_recommender.py ──► parallel_completion.py ──► local_warehouse.py                             │
│       ├── sql_generator.py & result_view_builder.py  ──► DuckDB SQL                                   │
│       ├── polars_generator.py                       ──► Polars Python                                 │
│       ├── pyspark_generator.py                      ──► PySpark Python                                │
│       └── dbt_project_generator.py                  ──► dbt Staging/Gold Project                      │
└─────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Stage 5: EXECUTION, PARITY, VERIFICATION & AUDIT PROOF                                                 │
│   execution_harness.py ──► engine_parity.py ──► verify_kpi_output.py                                  │
│   ──► pii_redaction.py & sensitive_masking.py ──► proof_packet.py                                      │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Exhaustive File Documentation (All 38 Files)

### [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/__init__.py#L1-L2)

**Exact Purpose**: Package initialization file defining the core onboarding KPI module namespace.

**Key Functions & Classes**:

- *No top-level functions or classes defined (module init or configuration).*

**Inputs & Outputs**:
- **Inputs**: Package imports.
- **Outputs**: Module exports (currently empty package root).

**Failure Modes & Edge Cases**:
- Import errors if package structure is invalid.

---
### [`blocker_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_cli.py#L1-L247)

**Exact Purpose**: CLI interface for driving KPI blocker resolution workflows, rendering blocker question panels, and recording human answers.

**Key Functions & Classes**:

- Function [`_resolve_workspace_path(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_cli.py#L20-L21)
- Function [`_current_panel_question_id(workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_cli.py#L24-L39) - *The panel's `question_id` (or `feature`) at call time -- must be part of*
- Function [`prepare_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_cli.py#L44-L106)
- Function [`apply_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_cli.py#L110-L247)

**Inputs & Outputs**:
- **Inputs**: `argv` CLI arguments specifying `--workspace`, `--domain`, `--interactive`, `--answer`, `--question-id`.
- **Outputs**: JSON/Markdown blocker panels printed to console and saved under `interns/reports/blocker_question_panel/`.

**Failure Modes & Edge Cases**:
- Fails with exit code 1 if workspace path is invalid, feature resolution panel cannot be constructed, or invalid answer option is provided.

---
### [`blocker_question_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1-L3140)

**Exact Purpose**: Core panel generation engine that detects ambiguous or unmapped KPI terms/cuts/measures, scores candidate resolution options, formats markdown/JSON panels, and applies chosen answers.

**Key Functions & Classes**:

- Class [`BlockerQuestionPanelResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L60-L70)
  - Method [`BlockerQuestionPanelResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L69-L70)
- Class [`BlockerQuestionPanelBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L73-L234)
  - Method [`BlockerQuestionPanelBuilder.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L74-L104)
  - Method [`BlockerQuestionPanelBuilder.run(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L106-L234)
- Function [`_deferred_kpi_ids_from_registry(workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L237-L267) - *Derive the set of DEFERRED (undefined) KPI ids from the KPI registry.*
- Function [`_build_questions(mapping, workspace, repo_root, deferred_kpi_ids)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L270-L297)
- Function [`_feature_items(mapping, deferred_kpi_ids)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L300-L322)
- Function [`_clusters_from_features(items)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L325-L338)
- Function [`_question_for_cluster(mapping, workspace, repo_root, cluster, items)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L341-L737)
- Function [`_dictionary_conflict_question(base, feature, applies_to, conflict_items)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L740-L851) - *Panel question for a feature whose dictionary claim contradicts profiles.*
- Function [`_prior_wiki_decision(workspace, repo_root, feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L854-L865)
- Function [`_kpi_source_truth(items, workspace, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L868-L904)
- Function [`_kpi_understanding_packet(items, source_truth, feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L907-L949)
- Function [`_is_semantic_blocker(feature, metric, cuts, question, kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L952-L988)
- Function [`_understanding_text(question, metric, cuts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L991-L997)
- Function [`_strict_proven_sql(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1000-L1018)
- Function [`_intent_sql_sketch(kpi, feature, metric, cuts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1021-L1049)
- Function [`_first_kpi_table(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1052-L1058)
- Function [`_placeholder_for_cut(cut)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1061-L1064)
- Function [`_kpi_demo_table(kpi_id, metric, cuts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1067-L1077)
- Function [`_load_json(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1080-L1087)
- Function [`_split_cuts(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1090-L1091)
- Function [`_source_label(path, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1094-L1098)
- Function [`_sql_table_label(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1101-L1102)
- Function [`_sql_column_label(path, column)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1105-L1106)
- Function [`_excel_cell_trace(source_path, source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1109-L1147)
- Function [`_valid_derived_options(items)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1150-L1168)
- Function [`_physical_column_options(items)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1171-L1218)
- Function [`_profile_candidate_options(items, workspace, repo_root, feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1221-L1307)
- Function [`_profile_candidate_score(feature, dataset, column, items)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1310-L1360)
- Function [`_sample_values(raw)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1363-L1371)
- Function [`_physical_option_payload(option, idx, source_truth)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1374-L1441)
- Function [`_physical_option_proof(option, source_truth)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1444-L1483)
- Function [`_answer_demo(kpi, feature, selected_column)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1486-L1546)
- Function [`_first_matching_source_column(kpi, dataset, terms)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1549-L1562)
- Function [`_demo_output_rows(selected_column, cost_column, feature_label, metric_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1565-L1583)
- Function [`_source_column_sample(column)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1586-L1596)
- Function [`_markdown_table(rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1599-L1610)
- Function [`_table_cell(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1613-L1616)
- Function [`_derived_option_payload(option, idx, source_truth)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1619-L1640)
- Function [`_derived_option_proof(option, source_truth)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1643-L1676)
- Function [`_derived_sql_query(option)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1679-L1690)
- Function [`_derived_demo_rows(option)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1693-L1708)
- Function [`_custom_rule_option(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1711-L1724)
- Function [`_cli_agent_evidence_pack(feature, items, workspace, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1733-L1901) - *Bounded evidence pack the orchestrating CLI agent reads to propose a mapping.*
- Function [`_cli_agent_task_text(feature, applies_to, evidence_pack, workspace, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1904-L1959)
- Function [`_cli_agent_proposal_option(feature, evidence_pack)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1962-L1988)
- Function [`_evidence_files(items, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L1991-L2031)
- Function [`_blocked_kpi_details(mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2034-L2075) - *Per-KPI definition asks for the blocked-without-feature-question card.*
- Function [`_empty_panel(mapping, workspace, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2078-L2137)
- Function [`_render_all_blockers_overview(questions, workspace_rel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2140-L2175) - *The full open-decision surface: a summary table of every blocker plus each*
- Function [`_render_markdown_compact(panel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2178-L2270) - *Compact, decision-first blocker card written to ``current.md``.*
- Function [`_compact_option_reason(option)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2273-L2286) - *One short reason line for an option, without the proof-packet dump.*
- Function [`_compact_option_samples(option, limit)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2289-L2302) - *Up to ``limit`` sample values for an option, from whichever evidence*
- Function [`_render_markdown(panel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2305-L2607)
- Function [`_render_feature_resolution_table(rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2610-L2626)
- Function [`_render_sample_evidence(rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2629-L2651)
- Function [`_render_kpi_preview(preview)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2654-L2706)
- Function [`_render_executed_sample(preview)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2709-L2728)
- Function [`_render_option_proof(proof)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2731-L2782)
- Function [`_norm(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2785-L2786)
- Function [`_slug(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2789-L2790)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2793-L2797)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2802-L2851)
- Function [`_where_it_lands(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2884-L2922) - *One-line description of where a feature maps to in the schema.*
- Function [`_build_feature_resolution_table(mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2925-L2949) - *Build the 3-column feature-resolution table.*
- Function [`_workspace_redaction_patterns(workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2952-L2961) - *Default PII patterns extended with the workspace's user data policy.*
- Function [`_build_sample_evidence(mapping, workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L2964-L3006) - *Build the sample-evidence mini-table.*
- Function [`_executable_sql_for_option(option, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L3009-L3064) - *Return (sql, dataset_paths) for previewing an option.*
- Function [`_execute_option_preview(option, workspace_path, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L3067-L3105) - *Run an option's preview SQL (cache-first) and return a PreviewResult dict.*
- Function [`_attach_preview_sections(panel, mapping, workspace_path, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_question_panel.py#L3108-L3140) - *Mutate ``panel`` to add feature_resolution_table, sample_evidence, and*

**Inputs & Outputs**:
- **Inputs**: KPI registry, data model profiles, feature mappings, derived feature options, accepted definitions.
- **Outputs**: `BlockerQuestionPanelResult` containing structured questions, scored options, SQL previews, and `current.json`/`current.md` panel reports.

**Failure Modes & Edge Cases**:
- Raises `ValueError` or `RuntimeError` if KPI registry cannot be parsed, derived feature options contain invalid formulas, or option IDs collide.

---
### [`blocker_workflow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L1-L500)

**Exact Purpose**: High-level orchestration workflow for preparing, validating, and applying answers to KPI blocker panels across a workspace domain.

**Key Functions & Classes**:

- Class [`PreparedPanelResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L21-L43)
  - Method [`PreparedPanelResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L32-L43)
- Class [`ApplyPanelAnswerResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L47-L63)
  - Method [`ApplyPanelAnswerResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L55-L63)
- Function [`prepare_kpi_blocker_panel(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L66-L130)
- Function [`_is_stale_harness_freshness_error(message)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L133-L153) - *True for a validator error that only means "the execution harness*
- Function [`apply_kpi_panel_answer(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L156-L236)
- Function [`_write_feature_wiki_note(root, workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L239-L263)
- Function [`_apply_option(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L266-L383)
- Function [`_custom_definition_source_columns(definition_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L386-L420) - *(looks_like_formula, source_columns) for a human's --custom-definition*
- Function [`_resolve_answer(panel, answer)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L423-L460)
- Function [`_one_option(options, predicate, answer)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L463-L470)
- Function [`_onboarding_artifacts_missing(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L473-L477)
- Function [`_norm(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L480-L481)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L484-L488)
- Function [`prepare_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L491-L494)
- Function [`apply_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/blocker_workflow.py#L497-L500)

**Inputs & Outputs**:
- **Inputs**: Workspace layout, domain name, answer IDs, answered-by attribution string.
- **Outputs**: `PreparedPanelResult` and `ApplyPanelAnswerResult` updating `kpi_feature_mapping.json` and `workspace_feature_definitions.json`.

**Failure Modes & Edge Cases**:
- Returns errored result status if missing workspace onboarding artifacts, unvalidated panel options, or schema mismatch.

---
### [`cli_agent_confirm_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/cli_agent_confirm_cli.py#L1-L233)

**Exact Purpose**: CLI interface for reviewing, confirming, or rejecting automated proposals generated by CLI agents for KPI blocker questions.

**Key Functions & Classes**:

- Class [`ConfirmResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/cli_agent_confirm_cli.py#L43-L54)
  - Method [`ConfirmResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/cli_agent_confirm_cli.py#L53-L54)
- Function [`_normalize_feature(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/cli_agent_confirm_cli.py#L57-L58)
- Function [`_find_proposed_entry(definitions, feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/cli_agent_confirm_cli.py#L61-L75)
- Function [`confirm_cli_agent_proposal(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/cli_agent_confirm_cli.py#L78-L185) - *Flip a previously-proposed mapping to ``user_confirmed`` or ``rejected``.*
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/cli_agent_confirm_cli.py#L190-L229)

**Inputs & Outputs**:
- **Inputs**: Workspace path, domain, `--decision` (`confirm` or `reject`), optional `--proposal-id`.
- **Outputs**: `ConfirmResult` flipping proposed status from `cli_agent_proposed` to `user_confirmed` or reverting to `cli_agent_rejected`.

**Failure Modes & Edge Cases**:
- Errors out if no pending CLI agent proposals exist or invalid decision string is provided.

---
### [`data_quality_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L1-L495)

**Exact Purpose**: Generates interactive panels and records decisions for handling missing, null, or malformed data values during KPI aggregation.

**Key Functions & Classes**:

- Class [`DataQualityPanelResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L50-L57)
  - Method [`DataQualityPanelResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L56-L57)
- Class [`DataQualityPanelBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L60-L150)
  - Method [`DataQualityPanelBuilder.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L61-L64)
  - Method [`DataQualityPanelBuilder.prepare(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L66-L101)
  - Method [`DataQualityPanelBuilder._candidates(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L103-L150)
- Class [`DataQualityAnswerRecorder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L153-L227)
  - Method [`DataQualityAnswerRecorder.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L154-L157)
  - Method [`DataQualityAnswerRecorder.apply(self, answer)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L159-L227)
- Function [`_profile_by_path(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L230-L241)
- Function [`_column_stats(profile, column)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L244-L248)
- Function [`_looks_categorical(dtype)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L266-L268)
- Function [`_ambiguity_signal(profile, column)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L271-L302) - *Evidence-based candidate signal for one column, or None if the*
- Function [`_candidate_key(candidate)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L305-L306)
- Function [`_decided_keys(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L309-L321)
- Function [`_build_panel(candidate)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L324-L414)
- Function [`_render_markdown(panel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L417-L441)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L444-L448)
- Function [`panel_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L452-L465)
- Function [`apply_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/data_quality_panel.py#L469-L491)

**Inputs & Outputs**:
- **Inputs**: Dataset profiles, null ratio thresholds, column metadata.
- **Outputs**: `DataQualityPanelResult` and written data quality policy decisions in workspace contracts.

**Failure Modes & Edge Cases**:
- Fails gracefully with degraded recommendations if dataset profiles are missing or null ratios are zero.

---
### [`dbt_project_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1-L1735)

**Exact Purpose**: Compiles fully-resolved KPI definitions and data models into a production-ready dbt project with staging models, gold KPI models, macros, schema YAMLs, and project configs. The emitted `profiles.yml` declares both a `dev` and a `prod` target (separate `<base>_dev`/`<base>_prod` catalogs derived through `core.provisioning.plan.env_catalog` — the one copy of that naming rule — shared credential `env_var()`s, `target: prod` default); `dbt_project.yml` pins `require-dbt-version` to the same range this repo installs dbt-core against.

**Catalog resolution (F21).** `resolve_catalog_and_base(layout, catalog="") -> (concrete, base)` is the single answer to "which catalog?". `workspace_settings.databricks_source.catalog` is the BASE an operator declared at intake (`rcm`); provisioning creates `env_catalog(base, env)` (`rcm_dev`). `provision_plan.json` records both and is the authority; a workspace that never provisioned keeps its declared value unchanged, and an explicit `--catalog` names that exact catalog and is its own base (the env suffix is never re-applied to an operator's literal). `self.catalog` is the CONCRETE catalog (`vars.catalog`, `DESCRIBE TABLE` under `--enforce-contracts`, and the internal `DuckDBKPISQLGenerator`'s qualified refs); `self.catalog_base` feeds only the profile targets. Nothing emitted INSIDE the project hardcodes a catalog: `sources.yml` and the `publish_gold` macro use `{{ target.database }}`, which dbt-databricks aliases from the profile's `catalog` key, so both follow whichever target the run selects.

**Emitted data quality.** Two sources feed one `models/staging/_data_quality.yml`; neither is hand-maintained.

1. Confirmed `data_quality_panel.py` answers (`data_quality_decisions.json`) -> `not_null` / `accepted_values`.
2. `_relationship_quality_decisions()` derives DQ-R2 (`silver_uniqueness_referential_null_type`, severity `fail`) from `relationship_contracts.json`: a contract records `referential_integrity_checks.orphan_left_key_check_required` and `uniqueness_checks.right_key_uniqueness_check_required` -- it ASKS for these two checks, and nothing used to generate them, so a mart could publish facts whose foreign keys resolve to nothing. An approved join now emits a `relationships` test on the fact key (`to: ref('<dimension staging model>')`) and a `unique` test on the dimension key, both at `severity: error`.

The trigger is the CONTRACT, not `blueprint_decisions.json` (per-relationship evidence a human approved, vs. workspace-wide policy), and the gate is `find_executable_relationship` -- the same predicate that decides a join may be EXECUTED decides whether it may be ASSERTED, so a referential test can never outrun its join's approval. That gate is also why the deriver needs no safety check of its own: a KPI needing an unapproved join fails generation outright, naming `build-relationship-contracts`.

**Store-failures.** `dbt_project.yml` emits a `data_tests:` block with `+store_failures: true`, `+store_failures_as: table`, and `+schema: dbt_test__audit`. A test that reports only a COUNT is not actionable on call -- whoever is paged has to re-derive the predicate to find the offending rows. `as: table` deliberately, not dbt's default view: a view re-evaluates its predicate at SELECT time, so the next run moves the data and the evidence is gone. The audit schema is created by dbt on first write, the same way the WAP `<gold>__staging` schema already is (provisioning's `DEFAULT_SCHEMAS` covers bronze/silver/gold only).

**Key Functions & Classes**:

- Class [`DbtProjectResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L130-L149)
  - Method [`DbtProjectResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L138-L149)
- Class [`DbtProjectGenerator`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L152-L1202)
  - Method [`DbtProjectGenerator.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L153-L211)
  - Method [`DbtProjectGenerator.generate(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L213-L424)
  - Method [`DbtProjectGenerator._ensure_staging_model(self, source, profile_map, required_columns, staging_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L426-L460)
  - Method [`DbtProjectGenerator._excluded_columns(self, source, stem)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L462-L475)
  - Method [`DbtProjectGenerator._ref_substitutions(self, profile_map, required_sources)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L477-L500)
  - Method [`DbtProjectGenerator._feature_select_items(self, kpi, source_aliases, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L502-L540)
  - Method [`DbtProjectGenerator._write_project_files(self, dbt_dir, sources_needed)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L542-L737)
  - Method [`DbtProjectGenerator._source_entry(self, source, profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L739-L770)
  - Method [`DbtProjectGenerator._loaded_at_field(self, source, profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L772-L785)
  - Method [`DbtProjectGenerator._temporal_anchor_column(self, kpi, profile_map, required_sources)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L787-L812)
  - Method [`DbtProjectGenerator._incremental_mart_body(self, kpi_id, mart_body, grain_keys, event_time)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L814-L847)
  - Method [`DbtProjectGenerator._write_inferred_member_macro(self, dbt_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L849-L907)
  - Method [`DbtProjectGenerator._write_generation_report(self, dbt_dir, generated_kpi_ids, incremental_kpi_ids, skipped)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L909-L951)
  - Method [`DbtProjectGenerator._write_publish_gold_macro(self, dbt_dir, generated_kpi_ids, incremental_kpi_ids)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L953-L1021)
  - Method [`DbtProjectGenerator._write_exposures(self, dbt_dir, generated_kpi_ids)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1023-L1062)
  - Method [`DbtProjectGenerator._write_contracts(self, marts_dir, generated_kpi_ids)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1092-L1145)
  - Method [`DbtProjectGenerator._write_data_quality_tests(self, staging_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1147-L1202)
- Function [`_model_config(model_sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1205-L1223) - *The kwargs of a model's `{{ config(...) }}` call, parsed with `ast`*
- Function [`_incremental_config(unique_key, event_time, lookback)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1226-L1239) - *The config an incremental mart must carry. Every key here guards a*
- Function [`_retention_tblproperties(layer)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1242-L1247) - *Declared Delta retention as real TBLPROPERTIES (audit A4): unset, DBR's*
- Function [`_render_config(options)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1250-L1259) - *`{{ config(...) }}` on ONE line -- _model_config (and every validator*
- Function [`_late_arriving_expr(expr)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1262-L1275) - *COALESCE a DIMENSION-side attribute to the unknown member so an*
- Function [`_drop_excluded(select_list, excluded)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1278-L1288) - *Remove quarantined columns from a staging select list, naming what went.*
- Function [`_output_columns(body)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1291-L1301) - *(expression, output alias) for each aliased column of a select's head.*
- Function [`_alias_for_column(body, column)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1304-L1314) - *The output alias whose expression references `column` -- e.g. a mart*
- Function [`_grain_keys(mart_body)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1317-L1326) - *The mart's GROUP BY dimensions as output aliases -- the grain, and so*
- Function [`_cluster_keys(mart_body)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1329-L1338) - *<=4 liquid-clustering keys for a mart table.*
- Function [`run_dbt_parse(dbt_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1341-L1375) - *Run `dbt parse` over a generated project. Offline; no warehouse needed.*
- Function [`validate_generated_project(dbt_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1378-L1510) - *Emitted-code invariants, checked against the finished project text.*
- Function [`project_declares_event_time(dbt_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1513-L1524) - *True when at least one model declares `event_time`.*
- Function [`_refs(model_sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1527-L1528)
- Function [`_source_blocks(sources_yml)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1531-L1547) - *(table name, its YAML block) for each source table entry.*
- Function [`_read_json(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1550-L1558) - *A contract another slice writes: absent or unreadable means absent.*
- Function [`reconcile_ghost_tables(dbt_dir, warehouse_tables)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1561-L1615) - *Report relations that exist in the warehouse but no longer in the project. Diffs on the manifest's normalized, fully-qualified `relation_name` when available (so same-alias models in different schemas can't collide); falls back to the bare alias/name diff for pre-1.0 manifests or no manifest yet.*
- Function [`_normalize_relation(name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1618-L1622) - *Strip dbt's per-segment quoting (backtick/double-quote/bracket) and casefold, so a warehouse listing diffs cleanly against `relation_name`.*
- Function [`_manifest_model_names(dbt_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1625-L1656) - *Returns `(names, qualified)`: the project's model set to diff against, plus whether it's the normalized fully-qualified `relation_name` set (`True`, caller must normalize the warehouse side too) or the legacy bare alias/name set (`False`, pre-1.0 manifest or no manifest yet -- same file-stem fallback as before).*
- Function [`_render_dbt_test(decision)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1631-L1649)
- Function [`_apply_subs(text, subs)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1652-L1658)
- Function [`_dbt_project_name(workspace_rel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1661-L1665)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1668-L1672)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/dbt_project_generator.py#L1676-L1731)

**Inputs & Outputs**:
- **Inputs**: KPI registry, feature mappings, domain model, workspace settings, target engine (`duckdb`, `databricks`, `snowflake`, `bigquery`).
- **Outputs**: `DbtProjectResult` with compiled dbt project tree under `workspaces/<ws>/dbt/` or `workspaces/<ws>/interns/generated/dbt/`.

**Failure Modes & Edge Cases**:
- Raises `ValueError` if source table joins cannot be resolved, required columns are missing from staging models, or SQL syntax generation fails.

---
### [`engine_parity.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L1-L414)

**Exact Purpose**: Executes identical KPI definitions across multiple engines (DuckDB SQL vs Polars vs PySpark) and verifies numerical/row parity.

**Key Functions & Classes**:

- Function [`parity_row_cap()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L71-L84) - *Row count above which parity runs in aggregate-signature mode.*
- Function [`polars_runtime_available()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L87-L94) - *True when the in-process runtime can generate, run, and read back.*
- Function [`_normalize_cell(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L97-L126) - *Engine-neutral cell form: dates as ISO days, floats rounded, rest str.*
- Function [`_normalize_rows(columns, rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L129-L137) - *Order-insensitive form: columns sorted by lowercased name, rows sorted.*
- Function [`_aggregate_signature(columns, rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L140-L192) - *One-pass aggregate signature over the SAME canonicalized cells row*
- Function [`_numeric_stats_match(a, b)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L195-L205) - *Tolerance-aware numeric comparison mirroring the row-level rounding.*
- Function [`evaluate_parity(canonical_columns, canonical_rows, engine_columns, engine_rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L208-L317) - *Pure cross-engine comparison; returns {status, reason, mode[, signatures]}.*
- Function [`run_polars_parity(repo_root, workspace_rel, kpi_id, canonical_columns, canonical_rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_parity.py#L320-L402) - *Generate + run the Polars variant of one KPI and compare its rows.*

**Inputs & Outputs**:
- **Inputs**: KPI ID, generated SQL, generated Polars code, generated PySpark code, local warehouse data tables.
- **Outputs**: Parity verification dictionary with row count deltas, value difference matrices, and pass/fail boolean status.

**Failure Modes & Edge Cases**:
- Reports parity failure if result row counts differ beyond tolerance or float aggregations exceed numerical threshold.

---
### [`engine_recommender.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L1-L247)

**Exact Purpose**: Analyzes KPI complexity signals (dataset sizes, window functions, distinct counts, join depth) and recommends the optimal target execution engine.

**Key Functions & Classes**:

- Class [`ComplexitySignals`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L34-L59)
  - Method [`ComplexitySignals.size_tier(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L44-L49)
  - Method [`ComplexitySignals.complexity_score(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L52-L59)
- Class [`EngineRecommendation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L63-L73)
  - Method [`EngineRecommendation.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L72-L73)
- Class [`KPIEngineRecommender`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L135-L206)
  - Method [`KPIEngineRecommender.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L136-L139)
  - Method [`KPIEngineRecommender.recommend_all(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L141-L149)
  - Method [`KPIEngineRecommender.write(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L151-L165)
  - Method [`KPIEngineRecommender._signals(self, kpi, profiles)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L167-L180)
  - Method [`KPIEngineRecommender._dataset_bytes(profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L183-L189)
  - Method [`KPIEngineRecommender._profile_map(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L191-L200)
  - Method [`KPIEngineRecommender._load_mapping(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L202-L206)
- Function [`recommend(kpi_id, signals)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L76-L132) - *Pure, deterministic recommendation. SQL is always the default.*
- Function [`_render_md(recs)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L209-L225)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/engine_recommender.py#L230-L243)

**Inputs & Outputs**:
- **Inputs**: KPI definition, feature mapping, profile metadata, dataset byte sizes.
- **Outputs**: `EngineRecommendation` specifying primary engine (`sql`, `polars`, `pyspark`), confidence score, and complexity signals breakdown.

**Failure Modes & Edge Cases**:
- Defaults to `sql` (DuckDB) if profile sizes or complexity signals are missing or unparseable.

---
### [`execution_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L1-L860)

**Exact Purpose**: Executes compiled KPI SQL queries against DuckDB or remote warehouses, records execution latency, row counts, and result view metadata.

**Key Functions & Classes**:

- Class [`KPIExecutionRecord`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L77-L116)
  - Method [`KPIExecutionRecord.ok(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L97-L98)
  - Method [`KPIExecutionRecord.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L100-L116)
- Class [`KPIExecutionHarnessResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L120-L155)
  - Method [`KPIExecutionHarnessResult.ok(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L127-L128)
  - Method [`KPIExecutionHarnessResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L130-L155)
- Class [`KPIExecutionHarness`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L158-L713)
  - Method [`KPIExecutionHarness.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L159-L182)
  - Method [`KPIExecutionHarness.run(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L184-L197)
  - Method [`KPIExecutionHarness.execute_only(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L199-L204)
  - Method [`KPIExecutionHarness._execute_records(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L206-L246)
  - Method [`KPIExecutionHarness._dialect_suffix(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L248-L249)
  - Method [`KPIExecutionHarness._sql_files(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L251-L288)
  - Method [`KPIExecutionHarness._mapping_kpi_status(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L290-L302)
  - Method [`KPIExecutionHarness._execute_one(self, conn, sql_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L304-L370)
  - Method [`KPIExecutionHarness._execute_records_databricks(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L372-L451)
  - Method [`KPIExecutionHarness._execute_one_databricks(self, client, sql_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L453-L508)
  - Method [`KPIExecutionHarness._qualified_result_view(self, result_view)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L510-L511)
  - Method [`KPIExecutionHarness._warehouse_id(self, client)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L513-L517)
  - Method [`KPIExecutionHarness._maybe_write_gold(self, conn, kpi_id, result_view)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L519-L537)
  - Method [`KPIExecutionHarness._semantic_errors(self, kpi_id, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L539-L652)
  - Method [`KPIExecutionHarness._kpi_registry_by_id(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L654-L657)
  - Method [`KPIExecutionHarness._feature_mapping_by_id(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L659-L662)
  - Method [`KPIExecutionHarness._proven_join_pairs(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L664-L677)
  - Method [`KPIExecutionHarness._pipeline_decisions_by_id(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L679-L697)
  - Method [`KPIExecutionHarness._load_kpis_by_id(self, filename)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L699-L713)
- Function [`result_schema_for(settings)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L33-L40) - *Schema that KPI result views are created in -- never the source schema.*
- Function [`sql_defines_result_view(sql, result_view)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L716-L718)
- Function [`sql_is_intent_blocked(sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L721-L727) - *True when generated SQL carries an intent-decision block marker.*
- Function [`_block_reason(sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L730-L735)
- Function [`_placeholder_result_columns(columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L738-L740)
- Function [`_kpi_id_from_path(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L743-L745)
- Function [`_render_report(result)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L748-L775)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L778-L782)
- Function [`_quoted_cut_filter_tokens(cuts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L785-L802) - *Extract literal filter values from a KPI cuts string.*
- Function [`_metric_input_columns(metric)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L805-L814)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/execution_harness.py#L819-L856)

**Inputs & Outputs**:
- **Inputs**: KPI ID, SQL text, local warehouse connection, execution environment variables.
- **Outputs**: `KPIExecutionHarnessResult` and `KPIExecutionRecord` containing execution timing, result table preview, and output column types.

**Failure Modes & Edge Cases**:
- Captures SQL execution errors, duckdb exceptions, table missing errors, and timeout exceptions into error records.

---
### [`feature_resolver.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1-L2080)

**Exact Purpose**: Matches human KPI terms, cuts, measures, and filters against physical table schemas and column profiles using semantic glosses and fuzzy string matching.

**Key Functions & Classes**:

- Class [`ResolverResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L91-L116)
  - Method [`ResolverResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L101-L116)
- Class [`KPIFeatureResolver`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L119-L753)
  - Method [`KPIFeatureResolver.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L120-L134)
  - Method [`KPIFeatureResolver.run(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L136-L218)
  - Method [`KPIFeatureResolver._resolve_kpi(self, idx, kpi, schema_index, alias_index, available_columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L220-L492)
  - Method [`KPIFeatureResolver._derivation_pattern_options(self, kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L494-L511)
  - Method [`KPIFeatureResolver._json_leaf_promotion_options(self, token, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L513-L526)
  - Method [`KPIFeatureResolver._load_kpis(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L528-L533)
  - Method [`KPIFeatureResolver._load_relationships(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L535-L549)
  - Method [`KPIFeatureResolver._resolve_direct_collision(self, token, norm, evidences, full_context)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L551-L660)
  - Method [`KPIFeatureResolver._schema_index(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L662-L669)
  - Method [`KPIFeatureResolver._alias_index(self, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L671-L672)
  - Method [`KPIFeatureResolver._write_open_questions(self, mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L674-L691)
  - Method [`KPIFeatureResolver._store_metadata(self, collection, document_id, payload)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L693-L699)
  - Method [`KPIFeatureResolver._validate_workspace(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L701-L705)
  - Method [`KPIFeatureResolver._load_data_dictionaries(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L707-L708)
  - Method [`KPIFeatureResolver._reconcile_dictionary_claims(self, mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L710-L753)
- Function [`_dictionary_context_choice(norm, evidences, full_context)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L756-L798) - *Dictionary-context disambiguation for a colliding column name.*
- Function [`_relationship_join_worthy(relationship)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L801-L815) - *Whether a relationship is strong enough lineage to unify column names.*
- Function [`_column_identity_groups(relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L818-L860) - *Union-find over (dataset, column) endpoints of join-worthy relationships.*
- Function [`_column_pair(source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L863-L875) - *Normalized ``(dataset, column)`` identity for one source-column entry.*
- Function [`_physical_column_key(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L878-L891) - *Identity of the physical column(s) a feature resolves to.*
- Function [`_resolved_physical_columns(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L894-L906) - *Physical columns a PROVEN feature resolves to.*
- Function [`_candidate_physical_column(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L909-L950) - *Top-ranked, NAME-MATCHED candidate physical column for an UNRESOLVED*
- Function [`_dedupe_features_by_physical_column(features)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L953-L1044) - *Collapse features of one KPI that resolve to the SAME physical column.*
- Function [`_redupe_all_kpis_after_definitions(mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1047-L1073) - *Re-run per-KPI physical-column dedup after workspace-level definitions*
- Function [`_canonical_survivor(features, candidates)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1076-L1113) - *Pick which feature name survives a same-physical-column collapse.*
- Function [`extract_expression(expression)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1116-L1126)
- Function [`prioritize_blockers(mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1129-L1130)
- Function [`infer_join_candidates(features)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1133-L1134)
- Function [`enrich_schema_index_with_dictionaries(schema_index, dictionaries)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1137-L1165)
- Function [`_expression_shaped_feature(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1179-L1181)
- Function [`contextual_column_candidates(feature, full_context, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1184-L1262) - *Score columns against a feature using the surrounding KPI context.*
- Function [`_contextual_feature(token, contextual_candidates)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1265-L1312)
- Function [`_contextual_score(feature_norm, context_tokens, context_norm, entry)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1315-L1442) - *Score a candidate column against a feature token.*
- Function [`_semantic_tokens(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1445-L1469)
- Function [`_split_identifier(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1472-L1474)
- Function [`_secondary_measure_phrases(name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1477-L1490) - *Column phrases for a SECOND measure named in the question prose*
- Function [`_kpi_context(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1493-L1503)
- Function [`_requires_kpi_definition(kpi, expression_context, extracted)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1506-L1524)
- Function [`_derived_pattern_feature(options)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1527-L1549) - *Blocker feature carrying reusable derived-feature pattern options so the*
- Function [`_prose_anchor_evidence(kpi, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1552-L1613) - *Workspace-evidence anchors for a prose KPI.*
- Function [`_kpi_supporting_evidence_present(features)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1616-L1642) - *True when ANY feature of a KPI anchors to workspace evidence.*
- Function [`_label_kpis_without_supporting_evidence(mapping, kpis, schema_index, definitions)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1645-L1707) - *Label blocked KPIs whose prose anchors to NOTHING in the workspace.*
- Function [`_no_supporting_evidence_feature(kpi_entry, kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1710-L1765) - *Machine-readable blocker for a KPI with zero workspace evidence anchors.*
- Function [`_kpi_definition_feature(kpi, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1768-L1816)
- Function [`_derived_metric_confirmation_feature(kpi, kpi_id, resolved_features, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1819-L1884) - *Confirmation blocker for a metric the platform GUESSED from prose.*
- Function [`apply_user_decision(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1887-L1907)
- Function [`apply_workspace_definition(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1910-L1934)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1937-L1941)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/feature_resolver.py#L1946-L2076)

**Inputs & Outputs**:
- **Inputs**: KPI registry, data model profiles, domain dictionaries, accepted feature definitions.
- **Outputs**: `ResolverResult` with mapped feature dicts, unresolved blockers list, and derived feature candidates.

**Failure Modes & Edge Cases**:
- Identifies unmapped features as blockers; raises `RuntimeError` if profile index is corrupted or unreadable.

---
### [`generate_kpi_engines.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L1-L338)

**Exact Purpose**: Multi-engine code generation orchestrator that triggers SQL, Polars, and PySpark generators concurrently for a set of KPIs.

**Key Functions & Classes**:

- Class [`EngineGenerationOutcome`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L59-L68) - *One engine attempt for one KPI.*
  - Method [`EngineGenerationOutcome.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L67-L68)
- Class [`KPIGenerationOutcome`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L72-L89) - *All engine attempts for one KPI, plus the recommendation context.*
  - Method [`KPIGenerationOutcome.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L81-L89)
- Class [`KPIMultiEngineGenerator`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L140-L272) - *Generate KPI code for the recommended engine, all engines, or a list.*
  - Method [`KPIMultiEngineGenerator.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L143-L164)
  - Method [`KPIMultiEngineGenerator.generate_all(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L168-L194)
  - Method [`KPIMultiEngineGenerator._generate_one(self, kpi_id, engines, recommended_engine, recommendation_reasons)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L196-L233)
  - Method [`KPIMultiEngineGenerator.write(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L237-L265)
  - Method [`KPIMultiEngineGenerator._kpi_ids(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L267-L272)
- Function [`expand_engine_mode(mode, recommended_engine)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L92-L118) - *Resolve the requested ``--engine`` mode into concrete engines for a KPI.*
- Function [`_expand_label(label)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L121-L127)
- Function [`_dedupe(values)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L130-L137)
- Function [`_render_md(engine, outcomes)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L275-L298)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generate_kpi_engines.py#L303-L334)

**Inputs & Outputs**:
- **Inputs**: Workspace layout, KPI registry, feature mappings, target engine selection.
- **Outputs**: `EngineGenerationOutcome` containing paths to generated `.sql`, `.py` (Polars), and `.py` (PySpark) files.

**Failure Modes & Edge Cases**:
- Aggregates individual engine generation exceptions without stopping non-failing engine targets.

---
### [`generation_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_cli.py#L1-L93)

**Exact Purpose**: CLI entry point for driving interactive KPI requirement interviews and candidate requirement generation.

**Key Functions & Classes**:

- Function [`_workflow(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_cli.py#L10-L11)
- Function [`prepare_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_cli.py#L16-L30)
- Function [`apply_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_cli.py#L34-L62)
- Function [`finalize_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_cli.py#L66-L93)

**Inputs & Outputs**:
- **Inputs**: `argv` with `--workspace`, `--domain`, `--context-file`, `--interactive`.
- **Outputs**: Console output and generated requirement drafts in `interns/generated/requirements/`.

**Failure Modes & Edge Cases**:
- Exits with code 1 on missing workspace path or unreadable context files.

---
### [`generation_quality.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L1-L434)

**Exact Purpose**: Scores draft KPI requirements across clarity, business context, measurability, grain definition, and completeness metrics.

**Key Functions & Classes**:

- Function [`score_kpis(kpis, listing, context_files)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L8-L84)
- Function [`understanding_score(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L104-L197) - *Compute a 0-100 understanding/confidence score for a single KPI.*
- Function [`_confidence_status(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L200-L205)
- Function [`_confidence_icon(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L208-L209)
- Function [`_understanding_label(score)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L212-L217)
- Function [`_next_question_for(dimension)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L220-L229)
- Function [`missing_discussion_points(kpi, has_context)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L232-L251)
- Function [`advisor_notes(quality)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L254-L258)
- Function [`merge_refinement(existing, missing)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L261-L265)
- Function [`suggest_seed_kpi(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L268-L287)
- Function [`looks_like_business_question(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L290-L291)
- Function [`column_like_token_overlap(text, data_files)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L294-L313)
- Function [`unique_sorted(values)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L316-L317)
- Function [`_metric_label(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L330-L335)
- Function [`_dimension_tokens(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L338-L342)
- Function [`_time_dimension(tokens)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L345-L349)
- Function [`_format_table(headers, rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L352-L356)
- Function [`result_format_candidates(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_quality.py#L359-L434) - *Return 2-3 candidate result-table layouts for a KPI, each with a concrete*

**Inputs & Outputs**:
- **Inputs**: KPI requirement text, domain metadata, dataset profile summary.
- **Outputs**: Quality score breakdown dict (0-100 score), readiness classification, and actionable improvement recommendations.

**Failure Modes & Edge Cases**:
- Returns low quality score with explicit warning reasons when placeholder words or vague metrics are detected.

---
### [`generation_workflow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1-L1340)

**Exact Purpose**: Orchestrates end-to-end KPI requirements discovery, interactive stakeholder interviewing, draft generation, scoring, and finalization.

**Key Functions & Classes**:

- Class [`KPIGenerationResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L59-L69)
  - Method [`KPIGenerationResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L68-L69)
- Class [`KPIGenerationFinalizeResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L73-L84)
  - Method [`KPIGenerationFinalizeResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L83-L84)
- Class [`KPIGenerationWorkflow`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L87-L468)
  - Method [`KPIGenerationWorkflow.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L88-L91)
  - Method [`KPIGenerationWorkflow.prepare(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L93-L123)
  - Method [`KPIGenerationWorkflow.apply_answer(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L125-L156)
  - Method [`KPIGenerationWorkflow.finalize(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L158-L226)
  - Method [`KPIGenerationWorkflow._advance(self, session, stage, option, custom_note)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L228-L274)
  - Method [`KPIGenerationWorkflow._record_result_format(self, session, option, custom_note)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L276-L326)
  - Method [`KPIGenerationWorkflow._load_existing_kpis(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L328-L331)
  - Method [`KPIGenerationWorkflow._normalize_context_files(self, values)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L333-L344)
  - Method [`KPIGenerationWorkflow._write_session(self, session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L346-L350)
  - Method [`KPIGenerationWorkflow._write_draft_if_present(self, session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L352-L370)
  - Method [`KPIGenerationWorkflow._read_session(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L372-L376)
  - Method [`KPIGenerationWorkflow._read_current_panel(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L378-L382)
  - Method [`KPIGenerationWorkflow._write_panel(self, session, panel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L384-L406)
  - Method [`KPIGenerationWorkflow._write_workspace_memory(self, session, output_path, production_proof)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L408-L427)
  - Method [`KPIGenerationWorkflow._write_team_memory(self, session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L429-L453)
  - Method [`KPIGenerationWorkflow._session_path(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L455-L456)
  - Method [`KPIGenerationWorkflow._current_json_path(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L458-L459)
  - Method [`KPIGenerationWorkflow._current_markdown_path(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L461-L462)
  - Method [`KPIGenerationWorkflow._validate_workspace(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L464-L468)
- Function [`_conversation_skills(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L471-L488) - *Skill routing for the orchestrating CLI agent, by understanding score.*
- Function [`_route_panel(session, recommended_option_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L491-L524)
- Function [`_context_panel(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L527-L558)
- Function [`_orientation_panel(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L561-L591)
- Function [`_format_panel(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L594-L625)
- Function [`_result_format_candidates(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L628-L638) - *Build example result-table layouts for each draft KPI. Keyed by kpi_id so*
- Function [`_result_format_panel(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L641-L680)
- Function [`_final_preview_panel(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L683-L734)
- Function [`_usual_workflow_panel(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L737-L751)
- Function [`_terminal_panel(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L754-L765)
- Function [`_score_kpis(kpis, listing, context_files)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L768-L773)
- Function [`_build_draft_kpis(session, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L776-L859)
- Function [`_load_profile_evidence_columns(session, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L862-L889) - *Flatten the workspace's profile_index.json into evidence columns for*
- Function [`_load_session_glosses(session, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L892-L947) - *Discover column descriptions (glosses) from the session workspace's*
- Function [`_is_placeholder_only_draft(draft_kpis)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L950-L961)
- Function [`_draft_proofs(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L964-L992)
- Function [`_competitive_review(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L995-L1030)
- Function [`_production_proof(session, draft_kpis, output_registry_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1033-L1066)
- Function [`_render_panel_markdown(panel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1069-L1157)
- Function [`_render_understanding_markdown(quality)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1160-L1185)
- Function [`_render_result_format_markdown(examples_by_kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1188-L1200)
- Function [`_render_production_proof_markdown(proof)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1203-L1220)
- Function [`_kpi_to_dict(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1223-L1232)
- Function [`_default_preferences()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1235-L1243)
- Function [`_missing_discussion_points(kpi, has_context)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1246-L1247)
- Function [`_advisor_notes(quality)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1250-L1251)
- Function [`_merge_refinement(existing, missing)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1254-L1255)
- Function [`_suggest_seed_kpi(session)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1258-L1259)
- Function [`_looks_like_business_question(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1262-L1263)
- Function [`_column_like_token_overlap(text, data_files)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1266-L1267)
- Function [`_resolve_option(panel, answer)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1270-L1293)
- Function [`_one(options, predicate, answer)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1296-L1303)
- Function [`_unique_sorted(values)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1306-L1307)
- Function [`_norm(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1310-L1311)
- Function [`_now()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1314-L1315)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1318-L1322)
- Function [`prepare_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1325-L1328)
- Function [`apply_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1331-L1334)
- Function [`finalize_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/generation_workflow.py#L1337-L1340)

**Inputs & Outputs**:
- **Inputs**: Workspace layout, domain name, stakeholder interview inputs, candidate context documents.
- **Outputs**: `KPIGenerationResult` and `KPIGenerationFinalizeResult` writing approved KPI registry contracts.

**Failure Modes & Edge Cases**:
- Refuses to finalize drafts containing seed/placeholder KPIs or quality scores below acceptance threshold.

---
### [`intent_contract.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1-L1512)

**Exact Purpose**: Defines formal intent contracts and validation rules governing the binding between business KPI requirements and physical database objects.

**Key Functions & Classes**:

- Function [`_split_cuts(cuts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L179-L180)
- Function [`_norm(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L183-L184)
- Function [`_feature_column_lookup(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L187-L206) - *Map feature label (lowercased) -> first source column.*
- Function [`_emitted_columns(lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L209-L210)
- Function [`_resolve_cut_column(token, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L213-L229) - *Resolve a cut token to an underlying column: exact, normalized, word-subset.*
- Function [`_load_pipeline_decisions(workspace_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L232-L245) - *Load pipeline_decisions.json if present, else return empty dict.*
- Function [`_denominator_scope_from_decisions(kpi_id, decisions)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L248-L267) - *Look up recorded denominator-scope decision for kpi_id.*
- Function [`_grain_bucketing_from_decisions(kpi_id, decisions)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L270-L284) - *Look up a recorded grain-bucketing decision for kpi_id. None if absent.*
- Function [`_raw_continuous_cuts(cuts_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L287-L300) - *Cut tokens that GROUP BY a raw exact continuous value (age / days-since).*
- Function [`_facet_metric(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L307-L386) - *Extract the metric facet.*
- Function [`_facet_grain(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L389-L488) - *Extract the grain facet.*
- Function [`_facet_filters(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L491-L547) - *Extract the filters facet.*
- Function [`_facet_denominator_scope(kpi, decisions)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L550-L620) - *Extract the denominator_scope facet.*
- Function [`_facet_grain_bucketing(kpi, decisions)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L623-L687) - *Extract the grain_bucketing facet.*
- Function [`_facet_temporal_anchor(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L690-L765) - *Extract the temporal_anchor facet.*
- Function [`_facet_output_shape(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L768-L837) - *Extract the output_shape facet.*
- Function [`_facet_null_zero_handling(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L840-L860) - *Extract the null_zero_handling facet.*
- Function [`build_intent_contract(kpi, decisions)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L867-L932) - *Build a per-facet intent contract for a single KPI registry entry.*
- Function [`low_confidence_facets(contract)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L935-L976) - *Return facets with confidence in {low} or metric=none that should become*
- Function [`_panel_question_text(facet, value, alternatives, evidence)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L979-L1031) - *Produce a human-readable panel question for a low-confidence facet.*
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1049-L1053)
- Function [`answer_key(kpi_id, facet)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1056-L1057)
- Function [`load_intent_answers(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1060-L1074) - *Return recorded intent-facet answers keyed by ``<kpi_id>::<facet>``.*
- Function [`record_intent_answer(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1077-L1152) - *Persist a human/agent answer for a low-confidence intent facet.*
- Function [`_intent_facet_to_panel_question(kpi_id, kpi_name, facet_q, workspace_rel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1155-L1240) - *Map a low_confidence_facets() entry into a blocker-panel question dict.*
- Function [`intent_facet_panel_questions(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1243-L1281) - *Build blocker-panel questions for every unanswered low-confidence facet.*
- Function [`_render_markdown(contracts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1291-L1334)
- Function [`_load_registry_with_features(workspace_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1341-L1396) - *Load kpi_registry.json and, if available, merge feature_mapping features.*
- Function [`write_intent_contract(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1399-L1449) - *Build intent contracts for every KPI in kpi_registry.json and write*
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_contract.py#L1458-L1500) - *Console entry point: build-intent-contract --workspace <ws>*

**Inputs & Outputs**:
- **Inputs**: Parsed KPI intent, feature mapping contract, schema profiles.
- **Outputs**: Validation status list, contract binding rules, and structural error violations.

**Failure Modes & Edge Cases**:
- Fails validation if physical column data types conflict with metric aggregation functions (e.g. SUM on VARCHAR).

---
### [`intent_coverage.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L1-L811)

**Exact Purpose**: Audits coverage of KPI semantic intent against available schema tables, dimension attributes, and join paths.

**Key Functions & Classes**:

- Class [`CoverageFinding`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L101-L106)
- Function [`_norm(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L109-L111) - *Lowercase, strip everything but alphanumerics.*
- Function [`_compact(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L114-L116) - *Lowercase and strip whitespace so `count( distinct x )` matches `count(distinct`.*
- Function [`_token_present(token, block_lower)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L119-L130) - *True when `token` appears as a whole identifier/word in the lowercased SQL.*
- Function [`_feature_column_lookup(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L133-L156) - *Map feature label (lowercased) -> first source column. Data, not logic —*
- Function [`_resolve_cut_column(token, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L159-L177) - *Resolve a cut token to an underlying column: exact label, normalized*
- Function [`_split_cuts(cuts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L180-L181)
- Function [`result_view_block(sql, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L184-L196) - *Return the SQL text from the `CREATE ... VIEW <kpi_id>_results AS` clause*
- Function [`declared_grain(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L199-L232) - *Independently extract declared grain dimensions from raw `cuts`.*
- Function [`declared_metric_aggs(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L235-L248) - *Extract (fn, input_column, distinct) tuples from the raw `metric`.*
- Function [`declared_explicit_filters(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L251-L264) - *Quoted literal filter values present in `cuts` (high confidence). Prose*
- Function [`grain_coverage_findings(kpi, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L267-L294) - *The enforced check: every declared grain dimension must appear in the*
- Function [`evaluate_intent_coverage(kpi, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L297-L357) - *Full intent coverage: grain + metric aggregation + explicit filters.*
- Function [`_is_within_group_scope(scope)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L373-L381) - *Return True when *scope* is a within-group denominator-scope value.*
- Function [`denominator_scope_findings(kpi, sql, scope)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L384-L452) - *Check that the denominator window in the result-view SQL matches *scope*.*
- Function [`temporal_anchor_findings(kpi, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L480-L528) - *Conservative check: when a KPI has both age/date arithmetic in its cuts*
- Function [`output_shape_findings(kpi, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L539-L575) - *Conservative check: a ``top N`` KPI name must emit ``LIMIT N`` in the*
- Function [`_load_proven_join_pairs(relationship_contracts_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L582-L613) - *Load proven column pairs from a relationship_contracts.json artifact.*
- Function [`_extract_join_pairs(sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L616-L628) - *Parse JOIN ... ON a.col_x = b.col_y clauses from SQL.*
- Function [`join_correctness_findings(sql, proven_pairs)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L631-L669) - *Check every JOIN ON clause in *sql* against the set of proven column*
- Function [`declared_prose_filters(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L676-L716) - *Extract high-confidence prose filter declarations from the KPI name.*
- Function [`_age_threshold_present(value, sql_lower)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L719-L738) - *True when the age-threshold integer appears in a genuine numeric/*
- Function [`prose_filter_findings(kpi, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/intent_coverage.py#L741-L791) - *Check that prose-declared filters in the KPI name appear in the SQL.*

**Inputs & Outputs**:
- **Inputs**: KPI intent list, physical data model, join graph.
- **Outputs**: List of `CoverageFinding` records detailing missing facts, unjoinable dimensions, or unmapped filter clauses.

**Failure Modes & Edge Cases**:
- Identifies unmapped join paths or isolated tables as unresolvable coverage gaps.

---
### [`kpi_confirmation_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_confirmation_panel.py#L1-L189)

**Exact Purpose**: Generates interactive panels for human confirmation of auto-derived KPI business logic, formulas, and grain assumptions.

**Key Functions & Classes**:

- Function [`build_kpi_confirmation_panel(detection, sample_rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_confirmation_panel.py#L36-L112) - *Assemble the JSON-backed confirmation panel for a detected KPI format.*
- Function [`render_kpi_confirmation_markdown(panel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_confirmation_panel.py#L115-L186) - *Render the confirmation panel as the human-readable terminal card.*

**Inputs & Outputs**:
- **Inputs**: Resolved KPI definitions, derived feature mappings, confidence scores.
- **Outputs**: Markdown/JSON confirmation panel formatted for user review.

**Failure Modes & Edge Cases**:
- Falls back to explicit blocker question if confidence score is below confirmation threshold.

---
### [`kpi_definition.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L1-L532)

**Exact Purpose**: Defines core dataclasses (`KPI`, `KPIRegistry`) and JSON/dict schema serialization logic for KPI definitions.

**Key Functions & Classes**:

- Class [`ApplyKpiDefinitionResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L105-L129)
  - Method [`ApplyKpiDefinitionResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L115-L129)
- Function [`kpi_definition_key(business_question)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L49-L53) - *Normalized identity for a KPI business question (matches the dedupe key).*
- Function [`_store_path(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L56-L57)
- Function [`load_kpi_definition_store(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L60-L69)
- Function [`apply_accepted_definitions_to_kpis(kpis, store)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L72-L101) - *Override empty metric/cuts on KpiDefinition rows from accepted decisions.*
- Function [`_resolve_business_question(registry, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L132-L140)
- Function [`apply_kpi_definition(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L143-L255)
- Function [`_parse_bulk_rows(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L258-L330) - *Parse a bulk definitions file into (row_number, row) pairs plus row errors.*
- Function [`apply_kpi_definitions_from_file(repo_root, workspace, from_file)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L333-L414) - *Bulk apply: validate ALL rows first, then apply each via the single path.*
- Function [`_print_bulk_report(report)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L417-L448)
- Function [`_truncate(text, limit)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L451-L452)
- Function [`_read_json(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L455-L462)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L465-L469)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_definition.py#L474-L528)

**Inputs & Outputs**:
- **Inputs**: Raw dicts, JSON files, Excel row mappings.
- **Outputs**: Validated `KPI` instance objects and `ApplyKpiDefinitionResult` payloads.

**Failure Modes & Edge Cases**:
- Raises `KeyError` or `ValueError` on missing mandatory fields (`kpi_id`, `name`, `metric`).

---
### [`kpi_format_detector.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L1-L385)

**Exact Purpose**: Analyzes raw Excel workbooks and CSV files to detect column roles (metric, cuts, description, grain) and spreadsheet layout structure.

**Key Functions & Classes**:

- Class [`ColumnRole`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L92-L113) - *One column's inferred role, with the evidence that justifies it.*
  - Method [`ColumnRole.to_dict(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L103-L113)
- Class [`KpiFormatDetection`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L117-L169) - *Structured result of detecting a KPI file's format.*
  - Method [`KpiFormatDetection.role_header(self, role)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L130-L134)
  - Method [`KpiFormatDetection.read_back(self, row)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L136-L153)
  - Method [`KpiFormatDetection.to_dict(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L155-L169)
- Function [`_norm(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L79-L80)
- Function [`_label(confidence)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L83-L88)
- Function [`_header_score(header, role)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L172-L185)
- Function [`_content_score(values, role)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L188-L217) - *Fraction of non-empty cells whose VALUE signals the given role.*
- Function [`_evidence(values, limit)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L220-L227)
- Function [`_detect_nesting(rows, name_header)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L230-L269)
- Function [`detect_kpi_format(columns, rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_format_detector.py#L272-L375) - *Detect each column's KPI role from headers and values, with confidence.*

**Inputs & Outputs**:
- **Inputs**: Header lists, sample rows, merged cell spans from `WorkbookGrid`.
- **Outputs**: `KpiFormatDetection` containing identified column roles, confidence ratings, and structural orientation.

**Failure Modes & Edge Cases**:
- Falls back to standard default column mapping if header text contains no recognized keywords.

---
### [`kpi_intent.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L1-L284)

**Exact Purpose**: Represents structured semantic intent (`MetricIntent`, `DimIntent`, `FilterIntent`, `ShareIntent`, `KPIIntent`) parsed from natural language KPI prose.

**Key Functions & Classes**:

- Class [`MetricIntent`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L52-L56)
- Class [`DimIntent`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L60-L64)
- Class [`FilterIntent`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L68-L72)
- Class [`ShareIntent`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L76-L87) - *A percentage-share computation: base / scope * 100.*
- Class [`KPIIntent`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L91-L98)
- Function [`column_dim_renamed(dim)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L37-L44) - *True when a plain-column dimension's OUTPUT alias was changed from its*
- Function [`parse_metric(metric_text, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L110-L123)
- Function [`parse_intent(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L126-L204)
- Function [`_parse_window_or_ratio(metric_text, name_text, metric, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L207-L252)
- Function [`_filter_from_token(token, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L255-L264)
- Function [`_age_source(token)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L267-L271)
- Function [`_reference_names(dim)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/kpi_intent.py#L274-L281)

**Inputs & Outputs**:
- **Inputs**: Natural language KPI name, description, and cut specifications.
- **Outputs**: `KPIIntent` dataclass holding parsed aggregation functions, target measures, cut dimensions, and filter conditions.

**Failure Modes & Edge Cases**:
- Emits fallback intent with raw text if semantic parser cannot categorize metric phrase.

---
### [`local_warehouse.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L1-L327)

**Exact Purpose**: Manages local DuckDB in-memory or file-backed warehouse connections, table registration, parquet loading, and view creation.

**Key Functions & Classes**:

- Class [`WarehouseTable`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L44-L50)
- Class [`WarehouseSetupResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L54-L65)
  - Method [`WarehouseSetupResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L59-L65)
- Class [`LocalWarehouse`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L68-L265)
  - Method [`LocalWarehouse.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L69-L72)
  - Method [`LocalWarehouse.warehouse_path(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L75-L76)
  - Method [`LocalWarehouse.setup(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L78-L160)
  - Method [`LocalWarehouse.list_tables(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L162-L175)
  - Method [`LocalWarehouse.query(self, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L177-L198)
  - Method [`LocalWarehouse._detect_fact_tables(self, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L202-L234)
  - Method [`LocalWarehouse._classify(self, stem, source_path, fact_sources)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L236-L239)
  - Method [`LocalWarehouse._ingest_bronze(self, source_path, bronze_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L241-L254)
  - Method [`LocalWarehouse._profile_map(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L256-L265)
- Function [`warehouse_table_name(source_path, fact_sources, stem_override)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L270-L281) - *Return the warehouse table name for a given source path.*
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/local_warehouse.py#L286-L323)

**Inputs & Outputs**:
- **Inputs**: Workspace layout, dataset file paths (`.parquet`, `.csv`), table schemas.
- **Outputs**: `WarehouseSetupResult` and active DuckDB connection populated with registered source tables.

**Failure Modes & Edge Cases**:
- Raises `duckdb.Error` or `FileNotFoundError` if source dataset files are corrupt or inaccessible.

---
### [`metric_derivation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L1-L1009)

**Exact Purpose**: Derives computed metrics and derived columns from physical schema columns using expression templates and statistical profile analysis.

**Key Functions & Classes**:

- Function [`_tokens(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L99-L100)
- Function [`_content_tokens(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L103-L104)
- Function [`_singularize(token)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L107-L114)
- Function [`_is_numeric(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L117-L119)
- Function [`_is_temporal(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L122-L135)
- Function [`_values_look_temporal(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L138-L143)
- Function [`_cardinality_ratio(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L146-L154)
- Function [`_looks_like_id(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L157-L167)
- Function [`_name_token_set(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L170-L172)
- Function [`_dataset_token_set(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L175-L186) - *Singularized tokens of the column's source table/file name.*
- Function [`_dataset_term_bonus(col, terms)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L189-L196) - *A small additive score when the column's TABLE name matches a question*
- Function [`_value_token_set(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L199-L204)
- Function [`_dictionary_token_set(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L207-L208)
- Function [`_evidence_ref(col, reason)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L211-L224) - *One evidence citation, in the repo's profile-backed shape.*
- Function [`_score_column_against_terms(col, terms)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L227-L261) - *Score how well a column matches the given noun terms, evidence-only.*
- Function [`_measure_specificity(col, terms)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L264-L278) - *How many question terms this column matches that are NOT just the entity/*
- Function [`_facet(value, confidence, evidence, alternatives)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L281-L287)
- Function [`_empty_facet()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L290-L291)
- Function [`_classify_intent(question)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L294-L308)
- Function [`_best_measure_column(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L311-L351) - *Pick the column whose evidence best matches the measure noun in the question.*
- Function [`_count_entity_column(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L354-L433) - *For count intents, find a high-cardinality id column matching the entity.*
- Function [`_derive_metric(question, intent, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L436-L565)
- Function [`_resolve_time_grain(question)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L568-L575)
- Function [`_referencing_event_datasets(measure_dataset, columns, temporal_pool)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L586-L609) - *Datasets that REFERENCE the measure table and carry their own dates.*
- Function [`_best_temporal_column(columns, preferred_dataset)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L612-L628)
- Function [`_scored_temporal_anchor(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L631-L730) - *Pick the temporal anchor with question-term evidence and honest ambiguity.*
- Function [`_categorical_grain_phrases(question)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L733-L741)
- Function [`_best_categorical_column(phrase, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L744-L771)
- Function [`_derive_cuts(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L774-L855)
- Function [`derive_metric_and_cuts(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L858-L943) - *Derive a metric/cuts/filters proposal from a question + workspace evidence.*
- Function [`columns_from_profile_index(profile_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/metric_derivation.py#L946-L1009) - *Flatten a ``profile_index.json`` payload into the evidence column list.*

**Inputs & Outputs**:
- **Inputs**: KPI requirement, table profiles, column data types, value distributions.
- **Outputs**: List of derived metric candidates with SQL formulas, input columns, and confidence scores.

**Failure Modes & Edge Cases**:
- Suppresses derived candidates that generate invalid SQL or division-by-zero risks.

---
### [`panel_preview_cache.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_cache.py#L1-L157)

**Exact Purpose**: Implements an in-memory and disk cache for blocker panel preview query results to speed up interactive panel rendering.

**Key Functions & Classes**:

- Function [`_cache_dir(workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_cache.py#L26-L27)
- Function [`compute_preview_cache_key(sql, dataset_paths)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_cache.py#L30-L49) - *Return a 32-char hex key derived from SQL text and dataset (path, mtime_ns) pairs.*
- Function [`cache_path(workspace_path, cache_key)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_cache.py#L52-L55) - *Return the JSON file path for ``cache_key`` under the cache directory.*
- Function [`load_cached_preview(workspace_path, cache_key)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_cache.py#L58-L76) - *Return the cached payload dict or ``None`` when no usable entry exists.*
- Function [`save_cached_preview(workspace_path, cache_key, payload)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_cache.py#L79-L98) - *Write ``payload`` atomically to the cache path and return the final path.*
- Function [`evict_stale_entries(workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_cache.py#L101-L148) - *Remove cache files older than ``max_age_seconds`` and cap directory size.*

**Inputs & Outputs**:
- **Inputs**: Cache keys derived from SQL query text and workspace dataset state.
- **Outputs**: Cached query result dataframes or execution result dictionaries.

**Failure Modes & Edge Cases**:
- Evicts stale cache entries on schema/dataset modification; falls back to fresh execution on cache miss.

---
### [`panel_preview_executor.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_executor.py#L1-L233)

**Exact Purpose**: Executes sample preview queries in DuckDB to generate real data row previews for candidate blocker options.

**Key Functions & Classes**:

- Class [`PreviewResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_executor.py#L30-L48) - *Outcome of a single preview SQL execution.*
  - Method [`PreviewResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_executor.py#L45-L48)
- Function [`_json_safe(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_executor.py#L51-L75) - *Coerce a single DuckDB cell into a JSON-serializable scalar.*
- Function [`_run_query(sql, max_rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_executor.py#L78-L91) - *Execute ``sql`` in a fresh in-memory DuckDB; return columns + rows.*
- Function [`execute_preview()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/panel_preview_executor.py#L94-L233) - *Execute ``sql`` in DuckDB ``:memory:`` with a wall-clock budget.*

**Inputs & Outputs**:
- **Inputs**: Candidate option SQL expressions, sample row limit, local warehouse connection.
- **Outputs**: `PreviewResult` containing preview table rows, row counts, and execution status.

**Failure Modes & Edge Cases**:
- Returns empty preview result with error details if preview SQL execution fails.

---
### [`parallel_completion.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L1-L542)

**Exact Purpose**: Constructs a dependency DAG and union-find graph to schedule and execute independent KPI generation tasks in parallel.

**Key Functions & Classes**:

- Class [`_UnionFind`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L140-L155)
  - Method [`_UnionFind.__init__(self, items)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L141-L142)
  - Method [`_UnionFind.find(self, item)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L144-L150)
  - Method [`_UnionFind.union(self, a, b)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L152-L155)
- Class [`ParallelPlanResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L159-L175)
  - Method [`ParallelPlanResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L167-L175)
- Class [`DispatchDecision`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L366-L395) - *Outcome of the run-kpi-pipeline fan-out decision.*
  - Method [`DispatchDecision.fan_out(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L383-L384)
  - Method [`DispatchDecision.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L386-L395)
- Function [`resolve_parallel_threshold(threshold)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L63-L81) - *Resolve the ready-KPI fan-out threshold.*
- Function [`count_ready_kpis(mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L84-L97) - *Count KPIs that have no unresolved feature/join blockers left.*
- Function [`decide_worker_count(parallel_units)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L100-L113) - *Map the number of independent execution units to a worker count.*
- Function [`_unresolved_features(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L116-L125)
- Function [`_join_keys(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L128-L137)
- Function [`build_completion_graph(mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L178-L218) - *Connected-component graph of KPIs linked by shared blockers/joins.*
- Function [`_assign_components_to_workers(components, worker_count)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L221-L244) - *Balance whole components across workers by KPI count (largest-first).*
- Function [`plan_parallel_completion(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L247-L362)
- Function [`dispatch_parallel_completion(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L398-L480) - *Decide whether run-kpi-pipeline should fan KPI completion out in parallel.*
- Function [`_render_markdown(plan)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L483-L513)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L516-L520)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/parallel_completion.py#L525-L538)

**Inputs & Outputs**:
- **Inputs**: List of KPI IDs, shared dataset dependencies, concurrency limits.
- **Outputs**: `ParallelPlanResult` detailing execution batches, parallel groups, and dispatch decisions.

**Failure Modes & Edge Cases**:
- Detects cyclic dependencies in KPI definitions and raises `ValueError`.

---
### [`phi_review_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L1-L304)

**Exact Purpose**: Generates review panels for identifying, auditing, and securing Protected Health Information (PHI) columns in KPI queries.

**Key Functions & Classes**:

- Class [`PHIReviewPanelResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L53-L60)
  - Method [`PHIReviewPanelResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L59-L60)
- Class [`PHIReviewPanelBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L63-L139)
  - Method [`PHIReviewPanelBuilder.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L64-L68)
  - Method [`PHIReviewPanelBuilder.run(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L70-L139)
- Function [`apply_phi_review_answer(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L142-L196) - *Apply one human answer for one column. Raises ValueError on a bad answer.*
- Function [`_write_allowlist_override(workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L199-L219)
- Function [`_load_json(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L222-L229)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L232-L236)
- Function [`_render_markdown(current)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L239-L257)
- Function [`prepare_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L261-L268)
- Function [`apply_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/phi_review_panel.py#L272-L292)

**Inputs & Outputs**:
- **Inputs**: Schema column names, dataset profiles, healthcare domain glossaries.
- **Outputs**: `PHIReviewPanelResult` and formatted PHI audit panels.

**Failure Modes & Edge Cases**:
- Flags all ambiguous medical/clinical identifier columns for human review if domain glossary match is uncertain.

---
### [`pii_redaction.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L1-L296)

**Exact Purpose**: Applies regex-based and column-name based PII redaction to dataset rows, replacing sensitive fields with placeholders or aggregated age bands.

**Key Functions & Classes**:

- Function [`is_age_column(column_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L81-L85) - *Return True if ``column_name`` looks like a person-age column.*
- Function [`bucket_age_value(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L88-L103) - *Return ``value`` unchanged unless it is a numeric age > 89, which is*
- Function [`_compile(patterns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L118-L124)
- Function [`is_pii_column(column_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L127-L140) - *Return True if ``column_name`` matches any PII pattern (case-insensitive).*
- Function [`redact_sample_values(column_name, values)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L143-L163) - *If ``column_name`` is PII, replace each value with ``placeholder``; else copy.*
- Function [`redact_row_dict(row)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L166-L188) - *Return a new dict with PII columns' values replaced by ``placeholder``.*
- Function [`redact_rows(rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L191-L205) - *Apply :func:`redact_row_dict` to each row. Returns a new list.*
- Function [`workspace_redaction_patterns(workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L208-L229) - *Effective display-redaction patterns for a workspace: the built-in*
- Function [`workspace_aggregate_ages(workspace_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L232-L246) - *Whether ages over 89 should be Safe-Harbor bucketed to "90+" on rendered*
- Function [`redact_table_rows(columns, rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py#L249-L278) - *Redact row-major rows (tuples/lists) by column for tabular DISPLAY.*

**Inputs & Outputs**:
- **Inputs**: Table rows, column headers, PII pattern rules, age aggregation flags.
- **Outputs**: Redacted row lists and anonymized preview tables.

**Failure Modes & Edge Cases**:
- Preserves original structure while replacing non-null values with redaction placeholders if pattern matches.

---
### [`polars_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L1-L722)

**Exact Purpose**: Generates standalone, executable Polars Python scripts for KPI computation using Polars DataFrames and LazyFrames.

**Key Functions & Classes**:

- Class [`PolarsGenerationResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L51-L58)
  - Method [`PolarsGenerationResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L57-L58)
- Class [`PolarsKPIGenerator`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L61-L697)
  - Method [`PolarsKPIGenerator.__init__(self, repo_root, workspace, dialect, catalog, schema)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L62-L75)
  - Method [`PolarsKPIGenerator.generate(self, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L77-L164)
  - Method [`PolarsKPIGenerator._grain_band_width(self, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L166-L184)
  - Method [`PolarsKPIGenerator._emit_script(self, kpi, kpi_id, intent, required_sources, source_aliases, base_source, relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L188-L342)
  - Method [`PolarsKPIGenerator._reader(self, alias, source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L344-L360)
  - Method [`PolarsKPIGenerator._needed_columns(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L362-L380)
  - Method [`PolarsKPIGenerator._dim_out_name(dim, band_width)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L383-L393)
  - Method [`PolarsKPIGenerator._result_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L395-L415)
  - Method [`PolarsKPIGenerator._share_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L417-L546)
  - Method [`PolarsKPIGenerator._ratio_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L548-L561)
  - Method [`PolarsKPIGenerator._derive_dim_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L563-L613)
  - Method [`PolarsKPIGenerator._filter_exprs(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L615-L639)
  - Method [`PolarsKPIGenerator._filter_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L641-L643)
  - Method [`PolarsKPIGenerator._group_exprs(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L645-L648)
  - Method [`PolarsKPIGenerator._dim_select_cols(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L650-L651)
  - Method [`PolarsKPIGenerator._metric_agg_expr(self, m)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L653-L673)
  - Method [`PolarsKPIGenerator._agg_expr(self, metric)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L675-L679)
  - Method [`PolarsKPIGenerator._profile_map(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L682-L691)
  - Method [`PolarsKPIGenerator._load_mapping(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L693-L697)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/polars_generator.py#L700-L718)

**Inputs & Outputs**:
- **Inputs**: Parsed KPI intent, feature mappings, dataset parquet paths.
- **Outputs**: `PolarsGenerationResult` with generated `.py` script content performing Polars aggregations, joins, and filters.

**Failure Modes & Edge Cases**:
- Raises `NotImplementedError` for complex window functions or SQL features not directly supported by Polars generator.

---
### [`proof_packet.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L1-L834)

**Exact Purpose**: Assembles comprehensive auditability proof packets combining SQL code, execution metrics, sample outputs, column lineage, and profile evidence.

**Key Functions & Classes**:

- Class [`KPIProofPacketResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L29-L41)
  - Method [`KPIProofPacketResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L40-L41)
- Class [`KPIProofPacketBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L44-L263)
  - Method [`KPIProofPacketBuilder.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L45-L64)
  - Method [`KPIProofPacketBuilder.run(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L66-L166)
  - Method [`KPIProofPacketBuilder._validate_workspace(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L168-L172)
  - Method [`KPIProofPacketBuilder._validation_summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L174-L184)
  - Method [`KPIProofPacketBuilder._kpi_cards(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L186-L221)
  - Method [`KPIProofPacketBuilder._kpi_card(self, kpi_id, registry_kpi, mapped_kpi, plan_kpi, relationships, execution_record, validation)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L223-L263)
- Function [`_mapping_row(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L266-L285)
- Function [`_first_source(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L288-L296)
- Function [`_mapping_label(feature, source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L299-L307)
- Function [`_dataset_label(source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L310-L316)
- Function [`_feature_recommendation(feature, source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L319-L325)
- Function [`_confidence_from_state(state)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L328-L333)
- Function [`_excel_traceability(registry_kpi, mapped_kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L336-L342)
- Function [`_trace_row(field, source_value, normalized_value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L345-L353)
- Function [`_reliability_gates()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L356-L382)
- Function [`_gate(name, passed, pass_detail, fail_detail)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L385-L390)
- Function [`_derivations_confirmed(mapping_rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L393-L395)
- Function [`_relationship_gate_passed(datasets, relationships, plan_kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L398-L413)
- Function [`_kpi_status(gates)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L416-L426)
- Function [`_recommendation_class(features, gates)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L429-L437)
- Function [`_planned_sql_shape(kpi_id, kpi, rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L440-L453)
- Function [`_sql_summary(sql_path, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L456-L461)
- Function [`_execution_summary(record)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L464-L474)
- Function [`_next_action(status, gates, workspace, domain, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L477-L491)
- Function [`_summary(kpis)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L494-L503)
- Function [`_packet_status(kpis, validation, artifacts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L506-L515)
- Function [`_next_commands(workspace, domain, packet_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L518-L528)
- Function [`_data_engineering_evidence()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L531-L586) - *Aggregate the data-engineering / pipeline track into one evidence block.*
- Function [`_artifact_state(path, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L589-L596)
- Function [`_packet_id(artifacts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L599-L604)
- Function [`_sha256(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L607-L610)
- Function [`_load_json(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L613-L620)
- Function [`_kpi_index(kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L623-L627)
- Function [`_render_markdown(packet)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L630-L659)
- Function [`_render_data_engineering_evidence(evidence)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L662-L701) - *Render the data-engineering / pipeline evidence section.*
- Function [`_summary_table(kpis)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L704-L715)
- Function [`_render_kpi_card(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L718-L787)
- Function [`_sample_rows(mapping_rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L790-L802)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L805-L809)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/proof_packet.py#L814-L830)

**Inputs & Outputs**:
- **Inputs**: KPI ID, execution records, feature mappings, verification records, dataset profiles.
- **Outputs**: `KPIProofPacketResult` and compiled markdown/json proof reports under `interns/reports/proof_packets/`.

**Failure Modes & Edge Cases**:
- Marks proof packet status as incomplete if execution records or lineage verification are missing.

---
### [`pyspark_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L1-L700)

**Exact Purpose**: Generates production PySpark Python scripts for large-scale distributed KPI computation on Databricks or Spark clusters.

**Key Functions & Classes**:

- Class [`PySparkGenerationResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L50-L57)
  - Method [`PySparkGenerationResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L56-L57)
- Class [`PySparkKPIGenerator`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L60-L675)
  - Method [`PySparkKPIGenerator.__init__(self, repo_root, workspace, dialect, catalog, schema, app_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L61-L76)
  - Method [`PySparkKPIGenerator.generate(self, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L78-L172)
  - Method [`PySparkKPIGenerator._emit_script(self, kpi, kpi_id, intent, required_sources, source_aliases, base_source, relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L176-L408)
  - Method [`PySparkKPIGenerator._reader(self, alias, source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L410-L415)
  - Method [`PySparkKPIGenerator._needed_columns(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L417-L427)
  - Method [`PySparkKPIGenerator._result_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L429-L444)
  - Method [`PySparkKPIGenerator._share_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L446-L520)
  - Method [`PySparkKPIGenerator._ratio_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L522-L537)
  - Method [`PySparkKPIGenerator._derive_dim_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L539-L587)
  - Method [`PySparkKPIGenerator._filter_lines(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L589-L611)
  - Method [`PySparkKPIGenerator._dim_out_name(dim, band_width)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L614-L624)
  - Method [`PySparkKPIGenerator._group_cols(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L626-L627)
  - Method [`PySparkKPIGenerator._dim_select_cols(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L629-L630)
  - Method [`PySparkKPIGenerator._metric_agg_expr(self, m)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L632-L653)
  - Method [`PySparkKPIGenerator._agg_expr(self, metric)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L655-L658)
  - Method [`PySparkKPIGenerator._profile_map(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L660-L669)
  - Method [`PySparkKPIGenerator._load_mapping(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L671-L675)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pyspark_generator.py#L678-L696)

**Inputs & Outputs**:
- **Inputs**: Parsed KPI intent, feature mappings, target Spark catalog/schema.
- **Outputs**: `PySparkGenerationResult` containing complete PySpark script with DataFrame API transformations.

**Failure Modes & Edge Cases**:
- Fails with error details if join conditions or window specifications cannot be translated to Spark PySpark syntax.

---
### [`registry_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/registry_loader.py#L1-L124)

**Exact Purpose**: Loads and normalizes KPI registry files from Excel (`.xlsx`), CSV (`.csv`), or JSON (`.json`) into standard dictionary structures.

**Key Functions & Classes**:

- Function [`_read_json(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/registry_loader.py#L16-L23)
- Function [`load_kpi_definitions(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/registry_loader.py#L26-L55) - *Return a dict keyed by `kpi_id` of normalized KPI definitions.*
- Function [`render_kpi_block(entry)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/registry_loader.py#L58-L121) - *Return markdown lines for a single KPI's definition + SQL + result table.*

**Inputs & Outputs**:
- **Inputs**: File path to raw KPI registry document.
- **Outputs**: Normalized list of KPI raw record dictionaries.

**Failure Modes & Edge Cases**:
- Raises `FileNotFoundError` or `ValueError` if file format is unsupported or file cannot be opened.

---
### [`result_view_builder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L1-L1898)

**Exact Purpose**: Parses natural language KPI text into structured `ParsedKPI` ASTs and generates DuckDB result view SQL statements.

**Key Functions & Classes**:

- Class [`WindowSpec`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L160-L169) - *Window OVER clause for an aggregation.*
- Class [`Aggregation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L173-L185)
- Class [`Dimension`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L189-L204) - *A GROUP BY column.*
- Class [`FilterClause`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L208-L212)
- Class [`ParsedKPI`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L216-L250)
  - Method [`ParsedKPI.can_compose(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L249-L250)
- Function [`_as_of_date()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L32-L44) - *The as-of date pinned into generated date arithmetic (ISO `YYYY-MM-DD`).*
- Function [`_prose_temporal_unit(name_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L83-L90) - *The period a question asks to break out by ('each quarter over time' ->*
- Function [`_date_column_from_lookup(lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L93-L105) - *A date-typed column already in the resolved feature set, by name hint.*
- Function [`_norm_alias(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L253-L255)
- Function [`_dimension_alias(column, cut_label)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L258-L278) - *Output alias for a plain dimension column.*
- Function [`_quote(value, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L281-L286)
- Function [`_column_lookup(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L289-L318) - *Map feature label → underlying column name via source_columns.*
- Function [`_features_view_column_lookup(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L321-L350) - *Map feature label -> the column name the SQL FEATURES VIEW exposes it*
- Function [`_resolve_column(name, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L353-L382) - *Resolve a KPI cut/term to an underlying column.*
- Function [`_norm(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L385-L387) - *Lowercase, strip everything but alphanumerics.*
- Function [`_emitted_columns(lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L390-L392) - *The set of physical columns the features view actually emits.*
- Function [`_measure_input_columns(metric_text, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L395-L418) - *Columns consumed as an aggregate's ARGUMENT in the metric, lowercased.*
- Function [`_drop_measure_inputs(dimensions, measure_inputs)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L421-L433) - *Dimensions minus any that is a bare reference to a measure-input column.*
- Function [`_dataset_token_index(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L436-L460) - *Map each feature's source-dataset stem to the column it resolves to.*
- Function [`_fuzzy_ratio(a, b)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L463-L466)
- Function [`_resolve_group_column(token, lookup, kpi, fallback_columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L469-L538) - *Resolve a "for <group>" partition token to a real emitted column.*
- Function [`_denom_is_within_scope(denominator_scope)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L541-L546) - *True when an explicit within-group denominator scope was chosen.*
- Function [`_detect_time_bucket(token)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L549-L563) - *Return (bucket_unit, source_column, alias) when token is a time bucket hint.*
- Function [`_detect_secondary_measure(name_text, lookup, primary_fn)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L581-L615) - *A second measure asked for in the question prose but missing from the*
- Function [`_parse_aggregation(text, lookup, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L618-L655)
- Function [`_split_cuts(cuts_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L658-L662)
- Function [`_parse_filter(token, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L665-L677)
- Function [`_detect_window_intent(metric_text, name_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L680-L713) - *Detect window-function intent from KPI text. Returns a dict describing*
- Function [`raw_date_input_columns(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L716-L750) - *Source columns the result view consumes as RAW dates, lowercased.*
- Function [`_detect_event_date_column(cuts_text, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L753-L773) - *Discover the KPI's event/service date column, generically.*
- Function [`_band_expr(base, band_width)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L776-L786) - *Band a continuous integer expression into fixed-width ranges.*
- Function [`_band_label_expr(base, band_width)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L789-L800) - *Readable ``lo-hi`` range label for a banded continuous value.*
- Function [`_detect_date_arithmetic(cuts_text, lookup, as_of_expr, band_width, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L803-L853) - *Detect age/date-arithmetic expressions in cuts text. Returns*
- Function [`_is_share_metric(metric_text, window_intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L856-L870) - *True when the metric's result is a share/percentage.*
- Function [`_detect_raw_continuous_cuts(cuts_text, lookup)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L873-L906) - *Find cut tokens that GROUP BY a raw exact continuous value.*
- Function [`_build_grain_bucketing_block(raw_cuts, metric_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L909-L945) - *Build the structured hard-block payload proposing age/range bands.*
- Function [`_band_width_from_decision(grain_bucketing)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L948-L972) - *Resolve the band width (in the cut's own unit) from a grain decision.*
- Function [`_detect_having(text, aggregations)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L975-L985) - *Detect HAVING clauses from KPI text. Returns a list of SQL fragments*
- Function [`parse_kpi(kpi, denominator_scope, grain_bucketing, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L988-L1010) - *Parse a KPI, then drop any dimension that is the metric's own input.*
- Function [`_parse_kpi_branches(kpi, denominator_scope, grain_bucketing, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L1013-L1644) - *Parse a KPI registry entry into structured aggregations/dimensions/filters.*
- Function [`_window_sql(window)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L1647-L1655)
- Function [`_agg_expr_no_alias(agg, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L1660-L1680) - *Render an aggregation's SQL expression WITHOUT the trailing ``AS alias``.*
- Function [`_agg_sql(agg, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L1683-L1684)
- Function [`_filter_sql(filt, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L1687-L1690)
- Function [`build_result_view_sql(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/result_view_builder.py#L1693-L1887) - *Compose the result-view SQL for a KPI. Always returns a valid CREATE VIEW.*

**Inputs & Outputs**:
- **Inputs**: KPI definition, denominator scope, grain bucketing decisions, target dialect.
- **Outputs**: `ParsedKPI` AST objects and executable result view SQL queries with CTEs, window functions, and aggregations.

**Failure Modes & Edge Cases**:
- Emits marked fallback SQL with TODO comments when ratio denominators or window grains are ambiguous.

---
### [`sensitive_masking.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sensitive_masking.py#L1-L137)

**Exact Purpose**: Centralized utility for identifying sensitive columns and returning dialect-specific SQL/Python masking expressions (`SHA256`, `NULL`, `***`).

**Key Functions & Classes**:

- Function [`load_sensitive_columns(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sensitive_masking.py#L26-L56) - *Lowercased names of columns marked sensitive in semantic_contract.json.*
- Function [`feature_sensitive_columns(feature, sensitive)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sensitive_masking.py#L59-L76) - *Source column names of ``feature`` that are sensitive.*
- Function [`is_feature_sensitive(feature, sensitive)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sensitive_masking.py#L79-L81) - *True if any of the feature's source columns (or its name) is sensitive.*
- Function [`mask_sql_expr(column_expr, dialect)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sensitive_masking.py#L84-L93) - *SHA-256 hex mask for a SQL column expression.*
- Function [`pyspark_mask_expr(column_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sensitive_masking.py#L96-L98) - *SHA-256 hex mask for a PySpark column (string form for F.expr/selectExpr).*
- Function [`polars_mask_helper_lines()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sensitive_masking.py#L105-L126) - *Source lines defining the Polars masking helper, emitted into the script.*

**Inputs & Outputs**:
- **Inputs**: Column name, sensitive column list, target dialect (`duckdb`, `polars`, `pyspark`).
- **Outputs**: Masked column expression string or Polars/PySpark transformation code.

**Failure Modes & Edge Cases**:
- Falls back to `NULL` assignment if dialect-specific hash function is unsupported.

---
### [`sql_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1-L1371)

**Exact Purpose**: Authoritative generator for production DuckDB SQL queries, building multi-table staging CTEs, relationships, derived formulas, and conformed KPI outputs.

**Key Functions & Classes**:

- Class [`SQLGenerationResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L53-L65)
  - Method [`SQLGenerationResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L59-L65)
- Class [`DuckDBKPISQLGenerator`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L68-L914)
  - Method [`DuckDBKPISQLGenerator.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L69-L87)
  - Method [`DuckDBKPISQLGenerator.generate(self, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L89-L275)
  - Method [`DuckDBKPISQLGenerator._resolve_run_source_mode(self, required_sources)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L277-L304)
  - Method [`DuckDBKPISQLGenerator._staging_with_delta(self, staging_sql, profile_map, required_sources)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L306-L374)
  - Method [`DuckDBKPISQLGenerator._delta_write_sql(self, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L376-L383)
  - Method [`DuckDBKPISQLGenerator._ingest_bronze_delta(self, profile_map, required_sources)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L385-L410)
  - Method [`DuckDBKPISQLGenerator._profile_map(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L412-L421)
  - Method [`DuckDBKPISQLGenerator._resource_transform_settings(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L423-L432)
  - Method [`DuckDBKPISQLGenerator._staging_views(self, profile_map, required_sources, required_columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L434-L478)
  - Method [`DuckDBKPISQLGenerator._staging_ctes(self, profile_map, required_sources, required_columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L480-L535)
  - Method [`DuckDBKPISQLGenerator._extract_result_select_body(self, result_view_sql, result_view_ident)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L537-L558)
  - Method [`DuckDBKPISQLGenerator._stage_select_list(self, rel_path, profile, required_columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L560-L571)
  - Method [`DuckDBKPISQLGenerator._required_source_columns(self, kpi, profile_map, relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L573-L628)
  - Method [`DuckDBKPISQLGenerator._kpi_source_from(self, kpi, profile_map, stage_views, relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L630-L717)
  - Method [`DuckDBKPISQLGenerator._derived_formula_refs(self, kpi, base_source, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L719-L730)
  - Method [`DuckDBKPISQLGenerator._choose_feature_ref(self, feature, base_source, all_refs, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L732-L744)
  - Method [`DuckDBKPISQLGenerator._feature_expression(self, feature, source_aliases, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L746-L810)
  - Method [`DuckDBKPISQLGenerator._qualified_column(self, column, source_aliases, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L812-L821)
  - Method [`DuckDBKPISQLGenerator._registry_description(self, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L823-L838)
  - Method [`DuckDBKPISQLGenerator._load_mapping(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L840-L844)
  - Method [`DuckDBKPISQLGenerator._result_view_sql(self, kpi, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L846-L879)
  - Method [`DuckDBKPISQLGenerator._pipeline_decisions(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L881-L898)
  - Method [`DuckDBKPISQLGenerator._pinned_base_source(self, kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L900-L905)
  - Method [`DuckDBKPISQLGenerator.quote_ident(self, value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L907-L910)
  - Method [`DuckDBKPISQLGenerator.table_ident(self, table)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L912-L914)
- Function [`quote_ident(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L917-L918)
- Function [`_feature_source_refs(feature, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L921-L930)
- Function [`_split_dataset_column(value, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L933-L942)
- Function [`_derived_formula(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L945-L963)
- Function [`_formula_inputs(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L982-L1009)
- Function [`_declared_formula_refs(feature, base_source, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1012-L1048) - *(dataset, column) refs for a derived formula's inputs.*
- Function [`_bare_formula_columns(feature)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1051-L1083) - *The subset of a derived formula's declared source_columns that need*
- Function [`_choose_base_source(refs, profile_map, relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1086-L1106)
- Function [`choose_feature_ref(feature, base_source, all_refs, profile_map, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1109-L1159) - *Pick the ONE source ref this feature resolves to, preferring the base.*
- Function [`plan_required_sources(kpi, profile_map, repo_root, relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1162-L1225) - *The canonical source plan for a KPI: (base_source, required_sources, refs).*
- Function [`_grain_dimensions(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1228-L1235) - *KPI cut tokens for grain-compatibility scoring (mirrors the planner's*
- Function [`_source_for_column(column, base_source, profile_map)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1238-L1258)
- Function [`_relationship_join_condition(relationship, left_alias, right_alias, generator)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1261-L1274)
- Function [`_schema(profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1277-L1281)
- Function [`_repo_path(value, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1284-L1294)
- Function [`_source_group(source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1297-L1304)
- Function [`_unique_preserve_order(values)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1307-L1314)
- Function [`_df_to_md(df)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1317-L1330)
- Function [`_norm(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1333-L1334)
- Function [`_safe_name(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1337-L1338)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1341-L1345)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/sql_generator.py#L1350-L1367)

**Inputs & Outputs**:
- **Inputs**: KPI registry, feature mapping contract, dataset profiles, relationship definitions.
- **Outputs**: `SQLGenerationResult` containing complete executable SQL text with staging CTEs and final result view.

**Failure Modes & Edge Cases**:
- Raises `ValueError` if required source tables cannot be joined or unmapped feature columns are encountered.

---
### [`text_parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L1-L151)

**Exact Purpose**: Provides NLP text extraction utilities for parsing metric keywords, cut dimensions, filter expressions, and SQL query blocks.

**Key Functions & Classes**:

- Function [`is_template_kpi_row(name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L37-L44)
- Function [`infer_metric_and_cuts(name, description)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L47-L61) - *Return (metric, cuts) inferred from the workspace lexicon, or empty.*
- Function [`first_existing(lowered, candidates)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L64-L68)
- Function [`first_index(index, candidates)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L71-L75)
- Function [`cell_at(row, idx)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L78-L81)
- Function [`clean_cell(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L84-L87)
- Function [`extract_kpis_from_sql(text, source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/text_parser.py#L90-L151)

**Inputs & Outputs**:
- **Inputs**: Raw text strings, SQL text scripts, header cell values.
- **Outputs**: Extracted metric names, cut lists, filter conditions, or parsed KPI dicts from raw SQL.

**Failure Modes & Edge Cases**:
- Returns empty extraction lists when text contains no recognized metric keywords or patterns.

---
### [`verify_kpi_output.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L1-L718)

**Exact Purpose**: Self-grill verification suite that executes generated KPI queries, checks non-emptiness, validates non-null metrics, audits cut coverage, and tests cross-engine parity.

**Key Functions & Classes**:

- Class [`VerifyRecord`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L63-L94)
  - Method [`VerifyRecord.ok(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L77-L78)
  - Method [`VerifyRecord.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L80-L94)
- Class [`VerifyResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L98-L116)
  - Method [`VerifyResult.ok(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L103-L104)
  - Method [`VerifyResult.summary(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L106-L116)
- Class [`KPIOutputVerifier`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L119-L653)
  - Method [`KPIOutputVerifier.__init__(self, repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L120-L132)
  - Method [`KPIOutputVerifier.verify(self, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L136-L144)
  - Method [`KPIOutputVerifier.verify_sql_text(self, kpi_id, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L146-L151)
  - Method [`KPIOutputVerifier._verify_one(self, sql_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L155-L167)
  - Method [`KPIOutputVerifier._check_intent(self, record, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L171-L204)
  - Method [`KPIOutputVerifier._check_grain_explosion(self, record, kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L208-L222)
  - Method [`KPIOutputVerifier._gloss_tokens(self, text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L241-L244)
  - Method [`KPIOutputVerifier._workspace_glosses(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L246-L277)
  - Method [`KPIOutputVerifier._check_semantic_gloss(self, record, kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L279-L306)
  - Method [`KPIOutputVerifier._check_cut_coverage(self, record, kpi, lowered_sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L308-L337)
  - Method [`KPIOutputVerifier._check_parser_filters(self, record, kpi, lowered_sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L339-L355)
  - Method [`KPIOutputVerifier._check_garbage_filters(self, record, kpi, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L357-L372)
  - Method [`KPIOutputVerifier._execute(self, record, sql)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L376-L449)
  - Method [`KPIOutputVerifier._cross_engine_check(self, record, kpi_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L453-L503)
  - Method [`KPIOutputVerifier._pyspark_parity(self, record, kpi_id, out_col, s_rows, s_sum)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L505-L567)
  - Method [`KPIOutputVerifier._output_column(self, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L569-L576)
  - Method [`KPIOutputVerifier._sql_rows_and_sum(self, sql, result_view, out_col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L578-L617)
  - Method [`KPIOutputVerifier._sql_files(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L621-L628)
  - Method [`KPIOutputVerifier._kpi_by_id(self)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L630-L646)
  - Method [`KPIOutputVerifier._write_artifacts(self, result)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L648-L653)
- Function [`_render_report(result)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L656-L683)
- Function [`_rel(path, root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L686-L690)
- Function [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/verify_kpi_output.py#L695-L714)

**Inputs & Outputs**:
- **Inputs**: KPI ID, generated SQL text, execution environment, verification thresholds.
- **Outputs**: `VerifyResult` and `VerifyRecord` detailing pass/fail status across intent alignment, execution sanity, and data quality.

**Failure Modes & Edge Cases**:
- Marks verification as failed if output view is empty, contains only placeholder columns, or violates intent filters.

---
### [`workbook_structure.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/workbook_structure.py#L1-L112)

**Exact Purpose**: Reads raw `.xlsx` workbooks using `openpyxl` to extract structural grid layout, cell values, and merged cell span coordinates.

**Key Functions & Classes**:

- Class [`WorkbookGrid`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/workbook_structure.py#L21-L33) - *A flattened sheet plus the merge structure that flattening would lose.*
- Function [`_first_sheet(wb)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/workbook_structure.py#L36-L50) - *Return the first sheet in document order.*
- Function [`read_merged_spans(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/workbook_structure.py#L53-L77) - *Return merged-cell ranges as (col_index, row_start, row_end), 0-based into*
- Function [`read_workbook_grid(path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/workbook_structure.py#L80-L109) - *Read an .xlsx into columns + dict rows + merged spans for the detector.*

**Inputs & Outputs**:
- **Inputs**: File path to `.xlsx` Excel workbook.
- **Outputs**: `WorkbookGrid` containing flattened rows, header columns, and 0-indexed merged cell ranges `(col_idx, row_start, row_end)`.

**Failure Modes & Edge Cases**:
- Returns empty merge spans list if `openpyxl` is missing or file is not a valid Excel zip container.

---
## Code Hygiene & Integrity Audit

### 1. Dead Code & Unused Public Symbols
- `blocker_question_panel.py`: several helper rendering functions (e.g. `_render_diff_summary`) are unreferenced outside unit tests.
- `polars_generator.py` & `pyspark_generator.py`: `generate(...)` signatures export standalone methods that are only invoked through `generate_kpi_engines.py` or fallback tests.
- `phi_review_panel.py`: `PHIReviewPanelBuilder` has candidate scoring methods that are currently superseded by `blocker_question_panel.py`'s unified question panel engine.

### 2. Unwired Components & Placeholder Logic
- **38 Explicit `placeholder` / `TODO` Markers**: Found across `blocker_question_panel.py`, `feature_resolver.py`, `generation_workflow.py`, `pii_redaction.py`, `result_view_builder.py`, `sql_generator.py`, and `verify_kpi_output.py`.
- **Placeholder Refusals**: `generation_workflow.py` explicit hard-stop at line 174 & line 720 preventing finalization of seed/placeholder KPI drafts (`"Refusing to finalize a placeholder-only KPI draft"`).
- **Synthetic Email/Identifier Placeholders**: `dbt_project_generator.py` line 1031 emits synthetic placeholder email addresses in generated metadata when client author details are missing.
- **Doomed-Stub Safeguards**: `sql_generator.py` line 184 & `verify_kpi_output.py` line 439 reject SQL views exposing only `placeholder readiness columns`.

### 3. Logic Duplication & Architectural Overlap
- **SQL Generation Dual Paths**: SQL query construction exists in both `result_view_builder.py` (`build_result_view_sql`) for basic single-table/window parsing and `sql_generator.py` (`DuckDBKPISQLGenerator`) for multi-table conformed staging CTEs. `dbt_project_generator.py` duplicates CTE creation logic when rendering dbt Jinja models.
- **Sensitive Data Masking**: `pii_redaction.py` (regex row-level redaction) and `sensitive_masking.py` (SQL/Polars/PySpark column expression masking) maintain separate sensitive column lookup routines.
- **Metric Intent Parsing**: `kpi_intent.py`, `result_view_builder.py`, and `text_parser.py` all perform independent keyword tokenization and regex matching for aggregation types (`SUM`, `AVG`, `COUNT DISTINCT`).

### 4. Broken References & Dependency Smells
- **Optional Library Soft Degrades**: `workbook_structure.py` gracefully degrades if `openpyxl` is missing, but `result_view_builder.py` and `sql_generator.py` require `duckdb` and `polars` without runtime guardrails in CLI subcommands.
- **PySpark Dependency Assumption**: `pyspark_generator.py` assumes `pyspark` package presence when importing PySpark SQL functions, which causes `ImportError` if run in standard local Python environments lacking PySpark.
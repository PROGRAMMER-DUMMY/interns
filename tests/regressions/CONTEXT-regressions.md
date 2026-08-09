# Tests Regressions Context: `tests/regressions`

This document provides an exhaustive reference for all regression test suites in `tests/regressions`.

---

## Executive Overview & Architectural Model

`tests/regressions` contains per-phase regression test suites locking down bug fixes implemented across the `core/` remediation plan (`docs/core_audit/REMEDIATION_PLAN.md`). Every test module follows the naming scheme `test_core_p<N>_<slug>.py` or `test_<feature_slug>.py` and is fully local-safe, synthetic, and domain-agnostic.

---

## File Details

### 1. [`README.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/README.md)

- **Exact Purpose**: Documents regression test naming conventions (`test_core_p<N>_<slug>.py`), rules (synthetic fixtures, local-safe execution, ASCII status markers), and discovery rules.

### 2. [`test_airflow_assets_and_ci.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_airflow_assets_and_ci.py)

- **Exact Purpose**: Verifies Airflow 3 asset definitions, container compose configs, and CI workflow constraints.
- **Key Functions / Classes**:
  - [`StageAssetTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_airflow_assets_and_ci.py#L33): Tests stage asset declarations.
  - [`AirflowContainerTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_airflow_assets_and_ci.py#L53): Verifies Docker Compose / Dockerfile Airflow 3 setup.
  - [`CiWorkflowTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_airflow_assets_and_ci.py#L86): Checks Slim CI manifest and warehouse isolation.

### 3. [`test_audit_chain_hmac_opt_in.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_audit_chain_hmac_opt_in.py)

- **Exact Purpose**: Validates audit chain HMAC signatures and plain SHA-256 backward compatibility.
- **Key Functions / Classes**:
  - [`NoKeyBackwardCompatTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_audit_chain_hmac_opt_in.py#L27): Plain SHA-256 fallback when no key is set.
  - [`HmacOptInTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_audit_chain_hmac_opt_in.py#L40): Verifies HMAC key round-trips and fail-closed validation on mismatched keys.

### 4. [`test_blocker_panel_fallback_confidence.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_blocker_panel_fallback_confidence.py)

- **Exact Purpose**: Tests fallback scorer calibration and recommended option ID assignment in blocker question panels.
- **Key Functions / Classes**:
  - [`FallbackScorerCalibrationTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_blocker_panel_fallback_confidence.py#L34): Validates confidence scoring behavior for text containment vs partial matches.
  - [`RecommendedOptionIdCalibrationTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_blocker_panel_fallback_confidence.py#L75): Verifies recommended option selection thresholds.

### 5. [`test_catalog_entry_injection_guard.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_catalog_entry_injection_guard.py)

- **Exact Purpose**: Validates prompt injection guards on catalog entry descriptions, titles, publishers, and tags.
- **Key Functions / Classes**:
  - [`CatalogEntryInjectionGuardTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_catalog_entry_injection_guard.py#L29): Verifies hostile string neutralization while preserving benign metadata.

### 6. [`test_column_profile_summary_carries_new_signals.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_column_profile_summary_carries_new_signals.py)

- **Exact Purpose**: Ensures column profile summaries correctly forward cardinality ratios and value patterns.
- **Key Functions / Classes**:
  - [`ColumnProfileSummaryNewSignalsTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_column_profile_summary_carries_new_signals.py#L13): Verifies profile summary dict forwarding.

### 7. [`test_contextual_score_new_signals.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_contextual_score_new_signals.py)

- **Exact Purpose**: Tests feature resolution scoring incorporating identifier bonuses, currency pattern boosts, and categorical filters.
- **Key Functions / Classes**:
  - [`ContextualScoreNewSignalsTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_contextual_score_new_signals.py#L10): Checks scoring bonuses and reason strings across signal types.

### 8. [`test_core_p1_pii.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p1_pii.py)

- **Exact Purpose**: Phase 1 remediation regression suite covering PII/PHI masking parity, log redaction, and backend fail-closed behavior.
- **Key Functions / Classes**:
  - [`MaskingParityTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p1_pii.py#L28): SQL/Databricks cross-engine SHA-256 mask identity.
  - [`DisplayRedactionTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p1_pii.py#L121): Log and terminal output string redaction.
  - [`SensitivityShapeUnificationTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p1_pii.py#L161): Unifies sensitivity classification objects.
  - [`PciSingleSourceTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p1_pii.py#L195): Cardholder data pattern checks.
  - [`PhiGateFreshnessTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p1_pii.py#L211): Freshness validation for PHI review gates.
  - [`BackendFailClosedTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p1_pii.py#L266): Ensures unvetted execution backends raise rather than leak data.

### 9. [`test_core_p2_gates.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p2_gates.py)

- **Exact Purpose**: Phase 2 remediation regression suite testing Genie-lane, remote target, external root allowlists, and SSRF egress gates.
- **Key Functions / Classes**:
  - [`GenieLaneGateTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p2_gates.py#L16): Natural language query authorization.
  - [`RemoteTargetGateTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p2_gates.py#L96): Remote execution approval gate (`AUTORESEARCH_ALLOW_REMOTE_EXECUTION`).
  - [`BootstrapNoNetworkTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p2_gates.py#L131): Local-only offline bootstrapping.
  - [`ExternalRootAllowlistTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p2_gates.py#L160): File access confinement to workspace roots.
  - [`SsrfEgressTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p2_gates.py#L186): Validates outbound HTTP URL filtering.

### 10. [`test_core_p3_injection.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p3_injection.py)

- **Exact Purpose**: Phase 3 remediation regression suite for SQL/PySpark/Delta code generation safety and formula guards.
- **Key Functions / Classes**:
  - [`SqlSafetyTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p3_injection.py#L14): Identifiers and literal string escaping.
  - [`WriteDeltaParameterizationTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p3_injection.py#L64): Delta table writing parameterization.
  - [`MedallionEmitterValidationTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p3_injection.py#L78): AST and syntax validation of emitted SQL.
  - [`FilterValueRenderingTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p3_injection.py#L133): Escaping of slicer/filter value expressions.
  - [`GeneratorFilterEmissionTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p3_injection.py#L167): Safe SQL generator filter clauses.
  - [`DerivedFormulaGuardTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p3_injection.py#L218): Allowlist validation on user-supplied formulas.

### 11. [`test_core_p4_concurrency.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p4_concurrency.py)

- **Exact Purpose**: Phase 4 remediation regression suite for atomic file I/O, process pushd context managers, and SQLite WAL locking.
- **Key Functions / Classes**:
  - [`AtomicIoTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p4_concurrency.py#L17): Atomic file writes and replace operations.
  - [`MemoryStoreDurabilityTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p4_concurrency.py#L47): Metadata store persistence across crashes.
  - [`PushdTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p4_concurrency.py#L68): Thread-safe working directory management.
  - [`SqliteConcurrencyTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p4_concurrency.py#L116): SQLite WAL mode multi-threaded write locking.

### 12. [`test_core_p5_correctness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p5_correctness.py)

- **Exact Purpose**: Phase 5 remediation regression suite for Databricks status enums, optimization convergence, and token matching.
- **Key Functions / Classes**:
  - [`DatabricksSuccessEnumTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p5_correctness.py#L12): Databricks API response state parsing.
  - [`OptimizationConvergenceTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p5_correctness.py#L35): Optimizer loop convergence guarantees.
  - [`SubstringTokenMatchingTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p5_correctness.py#L57): Substring vs full token matching.
  - [`ParityCoverageTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p5_correctness.py#L89): Gold/silver parity check coverage.

### 13. [`test_core_p6_silent_except.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p6_silent_except.py)

- **Exact Purpose**: Phase 6 remediation regression suite ensuring unreadable data quality files and document parsing errors fail loudly.
- **Key Functions / Classes**:
  - [`DataQualityUnreadableTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p6_silent_except.py#L14): Prevents silent swallowing of DQ file read errors.
  - [`DocumentsFailLoudTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_p6_silent_except.py#L39): Asserts doc parsing errors surface exceptions.

### 14. [`test_core_relationship_contract_no_clobber.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_relationship_contract_no_clobber.py)

- **Exact Purpose**: Ensures single-dataset profiling and onboarding does not overwrite or clobber existing relationship contracts.
- **Key Functions / Classes**:
  - [`SingleDatasetNoClobberTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_core_relationship_contract_no_clobber.py#L111): Merging strategy tests for relationship contracts.

### 15. [`test_dashboard_debug_host_guard.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_dashboard_debug_host_guard.py)

- **Exact Purpose**: Verifies dashboard debug web servers bind only to localhost (`127.0.0.1`) by default.
- **Key Functions / Classes**:
  - [`DebugHostGuardTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_dashboard_debug_host_guard.py#L21): Host binding security assertions.

### 16. [`test_discovery_reads_any_storage.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_discovery_reads_any_storage.py)

- **Exact Purpose**: Tests discovery across local paths, cloud URIs (s3/adls/gcs), and remote directory walking.
- **Key Functions / Classes**:
  - [`UriDetectionTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_discovery_reads_any_storage.py#L59): URI scheme parsing.
  - [`AllowlistTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_discovery_reads_any_storage.py#L82): Storage location allowlist enforcement.
  - [`RemoteWalkTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_discovery_reads_any_storage.py#L112): Directory traversal on remote storage targets.
  - [`EndToEndDiscoveryTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_discovery_reads_any_storage.py#L155): Full storage discovery pipeline.

### 17. [`test_execution_backend_isolated_wiring.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_execution_backend_isolated_wiring.py)

- **Exact Purpose**: Verifies execution backend instantiation operates in isolation without global state leakage.
- **Key Functions / Classes**:
  - [`IsolatedBackendWiringTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_execution_backend_isolated_wiring.py#L42): Isolated execution factory tests.

### 18. [`test_exports_refuse_unreviewed.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_exports_refuse_unreviewed.py)

- **Exact Purpose**: Tests that artifact export commands refuse unreviewed or unapproved data models.
- **Key Functions / Classes**:
  - [`ScreenerReviewStateTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_exports_refuse_unreviewed.py#L24): Review state checking.
  - [`ExportRefusalTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_exports_refuse_unreviewed.py#L69): Export command refusal behavior.

### 19. [`test_expression_formula_vocabulary_extraction.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_expression_formula_vocabulary_extraction.py)

- **Exact Purpose**: Tests token and column extraction from SQL/Polars expression formulas.
- **Key Functions / Classes**:
  - [`FormulaVocabularyExtractionTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_expression_formula_vocabulary_extraction.py#L62): Formula parsing and token isolation.

### 20. [`test_expression_schema_aware_allowlist.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_expression_schema_aware_allowlist.py)

- **Exact Purpose**: Validates schema-aware expression evaluation against allowed column and function lists.
- **Key Functions / Classes**:
  - [`SchemaAwareAllowlistTests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_expression_schema_aware_allowlist.py#L28): Expression allowlist validation.

### 21–60. Additional Test Modules

The directory contains 39 additional regression test modules covering financial corroboration, artifact timestamps, guard pipeline stages, JSON derived feature options, profiling, KPI scenarios, medallion correctness, LLM truncation, log redaction, security, Unity Catalog identity, and solution blueprints:
- [`test_financial_correctness_requires_corroboration.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_financial_correctness_requires_corroboration.py)
- [`test_generated_artifact_not_older_than_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_generated_artifact_not_older_than_generator.py)
- [`test_guards_are_pipeline_stages.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_guards_are_pipeline_stages.py)
- [`test_json_leaf_derived_feature_option.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_json_leaf_derived_feature_option.py)
- [`test_json_nested_leaf_profiling.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_json_nested_leaf_profiling.py)
- [`test_kpi_012_scenario_end_to_end.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_kpi_012_scenario_end_to_end.py)
- [`test_kpi_views_land_in_gold.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_kpi_views_land_in_gold.py)
- [`test_listing_ignores_platform_output.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_listing_ignores_platform_output.py) — F18: the workspace file classifier must not read `ingestion/`, `dbt/`, `context/` or `.databricks/` back in as `dataset_evidence`, and must keep counting real inputs. Also pins `PLATFORM_OUTPUT_DIRS` against `sync_code.CODE_DIRS`.
- [`test_lingering_q1_quickwins.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q1_quickwins.py)
- [`test_lingering_q2_medallion_correctness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q2_medallion_correctness.py)
- [`test_lingering_q3_relationship_gates.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q3_relationship_gates.py)
- [`test_lingering_q4_substring_matching.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q4_substring_matching.py)
- [`test_lingering_q5_concurrency.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q5_concurrency.py)
- [`test_lingering_q6_wiring_decisions.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q6_wiring_decisions.py)
- [`test_lingering_q7_cleanup.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q7_cleanup.py)
- [`test_lingering_q8_ops_ci.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_lingering_q8_ops_ci.py)
- [`test_llm_engine_truncation_signal.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_llm_engine_truncation_signal.py)
- [`test_log_redaction_third_party_coverage.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_log_redaction_third_party_coverage.py)
- [`test_measure_input_is_not_a_dimension.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_measure_input_is_not_a_dimension.py)
- [`test_panel_never_recommends_platform_output.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_panel_never_recommends_platform_output.py)
- [`test_phi_gate_nested_json_leaf.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_phi_gate_nested_json_leaf.py)
- [`test_pipeline_does_not_ingest_own_output.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_pipeline_does_not_ingest_own_output.py)
- [`test_pipeline_refuses_on_core_drift.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_pipeline_refuses_on_core_drift.py)
- [`test_pipeline_unexpected_status_is_nonzero.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_pipeline_unexpected_status_is_nonzero.py)
- [`test_profiler_cardinality_and_value_pattern.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_profiler_cardinality_and_value_pattern.py)
- [`test_profiler_tb_scale_csv_laziness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_profiler_tb_scale_csv_laziness.py)
- [`test_profiler_tb_scale_csv_nullcount.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_profiler_tb_scale_csv_nullcount.py)
- [`test_profiler_tb_scale_databricks_stats.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_profiler_tb_scale_databricks_stats.py)
- [`test_profiler_tb_scale_spark_quickprofile.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_profiler_tb_scale_spark_quickprofile.py)
- [`test_security_s1_dbt_execution_gate.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_security_s1_dbt_execution_gate.py)
- [`test_security_s2_concurrent_writes.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_security_s2_concurrent_writes.py)
- [`test_security_s3_blocker_panel_injection.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_security_s3_blocker_panel_injection.py)
- [`test_self_ingestion_guard_sees_uc_names.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_self_ingestion_guard_sees_uc_names.py)
- [`test_share_never_sums.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_share_never_sums.py)
- [`test_solution_blueprint.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_solution_blueprint.py)
- [`test_state_health_monitor.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_state_health_monitor.py)
- [`test_tool_index_coverage.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_tool_index_coverage.py)
- [`test_trajectory_recorder_incremental_summary.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_trajectory_recorder_incremental_summary.py)
- [`test_uc_fqn_table_identity.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_uc_fqn_table_identity.py)
- [`test_uc_intake.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_uc_intake.py)
- [`test_workspace_root_is_contained.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/test_workspace_root_is_contained.py)

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None. All test suites are active and discovered by pytest.
- 🔌 **Unwired Components**: None.
- 👯 **Logic & Code Duplication**: None. Synthetic test fixtures are isolated per regression domain.
- ⚠️ **Broken References & Mismatches**: None.

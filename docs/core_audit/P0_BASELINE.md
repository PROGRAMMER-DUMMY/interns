# P0 — Pre-fix test baseline

Captured at the start of remediation (branch `fix/core-p0-hygiene`, off `core-audit` = `main` + audit docs),
**before any source fix**, so later phases can prove they only *reduce* the failure set.

## Command
```
python -m pytest tests/ -q
```

## Result (baseline)
```
24 failed, 1552 passed, 2 skipped, 6 subtests passed in 180.82s
```

These 24 failures are **pre-existing** — they correspond to defects catalogued in the audit
(`SUMMARY.md` themes / per-unit `*.md`). P0 does **not** fix them; they are tracked here so any
*new* failure introduced by a later phase is immediately visible, and so each phase can show its
target tests moving red -> green.

## Failing tests at baseline (24)
- tests/test_data_model_image_parser.py::DataModelImageParserTests::test_parses_ocr_text_into_schema_candidates
- tests/test_external_source_discovery.py::ExternalSourceDiscoveryTests::test_discovers_external_groups_and_drafts_source_selection
- tests/test_kpi_pipeline_wrapper.py::PipelineWrapperTests::test_pipeline_main_relationship_gate_fires_for_candidate_relationships
- tests/test_kpi_proof_packet.py::KPIProofPacketTests::test_packet_includes_catalog_route_pipeline_and_layered_harness_evidence_when_present
- tests/test_medallion_design_naming.py::MedallionDesignNamingTests::test_logical_entity_strips_source_suffixes
- tests/test_medallion_design_naming.py::MedallionDesignNamingTests::test_source_system_from_path_prefers_hospital_token
- tests/test_metadata_store.py::MetadataStoreTests::test_build_metadata_store_defaults_to_delta
- tests/test_op_signals.py::SignalsToSkillsTests::test_stuck_routes_to_kpi_analyst_and_self_grill
- tests/test_pipeline_deployment_plan.py::PipelineDeploymentPlanTests::test_dry_run_success_writes_deployment_contracts
- tests/test_reliability_suite.py::ReliabilitySuiteTests::test_failed_data_quality_harness_artifact_blocks_suite
- tests/test_reliability_suite.py::ReliabilitySuiteTests::test_failed_pipeline_execution_harness_artifact_blocks_suite
- tests/test_reliability_suite.py::ReliabilitySuiteTests::test_malformed_pipeline_execution_harness_blocks_suite
- tests/test_reliability_suite.py::ReliabilitySuiteTests::test_missing_required_data_quality_harness_blocks_suite
- tests/test_reliability_suite.py::ReliabilitySuiteTests::test_missing_required_pipeline_execution_harness_blocks_suite
- tests/test_reliability_suite.py::ReliabilitySuiteTests::test_runs_local_safe_checks_and_skips_project_harness_without_artifacts
- tests/test_result_view_builder.py::test_mismatched_grain_percentage_now_emits_window_function_instead_of_fallback
- tests/test_result_view_builder.py::PreviewRowCapTests::test_flow_preview_uses_preview_row_cap_constant
- tests/test_session_snapshot.py::SessionSnapshotTests::test_cli_named_session_uses_alias_and_timestamped_folder
- tests/test_source_catalog.py::SourceCatalogTests::test_finalize_selection_infers_source_from_draft_or_workspace
- tests/test_workflow_guard_harness.py::WorkflowGuardHarnessTests::test_command_log_flags_panel_not_read_and_time_budget
- tests/test_workflow_guard_harness.py::WorkflowGuardHarnessTests::test_external_profile_workspace_blocks_kpi_generation_and_manual_selection_copy
- tests/test_workflow_guard_harness.py::WorkflowGuardHarnessTests::test_external_profile_workspace_blocks_route_without_tool_registry_read
- tests/test_workflow_guard_harness.py::WorkflowGuardHarnessTests::test_flags_generated_pipeline_sql_raw_path_outside_catalog_bootstrap
- tests/test_workflow_guard_harness.py::WorkflowGuardHarnessTests::test_flags_generated_sql_raw_path_outside_catalog_bootstrap

## Rough phase mapping (for triage, not binding)
- `result_view_builder`, `medallion_design_naming` -> **P5** (result correctness, T9/T3; note `prefers_hospital_token` also touches T12 agnosticism)
- `external_source_discovery`, `source_catalog`, `pipeline_deployment_plan` -> **P2** (gates/SSRF/external-root) and/or **P5**
- `kpi_pipeline_wrapper` relationship gate -> **P5** (free-text join gate)
- `metadata_store` defaults-to-delta -> **P4** (storage/metadata upsert) — may be env/backend-dependent
- `reliability_suite`, `workflow_guard_harness`, `op_signals` -> **P7** (harness/trajectory wiring) — largest cluster
- `data_model_image_parser`, `kpi_proof_packet`, `session_snapshot` -> triage during their owning phase

## P0 changes that touch code
- `.gitignore`: added `mlruns/` + `mlflow.db` (MLflow local artifacts).
- `core/onboarding/workspace/flow.py`: regenerate the artifact MANIFEST on `complete`
  (was never auto-refreshed; went stale at "Present 17/29"). Additive, non-fatal; no test moved.

P0 leaves the count unchanged (24 failed) by design — it is hygiene + safety-net only.

# Tests Architecture Context: `tests`

This document provides an exhaustive reference for the test suite in [`tests`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests).

---

## Executive Overview & Architectural Model

The `tests` directory contains pytest unit, integration, and benchmark suites covering core orchestration, execution backends, governance contracts, profiling algorithms, blocker panel flows, and dbt generators.

---

## Subdirectories & Context Maps

- [`fixtures/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/fixtures/CONTEXT-fixtures.md): Shared test datasets, sample contracts, and mock schemas. See [`fixtures/CONTEXT-fixtures.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/fixtures/CONTEXT-fixtures.md).
- [`regressions/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/CONTEXT-regressions.md): Regression test cases verifying resolved system bugs. See [`regressions/CONTEXT-regressions.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/CONTEXT-regressions.md).

---

## Key Test Modules

- [`test_workspace_flow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_workspace_flow.py): End-to-end integration tests for workspace intent execution and lifecycle stages.
- [`test_kpi_execution_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_kpi_execution_harness.py): Validates SQL execution accuracy against local DuckDB and Databricks.
- [`test_blocker_panel_renderer_previews.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_blocker_panel_renderer_previews.py): Verifies panel rendering and SQL preview generation.
- [`test_phi_gate.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_phi_gate.py): Tests PHI/PII detection and redaction rules.
- [`test_dbt_project_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_dbt_project_generator.py): Validates generation of dbt models, sources, and profiles.
- [`test_dbt_generator_hardening.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_dbt_generator_hardening.py): Emitted-code invariants on the generated dbt project text (`EmittedProjectInvariantTests` et al.) -- liquid clustering, incremental merge safety, WAP publish swap, `dbt parse` gate, and (this task) `profiles.yml` dev/prod targets on separate catalogs plus `dbt_project.yml`'s `require-dbt-version`.
- [`test_platform_readiness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_platform_readiness.py): Unit tests for platform readiness diagnostic tool.
- [`test_provision_apply.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_provision_apply.py): Confirmation refusal, kill switch, idempotent skips, structured failures. Includes `ExternalLocationOverlapTests` (URL-prefix vs name identity, F14) and `CatalogStorageRootTests` (`MANAGED LOCATION` reaches catalog creation and is recorded in the plan, F15).
- [`test_ingestion_run.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_ingestion_run.py): `run-ingestion`'s refusal ladder, statement splitting, stop-on-first-failure, and safe re-runs. `SdkSeamTests` pins the one path every other test mocks away — the SDK client's real name and constructor (F19).
- [`test_dbt_state_publish.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_dbt_state_publish.py): `core.orchestration.dbt_state.publish_state`/`state_download_command` — two `databricks fs cp` calls per `target/` artifact (timestamped + `latest`), no call at all when `target/` is missing, stop-at-first-failure, redacted stderr tail, injectable recording runner (never touches the network).
- [`test_deploy_gates.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_deploy_gates.py): The five deployment gates on synthetic fixtures, plus the fail-closed contract — an unlistable medallion runs directory (G1) and a non-numeric panel `open_count` (G2) must return a blocking `GateVerdict`, never raise, because a gate that raises has blocked nothing.
- [`test_salt_store.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_salt_store.py): `materialize_salt_if_missing`'s write path against a fake home directory — a corrupt `secrets.toml` is refused with the other workspace's bytes intact, and a readable one is extended rather than replaced. Never touches the real home or Databricks.
- [`test_airflow_health.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_airflow_health.py): `core.orchestration.airflow_health.check_airflow_health` against a fake, injectable `http` — paused DAG makes `ok: False`, healthy scheduler + unpaused DAG makes `ok: True`, unhealthy scheduler makes `ok: False` even when unpaused, a connection error reports `scheduler: "unreachable"` (never raises), a per-DAG lookup failure after a reachable scheduler folds into `paused_dags` rather than reading as healthy, and the JWT never appears anywhere but the `Authorization` header/is redacted out of any surfaced error text. `test_orchestration_hardening.py`'s `BackfillPoolTests` (same file's static-source-inspection style as `DbtStateWiringOrderTests`, since Airflow isn't installed here) pins that `airflow_dag.build_dag()` wires the backfill task with `pool="backfill"` and documents the one-time `setup_pools` bootstrap (`airflow pools set backfill 2 "bounded replay capacity"`) in the module header.

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
- [`test_platform_readiness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_platform_readiness.py): Unit tests for platform readiness diagnostic tool.
- [`test_provision_apply.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_provision_apply.py): Confirmation refusal, kill switch, idempotent skips, structured failures. Includes `ExternalLocationOverlapTests` (URL-prefix vs name identity, F14) and `CatalogStorageRootTests` (`MANAGED LOCATION` reaches catalog creation and is recorded in the plan, F15).
- [`test_ingestion_run.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_ingestion_run.py): `run-ingestion`'s refusal ladder, statement splitting, stop-on-first-failure, and safe re-runs. `SdkSeamTests` pins the one path every other test mocks away — the SDK client's real name and constructor (F19).

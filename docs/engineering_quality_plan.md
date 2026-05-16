# Engineering Quality Plan

Goal: raise the project from a fast-moving internal platform to an 8+/10 maintainable,
testable, failure-explicit optimization control plane without rewriting the architecture.

## Operating Rules

- Keep behavior-preserving refactors small enough to verify in one pass.
- Prefer extracting pure logic first, then service boundaries, then CLI/UI wrappers.
- Add focused tests for each extracted module before moving to the next area.
- Do not hand-edit generated workspace contracts to hide validation failures.
- Treat broad exception handling in core workflows as debt unless it is explicitly
  best-effort telemetry or UI rendering.
- Run focused checks after each implementation slice.

## Current Status

Completed:

- Extracted dashboard git/process/build command behavior into `core/dashboard_services.py`.
- Added dashboard service tests in `tests/test_dashboard_services.py`.
- Extracted KPI expression parsing into `core/onboarding/feature_expression.py`.
- Added expression parser tests in `tests/test_feature_expression.py`.
- Added artifact contract/version validation in `core/onboarding/artifact_contracts.py`.
- Wired artifact version checks into `core/onboarding/workspace_artifact_validator.py`.
- Extended enterprise tests for unsupported KPI feature mapping versions.
- Extracted KPI blocker prioritization and join-candidate inference into
  `core/onboarding/feature_blockers.py`.
- Added blocker prioritization tests in `tests/test_feature_blockers.py`.
- Extracted derived feature evidence construction into
  `core/onboarding/derived_feature_evidence.py`.
- Added derived feature evidence tests in `tests/test_derived_feature_evidence.py`.
- Extracted schema/profile indexing and structural alias matching into
  `core/onboarding/schema_alias_matching.py`.
- Added schema alias matching tests in `tests/test_schema_alias_matching.py`.
- Extracted workspace definition application into `core/onboarding/workspace_definitions.py`.
- Added workspace definition tests in `tests/test_workspace_definitions.py`.
- Extracted user decision application into `core/onboarding/user_decisions.py`.
- Added user decision tests in `tests/test_user_decisions.py`.
- Reduced legacy compatibility wrappers and dead code in
  `core/onboarding/kpi_feature_resolver.py`.
- Extracted KPI generation scoring, advisor notes, refinement merging, and seed KPI
  suggestions into `core/onboarding/kpi_generation_quality.py`.
- Added KPI generation quality tests in `tests/test_kpi_generation_quality.py`.
- Extracted medallion naming, source-system, natural-key, watermark, and relative-path
  helpers into `core/medallion/design_naming.py`.
- Added medallion design naming tests in `tests/test_medallion_design_naming.py`.
- Extracted workspace onboarding KPI text parsing helpers into
  `core/onboarding/kpi_text_parser.py`.
- Added KPI text parser tests in `tests/test_kpi_text_parser.py`.
- Extracted profiler utility helpers into `tools/profiler_utils.py`.
- Added profiler utility tests in `tests/test_profiler_utils.py`.
- Added shared structured failure contracts in `core/failures.py`.
- Added typed remote-execution-denied, remote-unavailable, validation-blocker,
  and internal-bug failure records in execution/orchestration paths.
- Converted KPI blocker validation failures and medallion design JSON validation
  failures into `WorkflowBlockedError` with structured `StructuredFailure` payloads.
- Made medallion assertion execution failures visible in assertion results instead
  of silently swallowing them.
- Added failure contract tests in `tests/test_failure_contracts.py`.
- Extended artifact contracts to major generated artifacts:
  `kpi_registry.json`, `domain_model.json`, `profile_index.json`,
  `relationship_contracts.json`, and `source_to_target_plan.json`.
- Added `artifact_type`, `version`, and `generated_by` metadata to onboarding,
  KPI resolver, blocker panel, workspace definition, relationship contract, and
  source-to-target plan writers.
- Wired downstream contract checks into source-to-target planning and relationship
  contract loading.
- Added focused tests for stale/missing contract metadata rejection.
- Split workflow CLI entry points into dedicated modules for KPI generation, data
  model generation, and KPI blocker workflows while preserving compatibility wrappers.
- Added focused CLI separation tests in `tests/test_workflow_cli_modules.py`.
- Added focused execution backend tests in `tests/test_execution_backend.py`.
- Added focused metadata store tests in `tests/test_metadata_store.py`.
- Added repo hygiene guidance in `docs/repo_hygiene.md`.

Verified:

```powershell
uv run python -m unittest tests.test_enterprise_optimization tests.test_dashboard_services tests.test_feature_expression
uv run python -m unittest tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run python -m unittest tests.test_derived_feature_evidence tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run python -m unittest tests.test_schema_alias_matching tests.test_derived_feature_evidence tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run python -m unittest tests.test_workspace_definitions tests.test_schema_alias_matching tests.test_derived_feature_evidence tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run python -m unittest tests.test_user_decisions tests.test_workspace_definitions tests.test_schema_alias_matching tests.test_derived_feature_evidence tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run ruff check core\dashboard_services.py core\onboarding\feature_expression.py core\onboarding\artifact_contracts.py core\onboarding\workspace_artifact_validator.py tests\test_dashboard_services.py tests\test_feature_expression.py tests\test_enterprise_optimization.py
uv run ruff check core\onboarding\feature_blockers.py core\onboarding\kpi_feature_resolver.py tests\test_feature_blockers.py
uv run ruff check core\onboarding\derived_feature_evidence.py core\onboarding\kpi_feature_resolver.py tests\test_derived_feature_evidence.py
uv run ruff check core\onboarding\schema_alias_matching.py core\onboarding\kpi_feature_resolver.py tests\test_schema_alias_matching.py
uv run ruff check core\onboarding\workspace_definitions.py core\onboarding\kpi_feature_resolver.py tests\test_workspace_definitions.py
uv run ruff check core\onboarding\user_decisions.py core\onboarding\kpi_feature_resolver.py tests\test_user_decisions.py
uv run python -m unittest tests.test_kpi_generation_quality tests.test_medallion_design_naming tests.test_kpi_text_parser tests.test_profiler_utils tests.test_user_decisions tests.test_workspace_definitions tests.test_schema_alias_matching tests.test_derived_feature_evidence tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run ruff check core\onboarding\kpi_feature_resolver.py core\onboarding\kpi_generation_quality.py core\onboarding\kpi_generation_workflow.py core\onboarding\kpi_text_parser.py core\onboarding\workspace_onboarding.py core\medallion\design_naming.py core\medallion\design.py tools\profiler_utils.py tools\profiler.py tests\test_kpi_generation_quality.py tests\test_medallion_design_naming.py tests\test_kpi_text_parser.py tests\test_profiler_utils.py
uv run python -m unittest tests.test_failure_contracts tests.test_kpi_generation_quality tests.test_medallion_design_naming tests.test_kpi_text_parser tests.test_profiler_utils tests.test_user_decisions tests.test_workspace_definitions tests.test_schema_alias_matching tests.test_derived_feature_evidence tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run ruff check core\failures.py core\execution\backend.py core\orchestration\loop.py core\onboarding\kpi_blocker_workflow.py core\medallion\design.py core\medallion\build.py tests\test_failure_contracts.py
uv run ruff check core\onboarding\artifact_contracts.py core\onboarding\workspace_artifact_validator.py core\onboarding\workspace_onboarding.py core\onboarding\kpi_feature_resolver.py core\onboarding\blocker_question_panel.py core\onboarding\workspace_definitions.py core\onboarding\relationship_contracts.py core\onboarding\source_to_target_planner.py tests\test_failure_contracts.py
uv run python -m unittest tests.test_workflow_cli_modules tests.test_execution_backend tests.test_metadata_store tests.test_failure_contracts tests.test_kpi_generation_quality tests.test_medallion_design_naming tests.test_kpi_text_parser tests.test_profiler_utils tests.test_user_decisions tests.test_workspace_definitions tests.test_schema_alias_matching tests.test_derived_feature_evidence tests.test_feature_blockers tests.test_feature_expression tests.test_dashboard_services tests.test_enterprise_optimization
uv run ruff check core\onboarding\kpi_generation_workflow.py core\onboarding\data_model_generation_workflow.py core\onboarding\kpi_blocker_workflow.py core\onboarding\kpi_generation_cli.py core\onboarding\data_model_generation_cli.py core\onboarding\kpi_blocker_cli.py tests\test_workflow_cli_modules.py tests\test_execution_backend.py tests\test_metadata_store.py
uv run python -m compileall core dashboard.py tests
uv run python -m compileall core tools tests dashboard.py
```

## Phase 1: Decompose High-Risk Files

Status: completed.

Targets:

- `dashboard.py`
- `core/onboarding/kpi_feature_resolver.py`
- `core/onboarding/kpi_generation_workflow.py`
- `core/onboarding/workspace_onboarding.py`
- `core/medallion/design.py`
- `tools/profiler.py`

Completed actions:

1. Extracted dashboard command/service behavior into a testable service module.
2. Split KPI resolver logic into focused modules for expression parsing, blocker
   prioritization, derived evidence, schema aliases, workspace definitions, and
   user decisions.
3. Split KPI generation quality logic, workspace onboarding text parsing, medallion
   naming logic, and profiler utility logic into importable helper modules.
4. Added focused tests for each extracted module.

Acceptance criteria:

- Extracted modules are importable without CLI side effects.
- Existing public functions keep backward-compatible imports where practical.
- Tests pass after each slice.

## Phase 2: Make Failures Explicit

Status: completed.

Targets:

- `core/orchestration/loop.py`
- `core/execution/backend.py`
- `core/onboarding/*`
- `core/medallion/build.py`
- `core/medallion/design.py`
- `dashboard.py`

Completed actions:

1. Classified failures as:
   - user input blocker
   - validation blocker
   - missing dependency
   - remote execution denied
   - internal bug
   - non-fatal telemetry/UI failure
2. Added `StructuredFailure` and `WorkflowBlockedError` as shared failure contracts.
3. Wired typed failure payloads into:
   - Databricks remote-denied and unavailable backend selection/execution.
   - Orchestration abort telemetry and run-log records.
   - KPI blocker panel validation.
   - Medallion design preflight/input validation.
   - Medallion assertion execution reporting.
4. Left best-effort handling only around telemetry, optional UI state, and availability probes.

Acceptance criteria:

- Core workflow failures surface as actionable errors or blocker artifacts.
- Validator errors remain blockers.
- Remote execution still requires explicit approval.

## Phase 3: Strengthen Artifact Contracts

Status: completed.

Completed actions:

1. Added explicit contract metadata to major generated artifacts:
   - `artifact_type`
   - `schema_version` or `version`
   - `generated_by`
2. Validated supported versions before downstream use through:
   - `WorkspaceArtifactValidator`
   - `SourceToTargetPlanner`
   - `load_relationship_contracts`
3. Added tests for unknown-version rejection and missing-contract rejection.

Acceptance criteria:

- Downstream tools reject unknown artifact versions.
- Regeneration command is named in validation errors.
- Existing generated artifacts continue to validate after regeneration.

## Phase 4: Separate CLI/UI From Services

Status: completed.

Completed targets:

- `core/onboarding/kpi_generation_workflow.py` now delegates CLI wrappers to
  `core/onboarding/kpi_generation_cli.py`.
- `core/onboarding/data_model_generation_workflow.py` now delegates CLI wrappers to
  `core/onboarding/data_model_generation_cli.py`.
- `core/onboarding/kpi_blocker_workflow.py` now delegates CLI wrappers to
  `core/onboarding/kpi_blocker_cli.py`.
- `core/medallion/design_cli.py` and `core/medallion/build_cli.py` already own CLI formatting.
- `dashboard.py` uses `core/dashboard_services.py` for extracted dashboard service behavior.

Target shape:

```text
parse args -> call service -> print summary -> exit code
```

Acceptance criteria:

- Service modules return structured results and do not print.
- CLI modules own formatting and process exit codes.
- Dashboard calls services, not workflow internals or shell commands where a service exists.

## Phase 5: Improve Test Structure

Status: completed.

Completed actions:

1. Added focused suites for failure contracts, CLI separation, execution backend,
   metadata store, dashboard services, KPI resolver primitives, schema aliases,
   user decisions, workspace definitions, derived evidence, KPI generation quality,
   KPI text parsing, profiler utils, and medallion naming.
2. Kept `tests/test_enterprise_optimization.py` as the broad integration suite so
   existing cross-workflow coverage remains stable.
3. Established the next safe split direction for future maintenance: continue moving
   onboarding, blocker panel, source-to-target, and medallion integration tests into
   dedicated modules without deleting integration coverage prematurely.

Acceptance criteria:

- Focused suites can be run independently.
- Contract tests are easy to find and extend.
- Integration tests remain stable.

## Phase 6: Repo Hygiene

Status: completed.

Completed actions:

1. Added `docs/repo_hygiene.md` with explicit staging boundaries.
2. Identified scratch probes, raw workspace inputs, ignored generated outputs, human-owned
   `config/lock.toml`, and deleted workspace docs as review-before-commit items.
3. Left scratch deletion, raw-data untracking, and workspace-doc deletion untouched because
   those require separate explicit approval.
4. Verification ran without staging generated workspace output, raw data, logs, local
   databases, or secrets.

Acceptance criteria:

- `git status --short` is understandable.
- Production modules, tests, docs, and scratch files are clearly separated.

## Standard Verification

Focused checks for each slice:

```powershell
uv run python -m compileall core interns tools tests dashboard.py
uv run python -m unittest tests.test_enterprise_optimization
uv run ruff check <changed-files>
```

Use broader tests when touching shared orchestration, execution, or generated artifact contracts.

## Next Implementation Step

All six phases are implemented. Next maintenance step:

1. Review `git status --short` and stage only intended production, test, and docs files.
2. Decide separately whether to untrack existing raw workspace inputs or remove scratch probes.
3. Keep using focused tests plus the broad enterprise integration suite before commits.

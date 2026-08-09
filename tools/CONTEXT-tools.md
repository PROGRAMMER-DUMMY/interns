# Tools Architecture Context: `tools`

This document provides an exhaustive reference for all components in [`tools`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools).

---

## Executive Overview & Architectural Model

The `tools` directory contains repo-level CLI utilities, inspection scripts, profiling tools, artifact auditors, git hygiene tools, token reporting utilities, and workspace selection helpers.

---

## File Details

### 1. [`artifact_inventory.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/artifact_inventory.py)

- **Exact Purpose**: Inventories generated artifacts across workspaces, verifying checksums and schema integrity.
- **Key Functions / Classes**:
  - [`inventory_workspace_artifacts(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/artifact_inventory.py#L30-L90): Scans `interns/` directories and outputs artifact inventory table.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory.
  - *Outputs*: Inventory report JSON and stdout summary.
- **Failure Modes & Edge Cases**:
  - Highlights missing or corrupt contract files.

### 2. [`context_status.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/context_status.py)

- **Exact Purpose**: Context-**size** estimator for active workspace sessions. Reports an upper-bound estimate, in bytes, of how much of the orchestrating LLM's chat budget workspace artifacts are likely consuming — panel JSONs, the recent trajectory tail, session state, manifest, and any skill bodies the active panel references. It cannot see the real chat context, which is CLI-private. **Unrelated to `CONTEXT-<folder>.md` documentation coverage**, despite the similar name.
- **Key Functions / Classes**:
  - [`ContextItem`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/context_status.py#L32) / [`ContextStatus`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/context_status.py#L48): Per-artifact and rolled-up size records.
  - [`estimate_context(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/context_status.py#L92): Builds the estimate from workspace state.
  - Helpers: [`_file_size`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/context_status.py#L72), [`_trajectory_tail_size`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/context_status.py#L79) (last 50 KB only), [`_skill_body_size`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/context_status.py#L85).
- **Inputs & Outputs**:
  - *Inputs*: Workspace layout and its current panel/session artifacts.
  - *Outputs*: `ContextStatus` with per-item byte estimates. Importable only.
- **Failure Modes & Edge Cases**:
  - **No `main()` and no CLI entry point** — running `python tools/context_status.py` prints nothing and exits 0. Import `estimate_context` instead.
  - Deliberately workspace-agnostic; no CLI name is hardcoded.

### 3. [`dashboard_export_common.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/dashboard_export_common.py)

- **Exact Purpose**: Shared helper functions for exporting dashboard representations to external targets (PowerBI, Tableau, Streamlit).
- **Key Functions / Classes**:
  - [`format_export_payload(spec, data)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/dashboard_export_common.py#L15-L50): Formats dashboard specification for exporter modules.
- **Inputs & Outputs**:
  - *Inputs*: Dashboard specification dict, dataset payload.
  - *Outputs*: Formatted export dictionary.
- **Failure Modes & Edge Cases**:
  - Handles missing chart types by mapping to standard bar/line fallbacks.

### 4. [`dashboard_verify.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/dashboard_verify.py)

- **Exact Purpose**: CLI utility verifying dashboard layout specification validity, card rendering parameters, and data binding rules.
- **Key Functions / Classes**:
  - [`verify_dashboard_spec(spec_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/dashboard_verify.py#L20-L75): Runs schema checks on dashboard layout specs.
- **Inputs & Outputs**:
  - *Inputs*: Path to dashboard JSON/YAML spec file.
  - *Outputs*: Verification status stdout.
- **Failure Modes & Edge Cases**:
  - Rejects specs referencing non-existent dataset columns.

### 5. [`databricks_setup.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/databricks_setup.py)

- **Exact Purpose**: CLI onboarding wizard setting up Databricks authentication, host connection, SQL warehouse selection, and enterprise scope setup.
- **Key Functions / Classes**:
  - [`configure_databricks_scope(scope_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/databricks_setup.py#L25-L80): Interactively configures `config/lock.toml`.
- **Inputs & Outputs**:
  - *Inputs*: User prompt inputs or environment flags.
  - *Outputs*: Configured credential lockfile.
- **Failure Modes & Edge Cases**:
  - Tests authentication before writing credentials.

### 6. [`dataops.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/dataops.py)

- **Exact Purpose**: DataOps vendor helper functions for CI/CD pipeline triggers and environment validation.
- **Key Functions / Classes**:
  - [`run_dataops_checks()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/dataops.py#L10-L40): Checks git branch policies and commit standards.
- **Inputs & Outputs**:
  - *Inputs*: Workspace git state.
  - *Outputs*: Boolean validation check result.
- **Failure Modes & Edge Cases**:
  - Non-blocking execution warnings on uncommitted dev state.

### 7. [`generate_skill_adapters.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/generate_skill_adapters.py)

- **Exact Purpose**: CLI wrapper script generating skill adapters from agent specifications.
- **Key Functions / Classes**:
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/generate_skill_adapters.py#L5-L15): Triggers `core.skills.adapter_generator`.
- **Inputs & Outputs**:
  - *Inputs*: CLI arguments.
  - *Outputs*: Generated adapter files.
- **Failure Modes & Edge Cases**:
  - Dispatches directly to core skill adapter generator.

### 8. [`git_hygiene.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/git_hygiene.py)

- **Exact Purpose**: Checks repository git hygiene, ensuring `interns/` directories and dataset outputs are excluded by `.gitignore`.
- **Key Functions / Classes**:
  - [`audit_git_hygiene(repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/git_hygiene.py#L15-L55): Scans tracked git files for accidentally committed generated output.
- **Inputs & Outputs**:
  - *Inputs*: Repo root directory.
  - *Outputs*: Hygiene compliance report.
- **Failure Modes & Edge Cases**:
  - Warns if untracked raw data files are staged.

### 9. [`list_workspace_files.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/list_workspace_files.py)

- **Exact Purpose**: Fast, bounded workspace file classifier listing file paths up to a cap and categorizing KPI registries, data models, datasets, and docs. Deliberately import-cheap — it runs on the workspace-selection path, which must answer in seconds.
- **Key Functions / Classes**:
  - [`list_workspace_files(workspace_dir, max_files)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/list_workspace_files.py): Lists up to 200 files bypassing gitignore for selection confirmation boundaries.
  - [`PLATFORM_OUTPUT_DIRS`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/list_workspace_files.py#L31-L41): `("ingestion", "dbt", "context", ".databricks")` — directories the platform WRITES inside a workspace. A pinned local copy of [`sync_code.CODE_DIRS`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/sync_code.py#L45) plus the Databricks CLI's sync metadata; kept local rather than imported to keep the selection path light, with `tests/regressions/test_listing_ignores_platform_output.py` guarding the two against drift.
  - [`_is_platform_output(file)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/list_workspace_files.py): True when a path sits inside a generated directory. Matches whole path **segments**, so a user dataset merely named like one (`datasets/context.csv`) still registers as real input.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, max files limit.
  - *Outputs*: Categorized file list JSON/stdout, with per-file `roles` (`dataset_evidence`, `kpi_input`, `data_model_input`, `context_doc`) and `reasons`.
- **Failure Modes & Edge Cases**:
  - Stops listing after 200 files or 30 seconds to prevent terminal locks.
  - `dataset_evidence` excludes `/docs/`, `interns/` (filtered upstream) and `PLATFORM_OUTPUT_DIRS`. Without that last exclusion the classifier reads the platform's own emissions back as source data, which trips `WS-BUG-001` at critical severity on any cloud-native workspace once `generate-ingestion` has run — hard-blocking `prepare-kpi-blocker-panel` and the whole KPI path. (F18)

### 10. [`methodology_parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/methodology_parser.py)

- **Exact Purpose**: Parses methodology notes, business guidelines, and domain documents into semantic rules.
- **Key Functions / Classes**:
  - [`parse_methodology_document(file_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/methodology_parser.py#L20-L70): Extracts metric definitions and rules from markdown/text.
- **Inputs & Outputs**:
  - *Inputs*: Path to documentation file.
  - *Outputs*: Semantic rule objects.
- **Failure Modes & Edge Cases**:
  - Returns unparsed text blocks when rule syntax is ambiguous.

### 11. [`optimizer_finder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/optimizer_finder.py)

- **Exact Purpose**: Scans codebase for available optimization strategies, playbooks, and execution modules.
- **Key Functions / Classes**:
  - [`find_optimizers(repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/optimizer_finder.py#L25-L80): Returns catalog of optimization entry points.
- **Inputs & Outputs**:
  - *Inputs*: Repository root.
  - *Outputs*: Optimizer registry dict.
- **Failure Modes & Edge Cases**:
  - Ignores broken or unimportable strategy modules.

### 12. [`profiler.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/profiler.py)

- **Exact Purpose**: CLI dataset profiler entry point for running data model profiling and table sampling.
- **Key Functions / Classes**:
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/profiler.py#L40-L120): Triggers profiling on target workspace dataset directory.
- **Inputs & Outputs**:
  - *Inputs*: `--workspace` argument.
  - *Outputs*: Profile JSON artifacts in `interns/generated/profiles/`.
- **Failure Modes & Edge Cases**:
  - Reports missing dataset folder clearly without crashing.

### 13. [`profiler_utils.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/profiler_utils.py)

- **Exact Purpose**: Helper formatting functions for dataset profiler output.
- **Key Functions / Classes**:
  - [`format_bytes(num_bytes)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/profiler_utils.py#L10-L25): Formats raw byte sizes to human readable strings.
- **Inputs & Outputs**:
  - *Inputs*: Integer byte value.
  - *Outputs*: String representation (e.g. `12.5 MB`).
- **Failure Modes & Edge Cases**:
  - Handles zero or negative numbers gracefully.

### 14. [`python_excel_libraries_reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/python_excel_libraries_reference.md)

- **Exact Purpose**: Documentation reference for Polars and openpyxl Excel parsing best practices.
- **Key Content**: Comparative guide for handling multi-sheet Excel workbooks.

### 15. [`session_snapshot.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/session_snapshot.py)

- **Exact Purpose**: Captures CLI session state snapshots, workspace status, and uncommitted changes for handoffs.
- **Key Functions / Classes**:
  - [`generate_session_snapshot(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/session_snapshot.py#L35-L110): Writes session snapshot report.
- **Inputs & Outputs**:
  - *Inputs*: Active workspace path.
  - *Outputs*: Markdown snapshot file.
- **Failure Modes & Edge Cases**:
  - Redacts sensitive environment variables automatically.

### 16. [`skill_excerpt.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/skill_excerpt.py)

- **Exact Purpose**: Extracts targeted excerpts from `TOOLS.md` or skill definitions on demand to avoid reading large files whole.
- **Key Functions / Classes**:
  - [`extract_skill_section(skill_name, file_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/skill_excerpt.py#L15-L50): Returns section text for specified command.
- **Inputs & Outputs**:
  - *Inputs*: Command name, markdown path.
  - *Outputs*: Section markdown text.
- **Failure Modes & Edge Cases**:
  - Returns clear notice if requested section heading is missing.

### 17. [`state_consolidator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/state_consolidator.py)

- **Exact Purpose**: Consolidates workspace state records into single unified database or JSON report.
- **Key Functions / Classes**:
  - [`consolidate_workspace_state(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/state_consolidator.py#L20-L65): Merges run logs and metadata store collections.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory.
  - *Outputs*: Consolidated state summary.
- **Failure Modes & Edge Cases**:
  - Resolves duplicate event entries using timestamps.

### 18. [`token_report.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/token_report.py)

- **Exact Purpose**: Analyzes LLM token consumption across runs and agent invocations.
- **Key Functions / Classes**:
  - [`generate_token_report(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/token_report.py#L25-L85): Summarizes token usage by model and agent role.
- **Inputs & Outputs**:
  - *Inputs*: Run log event files.
  - *Outputs*: Token cost report table.
- **Failure Modes & Edge Cases**:
  - Calculates estimated costs based on standard model pricing sheets.

### 19. [`workflow_state_tripwire.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workflow_state_tripwire.py)

- **Exact Purpose**: Tripwire checking for illegal workspace state transitions or missing prerequisite onboarding steps.
- **Key Functions / Classes**:
  - [`check_workflow_tripwires(workspace_dir, intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workflow_state_tripwire.py#L20-L65): Validates preconditions before running pipeline commands.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory and target intent.
  - *Outputs*: Tripwire status report (passed/blocked).
- **Failure Modes & Edge Cases**:
  - Blocks execution if required contracts or profile indices are missing.

### 20. [`workspace_dashboard.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_dashboard.py)

- **Exact Purpose**: Launches repo-local Dash/Streamlit dashboard UI for visualizing workspace state and KPI results.
- **Key Functions / Classes**:
  - [`launch_workspace_dashboard(workspace_dir, port)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_dashboard.py#L25-L85): Spins up local server process.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, port integer.
  - *Outputs*: Local web server process.
- **Failure Modes & Edge Cases**:
  - Chooses alternative open port if default port is occupied.

### 21. [`workspace_gc.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_gc.py)

- **Exact Purpose**: Garbage collector purging obsolete temporary files, dry-run caches, and stale runs from `interns/`.
- **Key Functions / Classes**:
  - [`run_workspace_gc(workspace_dir, max_age_days)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_gc.py#L20-L70): Safely cleans old temporary run logs.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory, age limit.
  - *Outputs*: Purged file count and reclaimed byte size.
- **Failure Modes & Edge Cases**:
  - Performs dry-run by default unless explicit confirm flag is supplied.

### 22. [`workspace_pdf.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_pdf.py)

- **Exact Purpose**: PDF report generator exporting KPI results and data model profiles to PDF documents.
- **Key Functions / Classes**:
  - [`export_workspace_pdf(workspace_dir, output_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_pdf.py#L15-L55): Renders printable PDF document.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory, target PDF path.
  - *Outputs*: Generated PDF file.
- **Failure Modes & Edge Cases**:
  - Falls back to HTML output if PDF rendering engine is missing.

### 23. [`workspace_pptx.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_pptx.py)

- **Exact Purpose**: PowerPoint presentation generator exporting workspace executive summaries to PPTX slides.
- **Key Functions / Classes**:
  - [`export_workspace_pptx(workspace_dir, output_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_pptx.py#L15-L55): Renders PPTX slide deck.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, output file path.
  - *Outputs*: `.pptx` presentation file.
- **Failure Modes & Edge Cases**:
  - Gracefully handles missing python-pptx library with informative error.

### 24. [`workspace_selection_harness.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_selection_harness.py)

- **Exact Purpose**: Test harness for validating workspace selection intent resolution and file listing rules.
- **Key Functions / Classes**:
  - [`run_selection_harness(input_intent)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/workspace_selection_harness.py#L15-L50): Tests workspace matcher logic against fuzzy user input.
- **Inputs & Outputs**:
  - *Inputs*: Query string (e.g. `set rcm data`).
  - *Outputs*: Target workspace match object.
- **Failure Modes & Edge Cases**:
  - Asks for clarification when fuzzy input matches multiple workspace directories.

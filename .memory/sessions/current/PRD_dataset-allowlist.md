**Feature Name**: dataset-allowlist

**Problem Statement**: The KPI resolution pipeline surfaces irrelevant physical column candidates from unapproved datasets (e.g., Hospital B and Claims) because the system lacks a way to restrict the scope of datasets being profiled and mapped.

**User Stories**:
- As a data engineer, I want to explicitly define an allowlist of dataset directories so that the resolution pipeline only surfaces relevant options and does not waste compute profiling unused files.
- As a workspace orchestrator, I want this allowlist to be persisted in the workspace configuration so that I don't have to manually pass `--dataset-filter` flags on every downstream command.
- As a business analyst, I want `list-workspace-files` to still show all physical files on disk so I know what data is available in the workspace, even if it is excluded from the current pipeline.

**Technical Constraints**:
- **Mechanism**: Backend CLI Update. The pipeline tools (profiler, feature resolver, blocker panel) must dynamically read the configuration and filter out non-allowlisted datasets.
- **Persistence**: Workspace Configuration. The allowlist should be stored persistently within the workspace configuration (e.g., `config/tasks.json` task object or a workspace-level `interns/state/workspace_settings.json`).
- **Format**: Directory Allowlist (e.g., `["datasets/EMR/trendytech-hospital-a"]`).
- **Boundary Scope**: Pipeline Only. `list-workspace-files` must bypass this filter.
- **Affected Files**: Likely `core/onboarding/kpi_feature_resolver.py`, `core/profiling/data_model_profiler.py`, configuration loaders.

**Success Criteria**:
- [ ] A configuration file or mechanism exists to store the dataset directory allowlist.
- [ ] Running `uv run prepare-kpi-blocker-panel` after configuring the allowlist only surfaces options from the allowlisted directories.
- [ ] Running `uv run list-workspace-files` still lists files from directories that are NOT in the allowlist.

**Out of Scope**:
- Hiding the files from the physical file system or `list-workspace-files` command.
- Regex or glob-based file filtering.
- Dynamic CLI flags for the allowlist (since we chose workspace persistence).
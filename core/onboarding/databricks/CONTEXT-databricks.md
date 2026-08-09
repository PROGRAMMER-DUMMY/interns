# Databricks Package Architecture Context: `core/onboarding/databricks`

This document provides an exhaustive, file-by-file architectural and technical reference for all components in [`core/onboarding/databricks`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks).

---

## Executive Overview & Architectural Model

The `databricks` package provides cloud-native onboarding, asset manifest generation, Genie workspace specification, deployment gate evaluation, Unity Catalog intake provisioning, and guarded workspace deployment for Databricks Unity Catalog environments.

It guarantees zero unauthorized remote mutation: all remote actions require explicit human approval, dry-run mode is the default, and remote execution checks (`AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` and G1-G5 deployment gates) are enforced prior to any live API call.

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 Workspace Assets & Inputs                                   │
│           (Profiles, Contracts, Requirements, Reports, Dashboard, Wiki, Blueprints)         │
└──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                         assets.py                                           │
│                              DatabricksAssetManifestBuilder                                 │
│  - Maps workspace dataset profiles to Unity Catalog target FQNs (catalog.schema.table)      │
│  - Hashes repo-generated workspace assets for drift detection                               │
│  - Emits databricks_asset_manifest.json                                                     │
└──────────────────────┬───────────────────────────────────────────────┬──────────────────────┘
                       │                                               │
                       ▼                                               ▼
┌─────────────────────────────────────────────┐ ┌─────────────────────────────────────────────┐
│             genie_workspace.py              │ │                uc_intake.py                 │
│         GenieWorkspaceSpecBuilder           │ │                 run_intake()                │
│  - Builds genie_workspace_spec.json &       │ │  - Provisioning of storage credentials,    │
│    genie_operator_runbook.md                │ │    external locations, catalogs, schemas,   │
│  - Generates starter prompts & role-action  │ │    volumes, and external tables             │
│    permissions matrix                       │ │  - Evaluates blueprint status & G5 gate     │
└──────────────────────┬──────────────────────┘ └──────────────────────┬──────────────────────┘
                       │                                               │
                       └──────────────────────┬────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     deploy_gates.py                                         │
│                                    run_deploy_gates()                                       │
│  - Evaluates 5 pure local deployment gates:                                                 │
│    G1 (local-green), G2 (design-ratified), G3 (human-provenance),                          │
│    G4 (plan-freshness), G5 (remote-approval)                                                │
└──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                  workspace_deployer.py                                      │
│                DatabricksWorkspaceDeploymentPlanner & Deployer / Medallion                  │
│  - Generates dry-run plans (databricks_workspace_deployment_plan.json)                     │
│  - Re-verifies G3 & G5 gates + PHI/PCI gates before remote execution                        │
│  - Deploys workspace folders, files, and UC evidence tables / Medallion actions             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/__init__.py)

- **Exact Purpose**: Exports public classes, dataclasses, protocols, and runners for Databricks asset manifest building, Genie workspace specification, deployment planning, and execution.
- **Key Functions / Classes**:
  - Exports [`DatabricksAssetManifestBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L47), [`DatabricksAssetManifestResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L18), [`GenieWorkspaceSpecBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L46), [`GenieWorkspaceSpecResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L20), [`DatabricksWorkspaceDeploymentPlanner`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L160), [`DatabricksWorkspaceDeploymentResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L73), [`DatabricksWorkspaceDeployer`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L448), [`DeploymentOperation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L47), [`WorkspaceApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L101), [`run_deployment`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L504).
- **Inputs & Outputs**:
  - *Inputs*: None.
  - *Outputs*: Module exports (`__all__`).
- **Failure Modes & Edge Cases**: None.

---

### 2. [`assets.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py)

- **Exact Purpose**: Builds governed Databricks asset registration manifests (`databricks_asset_manifest.json`) without moving raw dataset bytes. Computes SHA256 hashes for repo-generated workspace assets to enable drift detection.
- **Key Functions / Classes**:
  - [`DatabricksAssetManifestResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L18-L45): Result dataclass returning summary dict with required specialist routing.
  - [`DatabricksAssetManifestBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L47-L252): Scans `profile_index.json` to generate Unity Catalog target table FQNs (`catalog.schema.table`), collects workspace assets from `solutions`, `evaluation`, `contracts`, `requirements`, `reports`, `dashboard`, and `wiki`, computes SHA256 hashes, and emits the manifest.
  - [`_workspace_assets()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L211-L252): Gathers file paths across workspace subdirectories, computing SHA256 hashes and edit policies.
  - Helper functions: [`_safe_name`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L254-L255), [`_table_name`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L258-L264), [`_asset_type`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L267-L276), [`_edit_policy`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L279-L282), [`_file_hash`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L285-L290), [`_rel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L293-L297), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L301-L327).
- **Inputs & Outputs**:
  - *Inputs*: `profile_index.json`, environment (`dev`), domain (`rcm`), catalog (`dev`), schema.
  - *Outputs*: `databricks_asset_manifest.json` written under `interns/generated/requirements/`.
- **Failure Modes & Edge Cases**:
  - Missing `profile_index.json` results in an empty dataset assets list.
  - Name collisions among dataset stems trigger hierarchical path suffix resolution (`_table_name`).

---

### 3. [`deploy_gates.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py)

- **Exact Purpose**: Pure local implementation of the 5 Databricks deployment gates described in PRD section 7. Performs read-only evaluation of local artifacts and environment without making remote calls.
- **Key Functions / Classes**:
  - [`GateVerdict`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L37-L50): Immutable dataclass holding gate name, pass boolean, evidence dict, and blocking reason.
  - [`check_local_green(workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L61-L102): Gate G1: Checks latest medallion run (`run.json`) for failed tables, degraded runs, unequal KPI diffs, and checks `kpi_execution_harness.json`.
  - [`check_design_ratified(workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L104-L125): Gate G2: Ensures zero open items in `medallion_design_panel/current.json`.
  - [`check_human_provenance(confirmed_by)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L128-L142): Gate G3: Enforces Human-Gate Provenance Rule (refuses empty or agent-asserted approval names).
  - [`check_plan_freshness(workspace, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L144-L167): Gate G4: Verifies deploy plan `inputs_hash` matches current manifest on disk.
  - [`check_remote_approval()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L169-L183): Gate G5: Verifies `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` in environment.
  - [`run_deploy_gates(repo_root, workspace_rel, confirmed_by)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L185-L197): Evaluates all 5 gates in sequence without short-circuiting.
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L200-L215): CLI runner for Gate G5 remote execution check.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, repo root, `confirmed_by` string, local medallion runs, design panel, deploy plan, environment variables.
  - *Outputs*: List of `GateVerdict` objects.
- **Failure Modes & Edge Cases**:
  - Missing medallion run, unreadable `run.json`, or unpassed execution harness causes Gate G1 to fail.
  - Open items in design panel cause Gate G2 to fail.
  - Unset `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` causes Gate G5 to fail.

---

### 4. [`genie_workspace.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py)

- **Exact Purpose**: Generates local, reviewable Databricks Genie workspace specifications, operator runbooks, decision records, and evolution memory from the asset manifest without mutating Databricks.
- **Key Functions / Classes**:
  - [`GenieWorkspaceSpecResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L20-L43): Dataclass returning specification counts and file paths.
  - [`GenieWorkspaceSpecBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L46-L395): Reads or builds asset manifest, constructs `genie_workspace_spec.json`, renders `genie_operator_runbook.md`, records `genie_workspace_decisions.json`, and appends entries to `evolution.md` and `lessons.json`.
  - [`_build_spec(manifest)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L118-L290): Builds complete specification including workspace folder hierarchy, SQL assets, validation workflow jobs, governance dashboards, Genie space starter prompts, role-action permission matrix, non-Genie fallback rules, and drift detection configuration.
  - [`_runbook(spec)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L292-L325): Formats human-readable markdown runbook for Genie operators.
  - [`_accepted_decisions(spec)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L327-L343): Formats accepted decisions dict.
  - [`_record_evolution(spec)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L345-L388): Appends evolution memory and structured lessons.
  - [`_starter_prompts(manifest, target_tables)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L397-L425): Generates domain-tailored starter prompts for the Genie space.
  - Helper functions: [`_rel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L427-L431), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L435-L463).
- **Inputs & Outputs**:
  - *Inputs*: `databricks_asset_manifest.json` (or auto-built if missing), environment, domain, catalog, schema.
  - *Outputs*: `genie_workspace_spec.json`, `genie_operator_runbook.md`, `genie_workspace_decisions.json`, `evolution.md`, `lessons.json`.
- **Failure Modes & Edge Cases**:
  - Invalid workspace path raises `FileNotFoundError` or `ValueError`.
  - Non-Genie fallback is explicitly recorded to ensure API/CI agents can deploy assets independently.

---

### 5. [`uc_intake.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py)

- **Exact Purpose**: Provisions Unity Catalog governance objects (storage credential, external location, catalog, schemas, volumes, external tables) based on an approved solution blueprint (`solution_blueprint.json`).
- **Key Functions / Classes**:
  - [`IntakeOperation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L55-L64): Dataclass representing a planned or executed Unity Catalog provisioning step.
  - [`IntakeResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L67-L79): Result dataclass returning operation counts and status.
  - [`UnityCatalogApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L81-L98): Protocol interface defining required Unity Catalog operations (exists/create checks, plus `list_external_locations`). Also satisfies [`ProvisioningApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py#L56-L71), which `apply-provisioning` consumes.
  - [`SdkUnityCatalogApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L99): Production implementation wrapping `databricks-sdk` clients.
    - [`list_external_locations()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L127-L140): `name -> url` for every visible external location. Unity Catalog rejects a create whose URL overlaps an existing location **regardless of its name**, so callers need the URLs, not just the names. (F14)
    - [`create_catalog(name, storage_root="")`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L149-L157): `storage_root` is the catalog's `MANAGED LOCATION`. Omitted, Unity Catalog falls back to the metastore root — which Default Storage accounts do not have, and a rootless create is then rejected. (F15)
  - [`_plan_operations(payload, role_arn)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L171-L214): Derives deduplicated intake operations from blueprint groups and dispositions (`external_table`, `volume`, `managed_table`).
  - [`run_intake(repo_root, workspace, role_arn, apply, api)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L216-L296): Main entry point. Verifies blueprint approval status, evaluates Gate G5 (`check_remote_approval`), handles dry-run planning, executes operations idempotently when `--apply` is set, and writes `uc_intake/current.json` and `current.md`.
  - [`_execute(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L298-L322): Sequentially executes planned operations against `UnityCatalogApi`, halting on first failure.
  - [`_apply_one(op, api, catalog, role_arn)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L324-L355): Idempotently applies a single intake operation.
  - [`_render_markdown(report)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L357-L389): Formats markdown report table.
  - CLI runner: [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L399-L422).
- **Inputs & Outputs**:
  - *Inputs*: `solution_blueprint.json` (from `WorkspaceLayout`), AWS IAM `--role-arn`, `--apply` flag, `UnityCatalogApi` (or Databricks SDK client).
  - *Outputs*: `uc_intake/current.json` and `uc_intake/current.md`.
- **Failure Modes & Edge Cases**:
  - Unapproved blueprint raises `PermissionError`.
  - Unset `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` when `--apply` is set raises `PermissionError`.
  - Missing `--role-arn` when `--apply` is set raises `ValueError`.
  - `managed_table` dispositions are marked `requires_copy` and excluded from automatic intake execution.

---

### 6. [`workspace_deployer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py)

- **Exact Purpose**: Generates guarded deployment plans (`databricks_workspace_deployment_plan.json`), executes workspace asset deployments (folders, files, evidence tables), and manages Medallion Unity Catalog deployments from approved plans.
- **Key Functions / Classes**:
  - [`DeploymentOperation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L47-L70): Dataclass representing a workspace deployment operation.
  - [`DatabricksWorkspaceDeploymentResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L73-L98): Dataclass summarizing deployment plan/execution outcomes.
  - [`WorkspaceApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L101-L105): Protocol defining workspace mutation methods (`mkdirs`, `upload_file`, `ensure_schema`, `execute_sql`).
  - [`DatabricksWorkspaceApi`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L108-L157): SDK adapter handling folder creation, base64 file upload, volume uploads, and SQL execution.
  - [`DatabricksWorkspaceDeploymentPlanner`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L160-L445): Reads `genie_workspace_spec.json`, creates operations for folders, workspace files, UC evidence tables, jobs, dashboards, and Genie spaces, and writes deployment reports and central audit records (`state/databricks/deployments/`).
  - [`DatabricksWorkspaceDeployer`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L448-L501): Sequentially executes supported deployment operations via `WorkspaceApi`, stopping on first error.
  - [`run_deployment(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L504-L574): Main runner for Genie workspace deployment. Evaluates G1-G5 deployment gates, enforces PHI/PCI sensitive-data gate, verifies Databricks health, and executes operations when `--apply` and `--confirm-remote-mutation` are set.
  - [`_require_remote_approval(confirm_remote_mutation)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L577-L582): Verifies confirmation flag and `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`.
  - Medallion UC deployment section:
    - [`UnityCatalogAction`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L797-L820): Dataclass representing a Medallion UC action.
    - [`MedallionDeploymentOutcome`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L823-L850): Dataclass capturing medallion deploy results.
    - [`verify_deploy_approval(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L865-L944): Verifies `deploy_approval.json` freshness, human attribution, gate verdicts, and plan hash binding.
    - [`build_unity_catalog_actions(plan)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L946-L1066): Builds ordered SQL and volume upload actions for catalogs, schemas, volumes, raw source uploads, and tables.
    - [`deploy_medallion_from_approval(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L1177-L1260): Entry point for `medallion deploy`. Consumes approval artifact, re-verifies G4 and G5 gates, executes UC actions, and marks approval consumed.
    - CLI entry points: [`medallion_deploy_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L1263-L1314), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L1318-L1349).
- **Inputs & Outputs**:
  - *Inputs*: `genie_workspace_spec.json`, `deploy_approval.json`, `deploy_plan.json`, Databricks credentials (`DATABRICKS_HOST`, `DATABRICKS_TOKEN`), `--apply` & `--confirm-remote-mutation` flags, `--confirmed-by` string.
  - *Outputs*: `databricks_workspace_deployment_plan.json`, `databricks_workspace_deployment_apply.md` / `dry_run.md`, central deployment index, `deploy_execution.json` / `.md`.
- **Failure Modes & Edge Cases**:
  - Unset `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` or missing `--confirm-remote-mutation` raises `PermissionError`.
  - Failed Gate G3 (empty or non-human `--confirmed-by`) raises `WorkflowBlockedError`.
  - PHI/PCI policy failure raises `WorkflowBlockedError`.
  - Single operation failure halts execution loop immediately to maintain deterministic state.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - None identified. All helper functions (`_safe_name`, `_table_name`, `_file_hash`, `_sql_quote`, `_slug`) are used.
- 🔌 **Unwired Components**:
  - `uc_intake.py` (`run_intake` / `apply-uc-intake`) is a newly built cloud-native intake pipeline that is fully functional but not yet the default path for local workspaces.
- 👯 **Logic & Code Duplication**:
  - `_rel` is reimplemented in [`assets.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/assets.py#L293-L297), [`genie_workspace.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/genie_workspace.py#L427-L431), [`uc_intake.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/uc_intake.py#L392-L396), and [`workspace_deployer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L293-L297, L726-L730, L931-L935).
  - Remote execution approval checking is duplicated between [`deploy_gates.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L169-L183) (`check_remote_approval`) and [`workspace_deployer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py#L577-L582) (`_require_remote_approval`).
- ⚠️ **Broken References & Mismatches**:
  - In `deploy_gates.py` ([`check_plan_freshness`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py#L155-L158)), `_load_manifest` is imported dynamically inside function scope from `core.medallion.deploy_plan`. If `core.medallion.deploy_plan` is missing or refactored, G4 execution fails with an exception rather than returning a clean verdict.

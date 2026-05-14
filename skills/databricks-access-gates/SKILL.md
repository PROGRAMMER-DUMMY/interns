---
name: databricks-access-gates
description: Use when Databricks work hits or may hit missing permissions, token scopes, Unity Catalog grants, workspace API access, SQL warehouse paths, storage policies, compute policies, Genie/dashboard/job creation permissions, data registration approvals, or any remote mutation gate. Ask the user for the exact missing access or approval before retrying remote Databricks actions.
---

# Databricks Access Gates

## Purpose

Use this skill before or after Databricks remote operations that require access, policy, or approval. Its job is to convert failures such as missing scopes, missing catalogs, missing workspace API permissions, bad warehouse paths, or policy gaps into a clear user approval/access request.

## Gate Workflow

1. Classify the operation:
   - `read_only`: health checks, listing catalogs, listing warehouses, `SHOW` queries.
   - `workspace_mutation`: creating folders, uploading files, changing workspace assets.
   - `uc_mutation`: creating catalogs, schemas, tables, grants, external locations, volumes.
   - `compute_mutation`: creating jobs, clusters, policies, warehouses, pipelines.
   - `ai_asset_mutation`: creating Genie spaces, Vector Search indexes, model endpoints, dashboards.
   - `data_movement`: uploading/registering datasets, creating external tables, copying data.

2. Check required local gates:
   - Remote mutation must require explicit user approval.
   - In this repo, remote execution/mutation also requires `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`.
   - Never treat credentials being present as approval.
   - Never print token values or secrets.

3. If a failure or missing prerequisite is detected, stop and ask for the exact missing item. Include:
   - what succeeded
   - what failed
   - the minimum permission/scope/policy needed
   - whether a retry is safe after the user updates access

4. Do not retry destructive or mutating operations until the user confirms the missing access/policy was added.

## Common Databricks Access Gaps

Use this mapping when explaining failures:

- `Provided access token does not have required scopes: workspace`
  - Ask for a token/OAuth credential that includes Workspace API access.
  - Needed for creating workspace folders, importing notebooks/files, and modifying workspace assets.

- Catalog does not exist
  - Ask whether to use an existing catalog, create a new catalog, or change the manifest.
  - Creating catalogs often requires metastore admin permissions and storage-location policy.

- Schema create denied
  - Ask for `CREATE SCHEMA` on the target catalog or ask for an approved existing schema.

- Table create denied
  - Ask for `CREATE TABLE`/`USE SCHEMA` on the target schema, or an approved managed/external location.

- SQL endpoint/warehouse invalid
  - List available warehouses read-only, then ask the user to update `DATABRICKS_HTTP_PATH` or approve using the discovered warehouse.

- Workspace file upload denied
  - Ask for Workspace API scope and permission to write under the target `/Workspace/...` path.

- Job creation blocked
  - Ask for job creation permission plus compute policy/serverless policy decision.
  - Keep jobs spec-only until compute ownership is approved.

- Dashboard or Genie creation blocked
  - Ask for the relevant Databricks entitlement/API permission and team ownership decision.
  - Keep dashboards and Genie spaces spec-only unless the API and ownership policy are clear.

- Raw dataset registration blocked
  - Ask for data movement approval, storage location policy, table naming, and owner.
  - Do not upload/register raw data just because credentials are available.

## User Prompt Pattern

Use a short access request like:

```text
Databricks is connected, but this action is blocked by access/policy:
- Missing: <specific scope/grant/policy>
- Needed for: <operation>
- Already succeeded: <safe summary>
- Retry after: <exact thing user/admin should add>

Please add/approve <specific item>, then tell me to retry.
```

For multiple missing items, ask for the smallest next unblocker first unless the user explicitly wants a full enterprise checklist.

## Enterprise Defaults

Prefer these defaults for this repo:

- Git owns source code, policies, specs, and manifests.
- Databricks owns operational assets and production evidence.
- Genie is an interactive operator, not source of truth.
- API/CI fallback must remain possible without Genie.
- DuckDB/local runs are smoke tests only for enterprise workflows.
- Project audit stays under `workspaces/<project>/interns/`.
- Cross-workspace Databricks status belongs under `state/databricks/deployments/`.


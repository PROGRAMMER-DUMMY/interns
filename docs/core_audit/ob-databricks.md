# ob-databricks — audit

## Purpose
`core/onboarding/databricks/` owns the Databricks-first enterprise deploy contract. It generates the
governed deployment artifacts (asset manifest -> Genie workspace spec) and provides the dry-run-first
deployment boundary that may apply reviewed workspace folder/file imports and Unity Catalog (UC)
governance schema/evidence-table setup only with explicit remote approval. Jobs, dashboards, Genie
spaces, and raw-dataset registration deliberately stay spec-only. Two deployment lanes exist:

- **Genie workspace-spec lane** (`run_deployment`/`main`, CLI `deploy-databricks-workspace`): folders,
  workspace files, UC schema, and evidence tables from `genie_workspace_spec.json`.
- **Medallion UC lane** (`deploy_medallion_from_approval`/`medallion_deploy_main`, CLI `medallion deploy`):
  consumes `interns/state/medallion/deploy_approval.json`, re-verifies gates G4/G5 at execution time,
  and performs the catalog/schema/volume/table deployment described in the approved `deploy_plan.json`.

The five deploy gates (`deploy_gates.py`) are pure local-read checks evaluated by `medallion apply-deploy`
(in `core/medallion/`) to mint that approval artifact.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 32 | Public re-exports | builders, planner, deployer, `run_deployment`, `WorkspaceApi` |
| `assets.py` | 312 | Builds `databricks_asset_manifest.json`: datasets->UC FQNs, generated files->workspace paths, per-file sha256 drift hashes | `DatabricksAssetManifestBuilder`, `DatabricksAssetManifestResult`, `_table_name`, `_file_hash` |
| `deploy_gates.py` | 211 | Five pure local-read gates (G1 local-green, G2 design-ratified, G3 human-provenance, G4 plan-freshness, G5 remote-approval); never short-circuits | `GateVerdict`, `check_*`, `run_deploy_gates`, `REMOTE_ENV` |
| `genie_workspace.py` | 460 | Turns manifest into review-only spec + runbook + decisions + evolution memory; no Databricks mutation | `GenieWorkspaceSpecBuilder`, `GenieWorkspaceSpecResult`, `_starter_prompts` |
| `workspace_deployer.py` | 1316 | Dry-run-first planner + guarded apply for both lanes; central deployment record under `state/databricks/deployments/` | `DatabricksWorkspaceDeploymentPlanner`, `DatabricksWorkspaceDeployer`, `DatabricksWorkspaceApi`, `run_deployment`, `verify_deploy_approval`, `build_unity_catalog_actions`, `deploy_medallion_from_approval` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | workspace_deployer.py:509-535 | Genie lane `run_deployment(apply=True)` mutates Databricks (mkdirs, upload_file, CREATE SCHEMA, CREATE TABLE) but enforces ONLY `--confirm-remote-mutation` + `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` + PHI gate. It never calls `run_deploy_gates`, so G1 local-green, G2 design-ratified, and **G3 human-provenance are entirely bypassed** for this lane. A remote apply happens with no human-attributed approval — directly contrary to the Human-Gate Provenance Rule the medallion lane enforces. | Require a fresh human-attributed approval artifact (or call `run_deploy_gates` and reject on any failure incl. G3) before the genie-lane apply; mirror `verify_deploy_approval`. |
| [DEAD] | workspace_deployer.py:158-175 | `DatabricksWorkspaceApi.post_api` is defined but has zero callers anywhere in the repo (grep confirms). It is the only place that builds a raw `Authorization: Bearer <token>` request and embeds remote response `body` into a RuntimeError. | Remove, or wire it to job/dashboard creation if that was the intent; if kept, ensure error body cannot echo secrets. |
| [NOT-PROD] | workspace_deployer.py:148-156, 159 | `DatabricksWorkspaceApi.execute_sql`/`post_api` reach into `db_client._extract_warehouse_id()` and `db_client.cfg.token`/`.host` — coupling to private members of `DatabricksClient`. Refactors of the client silently break the deployer. | Expose a public `warehouse_id`/typed accessor on `DatabricksClient`; depend on the public surface only. |
| [NOT-PROD] | workspace_deployer.py:470-506 | `DatabricksWorkspaceDeployer.apply` runs operations sequentially, catches every exception per-op and continues. There is no rollback and no stop-on-first-failure option; a failed `CREATE SCHEMA` still lets dependent `CREATE TABLE` ops attempt and fail. Partial-deploy states are recorded but not reconciled. | Add fail-fast for ordering-dependent ops (schema before tables), or document/verify idempotent re-run as the recovery path. |
| [BUG] | workspace_deployer.py:483-486 | `deploy_workspace_file` resolves a non-absolute `operation["source"]` against `Path.cwd()`, but planner sources are repo-root-relative (`self.repo_root / source_path`). If cwd != repo_root at apply time, the wrong file (or none) is uploaded. | Resolve against `planner.repo_root` recorded in the plan, not `Path.cwd()`. |
| [MISSING] | workspace_deployer.py:509-550 | Genie lane has no consume-once / freshness binding: re-running apply re-uploads everything and re-runs SQL with no approval invalidation. Only the medallion lane stamps `consumed_at` (1092-1099). Mostly mitigated by `mkdirs`/`CREATE IF NOT EXISTS`/`overwrite=True` idempotency, but there is no drift re-check against `source_sha256` before upload. | Verify recorded `source_sha256` against the on-disk file before upload; record an apply receipt to prevent silent re-apply. |
| [NOT-PROD] | assets.py:272-277 | `_file_hash` reads every tracked file fully for sha256 with no size cap; on large generated artifacts this is unbounded I/O during manifest build. Minor, but manifest build runs on every onboarding. | Acceptable for governed artifacts; consider skipping/streaming files above a size threshold. |
| [INTEGRATION] | workspace_deployer.py:1153-1236 + apply_deploy.py:41-67 | Medallion lane is correctly wired: `medallion apply-deploy` mints the human-attributed approval (source=human, gate verdicts, plan hash), and `deploy_medallion_from_approval` refuses on missing/consumed/agent-asserted/stale approval and re-checks G4+G5 at execution time. This lane is production-shaped; the genie lane (above) is the gap. | None — this is the reference pattern the genie lane should adopt. |

## Cross-package coupling
- `core/storage/workspace_layout.WorkspaceLayout` — all artifact paths (profiles, requirements, reports, memory).
- `core/execution/databricks_client.DatabricksClient` — health check, schema create, warehouse id; deployer touches private `_extract_warehouse_id` and `cfg.token/host` (see [NOT-PROD]).
- `core/governance/phi_gate.enforce_remote_sensitive_gate` — PHI/PCI gate on BOTH live apply paths (good).
- `core/medallion/deploy_plan._load_manifest` — G4 hash recompute (deploy_gates) and the source of `deploy_plan.json`.
- `core/medallion/apply_deploy` — the only writer of `deploy_approval.json`; consumes `deploy_gates.run_deploy_gates`.
- `core/medallion/medallion_cli` — registers `medallion deploy -> workspace_deployer:medallion_deploy_main`.
- `core/config.load`, `core/failures.WorkflowBlockedError`, `core/paths.PROJECT_ROOT`.
- Entry points (`pyproject.toml`): `prepare-databricks-assets`, `prepare-genie-workspace`, `deploy-databricks-workspace`. CI references in `.github/workflows/ci.yml` and `core/dev/green_gate.py`. Tested by `tests/test_workspace_deployer.py`, `tests/test_deploy_gates.py`, `tests/test_enterprise_optimization.py`.

## Verdict
The **medallion UC lane is production-ready**: dry-run-first, refusal-as-feature, human-provenance via the
approval artifact, plan-hash binding, consume-once, and G4/G5 re-verified at execution time. The deploy
gates themselves are correct, pure, and non-short-circuiting. Drift detection (sha256 manifest comparison)
and secret hygiene are sound — tokens live only in the SDK client and the request header, never logged, and
no secret values are written to artifacts.

The **genie workspace-spec lane is NOT production-ready**: its `--apply` path performs real remote
mutation (folders, file uploads, schema + evidence-table creation) while bypassing the five-gate /
human-provenance contract entirely — it trusts only the env flag + confirm flag. That is the one finding
that must block sign-off. Secondary issues: a dead `post_api` (the only raw-token request builder), cwd-based
source resolution in file upload, coupling to private client members, and no fail-fast/rollback on the
sequential apply. Recommend gating the genie lane behind the same approval-artifact contract as the
medallion lane before any production use.

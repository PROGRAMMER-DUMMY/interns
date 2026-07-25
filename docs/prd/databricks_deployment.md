# PRD — Databricks Deployment of Workspace Artifacts

**Project:** autoresearch / interns platform
**Date:** 2026-06-12
**Status:** Implemented -- plan generation AND apply both shipped. `core/onboarding/databricks/
workspace_deployer.py`'s medallion-deploy path (`deploy_medallion_from_approval`) consumes
`deploy_approval.json` and performs the real Unity Catalog deployment behind gates G1-G5, per
section 9 of this PRD. This doc's G1-G5 gate design and deploy boundary are still the accurate
reference; only this status line was stale.
**Relationship to prior work:** Extends the 2026-05-11 "Databricks Full Integration"
PRD (ExecutionBackend / TelemetryBackend / MLflow). That PRD covers HOW the
platform talks to Databricks (auth, backends, telemetry); this one covers WHAT
gets deployed per workspace and the governance around deploying it. Naming
mandates and immutable constraints from the prior PRD carry over unchanged
(ExecutionBackend, TelemetryBackend, no MCP servers, lock.toml human-only).

---

## 1. Problem Statement

Every workspace now produces deployable artifacts locally: medallion layers
(Bronze/Silver/Gold Delta tables), per-KPI solutions (SQL / Polars / PySpark),
result views, and dashboards. They run locally under DuckDB with cross-engine
parity verified. Nothing maps them onto a shared Databricks workspace where a
team can schedule, query, and govern them. Deployment must be: deterministic
(same inputs -> same plan), gated (humans approve; agents only plan), incremental
(unchanged tables are not rebuilt remotely), and reversible.

## 2. Source of Truth

`uv run medallion plan-deploy --workspace <workspace>` emits the canonical,
validated deployment plan:

- `interns/generated/medallion/deploy_plan.json` (machine, `schema_version` 1)
- `interns/generated/medallion/deploy_plan.md` (human summary)

The plan is PLAN-ONLY. No remote call is made by plan-deploy; apply requires the
existing remote-approval gate (`AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`) plus the
human provenance requirements in section 7. Everything below describes what the
plan encodes; the generator (`core/medallion/deploy_plan.py`) is the
implementation contract, and this document is its rationale.

## 3. Unity Catalog Mapping (`unity_catalog` block)

One catalog per deployment environment, one schema set per workspace:

| Local artifact | UC object | Naming |
|---|---|---|
| Bronze Delta tables | `<catalog>.<ws>_bronze.<table>` | table names from the medallion manifest (`bronze.<entity>__<source_system>`) |
| Silver conformed tables | `<catalog>.<ws>_silver.<table>` | silver contract names |
| Gold KPI outputs | `<catalog>.<ws>_gold.<kpi_id>_gold` | one table per KPI, grain from the source-to-target plan |
| Run/ops metadata (refresh manifest, run state) | `<catalog>.<ws>_ops.*` | mirrors `interns/state/medallion/` |
| Raw source files (when re-ingest on Databricks is chosen) | UC Volume `<catalog>.<ws>_bronze.raw` | original relative dataset paths preserved |

`<ws>` is the workspace folder name, lowercased, non-alphanumerics collapsed to
`_` — the same `_safe_name` normalization the generators already use. The
catalog name is a deployment parameter, never hardcoded (workspace-agnostic
rule).

## 4. Orchestration (`orchestration` block)

- One Databricks Job per workspace: `medallion_refresh_<ws>`, tasks ordered
  Bronze ingest -> Silver conform -> Gold per-KPI, mirroring local
  `medallion build` task order from the manifest.
- **Incremental semantics are identical to local**: the job consults the
  refresh manifest (mirrored to `<ws>_ops.refresh_manifest`) and skips tasks
  whose input fingerprints are unchanged — the same
  fingerprint = (source content, emitted SQL, upstream fingerprints) triple
  computed by `core/medallion/incremental.py`. `--force` maps to a job
  parameter `force_rebuild=true`.
- Schedule: none by default. Plans emit `schedule.type: "manual"` until a human
  sets one; agents must not invent cron schedules for cost reasons.
- Engine: Lakeflow Jobs (serverless job compute by default; see section 6).

## 5. Permissions and PHI (`permissions` block)

- The plan carries the workspace's PHI tier from the local PHI gate
  (`core/governance/phi_gate.py`). A workspace classified `phi` requires:
  - Gold tables granted to the analyst group only via dynamic views applying
    the same redactions `core/onboarding/kpi/pii_redaction.py` applies locally
    (sensitive columns from `semantic_contract.json`); base Silver/Bronze
    grants restricted to the pipeline service principal.
  - No table in the plan may widen access beyond what the local artifact
    exposes: if a column is redacted in local result views it must be redacted
    in the UC view. The plan validator fails (`valid: false`) on any uncovered
    sensitive column (`permissions.phi.target_covered`).
- Run-as: a per-workspace service principal owns the job; humans get
  read-only on `<ws>_gold` by default. CREATE/MODIFY on the schemas is held by
  the deployment role only.

## 6. Cost Guardrails (`cost_guardrails` block)

- Default compute: **serverless**. Classic clusters only when serverless is
  unavailable, under a cluster policy capping `max_workers: 2`,
  `autotermination_minutes: 15`, smallest node type.
- `job_timeout_seconds: 3600` per refresh; a refresh that cannot finish in an
  hour on these datasets indicates a defect, not a scaling need.
- Monitoring hooks: query-history scope (per `config/databricks_scopes.json`)
  is polled by the existing telemetry path; the plan records the query tags
  (`workspace`, `kpi_id`, `run_id`) every emitted statement must carry so cost
  attribution is per-KPI.

## 7. Deployment Gates (`deployment_gates` block)

A plan may be APPLIED only when all of the following hold, in order:

1. **Local green**: `medallion build` exits 0 with `tables_failed=0`; all
   Silver assertions pass; KPI row-equality diff has 0 unequal KPIs; the
   workspace execution harness passes; `green-gate` is green.
2. **Design ratified**: zero unconfirmed medallion design-panel decisions
   (no `--force-with-blockers` deployments, ever).
3. **Human provenance**: the apply command requires `--confirmed-by <name>`;
   an empty value records `source: agent` and the gate REFUSES to apply.
   This is the same provenance rule as relationship/review gates (BUG-014
   residual) extended to deployment.
4. **Plan freshness**: `source_manifest.inputs_hash` recorded in the plan must
   match a re-hash at apply time; a stale plan must be regenerated, not
   applied.
5. **Remote approval**: `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` set by the
   human in the executing shell — never by an agent, never persisted to a
   config file.

## 8. Rollback (`rollback` block)

- Strategy: Delta time travel + versioned artifact sets.
  - `RESTORE TABLE <fqn> TO VERSION AS OF <pre-deploy version>` for each
    affected Gold/Silver table (versions recorded in the plan at apply time).
  - Re-point the job to the previous artifact set: every apply records the git
    SHA of the repo state that produced the artifacts; rollback re-deploys the
    prior SHA's plan.
  - Local verification after rollback: re-run the execution harness against
    the restored tables via the Databricks ExecutionBackend (read-only).
- Bronze raw volumes are append-only; rollback never deletes source data.

## 9. Out of Scope (this PRD)

- ~~Live `plan-apply` implementation~~ SHIPPED 2026-06-12 up to the approval
  boundary: `core/onboarding/databricks/deploy_gates.py` implements the five
  gates from section 7; `medallion apply-deploy --workspace <ws>
  --confirmed-by <name> [--dry-run]` evaluates them, prints a verdict table,
  and on all-green records `interns/state/medallion/deploy_approval.json`
  (gate evidence + provenance + plan hash) — and STOPS. The actual workspace
  mutation (workspace_deployer consuming the approval artifact) is the next
  slice; it must refuse to run without a fresh approval artifact.
- MLflow experiment/telemetry deployment — already covered by the 2026-05-11
  PRD.
- Dashboard hosting (Lakeview) — deferred until Gold tables are live.

## 10. Known Limitations / Follow-ups

- ~~`permissions.phi.datasets` currently records absolute local paths~~ FIXED
  2026-06-12: paths are repo-relative and the plan validator rejects absolute
  paths.
- The refresh-manifest fingerprint helper is intentionally local to
  `core/medallion/incremental.py`; onboarding's
  `core/onboarding/workspace/incremental.py` grew a sibling implementation the
  same day. Unify into one shared helper when one of them next changes
  behavior (recorded duplication, not drift).
- Hostile_Synthetic (57 tables) is the scale test for plan generation; RCM is
  the correctness test. Run plan-deploy on both before implementing apply.

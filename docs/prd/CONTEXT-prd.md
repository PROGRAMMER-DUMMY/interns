# PRD Architecture Context: `docs/prd`

This document provides an exhaustive reference for all components in [`docs/prd`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd).

---

## Executive Overview & Architectural Model

The `docs/prd` directory contains Product Requirement Documents (PRDs) governing platform-wide features, integrations, and deployment lifecycles.

```
┌─────────────────────────────────────────────────────────────┐
│             Local Workspace Artifacts (DuckDB)              │
│       Bronze / Silver / Gold Tables & Solution View         │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼  uv run medallion plan-deploy
┌─────────────────────────────────────────────────────────────┐
│                 deploy_plan.json / deploy_plan.md           │
│                    (Machine & Human Plan)                   │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼  G1 - G5 Deployment Gates
┌─────────────────────────────────────────────────────────────┐
│         Unity Catalog Deployment & Orchestration Job         │
│   <catalog>.<ws>_bronze | <catalog>.<ws>_silver | _gold      │
└─────────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`databricks_deployment.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md)

- **Exact Purpose**: Defines the product requirement specification for deploying local workspace artifacts (Bronze/Silver/Gold Delta tables, per-KPI views, dashboards) onto a shared Databricks workspace and Unity Catalog governance environment.
- **Key Sections & Content**:
  - [`Problem Statement & Goals`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L19-L28): Solves mapping local DuckDB artifacts into Databricks deterministically, incrementally, with human gating, and reversibly.
  - [`Source of Truth`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L29-L42): `uv run medallion plan-deploy` emits `deploy_plan.json` (version 1) and `deploy_plan.md`. Generator is [`core/medallion/deploy_plan.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/deploy_plan.py).
  - [`Unity Catalog Mapping`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L43-L59): Maps local artifacts to UC objects (`<catalog>.<ws>_bronze`, `<ws>_silver`, `<ws>_gold`, `<ws>_ops`).
  - [`Orchestration`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L60-L74): Single Databricks Job `medallion_refresh_<ws>` with incremental execution mirroring local manifest fingerprints.
  - [`Permissions & PHI Guardrails`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L75-L91): Enforces redaction matching [`core/onboarding/kpi/pii_redaction.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/pii_redaction.py) via dynamic views.
  - [`Cost Guardrails`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L92-L103): Serverless compute defaults, cluster policies, and `job_timeout_seconds: 3600`.
  - [`Deployment Gates G1-G5`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L104-L122): Five mandatory gates (Local green, Design ratified, Human provenance `--confirmed-by`, Plan freshness, Remote approval `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`). Implemented in [`core/onboarding/databricks/deploy_gates.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/deploy_gates.py).
  - [`Rollback Strategy`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L123-L135): Uses Delta time travel (`RESTORE TABLE ... TO VERSION AS OF`) and SHA artifact locking.
  - [`Status & Implementation Scope`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/databricks_deployment.md#L5-L9): Shipped via [`core/onboarding/databricks/workspace_deployer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/workspace_deployer.py).
- **Inputs & Outputs**:
  - *Inputs*: Local medallion tables, KPI SQL solutions, workspace contracts, human confirmation parameters.
  - *Outputs*: Unity Catalog Delta tables, Databricks Job definitions, `deploy_plan.json`, `deploy_approval.json`.
- **Failure Modes & Edge Cases**:
  - *Stale Plan*: Re-hashing inputs at apply time rejects stale deployment plans.
  - *Agent Provenance Violation*: Missing `--confirmed-by <human_name>` triggers gate refusal.
  - *Uncovered PHI*: Fails validation (`valid: false`) if any sensitive column in local result views lacks UC dynamic view coverage.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**: None. G1-G5 gates and medallion deployer are wired into `medallion apply-deploy` CLI commands.
- 👯 **Duplication & Overlap**: Section 10 notes refresh-manifest fingerprint helper duplication between `core/medallion/incremental.py` and `core/onboarding/workspace/incremental.py`.
- ⚠️ **Mismatches & Risks**: `index.md` was expected in `docs/prd/` per legacy documentation trees, but `databricks_deployment.md` is the primary PRD in this directory.

# Production Roadmap — autoresearch KPI/Data Platform

**Status:** PARKED pending local validation. The user is validating the local
end-to-end flow first; enterprise work begins only after the local flow works as
intended. This is the living plan for that next stage — update it as state changes.

**Target:** an enterprise-level, multi-team/multi-org production product.
**Today:** a deep single-user CLI + Dash platform with strong KPI/data-engineering
logic and partial Databricks integration — but none of the enterprise/multi-tenant
foundation yet.

---

## Current state (verified 2026-05-30)

Working / committed:
- Multi-runtime KPI engine (SQL / Polars / PySpark) with parity, governed generation,
  panel contracts, reliability harnesses (as modules), routing, handoff.
- Green gate: curated CI suite (99) + enterprise suite (78) passing locally.
- Cross-CLI guardrails (uv-run-for-tests, secret-display), `.env` loader (`with-env`),
  MCP scaffold, config index + local-config docs, Databricks scope reference + tooling.
- Databricks: SDK client, warehouse/jobs execution backends, UC schema/table writes,
  MLflow; `config/lock.toml` wired (healthdata/default, warehouse mode).

Scan findings (the gaps):
- No service/API layer (FastAPI/Flask/server) — CLI + Dash only.
- No auth / RBAC / tenancy modules — "multi-tenant" = folders under `workspaces/`.
- Secrets = `.env` only; one shared Databricks PAT.
- CI exists (`.github/workflows/ci.yml`) but has never run.
- Docker + docker-compose present; no IaC, no dev/stage/prod environments.

---

## Gap analysis by domain

Status: `[done]` `[partial]` `[missing]`

| Domain | Status | Remaining to wire |
|--------|--------|-------------------|
| Compliance (PHI/HIPAA) | `[missing]` **URGENT** | Patient records are PHI; trial workspace is not HIPAA-covered. De-identify OR move to BAA infra + encryption-at-rest + audit + access controls before real PHI. |
| AuthN/AuthZ + multi-tenancy | `[missing]` | User auth (SSO/OAuth), org/tenant model, per-tenant data + credential isolation, RBAC. |
| Service/API layer | `[missing]` | Backend service (FastAPI), REST/job API, async execution, real frontend. |
| Secrets/credentials | `[partial]` | Vault (cloud KMS / Databricks secrets / Vault); per-tenant OAuth M2M service principals (not one PAT); rotation. |
| Databricks integration | `[partial]` | Deployer apply paths (genie/dashboards/jobs/UC-registration are spec-only); Genie Conversation API; per-tenant catalog/schema (lock.toml is one global value). |
| CI/CD + deploy | `[partial]` | Run CI to green; IaC (Terraform / Databricks Asset Bundles); dev/stage/prod environments; release process. |
| Reliability/observability | `[partial]` | Activate dormant `.example` harness configs; monitoring, alerting, SLAs, error tracking; wire MLflow telemetry. |
| Data governance | `[partial]` | PII classification/masking, data-quality enforcement, row/column security, retention (critical for PHI). |
| Productization | `[partial]` | Consolidate the ~90-CLI surface (overengineering flagged); packaging/versioning; activate harness configs. |
| Scale/cost | `[missing]` | Per-tenant quotas, Databricks cost controls, resource isolation. |

---

## Recommended sequence

- **Phase 0 — Protect & validate (now):** local end-to-end flow works as intended;
  push branch + run CI to green; resolve PHI exposure (de-identify trial data or stop
  using real records); decide tenancy model (recommended: catalog-per-tenant in UC).
- **Phase 1 — Enterprise foundation:** multi-tenant data model + per-tenant OAuth M2M
  service principals + secrets vault; stand up the service/API layer.
- **Phase 2 — Databricks completeness:** wire deployer apply paths + Genie Conversation
  API + per-tenant catalog/schema targeting.
- **Phase 3 — Compliance & governance:** audit logging, PII classification/masking,
  RBAC, HIPAA controls.
- **Phase 4 — Ops:** CI/CD pipelines, IaC, environments, monitoring/alerting,
  activate reliability harnesses.
- **Phase 5 — Productize:** CLI consolidation, packaging, docs.

---

## Headline risks

1. **PHI compliance** — real patient data in a non-compliant trial. Address first.
2. **No auth/multi-tenancy** — the largest architectural lift; everything tenant-aware
   depends on it. Decide the model before building tenant features.
3. **One shared token** — fine for the sandbox, wrong for production
   (-> per-tenant service principals / OAuth M2M).

---

## Next trigger

After the local flow is validated as working-as-intended, resume at **Phase 0**
(push + CI green + PHI decision + tenancy-model decision), then design Phase 1.

*Confidence: medium-high on the structural gaps (scan-verified); lower on
exhaustiveness — not all 307 source files were read, so treat per-domain "remaining"
as the major items, not a line-complete backlog.*

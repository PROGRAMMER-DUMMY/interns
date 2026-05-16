# Medallion Architect — Implementation Guide

This directory is the canonical implementation guide for the **Medallion Architect agent** — a multi-phase platform feature that adds a Bronze/Silver/Gold layered pipeline to the autoresearch workspace toolchain.

The companion PRD is at `docs/PRD_medallion_architect.md`. **The PRD locks the *what*; this guide locks the *how*.** Read order:

| # | Doc | Audience | When to read |
|---|---|---|---|
| 0 | [00-overview.md](00-overview.md) | Everyone | First. Problem framing + vision. |
| 1 | [01-architecture.md](01-architecture.md) | Engineers, reviewers | Before touching code. The 13 grilled decisions, 5 amendments, contract catalog, integration map. |
| 2 | [02-conventions.md](02-conventions.md) | Engineers | Before touching code. File layout, idempotency, dual-format JSON+MD, secret handling. |
| 3 | [03-phase-P0-foundation.md](03-phase-P0-foundation.md) | Engineers | When extending what shipped. |
| 4 | [04-phase-P1-build-and-governor.md](04-phase-P1-build-and-governor.md) | Engineers | Implementing P1. |
| 5 | [05-phase-P2-delta-and-databricks.md](05-phase-P2-delta-and-databricks.md) | Engineers | Implementing P2. |
| 6 | [06-phase-P3-pii-at-rest.md](06-phase-P3-pii-at-rest.md) | Engineers, security review | Implementing P3. Hard prerequisite for production healthcare use. |
| 7 | [07-phase-P4-dynamic-models.md](07-phase-P4-dynamic-models.md) | Engineers | Implementing P4. Hard prerequisite for cost control. |
| 8 | [08-phase-P5-lineage-and-mlflow.md](08-phase-P5-lineage-and-mlflow.md) | Engineers, SRE | Implementing P5. |
| 9 | [09-testing.md](09-testing.md) | Engineers | Throughout implementation. |
| 10 | [10-operations.md](10-operations.md) | Operators, on-call | After P0 lands on a workspace. |

## Phase status (as of 2026-05-15)

| Phase | Status | Exit criterion |
|---|---|---|
| **P0** Foundation | ✅ shipped | `design-medallion --cheap` produces ratifiable manifest on Healthcare RCM |
| **P1** Build + Governor | ⬜ pending | Acceptance #1, #3, #4 (subset) |
| **P2** Delta + Databricks | ⬜ pending | Acceptance #2, #6 |
| **P3** PII at rest | ⬜ pending | Acceptance #5 |
| **P4** Dynamic models | ⬜ pending | Acceptance #7 |
| **P5** Lineage + MLflow | ⬜ pending | Acceptance #8 |

Acceptance criteria numbering refers to PRD Section 20.

## How to use this guide

- **Implementing a phase**: start at the matching phase doc; do not skip prerequisites listed in its "Prerequisites" section.
- **Reviewing a PR for a phase**: read the phase doc's "Acceptance Criteria" and "Risks" sections; check the PR against them.
- **Operating a workspace post-ship**: jump to [10-operations.md](10-operations.md) — runbook + troubleshooting.
- **Adding a new model provider**: only [07-phase-P4-dynamic-models.md](07-phase-P4-dynamic-models.md) matters.
- **Adding a new substrate (e.g., Iceberg)**: read [01-architecture.md](01-architecture.md) §"Substrate portability" then [05-phase-P2-delta-and-databricks.md](05-phase-P2-delta-and-databricks.md) as a template.

## Non-negotiable rules

These come up in every phase. Internalize them before writing code.

1. **Generated artifacts are never hand-edited.** If the JSON contract is wrong, fix the agent or the orchestrator and regenerate. MD files are regenerated from JSON every run.
2. **Inputs hash is the idempotency anchor.** Two runs with the same `inputs_hash` produce byte-identical artifacts.
3. **Gold derives from Silver only.** This is a HIPAA structural invariant, enforced by the validator. There is no legitimate exception.
4. **Bronze stores raw; Silver hashes PII.** Never store raw PII in Silver. Never reference Bronze from Gold.
5. **Composite natural keys by default for multi-source data.** Flat keys are an explicit opt-in via `workspace_feature_definitions.json`.
6. **Every assertion failure routes through the Governor — no swallowed errors.**
7. **No silent demotion of compute target.** Permissive-mode fallback writes `degraded_run: true` to the run state.
8. **No silent model defaults.** Model selection is discovered + classified at run start; tier assignment is auditable.
9. **Cache key includes model tier + prompt strategy version.** Outputs from different tiers must never collide.
10. **Every contract has paired JSON (source of truth) + MD (PR review surface).** The MD regenerates from the JSON.

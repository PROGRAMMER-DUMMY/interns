# Plans — Index

Implementation roadmaps and forward-looking platform plans. These are aspirational, not active
reference, and may be parked. Tagged `[plan]` in `docs/README.md`.

**Location rule**: multi-phase working/execution plans (not just aspirational roadmaps) belong
here too, not in a CLI-specific config directory (`~/.claude/plans/`, `~/.codex/`, `~/.gemini/`,
or any future tool's own state dir). This platform is driven by multiple interchangeable agentic
CLIs (`claude-code`, `codex`, `gemini-cli` — see `core/agents/llm_engine.py`), and only files
inside the repo itself are readable by all of them, present or future. A plan tracked in a
CLI-private directory is invisible to a teammate using a different tool, and disappears entirely
if that tool's config is wiped. Write and update the plan file here from the start of a
multi-phase task, not just after it's done.

| File | What it is | Use for |
|------|------------|---------|
| `PRODUCTION_ROADMAP.md` | Enterprise productionization roadmap (PHI, tenancy, auth/RBAC, service layer). PARKED pending local validation | Enterprise gap analysis; Phase 0+ productionization sequencing |
| `agent_reliability_platform_plan.md` | Plan to turn the governed KPI/data workflow into a final-product reliability platform | Reliability-platform direction and milestones |
| `platform_reliability_and_sql_coverage_plan.md` | SQL-coverage + reliability roadmap from the workspace-agnosticism + advanced-SQL session | Advanced-SQL coverage and reliability roadmap |
| `dataops_build_plan.md` | How enterprise pipelines are built/operated at GB->TB scale, per-stage with services + medallion layers + cadence, healthcare-RCM examples; the build/run two-clock model and the emitted scheduled-pipeline bundle | DataOps stage build-out; what structure to emit so a workspace runs unattended daily/weekly/monthly |
| `dashboard_scaleout_design.md` | Phase 3 TB-scale dashboard/KPI-compute design (distributed warehouse/object-store/cluster) -- deliberately deferred until real data volume triggers it; phases 0-2 (caching, connection reuse, dbt spike) are done | Reference before triggering a TB-scale dashboard buildout; the explicit trigger conditions to check first |
| `security_governance_hardening_2026-07.md` | Completed 4-phase security/governance remediation (dbt/Airflow production-execution gate, unlocked-shared-write concurrency pattern across 7 sites, injection-guard coverage in the KPI blocker panel, meta-harness scope documentation) | Reference for what was found/fixed and why; first plan migrated here from a CLI-specific location, establishing the location rule above |

When adding a file here: add one row above, then confirm `docs/README.md` still points at this
folder (it points at the folder, not individual files — keep it that way).

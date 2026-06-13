# Project Sections And Evaluation Lanes

This is a local development management note. It is intentionally ignored by git.

## Purpose

The project has multiple overlapping systems: governed workspace workflows, KPI/query proof,
medallion pipeline design, agent/skill routing, validation harnesses, dashboard presentation, and
Databricks deployment. Treating all of them as one workflow creates confusion. Use these sections
to focus implementation, review, and evaluation one lane at a time.

## Root Files

| Area | Purpose | Notes |
|---|---|---|
| `README.md` | Human orientation and CLI guide | Useful overview, not the strict policy authority. |
| `AGENTS.md` | Main operating rules for all agents | Canonical behavior guide. |
| `GEMINI.md`, `CLAUDE.md`, `CODEX.md` | Tool-specific agent instructions | Can drift from `AGENTS.md`; review after major policy changes. |
| `TOOLS.md` | Human-readable tool registry | Agents must inspect before choosing workflows. |
| `.agents/tools.json` | Machine-readable tool registry | Routing source of truth for tools. |
| `CONTEXT.md` | Domain architecture and vocabulary | High-level concepts and package map. |
| `pyproject.toml` | CLI entrypoints and dependencies | Defines the real command surface. |
| `dashboard.py` | Large dashboard app | Should be treated as its own UI/reporting lane. |
| `.env` | Local secrets/config | Never print, stage, or commit. |
| `.gitignore` | Safety boundary | Protects raw data, local outputs, and development-only docs. |

## Core Packages

| Package | Owns | Evaluation Goal |
|---|---|---|
| `core/onboarding/` | Main governed workspace workflows | Workflows produce correct artifacts and next steps. |
| `core/onboarding/kpi/` | KPI parsing, mapping, blocker panels, SQL, proof | Prevent wrong KPI assumptions and bad SQL. |
| `core/onboarding/data_model/` | Data model generation, image parsing, blockers | Convert weak docs/images into governed contracts. |
| `core/onboarding/relationships/` | Source-to-target plans and relationship contracts | Prove joins before executable SQL. |
| `core/onboarding/sources/` | External/source catalog intake | Prevent random folder/data ingestion. |
| `core/onboarding/harness/` | Workflow, project, AI app/CLI, reliability checks | Score system behavior. |
| `core/onboarding/memory/` | Workspace/team memory and definitions | Avoid stale or unsafe reuse. |
| `core/medallion/` | Bronze/silver/gold design, build, lineage | Separate production pipeline from KPI proof. |
| `core/storage/` | Workspace layout, settings, metadata, external guards | Protect workspace boundaries and allowlists. |
| `core/skills/` | Skill adapter generation | Keep Gemini/Claude/Codex aligned. |
| `core/agents/` | Agent registry, LLM routing, code mutation | Agent orchestration layer. |
| `core/execution/` | Local/Databricks execution backends | Runtime boundary. |
| `core/governance/` | Semantic contracts, policy, evaluators | Correctness and approval gates. |
| `core/optimization/` | Optimization memory, planning, evolution | Experiment learning. |
| `core/context/` | Context manifests/pages | Bounded context for agents. |
| `core/presentation/` | Reports, diagrams, workbook exports | Stakeholder outputs. |
| `core/resource/` | Resource preflight | Prevent unsafe heavy local runs. |

## Skills

| Skill | Owns |
|---|---|
| `workspace-governance` | Safety, workspace boundaries, git hygiene. |
| `task-onboarding` | New workspace setup. |
| `workspace-kpi-query-optimizer` | End-to-end governed KPI workflow. |
| `kpi-analyst` | KPI meaning, query/result review, artifact classification. |
| `data-engineering-pipeline-design` | Source-to-target, ETL/ELT, medallion. |
| `domain-model` | Business/data terminology and mappings. |
| `feature-derivation-library` | Candidate derived features, not proof. |
| `clarify-ambiguity` | One targeted question on high-impact uncertainty. |
| `grill-requirements` | Full requirement interview. |
| `stakeholder-memory` | Saved preferences/decisions. |
| `to-solution-brief` | Implementation brief. |
| `databricks-access-gates` | Remote/Databricks approval/access blockers. |
| `evolution` | Lessons learned. |

## Other Folders

| Folder | Purpose | Concern |
|---|---|---|
| `.agents/` | Generated shared adapters, tool indexes, sessions | Generated/session files can be noisy. |
| `.gemini/`, `.claude/`, `.codex/` | Tool-specific agent configs/adapters | Must stay aligned with canonical skills. |
| `tools/` | Smaller CLI utilities | Some overlap with `core` scripts. |
| `tests/` | Unit and harness tests | Needs lane-specific test groups. |
| `docs/` | Architecture/reference/bugs/local planning | Keep local-only management docs ignored. |
| `plugins/` | Codex plugin bundle | Separate extension surface. |
| `interns/` | Built-in intern agents | Older agent model, overlaps with skills/adapters. |
| `workspaces/` | Customer/project data and generated outputs | Separate source inputs from ignored `interns/` outputs. |
| `config/` | Harness/source catalog/settings examples | `config/tasks.json` may be empty and should not be treated as the only active-workspace signal. |

## Workflow Evaluation Lanes

| Lane | Goal | Main Artifacts | Done Means |
|---|---|---|---|
| 1. Workspace Intake | Pick the right workspace/files safely | `list-workspace-files`, `workspace_settings.json`, input inventory | Correct workspace, dataset allowlist, no raw data leakage. |
| 2. KPI Understanding | Parse KPI sheet and business intent | `kpi_registry.json`, `kpi-analyst`, KPI generation panels | KPI meaning, grain, filters, formulas are clear. |
| 3. Feature Mapping | Map KPI terms to columns/rules | `kpi_feature_mapping.json`, blocker panels, workspace definitions | No unresolved features; no typo keywords as features. |
| 4. Data Model And Joins | Prove tables and relationships | `domain_model.json`, `relationship_contracts.json` | Only executable-approved joins used. |
| 5. Source-To-Target | Decide source, grain, engine, layer | `source_to_target_plan.json` | KPI implementation plan is not blocked. |
| 6. Medallion Pipeline | Bronze/silver/gold cleaning/conforming | `bronze_silver_standards.json`, `pipeline_plan.json`, medallion manifests | Silver/gold layers exist and pass checks. |
| 7. KPI SQL Proof | Generate/run KPI queries | `generated/solutions/*.sql`, `kpi_execution_harness.md` | Clearly labeled as proof/report/gold output. |
| 8. Validation And Release | Score readiness | `validate-workspace-artifacts`, `project_harness`, `agent_benchmark` | Release gate says what is ready vs blocked. |
| 9. Agent/Skill Routing | Make Gemini/Claude/Codex behave consistently | `skills/*`, `.agents/*`, `.gemini/*` | Agents choose correct lane/tool and avoid freehand. |
| 10. Deployment | Databricks/Genie/remote promotion | Databricks manifests/specs/deployment plan | Dry-run reviewed; remote mutation explicitly approved. |

## Main Confusion To Avoid

The repo currently has three overlapping systems:

1. Old intern/agent system: `interns/`, `core/agents/`
2. New skill/adapter system: `skills/`, `.agents/`, `.gemini/`, `.claude/`, `.codex/`
3. Workspace workflow engine: `core/onboarding/`, `workspace-flow`, harnesses

All three can be useful, but agents must know which lane they are operating in before generating
outputs. The most important operational distinction is:

```text
KPI proof/query output is not automatically a bronze/silver/gold production pipeline.
```

## Known Gaps

1. No single lane-status artifact says whether a workspace is currently raw proof, silver-ready, or
   gold-certified.
2. KPI result reports can look production-ready even when they are local proof views.
3. Medallion tools exist but are not always the default route after KPI mapping.
4. `config/tasks.json` may be empty, so active workflow must be established from workspace scans and
   user confirmation.
5. Stale generated memory or wiki files can confuse agents if they are not cleaned or marked obsolete.
6. Skill ownership overlaps and needs explicit routing discipline.

## Recommended Focus Order

1. Create or generate a lane/status artifact for active workspaces.
2. Label KPI result reports as `raw proof`, `reporting query`, `silver-ready`, or `gold-certified`.
3. Make medallion/pipeline route the default for recurring or production KPI work.
4. Clean stale typo artifacts and obsolete generated memory.
5. Add an active task/workflow declaration when a session starts.

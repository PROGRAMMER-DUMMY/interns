# Agent Reliability Platform Plan

This plan turns the current governed KPI/data workflow into a final-product reliability platform.
It extracts useful patterns from Hermes Agent, CodeGraph, agentmemory, Understand-Anything, Forge,
and CLI-Anything without copying their architectures wholesale.

## Product Thesis

The product is an evidence-backed operating layer for AI data-engineering agents. It records what
agents did, validates whether they followed the required workflow, connects every artifact to its
evidence, remembers accepted decisions with confidence, and presents proof to reviewers.

The existing core remains authoritative:

- workspace onboarding and profiling;
- KPI registry normalization;
- feature mapping and blocker panels;
- source-to-target planning and relationship contracts;
- KPI SQL generation and execution harnesses;
- proof packets, project harness, workflow guardrails, AI app harness, and AI CLI harness.

The new layer makes that work replayable, auditable, graph-connected, and easier to operate across
Codex, Claude, Gemini, and other CLI agents.

## External Pattern Intake

| Source | Pattern to adopt | Adaptation for this repo |
| --- | --- | --- |
| Hermes Agent | skills, persistent memory, scheduled tasks, command approval modes, messaging gateways | workspace-specific operating profiles, scheduled local-safe checks, explicit approval classes, future Slack/Teams blocker answers |
| CodeGraph | pre-indexed local graph, impact analysis, watcher sync, graph-first agent instructions | local workspace evidence graph over KPIs, profiles, mappings, SQL, reports, harness findings, and decisions |
| agentmemory | memory lifecycle, hooks, hybrid retrieval benchmarks, health checks, delete audit policy | confidence-scored mapping and decision memory with expiry, provenance, retrieval tests, and deletion audit |
| Understand-Anything | interactive graph, guided tours, domain/business view, diff impact, persona views | reviewer dashboard with BA, engineer, and governance views over the same proof graph |
| Forge | StepTracker, StepEnforcer, ResponseValidator, ErrorTracker, retry nudges, batch eval | required-step enforcement for workspace workflows and command failure recovery checks |
| CLI-Anything | CLI registry, generated skills, JSON-safe output contracts, smoke/E2E tests, platform hardening | generated agent skills from `.agents/tools.json`, JSON-first tool contracts, Windows compatibility checks |

## Target Architecture

```text
Agent / User / CLI
  -> Trajectory Recorder
  -> Tool-Call Reliability Layer
  -> Workspace Evidence Graph
  -> Confidence-Scored Memory
  -> Harness and Benchmark Scoring
  -> Proof Packet and Reviewer Dashboard
```

## Capability Roadmap

### P0: Trajectory Recorder

Record the agent journey as append-only JSONL under the active workspace:

```text
workspaces/<project>/interns/state/trajectory.jsonl
workspaces/<project>/interns/reports/trajectory/current.json
workspaces/<project>/interns/reports/trajectory/current.md
workspaces/<project>/interns/generated/evidence/trajectory/current.json
```

Events:

- session start and finish;
- user intent or correction;
- command start/result;
- file or artifact write;
- validation result;
- blocker question shown;
- blocker decision accepted/rejected;
- retry or recovery action;
- harness run and score.

Design constraints:

- append-only event log;
- local-safe and workspace-scoped;
- secret redaction before writing;
- JSON-first records with Markdown summary;
- usable by `validate-workflow-guardrails`;
- no dependency on external LLM or remote service.

Success criteria:

- a failed command without later recovery becomes detectable;
- unsupported shell commands become visible in the same workspace log;
- blocker panels can be tied to the validation state that preceded them;
- the project harness can include trajectory health as a score input.

### P0: Step-Level Reliability Checks

Extend `validate-workflow-guardrails` to inspect trajectory records and fail or warn when:

- a failed command has no retry/recovery event;
- a blocker question was shown without a recent successful artifact validation;
- raw datasets were read before profile evidence;
- a non-portable shell command was used on Windows;
- generated SQL was produced before required mapping, relationship, or source-to-target proof;
- a stale artifact was used after the workspace fingerprint changed.

This is the Forge-inspired StepTracker/StepEnforcer layer, but expressed as local deterministic
checks over our own artifacts.

### P1: Workspace Evidence Graph

Build a graph artifact that links:

- source files and source rows;
- KPI IDs and terms;
- profile columns and datasets;
- feature mappings and accepted workspace definitions;
- blocker questions and answers;
- relationship contracts and source-to-target plan entries;
- generated SQL and execution outputs;
- harness findings, warnings, and scores;
- memory entries and their evidence.

Output:

```text
workspaces/<project>/interns/generated/evidence_graph/graph.json
workspaces/<project>/interns/reports/evidence_graph/current.md
```

Required queries:

- `why <feature>`: explain which evidence supports a feature mapping.
- `impact --feature <name>`: list KPIs, SQL files, reports, and memory affected.
- `introduced --term <name>`: show which artifact first introduced a term such as `created_at`.
- `stale`: show graph nodes whose source artifact changed after they were generated.

Initial implementation status:

- `build-workspace-evidence-graph` writes graph JSON and Markdown from existing generated
  artifacts.
- V1 includes KPI, term, feature, column, dataset, artifact, SQL, trajectory event, relationship,
  plan, report, and harness finding nodes.
- V1 includes summary query payloads for introduced terms, feature impact, and column impact.

### P1: Confidence-Scored Memory Lifecycle

Upgrade workspace and team memory entries into a common shape:

```json
{
  "memory_type": "accepted_mapping",
  "claim": "Payer maps to transactions.PayorID",
  "scope": "workspace",
  "confidence": "user_confirmed",
  "evidence": [],
  "created_at": "2026-05-22T00:00:00Z",
  "last_verified": "2026-05-22T00:00:00Z",
  "expires_if": ["profile_schema_changes", "source_kpi_changes"],
  "retrieval_tags": ["payer", "payorid", "transactions"],
  "status": "active"
}
```

Confidence ladder:

```text
candidate_inferred -> profile_backed -> user_confirmed -> execution_verified -> regression_protected
```

Memory must be useful and safe. Stale or weak memory can prefill drafts, but it cannot authorize
executable generation without current-workspace evidence or user confirmation.

### P1: Generated Agent Skills From Tool Registry

Promote tool adapter generation into a product surface:

- generate Codex, Claude, Gemini, and generic CLI instructions from `.agents/tools.json`;
- include required evidence order, safety class, outputs, recovery hints, and examples;
- validate that every `pyproject.toml` script used by agents has a registry entry;
- add smoke tests for generated skills.

This extends the existing `generate-skill-adapters` command instead of creating a competing tool.

### P2: Reviewer Dashboard And Guided Proof View

Build a dashboard that reads only generated artifacts and presents:

- KPI list with readiness status;
- blocker queue and accepted decisions;
- mapping table with confidence and evidence;
- generated SQL and result sample;
- evidence graph and impact view;
- trajectory timeline;
- harness scores and open risks.

Persona views:

- BA view: business definition, KPI row, accepted mapping, open questions.
- Engineer view: datasets, joins, grain, SQL, execution result, failed validations.
- Governance view: approvals, evidence state, harness score, blocked gates, memory confidence.

### P2: Scheduled Regression Checks

Add local-safe scheduled check definitions:

- stale artifact scan;
- nightly workflow guard;
- memory health check;
- graph stale-node check;
- harness baseline compare.

Remote execution remains gated by existing approval policy.

### P2: Agent Behavior Benchmark Suite

Build scenario packs for:

- workspace startup and confirmation;
- fresh onboarding;
- blocker panel use;
- command failure and recovery;
- unsupported command avoidance;
- raw-data access policy;
- KPI-to-SQL proof path;
- cleanup dry-run and delete approval.

The suite should run through `run-ai-cli-harness` using stub transcripts by default and real CLI
execution only with explicit approval.

## Implementation Order

1. Implement `record-workspace-trajectory`.
2. Teach `validate-workflow-guardrails` to consume trajectory logs by default.
3. Add trajectory health into `validate-project-harness`.
4. Build `workspace-evidence-graph` from existing generated artifacts.
5. Add memory confidence schema and health report.
6. Strengthen `generate-skill-adapters` with recovery guidance and contract checks.
7. Build reviewer dashboard from graph, proof packet, trajectory, and harness reports.
8. Add scheduled local-safe checks and benchmark scenario packs.

## Non-Goals

- Do not replace existing onboarding, KPI, SQL, memory, or harness tools.
- Do not depend on external services for core validation.
- Do not use memory as proof when current workspace evidence is missing.
- Do not let scheduled tasks perform deletes, remote execution, or production writes.
- Do not expose secrets or raw datasets in reports.

## First Slice

The first implementation slice is the trajectory recorder because it is the data substrate for
step enforcement, evidence graph construction, memory audits, dashboard timelines, and agent
behavior benchmarks.

## Chosen Recording Policy

Use the hybrid policy:

- controlled workflow tools record trajectory events automatically;
- external CLIs can still call `record-workspace-trajectory` manually or provide transcripts to
  `run-ai-cli-harness`;
- trajectory recording must be best-effort and must not break the primary workflow.

Initial controlled integrations:

- `workspace-flow`
- `prepare-kpi-blocker-panel`
- `apply-kpi-panel-answer`
- `validate-project-harness` reads workflow guardrail health and includes it in the top-level
  readiness score.

Later integrations should cover source-to-target planning, relationship contracts, SQL generation,
and AI harness run summaries.

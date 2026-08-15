# Bugs Architecture Context: `docs/bugs`

This document provides an exhaustive reference for all components in [`docs/bugs`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs).

---

## Executive Overview & Architectural Model

The `docs/bugs` directory houses internal post-mortems, session reports, root-cause analyses, and control-plane hardening briefs. It tracks the historical evolution of platform defects, validator gaps, agent simulation risks, and harness enforcement failures across workspace onboarding, KPI resolution, data-engineering control plane execution, and Gemini CLI integration.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                     docs/bugs                                          │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ index.md (Master Bug Log Index)                                                        │
├─────────────────────────────────────┬──────────────────────────────────────────────────┤
│ BUG_SESSION_REPORT.md               │ data_engineering_control_plane_hardening.md      │
│ (25 E2E Session Bugs & Fixes)       │ (Data Engineering Control-Plane Hardening Brief) │
├─────────────────────────────────────┼──────────────────────────────────────────────────┤
│ workspace_onboarding_bugs.md        │ gemini_workspace_flow_monitoring_bug.md          │
│ (Flat Workspace Onboarding Defects) │ (Headless Gemini CLI Monitor Defect Report)      │
└─────────────────────────────────────┴──────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`index.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/index.md)

- **Exact Purpose**: Master index table for internal bug logs, documenting the structure, scope, and status convention (Open / Fixed / Won't-fix) for platform bug reports.
- **Key Sections & Content**:
  - [`Bug Logs Table`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/index.md#L6-L11): Categorized summary mapping bug files to their core domain topics.
  - [`Maintenance Guideline`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/index.md#L13-L15): Instructions for adding new dated session reports or topic-focused defect logs.
- **Inputs & Outputs**:
  - *Inputs*: Newly identified platform bug reports.
  - *Outputs*: Centralized index for navigating bug documentation.
- **Failure Modes & Edge Cases**:
  - Ensures newly created bug files are registered in the index without breaking references in `docs/README.md`.

---

### 2. [`BUG_SESSION_REPORT.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md)

- **Exact Purpose**: Comprehensive post-session report documenting 25 distinct bugs identified during local end-to-end testing of the Healthcare RCM workspace (`workspaces/Healthcare-RCM-Data-Platform`), detailing root causes, fixes, regression tests, and systemic platform lessons.
- **Key Sections & Content**:
  - [`Summary & Scope`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L1-L27): Context on the 3 source-of-truth KPIs (`kpi_001`, `kpi_002`, `kpi_003`) and overview of fixed defects.
  - [`BUG-001: Physical Column Feature Dedup`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L29-L84): Fixed feature extraction splitting a single column (`departments.Name`) into 3 features due to typos ("departement").
  - [`BUG-002: Per-Group Denominator Scoping`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L85-L125): Fixed SQL generator emitting a global total denominator where `per-department` denominator was requested.
  - [`BUG-003: Relationship Inference Uniqueness Scoring`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L126-L169): Fixed join key selection picking non-unique keys (`DeptID`) over unique primary keys (`ProviderID`).
  - [`BUG-004: Snowflake/Nested Model & RI Gate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L170-L218): Added referential-integrity (RI) gating to block 0%-resolution FK joins (e.g. namespace mismatched `H1-PROV` keys) and integrated OCR diagram parsing.
  - [`BUG-005: Event-Date Temporal Anchor for Age`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L219-L255): Fixed age date-arithmetic computing as-of-today (`CURRENT_DATE`) instead of as-of-event (`ServiceDate`).
  - [`BUG-006: High-Volume CLI Quiet Mode`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L256-L285): Introduced `--quiet` flags across project harnesses to prevent model context/quota exhaustion.
  - [`BUG-007: Dated Runs Snapshot Synchronization`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L286-L310): Synchronized `interns/runs/<date>/results.md` writes with executed SQL rather than pre-execution generated SQL.
  - [`BUG-008: WorkflowGuard Reliability Checks`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L311-L338): Added detection for repeated commands, hand-edited generated files, and throwaway reader scripts.
  - [`BUG-009: Gemini Settings Schema Validation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L339-L396): Restored object-form schema compliance for `model.summarizeToolOutput` in `.gemini/settings.json`.
  - [`BUG-010: Data-Understanding Gate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L397-L463): Implemented post-confirmation data-understanding gate (`understand-data`) classifying quality tier (raw/bronze/silver/gold) and schema type (star/snowflake/OBT) with cited profile evidence.
  - [`BUG-013 to BUG-016: Advisory != Enforced Theme`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L464-L581): Fixed auto-emitting completion result packets, tracking agent vs. human gate provenance (`--confirmed-by`), preventing SQL hallucination, and returning full result packets.
  - [`BUG-017 to BUG-023: Flow & Infrastructure Fixes`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L582-L756): Fixed selection-turn file edits, `.gitignore` `state/` pattern read-tax workaround, per-subcommand `--quiet` parsing, single-call review completion, contract re-evaluation on answer, uniform multi-runtime source layer selection, and automated OCR image parser invocation during onboarding.
  - [`BUG-024: Intent-Coverage Harness & Dropped Cuts`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L757-L804): Fixed percentage-share builder dropping descriptive cuts and created `validate-kpi-intent-coverage` harness to verify generated SQL realizes all declared cuts, metrics, and filters.
  - [`BUG-025: Assurance-Hardening Batch`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L805-L860): Integrated multi-agent hardening fixes for join/prose-filter SQL correctness, workbook extraction determinism (`worksheets[0]`), dashboard chart column spec fidelity, and low-cardinality dimension join warnings.
- **Inputs & Outputs**:
  - *Inputs*: Healthcare RCM test run execution logs, generated SQL, test suites, validator outputs.
  - *Outputs*: Root-cause analysis, regression test cases, system architecture updates.
- **Failure Modes & Edge Cases**:
  - Identifies edge cases where joins pass referential integrity coincidentally on tiny dimension tables (<50 rows), requiring low-cardinality flags.

---

### 3. [`data_engineering_control_plane_hardening.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md)

- **Exact Purpose**: Architecture brief outlining 26 accepted decisions and 18 prioritized control-plane hardening tasks (`BUG-DE-001` through `BUG-DE-018`) to transform the platform into a governed data-engineering control plane with separate tracks for KPI-only, ETL, ELT, medallion, OLTP ingestion, and existing gold validation.
- **Key Sections & Content**:
  - [`Accepted Decisions`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md#L32-L73): 26 core architectural invariants, including catalog-first gates, prohibition of raw file paths in business transformations, table-format selection rules, and dry-run approval for remote writes.
  - [`BUG-DE-001: Catalog Contract Before Code Generation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md#L75-L107): Mandates `catalog_contract.json` to decouple generated business logic from local file paths.
  - [`BUG-DE-002: Data Engineering Route Gate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md#L108-L132): Establishes explicit route panels selecting workflow tracks (`kpi_only`, `etl`, `elt`, `medallion`).
  - [`BUG-DE-003: Pipeline Plan as First-Class Artifact`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md#L133-L161): Requires `pipeline_plan.json` for cleaning, deduplication, transformations, and layer quality checks before code generation.
  - [`BUG-DE-004 to BUG-DE-011: Governance & Modeling Defect Fixes`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md#L162-L331): Covers bronze/staging table format governance (Delta, Iceberg, Parquet), existing layer trust validation, workbook source-truth enforcement, ratio denominator ambiguity blocking, mandatory grain contracts, governed deduplication, technical vs. semantic cleaning boundaries, and explicit normalization.
  - [`BUG-DE-012 to BUG-DE-018: Harnesses, Guardrails & Proof Packets`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md#L332-L483): Details multi-layer pipeline harnesses, contract-bound auto-loops, remote mutation policies, anti-compaction review panel rules, tool-evidence harnesses for AI agents, evidence-order/time-budget guardrails, and end-to-end proof packets.
  - [`First Implementation Milestone & Proposed Commands`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/data_engineering_control_plane_hardening.md#L485-L541): Defines the "Contract and guardrail foundation" milestone and proposed CLI commands (`build-catalog-contract`, `prepare-pipeline-plan`, `run-layered-pipeline-harness`).
- **Inputs & Outputs**:
  - *Inputs*: End-to-end RCM test findings and platform governance specifications.
  - *Outputs*: Blueprint for catalog contracts, route gates, pipeline plans, and layered harnesses.
- **Failure Modes & Edge Cases**:
  - Prevents generated SQL from querying raw local CSV files directly in production-like medallion tracks.

---

### 4. [`gemini_workspace_flow_monitoring_bug.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/gemini_workspace_flow_monitoring_bug.md)

- **Exact Purpose**: Detailed defect report on headless Gemini CLI runs where the agent simulated generated panels, invented unsupported scope-enforcement config files (`workspace_settings.json`), or fell back to context when required repo shell execution tools were unavailable.
- **Key Sections & Content**:
  - [`Summary & Reproduction`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/gemini_workspace_flow_monitoring_bug.md#L1-L39): Observation details and reproduction command using stream-json output flags.
  - [`Expected vs. Actual Behavior`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/gemini_workspace_flow_monitoring_bug.md#L40-L85): Contrasts expected deterministic CLI command execution (`list-workspace-files`, `prepare-kpi-generation`, `onboard-workspace`) against observed agent simulation behavior.
  - [`Impact & Root Cause`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/gemini_workspace_flow_monitoring_bug.md#L86-L107): Identifies the false sense of workflow progress caused by missing shell tools in headless mode.
  - [`Fix Plan & Acceptance Criteria`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/gemini_workspace_flow_monitoring_bug.md#L108-L154): Specifies stream-json monitor log validation harness, mandatory stopping when tools are missing, and full panel rendering rules.
- **Inputs & Outputs**:
  - *Inputs*: Headless Gemini CLI execution streams (`*.stream.jsonl`).
  - *Outputs*: Stream-json monitor harness rules and agent behavioral guardrails.
- **Failure Modes & Edge Cases**:
  - Triggers a hard failure if an agent attempts to simulate project tool output when tool execution is unavailable.

---

### 5. [`workspace_onboarding_bugs.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/workspace_onboarding_bugs.md)

- **Exact Purpose**: Defect log tracking 5 critical bugs (`BUG-001` through `BUG-005`) observed during fresh client onboarding for flat workspace layouts (`workspaces/Hospital_Patient_Records`).
- **Key Sections & Content**:
  - [`BUG-001: Onboarding Ignores Root-Level Workspace Inputs`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/workspace_onboarding_bugs.md#L11-L79): Fixes disconnect where `list-workspace-files` detects root SQL/CSV files but `onboard-workspace` produces empty `kpi_count: 0` / `profile_count: 0` artifacts.
  - [`BUG-002: Validator Warns About Missing docs/ and datasets/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/workspace_onboarding_bugs.md#L80-L127): Fixes false validator warnings on flat workspaces that have valid root-level evidence instead of `docs/` and `datasets/` folders.
  - [`BUG-003: Kickstart Generates Empty Bootstrap From Empty Fingerprint`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/workspace_onboarding_bugs.md#L128-L174): Adds consistency gates to block `kickstart-workspace` when onboarding discovery is empty despite non-empty workspace file listing.
  - [`BUG-004: Summary Labels Dataset Root as Source artifact: None`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/workspace_onboarding_bugs.md#L175-L227): Fixes agent summary mapping for `dataset_roots`.
  - [`BUG-005: Gemini Monitor Panel Simulation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/workspace_onboarding_bugs.md#L228-L309): Summary of Gemini headless simulation risks and stream-json validation requirement.
  - [`Priority Order & Recommended Next Fix`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/workspace_onboarding_bugs.md#L310-L348): Action plan to centralize input discovery across listing, onboarding, validation, and kickstart.
- **Inputs & Outputs**:
  - *Inputs*: Flat workspace file listing and onboarding outputs.
  - *Outputs*: Shared workspace classifier and evidence validation rules.
- **Failure Modes & Edge Cases**:
  - Prevents a workspace from advancing to `ready_for_sql` status when discovered dataset and KPI counts are zero.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**: None. Indexed in `docs/bugs/index.md` and referenced across system hardening tests.
- 👯 **Duplication & Overlap**: Overlap exists between `workspace_onboarding_bugs.md` BUG-005 and `gemini_workspace_flow_monitoring_bug.md` (both analyze headless Gemini tool-missing behavior). Overlap also exists between `BUG_SESSION_REPORT.md` (BUG-010, BUG-024, BUG-025) and `data_engineering_control_plane_hardening.md` regarding catalog gates and intent coverage.
- ⚠️ **Mismatches & Risks**: Platform theme "advisory != enforced" highlights that documented rules must be backed by hard validator/harness checks; otherwise agents will bypass or simulate them.

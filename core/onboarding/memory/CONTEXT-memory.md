# Memory Architecture Context: `core/onboarding/memory`

This document provides an exhaustive, file-by-file reference for all components in [`core/onboarding/memory`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory).

---

## Executive Overview & ASCII Architectural Model

The `memory` package manages persistent workspace definitions, user decisions, wiki memory reuse cards, decision history logging, and memory confidence health validation. It ensures accepted business rules, canonical feature mappings, and domain entities are safely recorded, locked against race conditions, and reused across KPIs and workspaces.

```
┌─────────────────────────────────┐
│     User Decision Interface     │
│       (user_decisions.py)       │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐        ┌─────────────────────────────────┐
│  Workspace Definitions Store    ├───────►│    Wiki Memory Reuse Engine     │
│   (workspace_definitions.py)    │        │         (wiki_memory.py)        │
└────────────────┬────────────────┘        └────────────────┬────────────────┘
                 │                                          │
                 ▼                                          ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                       Memory Health & Audit Validator                      │
│                                 (health.py)                                │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/__init__.py#L1-L11)

- **Exact Purpose**: Package initialization file exposing primary memory structures and functions.
- **Key Functions / Classes**:
  - Exports [`WikiMemoryResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/__init__.py#L3), [`WorkspaceWikiMemoryBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/__init__.py#L3), and [`apply_workspace_definition`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/__init__.py#L4).
- **Inputs & Outputs**: Exports public API components.
- **Failure Modes & Edge Cases**: None.

---

### 2. [`health.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/health.py#L1-L578)

- **Exact Purpose**: Validates confidence-scored memory artifacts (`workspace_feature_definitions.json`, `team_memory/*.json`, etc.) without reading raw datasets or remote state.
- **Key Functions / Classes**:
  - [`MemoryHealthResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/health.py#L55-L73): Dataclass encapsulating memory health validation metrics.
  - [`MemoryHealthValidator(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/health.py#L76-L203): Discovers local and team JSON memory artifacts, evaluates entries for confidence bands (`high`, `medium`, `low`, `untrusted`), checks expiration (`is_expired`), and generates health findings (`critical`, `warning`, `healthy`).
  - [`_confidence(item, status)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/health.py#L307-L322): Calculates entry confidence score based on explicit numerical fields or status markers (`APPROVED_STATUS_MARKERS`, `WEAK_STATUS_MARKERS`).
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/health.py#L560-L578): CLI entry point for memory health validation.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, optional `--no-team-memory` flag.
  - *Outputs*: `MemoryHealthResult`, JSON and Markdown report artifacts under `interns/reports/memory_health/` and `interns/evidence/memory_health/`.
- **Failure Modes & Edge Cases**:
  - Malformed or invalid JSON memory files trigger `critical` severity `invalid_json` findings without crashing the run.

---

### 3. [`user_decisions.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/user_decisions.py#L1-L139)

- **Exact Purpose**: Handles application of direct user feature decisions to KPI feature mappings, requirements logs, and decision history.
- **Key Functions / Classes**:
  - [`apply_user_decision(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/user_decisions.py#L17-L75): Legacy direct-apply helper that wraps operations in `with workspace_lock(workspace_path):`, updates `kpi_feature_mapping.json`, recomputes mapping status, and appends to decision history.
  - [`apply_decision_to_feature(item, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/user_decisions.py#L78-L112): Mutates individual feature mapping dictionaries with state, resolution type, evidence, and timestamps.
  - [`append_user_decision_requirement(layout, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/user_decisions.py#L114-L139): Atomically appends user decisions to `interns/generated/requirements/requirements.json` using `read_json_or_quarantine`.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, KPI ID, feature name, decision state, resolution type, evidence note, optional source columns.
  - *Outputs*: Updated feature mapping summary dictionary.
- **Failure Modes & Edge Cases**:
  - Raises `ValueError` for unsupported decision states or if target `kpi_id`/`feature` is not found in the mapping.
  - Corrupt `requirements.json` triggers quarantine via `read_json_or_quarantine`.

---

### 4. [`wiki_memory.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/wiki_memory.py#L1-L582)

- **Exact Purpose**: Constructs scoped, reviewable reuse cards and maintains team-wide wiki memory indexes (`state/team_memory/wiki_memory_index.json`) for cross-KPI and cross-workspace decision reuse.
- **Key Functions / Classes**:
  - [`WikiMemoryResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/wiki_memory.py#L20-L33): Dataclass summarizing wiki memory preparation metrics.
  - [`WorkspaceWikiMemoryBuilder(repo_root, workspace, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/wiki_memory.py#L35-L270): Collects definitions from KPI registries, data models, and workflow checkpoints. Uses `named_lock` on `.wiki_memory.lock` to merge definitions into shared team memory safely across concurrent workspaces.
  - [`_reuse_cards(definitions, shared)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/wiki_memory.py#L183-L220): Evaluates local definitions against shared memory to recommend actions (`record_new_definition`, `review_conflict`, `auto_fill_draft_block_execution`, `reuse_available`, `suggest_only`).
  - [`prepare_main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/wiki_memory.py#L571-L582): CLI entry point.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, domain.
  - *Outputs*: `WikiMemoryResult`, updated `wiki_memory_index.json`, candidate JSON file `wiki_memory_candidates.json`, and reports under `interns/reports/wiki_memory/`.
- **Failure Modes & Edge Cases**:
  - Conflicting definitions between workspaces trigger `review_conflict` cards rather than overwriting definitions.
  - Uses cross-workspace `named_lock` to prevent concurrent team memory write corruption.

---

### 5. [`workspace_definitions.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/workspace_definitions.py#L1-L449)

- **Exact Purpose**: Manages workspace-level reusable feature definitions (`workspace_feature_definitions.json`) and applies them automatically across all applicable KPIs in a workspace.
- **Key Functions / Classes**:
  - [`apply_workspace_definition(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/workspace_definitions.py#L32-L123): Accepts reusable feature definition, validates non-expression tokens, updates `workspace_feature_definitions.json` atomically, and applies definition across KPI feature mappings.
  - [`load_workspace_definitions(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/workspace_definitions.py#L203-L233): Loads definitions using `read_json_or_quarantine` to fail loud on corruption.
  - [`upsert_workspace_definition(definitions, record)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/workspace_definitions.py#L235-L259): Inserts or updates definition records sorted by feature and KPI applicability.
  - [`apply_workspace_definitions_to_mapping(mapping, definitions)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/workspace_definitions.py#L265-L271): Applies stored workspace definitions to a feature mapping in order of specificity.
  - [`recompute_mapping_status(mapping)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/workspace_definitions.py#L335-L352): Recomputes overall KPI readiness status (`ready_for_sql` vs `blocked_questions_pending`), updates open questions, and retires stale `no_supporting_evidence` synthetic blockers once real evidence exists.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, feature name, decision state, resolution type, evidence note, definition text, optional source columns / KPI lists.
  - *Outputs*: Updated mapping summary dictionary, persisted `workspace_feature_definitions.json`.
- **Failure Modes & Edge Cases**:
  - Expression keywords (e.g. `distinct`, `disitnct` in `NON_FEATURE_TOKENS`) are rejected as non-feature tokens and raise `ValueError`.
  - Corrupt definition files cause `read_json_or_quarantine` to quarantine the file and raise rather than returning an empty skeleton that erases saved definitions.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None. All functions are active and covered by unit tests.
- 🔌 **Unwired Components**:
  - `user_decisions.py` contains `apply_user_decision`, marked as a legacy direct-apply path (`feature_resolver.py --apply-decision`). It has been hardened with explicit `with workspace_lock(workspace_path):` handling.
- 👯 **Logic & Code Duplication**:
  - Path normalization `_rel(path, root)`, timestamp generation `_now()`, and string normalization (`_norm`/`_slug`) are implemented in both `health.py` and `wiki_memory.py`.
  - `read_json_or_quarantine` is used in `user_decisions.py` and `workspace_definitions.py`, whereas `health.py` and `wiki_memory.py` use local `_load_json` helpers.
- ⚠️ **Broken References & Mismatches**: None found. All imports between `user_decisions.py`, `workspace_definitions.py`, and `wiki_memory.py` resolve correctly.

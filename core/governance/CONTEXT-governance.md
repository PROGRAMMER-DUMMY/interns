# Core Governance Architecture Context: `core/governance`

This document provides an exhaustive reference for all components in [`core/governance`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance).

---

## Executive Overview & Architectural Model

The `core/governance` package implements policy verification, audit log chains, PHI/PII redaction gates, evaluation standards, mode policies, operational signals, and injection protection guards.

```
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│   phi_gate.py    ├───────►│  audit_chain.py  ├───────►│ data_policy.py   │
└──────────────────┘        └──────────────────┘        └──────────────────┘
         │                           │                           │
         ▼                           ▼                           ▼
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│ injection_guard  │        │   evaluator.py   │        │ op_signals.py    │
└──────────────────┘        └──────────────────┘        └──────────────────┘
```

---

## File Details

### 1. [`audit_chain.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/audit_chain.py)

- **Exact Purpose**: Cryptographically verifiable event and action audit logging chain for workspace operations.
- **Key Functions / Classes**:
  - [`AuditChain`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/audit_chain.py#L20-L80): Appends tamper-evident hash records to `interns/state/events.jsonl`.
- **Inputs & Outputs**:
  - *Inputs*: Action payload, timestamp, operator signature.
  - *Outputs*: Audit record with SHA-256 hash pointer.
- **Failure Modes & Edge Cases**:
  - Detects modified past audit entries and flags chain corruption.

### 2. [`contracts.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/contracts.py)

- **Exact Purpose**: Governance contract definitions, metric standards, and validation constraints.
- **Key Functions / Classes**:
  - [`GovernanceContract`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/contracts.py#L15-L45): Schema definition for project data governance.
- **Inputs & Outputs**:
  - *Inputs*: Contract specification dictionary.
  - *Outputs*: Validated governance object.
- **Failure Modes & Edge Cases**:
  - Returns validation errors when mandatory governance rules are missing.

### 3. [`data_policy.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/data_policy.py)

- **Exact Purpose**: Enforces dataset access rules, isolation policies, and dataset allowlist restrictions.
- **Key Functions / Classes**:
  - [`DataPolicyManager`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/data_policy.py#L25-L75): Checks datasets against `dataset_allowlist` in `workspace_settings.json`.
- **Inputs & Outputs**:
  - *Inputs*: Dataset path or table name.
  - *Outputs*: Boolean allow/deny decision.
- **Failure Modes & Edge Cases**:
  - Blocks access to non-allowlisted data paths in restricted workspace modes.

### 4. [`evaluator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/evaluator.py)

- **Exact Purpose**: KPI execution scoring, accuracy evaluation, and output verification harness.
- **Key Functions / Classes**:
  - [`evaluate_kpi_execution(expected, actual)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/evaluator.py#L30-L90): Calculates precision, recall, row-count parity, and numeric drift.
- **Inputs & Outputs**:
  - *Inputs*: Ground truth dataset, generated KPI dataset.
  - *Outputs*: Scoring report with metric match percentages.
- **Failure Modes & Edge Cases**:
  - Handles schema mismatches gracefully by recording failed column comparisons.

### 5. [`injection_guard.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/injection_guard.py)

- **Exact Purpose**: Prevents prompt injection, malicious SQL injection, and command injection attacks in LLM inputs/outputs.
- **Key Functions / Classes**:
  - [`sanitize_prompt_input(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/injection_guard.py#L20-L55): Filters system prompt override strings and unsafe tokens.
- **Inputs & Outputs**:
  - *Inputs*: Raw user or dataset text.
  - *Outputs*: Sanitized string safe for model evaluation.
- **Failure Modes & Edge Cases**:
  - Rejects text containing explicit command execution payloads or jailbreak patterns.

### 6. [`mode_policy.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/mode_policy.py)

- **Exact Purpose**: Controls execution modes (`local_files`, `databricks_additive`, `databricks_exclusive`).
- **Key Functions / Classes**:
  - [`get_active_mode(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/mode_policy.py#L15-L45): Reads active mode policy from `workspace_settings.json`.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory path.
  - *Outputs*: Mode enum string.
- **Failure Modes & Edge Cases**:
  - Defaults to `local_files` mode if unconfigured.

### 7. [`op_signals.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/op_signals.py)

- **Exact Purpose**: Operational signal emitter tracking lifecycle events and workspace state transitions.
- **Key Functions / Classes**:
  - [`emit_op_signal(workspace_dir, signal_type, payload)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/op_signals.py#L20-L65): Emits structured operational signal.
- **Inputs & Outputs**:
  - *Inputs*: Workspace path, signal name, event details.
  - *Outputs*: Appended signal record in state store.
- **Failure Modes & Edge Cases**:
  - Ensures atomic append operations without blocking main execution thread.

### 8. [`phi_gate.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/phi_gate.py)

- **Exact Purpose**: Protected Health Information (PHI) and Personally Identifiable Information (PII) scanning and redaction gate.
- **Key Functions / Classes**:
  - [`scan_and_redact_phi(df_or_text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/phi_gate.py#L40-L150): Detects SSNs, MRNs, names, DOBs, phone numbers, and redacts them before logging or dashboard presentation.
- **Inputs & Outputs**:
  - *Inputs*: DataFrames, SQL strings, or log text.
  - *Outputs*: Redacted copy with sensitive values masked.
- **Failure Modes & Edge Cases**:
  - Preserves analytical schema while hashing or redacting individual identifiers.

### 9. [`provenance.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/provenance.py)

- **Exact Purpose**: Source data lineage and output provenance metadata tracker.
- **Key Functions / Classes**:
  - [`track_provenance(output_path, source_paths, transformations)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/provenance.py#L15-L50): Records lineage graph for generated tables and reports.
- **Inputs & Outputs**:
  - *Inputs*: Target artifact path, source dataset paths.
  - *Outputs*: Lineage manifest object.
- **Failure Modes & Edge Cases**:
  - Flags orphaned artifacts lacking verified source provenance.

### 10. [`selection_guard.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/selection_guard.py)

- **Exact Purpose**: Harness-agnostic enforcement of the AGENTS.md > Step 0 HARD STOP — no file mutation during a `set <workspace>` selection turn until the user confirms the file set. Moves the rule from prose (previously stated in both AGENTS.md and CLAUDE.md, and unenforced) into mechanism.
- **Key Functions / Classes**:
  - [`is_selection_prompt(prompt, cwd)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/selection_guard.py): True for `set <ws>`, `set current workspace to <ws>`, or a lone token matching a folder under `workspaces/`.
  - [`handle_event(event)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/selection_guard.py): Normalized dispatch — a payload carrying a prompt arms/disarms; a payload carrying a mutating tool name is gated. Returns the process exit code.
  - [`marker_path(session_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/selection_guard.py): Per-session marker in the OS temp dir, so no state lands in the repo.
- **Inputs & Outputs**:
  - *Inputs*: A harness hook event as JSON on stdin. Field names are normalized (`tool_name`/`toolName`/`tool`, `prompt`/`user_prompt`/`userPrompt`), so no single CLI's event schema is assumed.
  - *Outputs*: Exit 0 = allow, exit 2 = block with the remediation message on stderr.
- **Cross-CLI registration** (AGENTS.md: this repo is driven by interchangeable CLIs):
  - `claude-code` — `.claude/settings.json`, `PreToolUse` (Edit|Write|NotebookEdit) + `UserPromptSubmit`.
  - `codex` — `.codex/hooks.json`, same event/matcher shape, matcher includes `apply_patch`. **Unverified**: Codex's support for `PreToolUse`/`UserPromptSubmit` is not documented in this repo; the existence-guard idiom makes a no-op safe either way.
  - `gemini-cli` — no hook surface. It enforces via the `tools.allowed` allowlist in `.gemini/settings.json`, which currently contains **no** file-write tool, so writes already require an explicit prompt there. Nothing to register.
- **Failure Modes & Edge Cases**:
  - Fails **open** (exit 0) on malformed JSON, non-dict payloads, or any internal error — a guard must never wedge a session.
  - A *missing* script fails **closed** at the harness level (the interpreter errors before this module runs), so every registration uses the `[ ! -f "..." ] || python "..."` existence guard.
  - Disarms on the user's next non-selection message, which is by definition the confirmation turn.
  - Bare-token matching is scoped to real `workspaces/` folder names so ordinary prose cannot arm the guard.
- **Tests**: [`tests/test_selection_guard.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_selection_guard.py) (13 cases: rule, cross-harness field/tool-name normalization, stdin contract, fail-open).

### 11. [`semantic_contract.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/semantic_contract.py)

- **Exact Purpose**: Semantic layer contract specifications for metric definitions, dimensions, and cuts.
- **Key Functions / Classes**:
  - [`SemanticContract`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/semantic_contract.py#L20-L60): Validates business definitions against raw data schema types.
- **Inputs & Outputs**:
  - *Inputs*: KPI metric JSON definition.
  - *Outputs*: Validated semantic contract object.
- **Failure Modes & Edge Cases**:
  - Raises error when a metric expression references missing fact or dimension columns.

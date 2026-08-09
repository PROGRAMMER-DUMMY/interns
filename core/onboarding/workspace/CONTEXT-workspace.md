# Workspace Onboarding System (`core/onboarding/workspace`) Context Documentation

## 1. Executive Overview & ASCII Architectural Model

### Executive Overview
The `core/onboarding/workspace` subsystem provides a governed, local-first and cloud-extensible control plane for discovering, profiling, validating, orchestrating, and delegating data-engineering workflows across client workspaces. It manages the complete workspace lifecycle:
- **Intake & Selection**: Scanning and classifying raw data, documentation, and KPI registries without mutating workspace state ([`kickstart.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L1-L523)).
- **Bootstrap & Incremental Onboarding**: Fingerprinting inputs (`size + mtime_ns + sha256`), profiling datasets (Polars/DuckDB/Databricks), extracting KPI registries, building semantic contracts, and skipping unchanged datasets ([`bootstrap.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L1-L254), [`incremental.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L1-L275), [`onboarding.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1-L2692)).
- **Idempotent CLI Envelope**: Wrapping commands with process locking (`workspace_lock`), execution timing, trajectory recording, and state tripwire checks ([`cli_runner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L1-L326), [`idempotency.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L1-L195)).
- **Feature & Vocabulary Research**: Extracting domain-agnostic vocabulary from profiles, data dictionaries, and contracts ([`research.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L1-L398), [`vocabulary_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L1-L274)).
- **Validation & Bug Detection**: Validating 15+ contract types, checking generator freshness, detecting product bugs ([`validation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1-L1308), [`bugs.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L1-L532)).
- **Flow Orchestration & Specialist Delegation**: Running multi-stage stateful execution sessions, rendering blocker/checkpoint panels, and routing specialist subagents with programmatic verdicts and lean handoff briefs ([`flow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L1-L3871), [`flow_panels.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L1-L640), [`flow_io.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_io.py#L1-L22), [`delegation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L1-L853), [`handoff_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/handoff_cli.py#L1-L82), [`workflow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L1-L483)).
- **Reset & Cleanup**: Safely resetting generated artifacts while preserving raw user inputs and settings ([`cleanup.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L1-L476)).

### ASCII Architectural Model

```
                                  +-----------------------+
                                  | Operator / CLI Agent  |
                                  +-----------+-----------+
                                              |
                                              v
                                  +-----------------------+
                                  |     cli_runner.py     |
                                  | (Workspace Lock,      |
                                  |  Trajectory, Timing,  |
                                  |  Idempotency Check)   |
                                  +-----------+-----------+
                                              |
         +------------------------------------+------------------------------------+
         |                                    |                                    |
         v                                    v                                    v
+------------------+                +------------------+                +------------------+
|   kickstart.py   |                |   bootstrap.py   |                |     flow.py      |
| (Discovery &     |                |  incremental.py  |                | (Stateful Engine |
|  Task Seeding)   |                |  onboarding.py   |                |  Session Loop)   |
+--------+---------+                +--------+---------+                +--------+---------+
         |                                   |                                   |
         v                                   v                                   v
+------------------+                +------------------+                +------------------+
|   research.py    |                |  validation.py   |                |  flow_panels.py  |
| vocabulary_panel |                |     bugs.py      |                |    flow_io.py    |
+------------------+                +------------------+                +--------+---------+
                                                                                 |
                                                                                 v
                                                                        +------------------+
                                                                        |  delegation.py   |
                                                                        |  handoff_cli.py  |
                                                                        | (Specialists &   |
                                                                        |  Handoff Briefs) |
                                                                        +------------------+
```

---

## 2. Exhaustive Documentation of All 18 Files

### 1. `__init__.py`
- **File Link**: [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/__init__.py#L1-L25)
- **Exact Purpose**: Module initializer and public symbol exporter for workspace onboarding workflows.
- **Key Functions/Classes**:
  - `__all__`: ([L10-L24](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/__init__.py#L10-L24)) - Re-exports 13 key classes: `ArtifactValidationResult`, `AutoBootstrap`, `BootstrapResult`, `DatabricksReadiness`, `KickstartResult`, `OnboardingResult`, `WorkspaceArtifactValidator`, `WorkspaceBugDetector`, `WorkspaceBugReport`, `WorkspaceKickstarter`, `WorkspaceOnboarder`, `WorkspaceWorkflowOrchestrator`, `WorkspaceWorkflowResult`.
- **Inputs & Outputs**:
  - Inputs: Package imports.
  - Outputs: Public namespace for `core.onboarding.workspace`.
- **Failure Modes & Edge Cases**:
  - Fails at import time if submodules contain syntax or circular import errors.

---

### 2. `bootstrap.py`
- **File Link**: [`bootstrap.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L1-L254)
- **Exact Purpose**: Automatic local-safe workspace readiness checker and bootstrapper. It evaluates whether a workspace has up-to-date generated artifacts under `interns/` or triggers `WorkspaceOnboarder`.
- **Key Functions/Classes**:
  - `DatabricksReadiness`: ([L22-L27](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L22-L27)) - Data class representing Databricks connection readiness and approval requirement.
  - `BootstrapResult`: ([L31-L59](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L31-L59)) - Immutable result containing action (`reuse` vs `generated`), manifest path, fingerprint, and Databricks readiness status.
    - `summary()`: ([L40-L59](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L40-L59)) - Serializes result into dictionary format.
  - `AutoBootstrap`: ([L62-L238](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L62-L238)) - Orchestrates readiness checks and onboarding invocation.
    - `__init__()`: ([L63-L76](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L63-L76)) - Initializes layout and metadata store.
    - `ensure_ready()`: ([L78-L127](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L78-L127)) - Main entrypoint checking artifact currency; runs onboarding if stale.
    - `compute_fingerprint()`: ([L129-L137](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L129-L137)) - Computes SHA256 digest over input file paths, sizes, and content.
    - `check_databricks_readiness()`: ([L139-L180](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L139-L180)) - Evaluates Databricks configuration and gates live health check behind `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`.
    - `_fingerprint_inputs()`: ([L182-L195](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L182-L195)) - Discovers `docs/` and `datasets/` files to fingerprint.
    - `_is_current()`: ([L197-L209](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L197-L209)) - Validates fingerprint match and required artifact existence.
    - `required_artifacts()`: ([L211-L219](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L211-L219)) - Returns paths for solution SQL, experiment, evaluator, semantic contract, domain model, and profile index.
    - `_read_manifest()`: ([L221-L227](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L221-L227)) - Reads `bootstrap_manifest.json`.
    - `_write_status()`: ([L229-L238](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L229-L238)) - Writes `bootstrap_status.json` and updates metadata store.
  - `_file_hash()`: ([L241-L246](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L241-L246)) - Utility for chunked file SHA256 hashing.
  - `_rel()`: ([L249-L253](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L249-L253)) - Normalizes relative paths.
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `task` dictionary, `Config`, `sample_rows`.
  - Outputs: `BootstrapResult`, `bootstrap_manifest.json`, `bootstrap_status.json`.
- **Failure Modes & Edge Cases**:
  - Databricks health checks make network calls; skipped in local-safe mode to prevent round-trips unless remote execution is explicitly enabled.
  - Missing or unreadable manifest falls back safely to full onboarding generation.

---

### 3. `bugs.py`
- **File Link**: [`bugs.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L1-L532)
- **Exact Purpose**: Detects workspace-level product bugs (e.g. contradictions between file listing and onboarding, parser artifact questions, scoped definition overwrite risks) and generates governed bug reports.
- **Key Functions/Classes**:
  - `WorkspaceBug`: ([L29-L45](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L29-L45)) - Dataclass representing a detected bug, severity, impact, and fix direction.
    - `blocks_workflow`: ([L44-L45](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L44-L45)) - Evaluates if severity is `critical` or `high`.
  - `WorkspaceBugReport`: ([L49-L70](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L49-L70)) - Summary report of all detected bugs in a workspace.
    - `summary()`: ([L61-L70](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L61-L70)) - Serializes report into dictionary.
  - `WorkspaceBugDetector`: ([L73-L343](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L73-L343)) - Core detector class running heuristic rules against workspace state.
    - `run()`: ([L80-L111](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L80-L111)) - Runs all detection checks.
    - `write_report()`: ([L113-L124](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L113-L124)) - Writes `bug_report.json` and `bugs/current.md`.
    - `_detect_listing_onboarding_contradiction()`: ([L126-L193](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L126-L193)) - Detects `WS-BUG-001` (listing finds inputs but onboarding artifacts are empty).
    - `_detect_panel_artifact_question()`: ([L195-L249](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L195-L249)) - Detects `WS-BUG-002` (blocker panel asks about parser artifacts like `average`, `base`, `total`).
    - `_detect_scoped_definition_overwrite_risk()`: ([L251-L343](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L251-L343)) - Detects `WS-BUG-003` (scoped workspace feature definitions risk overwriting each other).
  - Helper Functions: `_evidence_summary` ([L346-L380](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L346-L380)), `_role_count` ([L383-L384](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L383-L384)), `_current_feature_kpis` ([L387-L403](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L387-L403)), `_feature_coverage_is_preserved` ([L406-L418](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L406-L418)), `_definition_coverage` ([L421-L428](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L421-L428)), `_markdown_report` ([L431-L482](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L431-L482)), `main` ([L515-L527](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L515-L527)).
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace` path, input inventory, profile index, contracts.
  - Outputs: `WorkspaceBugReport`, `bug_report.json`, `bugs/current.md`.
- **Failure Modes & Edge Cases**:
  - Catches contradictory states early; missing contract files yield empty dictionaries rather than crashing.

---

### 4. `cleanup.py`
- **File Link**: [`cleanup.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L1-L476)
- **Exact Purpose**: Plans and executes workspace reset operations, removing generated `interns/` artifacts, deployment indexes, and stale task references without deleting user source documents or datasets (`docs/`, `datasets/`).
- **Key Functions/Classes**:
  - `CleanupAction`: ([L22-L26](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L22-L26)) - Class representing an individual cleanup step (action, target, reason, status).
  - `WorkspaceCleanupResult`: ([L30-L40](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L30-L40)) - Overall result of plan or apply operation.
  - `WorkspaceReferenceCleaner`: ([L43-L377](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L43-L377)) - Core engine enforcing safety boundaries.
    - `plan()`: ([L64-L132](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L64-L132)) - Constructs dry-run list of cleanup actions.
    - `apply()`: ([L134-L173](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L134-L173)) - Executes planned deletions and file rewrites.
    - `_require_delete_confirmation()`: ([L175-L183](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L175-L183)) - Enforces hard permission check for deletion.
    - `_repo_state_actions()`: ([L185-L254](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L185-L254)) - Discovers repo-level runtime logs and Databricks deployment references.
    - `_rewrite_task_config()`: ([L256-L271](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L256-L271)) - Removes workspace references from `config/tasks.json`.
    - `_rewrite_deployment_index()`: ([L273-L285](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L273-L285)) - Prunes deployment index entries.
    - `_rewrite_wiki_memory_index()`: ([L287-L304](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L287-L304)) - Prunes team wiki memory entries.
    - `_ensure_deletable()`: ([L333-L340](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L333-L340)) - Safety check restricting deletions to `interns/`, `wiki/`, and repo `state/`.
    - `_preserve_workspace_settings()`: ([L342-L350](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L342-L350)) - Backs up `workspace_settings.json` before deleting `interns/`.
  - Functions: `run_cleanup` ([L380-L411](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L380-L411)), `main` ([L416-L471](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cleanup.py#L416-L471)).
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace`, boolean flags (`apply`, `confirm_delete`, `remove_workspace_interns`, `remove_repo_state`, `remove_task_config`).
  - Outputs: `WorkspaceCleanupResult`, mutated task/deployment/wiki index files.
- **Failure Modes & Edge Cases**:
  - Raises `PermissionError` if `--confirm-delete` does not exactly match `--workspace`.
  - Raises `ValueError` if attempting to delete paths outside authorized state directories.

---

### 5. `cli_runner.py`
- **File Link**: [`cli_runner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L1-L326)
- **Exact Purpose**: Governed CLI runner envelope providing workspace process locking, execution timing, trajectory recording, idempotency checking, state tripwire verification, and skill activation signaling.
- **Key Functions/Classes**:
  - `_is_mutating()`: ([L51-L53](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L51-L53)) - Checks if command name starts with mutating prefixes (`apply-`, `finalize-`, `onboard-`, etc.).
  - `resolve_workspace_path()`: ([L56-L57](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L56-L57)) - Resolves absolute workspace path.
  - `_snapshot_state_safe()`: ([L60-L68](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L60-L68)) - Best-effort snapshot of user-decided state before command execution.
  - `_verify_state_safe()`: ([L71-L84](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L71-L84)) - Verifies user-decided state was preserved after command execution.
  - `_payload_from_result()`: ([L87-L99](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L87-L99)) - Extracts payload dictionary from command result objects.
  - `run_workspace_command()`: ([L102-L322](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/cli_runner.py#L102-L322)) - Core envelope function wrapping CLI executions. Handles idempotency replays, locks, events, and skill suggestions.
- **Inputs & Outputs**:
  - Inputs: `command`, `workspace`, `repo_root`, zero-arg callable `fn`, `op_args`, `allow_replay`, `record_idempotent`, etc.
  - Outputs: Returns exit code `0` (success/replay) or `2` (lock timeout). Prints JSON payload.
- **Failure Modes & Edge Cases**:
  - Catches `WorkspaceLockTimeout` and returns exit code 2.
  - Re-executes `fn()` on idempotent replays when possible to ensure reported counters match live state rather than returning stale cached counts.
  - **A structured failure is never recorded as an applied op.** `record_op` fires only when the payload does not carry an explicit `ok: False`. The cloud-first commands report refusals and failures as a payload rather than by raising, so recording on "`fn()` returned" stamped runs that executed nothing as done, and every retry afterwards came back `idempotent_replay` demanding `--allow-replay` for work that never happened (F24). Payloads with no `ok` key are unaffected.

---

### 6. `delegation.py`
- **File Link**: [`delegation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L1-L853)
- **Exact Purpose**: Manages stage-triggered specialist delegation for `workspace-flow`. Provides programmatic verdicts for workflow stages and formats lean handoff briefs (`.md`) for side agents.
- **Key Functions/Classes**:
  - `DelegationVerdict`: ([L37-L42](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L37-L42)) - Container for programmatic check results (`status`, `summary`, `details`).
  - `DelegationRequest`: ([L46-L65](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L46-L65)) - Briefing instructions for subagent invocation.
  - `DelegationEvent`: ([L69-L113](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L69-L113)) - Recorded delegation event log entry.
    - `to_trajectory_event()`: ([L96-L113](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L96-L113)) - Converts event for `trajectory.jsonl`.
  - Constants & Routing Maps:
    - `STAGE_ROUTING`: ([L220-L364](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L220-L364)) - Mapping of workflow stages to required specialists and suggested skills. `tests.test_agent_skill_routing::test_every_skill_is_routed` fails if any skill under `skills/` is unrouted, so adding a skill means adding it here in the same change. `context-map-sync` sits on `regression_review` because it fires on CODE change (new symbol, changed signature or CLI flag, changed failure mode), not on a workspace stage.
  - Core Functions:
    - `routing_for()`: ([L367-L374](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L367-L374)) - Retrieves agent and skill roster for a given stage.
    - `_build_delegation_request()`: ([L377-L395](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L377-L395)) - Constructs `DelegationRequest` from stage brief templates.
    - `record_delegation()`: ([L398-L438](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L398-L438)) - Evaluates `verdict_fn`, writes handoff file, and records trajectory event.
    - `_write_delegation_handoff()`: ([L441-L482](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L441-L482)) - Writes `interns/state/handoffs/<stage>__<agent>.md`.
    - `_append_trajectory()`: ([L485-L497](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L485-L497)) - Appends event to `trajectory.jsonl` under `workspace_lock`.
    - `render_delegation_markdown()`: ([L500-L515](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L500-L515)) - Formats delegation events into Markdown.
    - `recent_delegations()`: ([L518-L545](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L518-L545)) - Reads last N delegation records from trajectory log.
  - Stage Verdict Calculators:
    - `verdict_from_relationship_summary()`: ([L548-L577](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L548-L577))
    - `verdict_from_source_to_target_summary()`: ([L580-L599](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L580-L599))
    - `verdict_from_validation_summary()`: ([L602-L622](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L602-L622))
    - `verdict_from_kpi_completion()`: ([L625-L651](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L625-L651))
    - `verdict_from_verification()`: ([L654-L684](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L654-L684))
    - `verdict_from_kpi_definition()`: ([L687-L714](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L687-L714))
    - `verdict_from_engine_generation()`: ([L717-L741](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L717-L741))
    - `verdict_from_result_review()`: ([L744-L817](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L744-L817)) - Inspects result content for zero rows and all-blank columns.
    - `verdict_from_dashboard_summary()`: ([L820-L834](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L820-L834))
- **Inputs & Outputs**:
  - Inputs: Stage name, contract summaries, layout.
  - Outputs: `DelegationEvent`, handoff Markdown file under `interns/state/handoffs/`.
- **Failure Modes & Edge Cases**:
  - Catches exceptions inside `verdict_fn` and logs `status="error"` gracefully.

---

### 7. `flow.py`
- **File Link**: [`flow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L1-L3871)
- **Exact Purpose**: Central orchestrator engine for `workspace-flow`. Implements stateful multi-stage session management, step advancing, quality gates, result preview generation, and CLI subcommands.
- **Key Functions/Classes**:
  - `WorkspaceFlowResult`: ([L122-L133](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L122-L133)) - Dataclass holding flow operation summary.
  - `WorkspaceFlow`: ([L136-L1946](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L136-L1946)) - Stateful workspace flow manager.
    - `__init__()`: ([L137-L154](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L137-L154))
    - `from_session()`: ([L157-L170](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L157-L170)) - Factory initializing flow from existing session ID.
    - `start()`: ([L172-L209](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L172-L209)) - Starts or resumes flow session.
    - `answer()`: ([L211-L285](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L211-L285)) - Applies answer option and advances flow.
    - `review()`: ([L287-L355](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L287-L355)) - Generates review panel for current state.
    - `diff()`: ([L361-L369](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L361-L369)) - Computes diff relative to baseline.
    - `results()`: ([L371-L388](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L371-L388)) - Emits KPI result packets.
    - `_advance_until_stop()`: ([L390-L1012](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L390-L1012)) - Core state machine loop executing stages until an answer or gate is required.
    - `_emit_side_outputs()`: ([L1014-L1160](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L1014-L1160)) - Generates dbt models, presentation cards, and dashboard specs.
    - `_open_live_dashboard()`: ([L1162-L1202](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L1162-L1202)) - Launches live dashboard background process.
    - `_run_data_quality_gate()`: ([L1229-L1254](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L1229-L1254)) - Evaluates data quality contracts.
    - `_run_data_understanding_gate()`: ([L1279-L1352](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L1279-L1352)) - Evaluates data quality tiers and schema types.
    - `_write_result_preview()`: ([L1452-L1715](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L1452-L1715)) - Executes SQL queries and builds markdown/JSON result previews.
  - Helper Functions:
    - `latest_open_session()`: ([L2046-L2085](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2046-L2085))
    - `latest_session()`: ([L2088-L2123](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2088-L2123))
    - `write_session_handoff()`: ([L2152-L2211](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2152-L2211))
    - `compute_workflow_diff()`: ([L2214-L2402](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2214-L2402))
    - `_emit_result_packet()`: ([L2613-L2713](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2613-L2713))
    - `main()`: ([L2939-L3386](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2939-L3386))
    - `pipeline_main()`: ([L3418-L3867](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L3418-L3867))
- **Inputs & Outputs**:
  - Inputs: Intent flags, session IDs, option answers, workspace path.
  - Outputs: Session state JSON, current panel files (`current.json`/`current.md`), KPI result packets.
- **Failure Modes & Edge Cases**:
  - Handles stale result packets by checking underlying KPI SQL timestamps.
  - Handles execution errors during DuckDB result view generation without corrupting session state.

---

### 8. `flow_io.py`
- **File Link**: [`flow_io.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_io.py#L1-L22)
- **Exact Purpose**: Isolated JSON reader utility extracted from `flow.py` to allow `flow_panels.py` to perform file IO without causing a circular import back into `flow.py`.
- **Key Functions/Classes**:
  - `_read_json()`: ([L14-L21](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_io.py#L14-L21)) - Safely reads JSON file and returns dictionary.
- **Inputs & Outputs**:
  - Inputs: `Path` instance.
  - Outputs: `dict[str, Any]` (or `{}` on missing/corrupt file).
- **Failure Modes & Edge Cases**:
  - Catches `JSONDecodeError` and missing file exceptions silently, returning an empty dictionary.

---

### 9. `flow_panels.py`
- **File Link**: [`flow_panels.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L1-L640)
- **Exact Purpose**: Pure rendering and compaction functions for flow panels, resolution reviews, data understanding gates, and result markdown outputs.
- **Key Functions/Classes**:
  - `_compact_panel()`: ([L22-L64](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L22-L64)) - Assembles standardized compact panel dictionary.
  - `_render_panel_markdown()`: ([L66-L221](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L66-L221)) - Renders panel dictionary into human-readable Markdown card.
  - `_build_kpi_resolution_review()`: ([L223-L265](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L223-L265)) - Builds structured KPI resolution review payload.
  - `_render_resolution_review()`: ([L267-L322](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L267-L322)) - Renders resolution review table and mapping details.
  - `_build_hidden_panel_harness()`: ([L324-L348](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L324-L348)) - Checks panel against regression rules.
  - `_extract_source_filters()`: ([L364-L392](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L364-L392)) - Extracts comparison filters and literals from KPI cuts.
  - `_summarize_current_data_model()`: ([L424-L462](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L424-L462)) - Summarizes tables and relationships.
  - `_render_data_understanding_markdown()`: ([L483-L578](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L483-L578)) - Renders data quality tier and schema type evidence.
  - `_run_cost_lines()`: ([L580-L612](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L580-L612)) - Renders honest warehouse cost information.
  - `_render_results_markdown()`: ([L615-L632](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_panels.py#L615-L632)) - Formats query result tables into Markdown.
- **Inputs & Outputs**:
  - Inputs: Raw panel data, contract dictionaries, layout.
  - Outputs: Markdown strings, formatted table blocks, compacted panel dictionaries.
- **Failure Modes & Edge Cases**:
  - Handles missing cost reconciliation data by explicitly reporting "not reconciled" rather than falsely showing `$0.00`.

---

### 10. `handoff_cli.py`
- **File Link**: [`handoff_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/handoff_cli.py#L1-L82)
- **Exact Purpose**: Command-line utility for side agents to fetch and render delegation handoff briefs stored under `interns/state/handoffs/`.
- **Key Functions/Classes**:
  - `_handoffs_dir()`: ([L21-L23](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/handoff_cli.py#L21-L23)) - Resolves `interns/state/handoffs` directory.
  - `_latest()`: ([L26-L39](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/handoff_cli.py#L26-L39)) - Finds newest handoff file using nanosecond mtime and lexicographical name tiebreaking.
  - `main()`: ([L42-L77](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/handoff_cli.py#L42-L77)) - Argument parser for `latest` and `render` subcommands.
- **Inputs & Outputs**:
  - Inputs: `--workspace`, `--repo-root`, `--stage`, `--agent`, `--path-only`.
  - Outputs: Prints file path or handoff Markdown content to stdout.
- **Failure Modes & Edge Cases**:
  - Returns exit code 1 and prints `(no handoff found)` if target file does not exist.

---

### 11. `idempotency.py`
- **File Link**: [`idempotency.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L1-L195)
- **Exact Purpose**: Manages deterministic operation hashing and persistent log tracking in `interns/state/applied_ops.jsonl` to ensure `apply-*` commands are idempotent.
- **Key Functions/Classes**:
  - `compute_op_id()`: ([L29-L40](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L29-L40)) - Computes 16-character SHA256 hex digest for arbitrary arguments.
  - `fingerprint_paths()`: ([L43-L64](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L43-L64)) - Computes composite content hash over file paths to incorporate artifact state into `op_id`.
  - `AppliedOp`: ([L68-L74](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L68-L74)) - Frozen dataclass representing a logged operation.
  - `_iter_records()`: ([L77-L92](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L77-L92)) - Iterates through lines of `applied_ops.jsonl`.
  - `is_duplicate_op()`: ([L95-L101](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L95-L101)) - Checks if `op_id` exists in the log.
  - `record_op()`: ([L104-L141](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L104-L141)) - Atomically checks duplicate status and appends new `AppliedOp` record under `workspace_lock`.
  - `get_applied_op()`: ([L144-L158](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L144-L158)) - Retrieves latest `AppliedOp` matching `op_id`.
  - `list_applied_ops()`: ([L161-L184](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/idempotency.py#L161-L184)) - Lists recorded operations in reverse chronological order.
- **Inputs & Outputs**:
  - Inputs: `workspace_path`, command name, arguments, payload dict.
  - Outputs: Boolean success, `AppliedOp` instances, `applied_ops.jsonl` entries.
- **Failure Modes & Edge Cases**:
  - Prevents race conditions by locking duplicate check and log append together inside a single critical section.

---

### 12. `incremental.py`
- **File Link**: [`incremental.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L1-L275)
- **Exact Purpose**: Implements incremental onboarding fingerprinting (`size + mtime_ns + sha256`) to skip profiling unchanged datasets and allow fast re-onboarding.
- **Key Functions/Classes**:
  - `fingerprint_file()`: ([L49-L56](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L49-L56)) - Returns fingerprint dictionary for a file.
  - `_content_hash()`: ([L59-L75](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L59-L75)) - Computes full SHA256 or partial SHA256 (head + tail + size) for files exceeding 64MB (`FULL_HASH_LIMIT_BYTES`).
  - `fingerprint_inputs()`: ([L78-L111](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L78-L111)) - Fingerprints a sorted list of relative paths, reusing hashes if size and mtime match prior manifest.
  - `ChangeSet`: ([L115-L125](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L115-L125)) - Dataclass tracking `unchanged`, `changed`, `added`, and `removed` file lists.
    - `nothing_changed`: ([L124-L125](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L124-L125)) - Returns True if no changes detected.
  - `diff_fingerprints()`: ([L128-L153](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L128-L153)) - Compares fingerprints by content hash, ignoring mtime shifts.
  - `load_manifest()`: ([L164-L179](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L164-L179)) - Reads `onboarding_manifest.json`.
  - `build_manifest_payload()`: ([L182-L203](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L182-L203)) - Assembles deterministic JSON manifest payload.
  - `write_manifest()`: ([L206-L212](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L206-L212)) - Writes `onboarding_manifest.json` and `onboarding_manifest.md`.
  - `render_manifest_markdown()`: ([L215-L247](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L215-L247)) - Formats manifest summary into Markdown.
  - `artifacts_exist()`: ([L250-L274](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/incremental.py#L250-L274)) - Verifies all recorded profile and output artifacts still exist on disk.
- **Inputs & Outputs**:
  - Inputs: `repo_root`, relative file paths, state directory `Path`.
  - Outputs: `ChangeSet`, `onboarding_manifest.json`, `onboarding_manifest.md`.
- **Failure Modes & Edge Cases**:
  - Partial hashing avoids reading gigabyte-scale datasets entirely into memory.
  - Missing artifacts invalidate skip optimization and force re-generation.

---

### 13. `kickstart.py`
- **File Link**: [`kickstart.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L1-L523)
- **Exact Purpose**: Bridges raw enterprise workspaces to the governed experiment loop. Discovers datasets and documentation, triggers bootstrap, seeds `config/tasks.json`, and records discovery questions.
- **Key Functions/Classes**:
  - `DiscoveredFile`: ([L59-L65](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L59-L65)) - Represents categorized document file.
  - `DiscoveredDataset`: ([L69-L73](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L69-L73)) - Represents dataset format and size.
  - `KickstartResult`: ([L77-L93](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L77-L93)) - Summary result of kickstart process.
  - `WorkspaceKickstarter`: ([L96-L418](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L96-L418)) - Engine performing discovery and task initialization.
    - `run()`: ([L121-L168](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L121-L168)) - Executes full kickstart workflow.
    - `discover()`: ([L170-L207](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L170-L207)) - Scans and classifies workspace files into categories (KPI registry, SLA, policy, contract, dictionary, data model, methodology).
    - `_task_from_bootstrap()`: ([L220-L264](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L220-L264)) - Assembles task dictionary from bootstrap artifacts.
    - `_upsert_task_config()`: ([L266-L290](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L266-L290)) - Updates `config/tasks.json` safely.
    - `_write_discovery()`: ([L292-L300](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L292-L300)) - Writes `enterprise_discovery.json`.
    - `_write_kickstart_questions()`: ([L302-L330](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L302-L330)) - Writes `open_questions.md`.
  - Helper Functions: `default_optimization_policy` ([L421-L422](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L421-L422)), `accepted_defaults` ([L425-L435](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L425-L435)), `main` ([L497-L518](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L497-L518)).
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace` path, `task_id`, `domain`.
  - Outputs: `KickstartResult`, `enterprise_discovery.json`, task entry in `config/tasks.json`, `open_questions.md`.
- **Failure Modes & Edge Cases**:
  - Aborts execution if `WorkspaceBugDetector` flags blocking bugs.

---

### 14. `onboarding.py`
- **File Link**: [`onboarding.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1-L2692)
- **Exact Purpose**: The core onboarding engine. Reads raw workspace inputs, extracts KPI definitions from Excel/CSV/JSON/Markdown/SQL, profiles local datasets and Databricks Unity Catalog tables, builds domain models and semantic contracts, and writes baseline SQL/evaluator scripts under `interns/`.
- **Key Functions/Classes**:
  - `WorkspaceInputs`: ([L74-L89](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L74-L89)) - Dataclass representing discovered data files, KPI registries, data models, and Databricks tables.
  - `KpiDefinition`: ([L92-L106](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L92-L106)) - Dataclass holding parsed KPI fields and metric/cuts provenance.
  - `OnboardingResult`: ([L110-L136](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L110-L136)) - Overall summary of generated artifacts and next steps.
  - `_is_platform_written_relation()`: ([L152-L189](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L152-L189)) - Critical guard excluding platform-written relations (`stg_`, `int_`, `fct_`, `kpi_*_results`) from source profiling loops.
  - `WorkspaceOnboarder`: ([L192-L2104](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L192-L2104)) - Primary engine class.
    - `run()`: ([L217-L221](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L217-L221)) - Acquires lock and executes `_run_locked()`.
    - `_extract_data_model_documents()`: ([L223-L282](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L223-L282)) - Parses PDF/DOCX documentation.
    - `_scan_documents()`: ([L341-L451](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L341-L451)) - Scans documentation for KPIs, open questions, and relationship notes.
    - `_run_locked()`: ([L631-L945](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L631-L945)) - Main onboarding pipeline: input discovery, incremental skip evaluation, KPI loading, profiling, contract generation, script generation, and metadata storage.
    - `discover_inputs()`: ([L947-L1000](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L947-L1000)) - Discovers datasets, registries, and models based on `databricks_source_mode`.
    - `load_kpis()`: ([L1082-L1115](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1082-L1115)) - Loads and parses KPI definitions across formats.
    - `profile_inputs()`: ([L1322-L1361](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1322-L1361)) - Profiles local datasets using `DataModelProfiler`.
    - `profile_databricks_tables()`: ([L1363-L1405](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1363-L1405)) - Profiles Databricks Unity Catalog tables via warehouse SQL sampling. This is the ONLY source of column-level evidence for a workspace with no local `datasets/` — discovery is metadata-only (`columns: null`), so KPI feature resolution is blind until this runs.
    - `_profiling_source_pair(source)`: **which (catalog, schema) actually holds the data now.** `databricks_source.catalog`/`.schema` describe the raw source declared at intake (`rcm`/`default`); once the cloud-first spine has run, the data lives where ingestion LANDED it — the provisioned catalog's bronze schema from `provision_plan.json` (`rcm_dev`/`bronze`). Querying the declaration finds nothing, `profile_index.json` stays `{"profiles": []}`, and every KPI feature stays `blocked_missing_evidence` (F25). A workspace pointing at pre-existing UC tables, which never provisioned, keeps its declared pair.
    - `_databricks_source_tables()` still degrades to `[]` when the warehouse is unreachable, but records the reason in `_databricks_discovery_error`, which the exclusive-mode zero-tables warning then prints. Without it an unreachable warehouse, a missing catalog, and a genuinely empty schema all read identically as "zero tables".
    - `_build_domain_model()`: ([L1407-L1429](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1407-L1429)) - Builds `domain_model.json`.
    - `_build_semantic_contract()`: ([L1495-L1529](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1495-L1529)) - Builds `semantic_contract.json`.
    - `_write_baseline_sql()`, `_write_experiment_script()`, `_write_evaluator_script()`, `_write_report()`: ([L1612-L1797](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L1612-L1797)) - Emits executable baseline code and human reports.
  - KPI Parsers: `_read_excel_kpis_with_detection` ([L2138-L2158](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2138-L2158)), `_extract_tabular_kpis` ([L2174-L2246](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2174-L2246)), `_read_xlsx_xml_kpis` ([L2249-L2298](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2249-L2298)), `_read_json_kpis` ([L2363-L2387](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2363-L2387)), `_read_markdown_kpis` ([L2390-L2478](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2390-L2478)), `_read_sql_comment_kpis` ([L2481-L2492](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2481-L2492)).
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace` path, sample row count, force flag.
  - Outputs: `OnboardingResult`, generated contracts in `interns/generated/contracts/`, profiles in `interns/generated/profiles/`, solution SQL, evaluation scripts.
- **Failure Modes & Edge Cases**:
  - Handles corrupt Excel files via fallback XML zip parsing (`_read_xlsx_xml_kpis`).
  - Ignores platform's own generated result views during source table discovery.

---

### 15. `research.py`
- **File Link**: [`research.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L1-L398)
- **Exact Purpose**: Derives a domain-agnostic vocabulary dictionary (`workspace_vocabulary.json`) from workspace profiles, data dictionaries, feature definitions, and KPI registries, complete with confidence scores and evidence sources.
- **Key Functions/Classes**:
  - `TermEvidence`: ([L51-L63](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L51-L63)) - Dataclass tracking term, evidence sources, sample values, and confidence score.
  - `VocabularyResult`: ([L67-L91](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L67-L91)) - Final vocabulary artifact container.
  - `WorkspaceResearcher`: ([L138-L379](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L138-L379)) - Core term mining engine.
    - `research()`: ([L147-L177](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L147-L177)) - Mines evidence across all sources and writes output.
    - `_mine_workspace_feature_definitions()`: ([L204-L214](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L204-L214)) - Mines high-confidence user-confirmed feature definitions.
    - `_mine_data_dictionary()`: ([L216-L230](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L216-L230)) - Mines term maps from CSV/Markdown/Text documentation.
    - `_mine_kpi_registry()`: ([L256-L282](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L256-L282)) - Mines terms and filter literals from KPI metric/cuts text.
    - `_mine_profiles()`: ([L284-L321](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L284-L321)) - Mines entity, column, and categorical sample value terms from dataset profiles.
    - `_classify_seed()`: ([L323-L332](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L323-L332)) - Heuristically classifies terms into `financial_terms`, `temporal_terms`, `identifier_terms`, or `entity_terms`.
    - `_overall_confidence()`: ([L347-L353](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L347-L353)) - Calculates overall weighted confidence.
    - `_write()`: ([L355-L379](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L355-L379)) - Emits `workspace_vocabulary.json`.
  - `research_workspace_vocabulary()`: ([L382-L385](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L382-L385)) - Public module entrypoint.
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace` path, contract files, dataset profiles.
  - Outputs: `VocabularyResult`, `workspace_vocabulary.json`.
- **Failure Modes & Edge Cases**:
  - Marks `needs_user_confirmation=True` when overall confidence drops below `0.4`.

---

### 16. `validation.py`
- **File Link**: [`validation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1-L1308)
- **Exact Purpose**: Exhaustively validates generated workspace contracts, profile indexes, feature mappings, question panels, solution SQL, execution harness results, generator freshness, and bug reports.
- **Key Functions/Classes**:
  - `ArtifactValidationResult`: ([L64-L84](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L64-L84)) - Dataclass holding lists of checked files, errors, and warnings.
  - `WorkspaceArtifactValidator`: ([L87-L1196](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L87-L1196)) - Main validation engine.
    - `run()`: ([L94-L116](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L94-L116)) - Sequentially executes all validation checks.
    - `_validate_generator_freshness()`: ([L132-L164](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L132-L164)) - Validates that generated artifacts are not older than generator source code.
    - `_validate_workspace_bugs()`: ([L166-L173](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L166-L173)) - Integrates `WorkspaceBugDetector` and converts blocking bugs to validation errors.
    - `_validate_profile_index()`: ([L198-L216](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L198-L216))
    - `_validate_kpi_registry()`: ([L250-L271](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L250-L271))
    - `_validate_feature_mapping()`: ([L273-L292](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L273-L292))
    - `_validate_dictionary_conflicts()`: ([L478-L580](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L478-L580)) - Detects conflicts between data dictionary entries and feature mappings.
    - `_validate_kpi_execution_harness()`: ([L802-L887](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L802-L887)) - Verifies execution harness status against baseline SQL.
    - `_validate_medallion_manifest()`: ([L1045-L1167](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1045-L1167)) - Validates bronze/silver/gold medallion layer manifests.
  - Helper Functions: `_collect_pii_columns_from_sc` ([L1255-L1281](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1255-L1281)), `main` ([L1293-L1303](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1293-L1303)).
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace` path, all `interns/` JSON and SQL artifacts.
  - Outputs: `ArtifactValidationResult`.
- **Failure Modes & Edge Cases**:
  - Flags missing mandatory keys, schema mismatches, stale generator stamps, and unappliable placeholder options as errors.

---

### 17. `vocabulary_panel.py`
- **File Link**: [`vocabulary_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L1-L274)
- **Exact Purpose**: Prepares vocabulary confirmation panels and applies user answers to approve or revise low-confidence terms in `workspace_vocabulary.json`.
- **Key Functions/Classes**:
  - `VocabularyConfirmationPanel`: ([L31-L45](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L31-L45)) - Dataclass holding panel paths and term counts.
  - `prepare_vocabulary_confirmation_panel()`: ([L48-L148](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L48-L148)) - Constructs `vocabulary_confirmation_panel/current.json` and `current.md`.
  - `apply_vocabulary_confirmation_answer()`: ([L151-L219](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L151-L219)) - Applies user decision (confirm/override) and updates `workspace_vocabulary.json`.
  - `_render_panel_markdown()`: ([L233-L267](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L233-L267)) - Formats vocabulary panel Markdown.
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace` path, optional `--answer` string.
  - Outputs: `VocabularyConfirmationPanel`, updated `workspace_vocabulary.json`, panel artifacts.
- **Failure Modes & Edge Cases**:
  - Preserves previously user-confirmed term states during re-runs.

---

### 18. `workflow.py`
- **File Link**: [`workflow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L1-L483)
- **Exact Purpose**: High-level orchestrator managing governed workspace checkpoints across three modes (`plan`, `local-safe`, `autopilot`), controlling bounded autopilot steps and presentation exports.
- **Key Functions/Classes**:
  - `WorkspaceWorkflowResult`: ([L28-L39](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L28-L39)) - Dataclass summarizing workflow checkpoint execution.
  - `WorkspaceWorkflowOrchestrator`: ([L42-L357](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L42-L357)) - Main orchestrator class.
    - `prepare()`: ([L62-L101](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L62-L101)) - Prepares onboarding, KPI/data-model generation, autopilot steps, validation, presentation export, and wiki memory.
    - `_ensure_onboarded()`: ([L103-L114](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L103-L114)) - Triggers onboarding if artifacts are missing.
    - `_run_bounded_autopilot()`: ([L130-L132](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L130-L132)) - Runs bounded KPI and data-model autopilot transitions.
    - `_autopilot_kpi_generation()`: ([L134-L151](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L134-L151)) - Advances KPI generation panel automatically for low-risk routes.
    - `_autopilot_data_model_generation()`: ([L153-L169](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L153-L169)) - Advances data-model generation panel automatically.
    - `_autopilot_kpi_blocker_once()`: ([L202-L219](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L202-L219)) - Applies recommended KPI blocker option if safe.
    - `_panel()`: ([L273-L344](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L273-L344)) - Assembles `workflow/current.json` panel payload.
  - Helper Functions:
    - `_safe_kpi_blocker_option()`: ([L375-L382](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L375-L382)) - Filters options to JSON-backed derived/physical column choices.
    - `_render_workflow_markdown()`: ([L409-L446](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L409-L446)) - Renders `workflow/current.md`.
    - `prepare_main()`: ([L468-L482](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L468-L482)) - CLI entrypoint `@anchored("prepare-workspace-workflow")`.
- **Inputs & Outputs**:
  - Inputs: `repo_root`, `workspace` path, `domain`, `mode` (`plan`, `local-safe`, `autopilot`).
  - Outputs: `WorkspaceWorkflowResult`, `workflow/current.json`, `workflow/current.md`.
- **Failure Modes & Edge Cases**:
  - Autopilot mode strictly enforces boundaries: automatically stops before relationship approval, DDL/SQL execution, remote execution, or file deletions.

---

## 3. Code Hygiene & Integrity Audit Section

### A. Dead Code & Unused Helpers
1. **`bugs.py`**:
   - `_current_feature_kpis()` ([L387-L403](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L387-L403)), `_feature_coverage_is_preserved()` ([L406-L418](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L406-L418)), `_definition_coverage()` ([L421-L428](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L421-L428)), `_role_count()` ([L383-L384](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L383-L384)) - Internal helper functions that are only used in conditional sub-branches or not referenced outside `bugs.py`.
2. **`onboarding.py`**:
   - `_read_excel_kpis()` ([L2134-L2135](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2134-L2135)) and `_read_tabular_kpis()` ([L2170-L2171](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2170-L2171)) are 1-line legacy wrapper shims that delegate directly to `_read_excel_kpis_with_detection` and `_extract_tabular_kpis`.
   - `_first_existing()` ([L2495-L2496](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2495-L2496)), `_cell_at()` ([L2575-L2576](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2575-L2576)), `_clean_cell()` ([L2579-L2580](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2579-L2580)), `_infer_metric_and_cuts()` ([L2567-L2568](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2567-L2568)) are direct re-exports of `core.onboarding.kpi.text_parser` functions that are never called internally within `onboarding.py`.
3. **`flow.py`**:
   - `_args_before_subcommand()` ([L2923-L2934](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2923-L2934)), `_utf8_safe_stdio()` ([L2898-L2914](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2898-L2914)), `_safe_duplicate_option()` ([L2026-L2031](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2026-L2031)) - Residual CLI parsing helpers preserved for backward compatibility.

### B. Unwired Components
1. **`delegation.py`**:
   - The `notification` stage entry in `STAGE_ROUTING` ([L291-L296](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L291-L296)) has `agents: []` and `skills: []` (speculative notification bridge not wired to any active CLI panel).
   - Cloud-first intake and pipeline alignment stages (`source_declaration`, `source_discovery`, `intake_interview`, `blueprint_review`, `velocity_lane_choice`, `provisioning`, `ingestion_generation`, `schema_drift_review`, `performance_optimization`) ([L309-L364](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L309-L364)) are defined in `STAGE_ROUTING` but are not yet automatically attached by local `flow.py` panels (attached by cloud intake panel writers).

### C. Logic Duplication Across Files
1. **`_now()` UTC Timestamp Helper**:
   - Defined independently in 4 files:
     - `research.py` ([L94-L95](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L94-L95))
     - `delegation.py` ([L116-L117](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/delegation.py#L116-L117))
     - `flow.py` ([L2570-L2571](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2570-L2571))
     - `vocabulary_panel.py` ([L222-L223](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L222-L223))
2. **`_rel()` Relative Path Helper**:
   - Defined independently in 7 files:
     - `bootstrap.py` ([L249-L253](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bootstrap.py#L249-L253))
     - `bugs.py` ([L506-L510](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L506-L510))
     - `flow.py` ([L2574-L2578](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow.py#L2574-L2578))
     - `kickstart.py` ([L483-L487](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/kickstart.py#L483-L487))
     - `validation.py` ([L1284-L1288](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1284-L1288))
     - `vocabulary_panel.py` ([L226-L230](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/vocabulary_panel.py#L226-L230))
     - `workflow.py` ([L459-L463](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L459-L463))
3. **`_load_json()` Defensive JSON Reader**:
   - Defined independently in 5 files:
     - `bugs.py` ([L485-L492](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/bugs.py#L485-L492))
     - `flow_io.py` ([L14-L21](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/flow_io.py#L14-L21))
     - `research.py` ([L98-L105](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py#L98-L105))
     - `validation.py` ([L1169-L1183](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1169-L1183))
     - `workflow.py` ([L449-L456](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/workflow.py#L449-L456))
4. **`_is_template_kpi_row()` Template Detector**:
   - Defined identically in both `onboarding.py` ([L2563-L2564](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/onboarding.py#L2563-L2564)) and `validation.py` ([L1199-L1208](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/validation.py#L1199-L1208)).

### D. Broken References & Import Integrity
- **Verification Result**: 100% OK. All 18 modules pass dynamic import checks without syntax or resolution errors. All contract references in `validation.py` match schema definitions in `core.onboarding.artifact_contracts`.

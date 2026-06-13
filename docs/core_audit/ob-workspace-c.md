# ob-workspace-C (cli-envelope/idempotency/delegation/misc) — audit

## Purpose
This slice covers the shared governed CLI envelope and its supporting plumbing for
`core/onboarding/workspace/`: the single `run_workspace_command` envelope every
`apply-*`/`finalize-*`/`prepare-*` is supposed to funnel through (lock + timing event +
idempotency ledger + trajectory recording + reliability tripwire + op-signal activation),
the dependency-free idempotency ledger (`applied_ops.jsonl`), the `STAGE_ROUTING`
agent/skill delegation map (locked by a coverage test), incremental re-onboarding
fingerprinting, the workflow checkpoint orchestrator, workspace-vocabulary research +
its confirmation panel, the handoff-fetch CLI, workspace cleanup/reset, and the
product-bug detector. Focus: does every mutating CLI really go through the envelope, are
op-ids deterministic, is the lock acquired/released safely on every path, and is the
delegation roster complete.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 25 | Package re-exports for the workspace onboarding API. | re-exports only |
| `cli_runner.py` | 326 | The shared governed CLI envelope. | `run_workspace_command`, `_is_mutating`, `_snapshot_state_safe`, `_verify_state_safe`, `_payload_from_result`, `resolve_workspace_path` |
| `idempotency.py` | 183 | Deterministic op-id + append-only `applied_ops.jsonl` ledger. | `compute_op_id`, `fingerprint_paths`, `AppliedOp`, `record_op`, `get_applied_op`, `is_duplicate_op`, `list_applied_ops` |
| `delegation.py` | 740 | `STAGE_ROUTING` agent/skill map + per-stage programmatic verdicts + handoff writer. | `STAGE_ROUTING`, `routing_for`, `record_delegation`, `DelegationEvent/Verdict/Request`, `verdict_from_*` (10), `render_delegation_markdown`, `recent_delegations` |
| `incremental.py` | 275 | Onboarding-input fingerprint manifest for skip/redo re-onboarding. | `fingerprint_file/_inputs`, `_content_hash`, `diff_fingerprints`, `ChangeSet`, `build_manifest_payload`, `write_manifest`, `load_manifest`, `artifacts_exist` |
| `workflow.py` | 480 | `prepare-workspace-workflow` checkpoint orchestrator (plan/local-safe/autopilot). | `WorkspaceWorkflowOrchestrator`, `WorkspaceWorkflowResult`, `_safe_*` gates, `prepare_main` |
| `research.py` | 398 | Evidence-derived per-workspace vocabulary (no curated domain lists). | `WorkspaceResearcher`, `TermEvidence`, `VocabularyResult`, `research_workspace_vocabulary` |
| `vocabulary_panel.py` | 275 | Vocabulary confirmation panel + apply (mutates `workspace_vocabulary.json`). | `prepare_vocabulary_confirmation_panel`, `apply_vocabulary_confirmation_answer`, `VocabularyConfirmationPanel` |
| `handoff_cli.py` | 82 | `uv run handoff latest/render` — side-agent reads its brief from a file. | `main`, `_latest`, `_handoffs_dir` |
| `cleanup.py` | 473 | `cleanup-workspace-references` dry-run/apply reset with hard delete-confirm gate. | `WorkspaceReferenceCleaner`, `run_cleanup`, `CleanupAction`, `WorkspaceCleanupResult`, `main` |
| `bugs.py` | 529 | `prepare-workspace-bug-report` — detects 3 workspace product-bug classes. | `WorkspaceBugDetector`, `WorkspaceBug`, `WorkspaceBugReport`, detectors `_detect_*` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [DUP] | `core/onboarding/kpi/blocker_cli.py:87-210` | `apply-kpi-panel-answer` (the single most-used apply command, per CLAUDE.md) hand-rolls the entire envelope — op-id, replay check, `time_command`, `workspace_lock`, `record_op`, trajectory events — instead of calling `run_workspace_command`. Lock/idempotency are correct, but it duplicates ~60 lines the envelope exists to remove. | Refactor to `run_workspace_command(..., record_idempotent=True, op_args={...})`. |
| [INTEGRATION] | `core/onboarding/kpi/blocker_cli.py:87-210` vs `cli_runner.py:217-300` | Because it bypasses the envelope, `apply-kpi-panel-answer` gets NEITHER the live reliability tripwire (`_snapshot_state_safe`/`_verify_state_safe`) NOR the op-signal/`suggested_skills` activation hook. The exact apply command MEMORY says wiped decisions (BUG-014 area) is the one not guarded by the live tripwire. | Route it through the envelope so the tripwire + op-signals fire on the highest-risk apply. |
| [INTEGRATION] | `vocabulary_panel.py:48-219`, `research.py` | `prepare_vocabulary_confirmation_panel` / `apply_vocabulary_confirmation_answer` have NO `[project.scripts]` entry and are not referenced by `flow.py` or `workflow.py` — only by tests. The apply writes `workspace_vocabulary.json` with no `workspace_lock` and no idempotency. Feature is built but unwired to any production path. | Either wire vocabulary confirmation into the flow/CLI behind the envelope, or mark it experimental. At minimum take the lock in the apply. |
| [BUG] | `cli_runner.py:174-205` | On the non-replay refresh path, after re-running `fn()` under the lock the function `return 0`s immediately: it skips `record_op`, the `tool_start`/`tool_result` trajectory events, the reliability tripwire, and op-signal activation. A forced re-read of state is silently un-audited (no trajectory event) compared to a first apply. | Run the refresh through the same post-`fn` block (events + tripwire) rather than early-returning; or document that replay refreshes are intentionally untracked. |
| [BUG] | `idempotency.py:29-40` | `compute_op_id` does not include any artifact content fingerprint by default; `fingerprint_paths` exists for exactly this but callers must opt in via `op_args`. The envelope's default `idempotent_args` (`cli_runner.py:150-153`) is metadata+workspace only, so a re-apply against a *rebuilt* artifact with identical CLI args is treated as a duplicate and (with `--allow-replay` off) skipped/replayed against stale content — the very hazard the comment at `cli_runner.py:160-168` describes, only partly mitigated by the refresh-fn re-read. | Fold `fingerprint_paths(<source artifact>)` into the default op-id for artifact-mutating commands, or require apply CLIs to pass it. |
| [NOT-PROD] | `cli_runner.py:187-192` | The replay refresh swallows ALL exceptions (`except Exception: # keep cache`) and falls back to the cached payload with only a note. A genuine bug in `fn()` during a forced re-read is masked as "could not re-read current state". | Narrow to expected I/O errors, or surface `type(exc).__name__` in the note so real failures are visible. |
| [BUG] | `cleanup.py:142` | In `apply()`, `target = (self.repo_root / action.target).resolve()` joins repo_root with `action.target`, but `delete_tree` actions for the workspace `interns`/`wiki` store `_display_path` which is repo-relative — OK — yet `_repo_state_actions` stores some targets as raw `rel_path` (`RUNTIME_STATE_FILES`) and others as `_display_path`. The join is consistent only because both are repo-relative, but `_ensure_deletable` re-resolves and gates on `state_root`/interns/wiki, so an absolute `action.target` would bypass the join silently. Fragile coupling between target-string form and the join. | Store one canonical target form (always repo-relative posix) and assert it in `apply()`. |
| [DEAD] | `cleanup.py:198-200` | `_workspace_slug()` builds a deployment dir under `state/databricks/deployments/<slug>` using a slug of the full workspace key (`workspaces-demo`), while the deployment writer elsewhere may key by project name only — verify the slug matches what deployments actually write, otherwise the `delete_tree` never matches. | Confirm slug parity with the Databricks deployment writer; add a test fixture. |
| [DEAD] | `delegation.py:179-191` (`dashboard_refresh` brief) | `_STAGE_BRIEFS["dashboard_refresh"].context_keys` includes the literal `"workspaces/<ws>/dashboard/*.json"` with an unsubstituted `<ws>` placeholder (other briefs use `summary.*` keys). The handoff doc prints it verbatim. | Template it like the others or drop the placeholder. |
| [DUP] | `incremental.py:43-64` vs `idempotency.py:43-64` | Two near-identical content-fingerprint helpers (`_content_hash`/`fingerprint_file` vs `fingerprint_paths`) with different truncation (full sha256 vs 16-char, partial-hash threshold vs none). Divergent hashing of "the same file" across the two subsystems. | Unify on one fingerprint helper (MEMORY notes a planned "fingerprint-helper unification"). |
| [MISSING] | `delegation.py:421-429` `_append_trajectory` | Concurrent appends to `trajectory.jsonl` are not under the workspace lock (delegation runs inside flow which may hold it, but the helper itself assumes single-writer). Errors are silently swallowed (`except OSError: pass`), so a delegation event can vanish with no signal. | Acceptable if always called under the flow lock; otherwise document the invariant. |
| [NOT-PROD] | `bugs.py:489` | `_load_json` catches bare `except Exception` (not just JSON/OS), masking programming errors while reading workspace artifacts. | Narrow to `(json.JSONDecodeError, OSError)`. |

## Cross-package coupling
- `cli_runner.py` is the hub: imports `core.governance.op_signals` (verified present:
  `derive_op_signals`, `signals_to_skills`), `core.observability.events.time_command`,
  `core.onboarding.harness.trajectory_recorder.record_trajectory_event_safe`,
  `core.storage.workspace_lock` (`workspace_lock`, `WorkspaceLockTimeout`), and this
  slice's `idempotency`. It also lazily imports `tools.workflow_state_tripwire` +
  `core.storage.workspace_layout` for the reliability hook (best-effort, never raises).
- Envelope coverage confirmed via grep: `run_workspace_command` is called by
  `kpi/generation_cli.py`, `data_model/generation_cli.py`, `relationships/contracts.py`,
  `pipeline_plan.py`, `sources/external_intake_cli.py`, `data_quality.py`,
  `kpi/cli_agent_confirm_cli.py` — all apply/finalize there set `record_idempotent=True`.
  The notable HOLDOUT is `apply-kpi-panel-answer` (`kpi/blocker_cli.py`), which
  re-implements the envelope (see findings). `prepare-*` commands correctly omit
  idempotency.
- `STAGE_ROUTING` is locked by `tests/test_agent_skill_routing.py`, which discovers
  agents from `.claude/agents/` (10 present — `business-analyst`, `kpi-analyst`,
  `data-engineer`, `source-to-target-reviewer`, `sql-polars-pyspark-specialist`,
  `data-analyst`, `validation-gatekeeper`, `dashboard-engineer`, `databricks-engineer`,
  `workspace-flow-orchestrator`) and skills from `skills/` (17 present). All 10 agents
  and all 17 skills ARE routed (verified by reading the map). The prompt's "14 agents"
  is the historical count BEFORE the Phase-1 removals documented inline in `delegation.py`
  (agent-advisor-router, regression-sweep, feature-derivation twin, databricks-access-gates
  twin, integration-notification-operator) — not a coverage gap. `.gemini/agents/` mirrors
  the same 10.
- `delegation.py` writes handoffs + trajectory under `WorkspaceLayout.state_dir`;
  `handoff_cli.py` reads them back; both agree on `state/handoffs/<stage>__<agent>.md`.
- `workflow.py` orchestrates KPIGenerationWorkflow, DataModelGenerationWorkflow,
  blocker workflows, validator, presentation exporter, wiki-memory — broad fan-out;
  each step is try/except-wrapped into `self.warnings` so one failure never aborts the
  checkpoint (good), but also means a silently-degraded panel can look "prepared".

## Verdict
The envelope itself is well-engineered: lock acquisition/release is correct (the lock CM
is re-entrant and releases in `finally` on every path, including `WorkspaceLockTimeout`
and arbitrary `fn()` exceptions, which re-raise after recording a failed trajectory
event), the idempotency ledger is dependency-free and tolerant of corrupt JSONL lines
(skips them), op-ids are deterministic (`sort_keys`, sorted kwargs), and the
delegation roster is fully covered and test-locked. Production-readiness is held back by
three integration gaps rather than envelope defects: (1) `apply-kpi-panel-answer` —
the highest-traffic apply — bypasses the envelope and therefore the live reliability
tripwire/op-signal hooks; (2) the vocabulary confirmation panel mutates state with no
lock, no idempotency, no CLI, and no flow wiring; (3) the default op-id omits artifact
content fingerprints, so a re-apply against rebuilt content can be misclassified as a
duplicate (only partly mitigated by the replay-refresh re-read, which itself is
un-audited and swallows all exceptions). None are data-loss bugs given the lock + dry-run
gates, but the first two should be closed before relying on the envelope's "every
mutating CLI is guarded" guarantee. Counts below.

<!-- tag counts: BUG=3, INTEGRATION=3, NOT-PROD=2, DUP=2, DEAD=2, MISSING=1 -->

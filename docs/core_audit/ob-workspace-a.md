# ob-workspace-A (flow.py spine) — audit

## Purpose

`core/onboarding/workspace/flow.py` is the user-facing workspace-flow orchestration spine. It exposes
two pyproject console scripts — `workspace-flow` (`main`) and `run-kpi-pipeline` (`pipeline_main`) —
and the `WorkspaceFlow` state machine that drives a workspace from onboarding through KPI/SQL
generation to a human-gated completion. It ties together: onboarding (`WorkspaceOnboarder`),
data-understanding + data-quality gates, bronze/silver standards, KPI-definition gate, KPI blocker
panels, relationship contracts, source-to-target planning, DuckDB SQL generation, execution +
parity + validation harnesses, results preview, dashboard/wiki side outputs, delegation recording,
and the kpi-analyst semantic-review hard gate (`review --confirmed-by`). It also owns the session
record (`session.json`), panel artifacts (`current.{json,md}`), the dated `runs/` snapshot, handoffs,
GC, artifact inventory, context-status, and skill-excerpt utility subcommands.

## Files

| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| core/onboarding/workspace/flow.py | 4068 | Workspace-flow orchestration spine + 2 CLI entry points | `WorkspaceFlow` (start/answer/review/status/diff/results, `_advance_until_stop`, `_write_result_preview`, `_save_panel`, gates); module fns `compute_workflow_diff`, `_emit_result_packet`, `_print_cli_panel`, `_collect_gate_provenance`, `_kpi_review_signature`, `latest_open_session`/`latest_session`, `write_session_handoff`; `main`, `pipeline_main` |

## Findings

| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [DEAD] | flow.py:76,153-172 | Intents `full_kpi_sql` and `usual_workflow` are accepted and recorded but `_advance_until_stop` never branches on `state["intent"]`. After `start`, both run the identical pipeline. The intent distinction is cosmetic in the spine — only `kpi_generation` (route panel) is materially different. | Either drop the distinction to one non-generation intent, or make `_advance_until_stop` honor it (e.g. `usual_workflow` should defer to `WorkspaceWorkflowOrchestrator`, not run full SQL gen). |
| [DEAD] | flow.py:3222-3225 | `_SUBCOMMANDS` frozenset lists `"context-status"`, `"skill-excerpt"`, `"gc"`, `"handoff"`, `"artifacts"` but OMITS nothing harmful — however it is used ONLY by `_args_before_subcommand` for `--quiet`/`--json` pre-scan. It correctly enumerates all 10 subparsers, so it is consistent; but it is a hand-maintained duplicate of `sub.choices` that will silently desync if a subcommand is added. | Derive `_SUBCOMMANDS` from `sub.choices.keys()` after building the parser, or add a test asserting equality. |
| [NOT-PROD] | flow.py:1283-1284, 1319-1320, 1362-1363, 820-821, 850-851, 862-863, 2837-2838 | Multiple broad `except Exception: pass` / silent swallows in result preview, share-sum check, timezone set, dashboard export, packet append, and gate-provenance intent facets. A failing parity/share/export degrades silently with no step record in several of these (unlike the wiki/dashboard blocks which DO record `failed`). | Narrow excepts and at minimum `self._record_step(..., "failed", {"error": str(exc)})` so silent partial failures are auditable. |
| [BUG] | flow.py:1321-1325, 1445-1449 | `_write_result_preview` does a process-global `os.chdir(self.repo_root)` and restores in `finally`. This is not thread/async safe and corrupts CWD for any concurrent work (the pipeline advertises a `parallel_kpi_completion` fan-out route at 3848). A crash between chdir and the try restores correctly, but concurrent callers race. | Pass `repo_root` into the SQL readers / use absolute paths instead of `chdir`; eliminate global CWD mutation. |
| [BUG] | flow.py:996-1021 | The kpi-analyst `blocked` verdict path returns a `blocked` panel but does NOT clear/invalidate `state["kpi_analyst_review"]`. After the operator regenerates and the signature changes, `review_current` correctly re-gates — but if the SAME signature is re-reached, a stale `blocked` verdict persists and re-blocks without a fresh review. | On regeneration or on `blocked`, expire the stored review (or require a fresh verdict for any non-ok prior state). |
| [INTEGRATION] | flow.py:153-154, 364-365 | `plan` mode short-circuits to `_workflow_checkpoint` in TWO places (in `start` only for non-generation intents, and again inside `_advance_until_stop`). A `kpi_generation` intent in `plan` mode skips the first guard, runs `KPIGenerationWorkflow.prepare()` (line 156-170), and never reaches the plan checkpoint — plan mode is silently ignored for the default intent. | Hoist the `plan`-mode checkpoint above the `kpi_generation` branch, or document that plan mode is incompatible with `kpi_generation`. |
| [INTEGRATION] | flow.py:3879-3900 | `pipeline_main` resume path: when an open session exists and status is NOT blocked/complete/needs_specialist_review, it calls `flow.status()` (a read-only no-op) instead of re-advancing. A session parked mid-pipeline at a non-terminal `running` stage will not progress on re-invocation — only `status` is echoed. | Re-advance via `_advance_until_stop` (or `answer`-style continue) for resumable non-gate states, not `status()`. |
| [NOT-PROD] | flow.py:1545-1568 | `_save_panel` writes `session.json` TWICE (`_write_state` at 1560 and again at 1568) with no atomic write (no temp+rename). A crash mid-write leaves a truncated session record; `_load_state` then raises `JSONDecodeError` and the session is unrecoverable. | Write to a temp file and `os.replace`; collapse the double write into one. |
| [MISSING] | flow.py:271-318 (`review`) | The review hard gate accepts a verdict but performs NO signature re-check at record time against the CURRENTLY generated KPIs — it trusts the signature embedded in the last panel summary (1280-285). If `review` is called when the current panel is NOT the gate panel (e.g. after `results`), it binds the verdict to whatever `kpi_signature` happens to be in that panel (possibly empty -> fallback recompute over completed_kpis), allowing an out-of-context verdict to satisfy a later gate. | Recompute the signature from the live registry/preview inside `review` and reject if no gate is currently open. |
| [NOT-PROD] | flow.py:99-1073 | `WorkspaceFlow` is a God-object: `_advance_until_stop` alone is ~720 lines spanning 9 stages with deeply nested panels. Untestable in isolation; every stage change risks the whole spine. | Decompose (see Verdict). |
| [DUP] | flow.py:3540-3562 vs 4001-4017 | `--require-human-gates` agent-gate-blocking logic is copy-pasted between `main` (review subcommand) and `pipeline_main` completion. Same for the gate-provenance + headline rendering (3069-3079 vs 4049-4054) and result-packet emission. | Extract a shared `enforce_human_gates(panel_payload) -> int|None` and a `emit_completion(...)` helper. |
| [DUP] | flow.py:2384-2461 | `latest_open_session` and `latest_session` are near-identical scans differing only in the status/age filter. | Parameterize one scanner with a predicate. |
| [NOT-PROD] | flow.py:747-769, 770-870 | Two-space-indented `try/except/else` blocks under `if _wiki_on:` / `if _dash_on:` are valid but non-standard (2-space inside 8-space context), a readability/maintenance hazard in the most side-effect-heavy region. | Reflow to standard 4-space nesting; extract `_emit_side_outputs`. |
| [INTEGRATION] | flow.py:857-863 | Dashboard auto-open calls `webbrowser.open` from within the pipeline; gated by env + test-module detection (`"pytest" in sys.modules`). In a server/CI context that is not a test but is headless, this will still attempt to open a browser (best-effort except swallows it). Acceptable but couples a UI side effect into the completion path. | Gate on an explicit `AUTORESEARCH_OPEN_DASHBOARD` default-off in non-interactive contexts. |
| [BUG] | flow.py:1304, 692-721 | `KPI_ENGINE_PARITY` parity + share checks run inside `_write_result_preview`, which is invoked BEFORE the harness/validation `ok` checks gate (preview at 722 happens only after harness.ok). But `results()` (334-351) calls `_write_result_preview` directly with no harness/validation precondition, so `workspace-flow results` can render previews for SQL that never passed the harness, with no staleness/validity guard beyond `_result_packet_stale_kpis`. | Have `results()` warn when the last harness/validation run was not `ok`, or re-check before previewing. |

## Cross-package coupling

flow.py imports from ~20 modules across the repo and is the convergence point of the whole onboarding
stack:

- onboarding: `WorkspaceOnboarder`, `BronzeSilverStandardsBuilder`, data_understanding classifiers,
  `DataQualityHarness`/`DuplicateReviewPanel`/`DuplicateDecisionRecorder`, `DataEngineeringRoutePlanner`.
- kpi: `KPIGenerationWorkflow`, `prepare_kpi_blocker_panel`/`apply_kpi_panel_answer`,
  `DuckDBKPISQLGenerator`, `KPIExecutionHarness`, `run_polars_parity`, `WorkspaceArtifactValidator`,
  `KPIOutputVerifier`, `intent_contract` (lazy), `load_kpi_definitions`/`render_kpi_block`.
- relationships: `RelationshipContractBuilder`, `SourceToTargetPlanner`.
- workspace: `delegation` (STAGE_ROUTING + 9 `verdict_from_*` fns + `record_delegation`),
  `WorkspaceWorkflowOrchestrator` (plan mode), `panel_contract.normalize_decision_panel`.
- governance: `provenance.decision_source` (human vs agent).
- presentation/storage: `console_tables`, `WorkspaceLayout`.
- tools: `artifact_inventory`, `list_workspace_files`, `state_consolidator`, `workspace_gc`,
  `context_status` (lazy), `skill_excerpt` (lazy); plus `core.dashboard`, `core.wiki`.

All referenced symbols were confirmed to exist (blocker_workflow, delegation, provenance,
verify_kpi_output, intent_contract, WorkspaceLayout properties). Both console scripts are correctly
registered in `pyproject.toml` (lines 79-80). No dangling imports found. The coupling is wide but
real — this file is the single integration nexus, which is exactly why it is the largest God-object.

## Verdict

Functionally the spine is broadly correct and unusually well-guarded for human gates: the kpi-analyst
semantic review is a genuine hard gate (signature-bound, source:human vs agent provenance via
`decision_source`, `--require-human-gates` enforcement, BUG-014/020 fixes present), relationship
approvals carry provenance, and the result-packet staleness guard (BUG-015/024) is real. Resume and
idempotency are largely handled (`latest_open_session`, BUG-021 continue path). It is NOT yet
production-clean: process-global `chdir` in the preview executor (concurrency hazard against the
advertised parallel route), non-atomic double-write of `session.json`, several silent excepts that
hide partial failures, a stale-`blocked`-review edge, a `plan`-mode-ignored-for-default-intent
integration gap, and a resume path that only `status()`-es non-gate running sessions.

Decomposition is warranted; recommended extraction order:

1. **Result/packet layer** — pull `_write_result_preview`, `_write_runs_snapshot`, `_render_results_markdown`,
   `_emit_result_packet`, `_result_view`, `_share_sum_check`, `_result_packet_stale_kpis` into
   `flow_results.py`. Highest payoff: removes the `chdir` hazard locally and isolates the largest
   testable unit. Fix the `chdir` while extracting.
2. **CLI layer** — split `main`/`pipeline_main` + argparse + `_print_cli_panel` + `_emit_result_packet`
   call sites into `flow_cli.py`, and dedupe the `--require-human-gates`/completion-emit logic into a
   shared helper. Removes the two [DUP] blocks.
3. **Stage engine** — extract `_advance_until_stop` into a `FlowStages` class with one method per stage
   (data_quality, kpi_definition, kpi_blocker, relationships, source_to_target, generation, harness,
   review_gate, completion) returning a `StageResult`. This is the core refactor that makes the spine
   testable and lets `full_kpi_sql`/`usual_workflow`/`plan` branch correctly.
4. **Panel/markdown rendering** — move the `_render_*` / `_compact_panel` / resolution-review helpers
   into `flow_panels.py`.
5. **Session/state** — `_save_panel`, `_load_state`/`_write_state` (with atomic write), `latest_*`,
   `write_session_handoff`, `compute_workflow_diff`, `_collect_gate_provenance` into `flow_state.py`.

Leave `WorkspaceFlow` as a thin coordinator wiring those five modules.

# ob-harness — audit

## Purpose
`core/onboarding/harness/` is the guardrail / reliability layer of the control plane. It
aggregates cross-artifact gates and reliability checks that decide whether a workspace's
generated work is releasable:

- **project_harness.py** — the headline gate (`validate-project-harness` / `harness project`).
  Runs ~12 sub-checks (workspace-artifact validation, KPI execution, cross-engine parity,
  agent benchmark/release gates, git hygiene, AI-CLI harness, workflow guardrails, evidence
  graph, layered/pipeline-execution/data-quality harnesses, generation scoring), computes a
  weighted score, and returns `ok = score >= threshold and not hard_blockers`.
- **workflow_guard_harness.py** — the reliability engine. ~15 checks over the recorded
  trajectory, command log, and panels: unsupported shell/raw-data reads, invented temporal
  features, stalled/slow steps, failed-without-recovery, completion-claim-over-unrecovered-
  failure, incomplete workflow, session-not-monitored, repeated commands, hand-edited
  generated artifacts, throwaway reader scripts, roster utilization / required-specialist
  firing, dashboard vision review.
- **reliability_suite.py** — thin aggregator over workflow-guard + evidence-graph +
  project-harness.
- **trajectory_recorder.py** — append-only JSONL event log + redaction; the data source the
  guard reads. `record_trajectory_event_safe` is the best-effort write API.
- **ai_app_harness.py / ai_cli_harness.py** — scenario-suite drivers (LLM-app eval and
  governed CLI-agent eval).
- **engine_generation_harness.py / intent_coverage_harness.py / layered_pipeline_harness.py /
  pipeline_execution_harness.py** — static/structural per-KPI and per-pipeline gates.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 2 | Package docstring only | — |
| `ai_app_harness.py` | 913 | Dependency-free LLM-app eval suite (stub + opt-in HTTP); 6 eval types incl. result-table baseline-regression | `AIAppHarness`, `_evaluate`, `_baseline_regressions`, `_call_http_ai` |
| `ai_cli_harness.py` | 592 | Governed CLI-agent eval; command-policy + workflow_guard eval types; opt-in real CLI exec | `AICLIHarness`, `_command_policy`, `_workflow_guard`, `_call_cli` |
| `engine_generation_harness.py` | 410 | Static coherence of generated SQL/Polars/PySpark per KPI (no Spark exec) | `EngineGenerationHarness`, `_check_polars/_pyspark/_sql`, `_compile_errors` |
| `intent_coverage_harness.py` | 280 | Asserts generated SQL realizes KPI declared grain/metric/filters | `KPIIntentCoverageHarness`, `evaluate_intent_coverage` (imported) |
| `layered_pipeline_harness.py` | 90 | Medallion pipeline-plan structural checks (catalog/route/dedup-gate) | `LayeredPipelineHarness` |
| `pipeline_execution_harness.py` | 111 | Presence + PHI-redacted sample of generated `pipeline_layers.sql` | `PipelineExecutionHarness`, `_redacted_sample` |
| `project_harness.py` | 739 | Weighted cross-artifact release gate; collects blockers/warnings/next-commands | `ProjectHarness`, `_score`, `_hard_blockers`, `_workflow_reliability_blockers` |
| `reliability_suite.py` | 344 | Aggregates workflow-guard + evidence-graph + project-harness | `ReliabilitySuite` |
| `trajectory_recorder.py` | 347 | JSONL trajectory record/render + secret redaction | `WorkspaceTrajectoryRecorder`, `record_trajectory_event_safe`, `load_trajectory`, `_redact` |
| `workflow_guard_harness.py` | 1515 | ~15 reliability/guardrail checks over trajectory/command-log/panels | `WorkflowGuardHarness`, `_check_*`, `_is_completion_claim`, `_allowed_tool_names` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [NOT-PROD] | cli_runner.py:223,310-320 (consumer of trajectory) | `slow_step` is effectively dead on the real path. `time_command` populates `event_details` with timing, but the `tool_result` record writes `metadata={**base_metadata, "result": payload}` and never merges `duration_ms`. `_duration_ms` reads `metadata.duration_ms`, so the >120s slow-step check only fires on hand-fed test fixtures. | Merge `event_details` (incl. `duration_ms`) into the recorded `tool_result` metadata. |
| [BUG] | workflow_guard_harness.py:599-642 (`_check_stalled_steps`) | Stall pairing uses `_tool_id`, which falls back to `metadata.tool` = the command string. If the same command runs twice, the 2nd `tool_start` overwrites the 1st in `open_starts` and one `tool_result` clears it → a genuinely stalled first call is not flagged (false negative). | Pair on a per-invocation id (`op_id` is already in metadata) rather than the command text. |
| [BUG] | workflow_guard_harness.py:549-587 (`_fired_specialists`/`_specialist_fired`) | `_fired_specialists` adds the full `command + summary` blob of every trajectory record into `fired`; `_specialist_fired` returns True if the token is a substring of ANY signal. Any step whose text mentions the specialist name (e.g. a recommendation/next-command line) marks it fired. This makes `required_specialist_not_fired` near-impossible to trip — advisory, not enforced (the exact gap the check claims to close). | Restrict "fired" evidence to structured signals (hand-off notes, recorded reviews, `activated_specialists`), not free-text command/summary blobs. |
| [NOT-PROD] | workflow_guard_harness.py:452-477,479-547 | `roster_not_routed` and `required_specialist_not_fired` default to `warning` severity (`roster_severity="warning"`), and project_harness routes both as warning-only reliability codes. The "roster is idle / advisory only" condition never blocks a release by default. | Make roster severity `error` by default for terminal stages, or document it as advisory. |
| [NOT-PROD] | project_harness.py:302-349,544-546 | `generation_scoring` is explicitly warning-only ("never a hard blocker") and is not in `_hard_blockers`, so a generation panel missing its understanding/confidence score cannot block. Reasonable, but means the "must carry a score" rule is unenforced at the gate. | Acceptable if intentional; otherwise promote to a hard blocker. |
| [BUG] | workflow_guard_harness.py:73-87,765-849 (`COMPLETION_CLAIM_TOKENS`) | Completion tokens include very generic words (`results`, `complete`, `fully`, `proof`). A `tool_result` summary like "Ran ... results" or any command containing `results` is treated as a completion claim. Combined with the broad token set, this risks false-positive `completion_claim_over_unrecovered_failures` and over-eager clearing of `outstanding` via the recovery branch. | Tighten completion detection to explicit result/proof artifacts or a dedicated event_type rather than substring of generic words. |
| [INTEGRATION] | green_gate.py:1-219; harness_cli.py | Green gate runs the harness *test modules* in-process; it never executes the harness CLIs (`validate-project-harness`, `harness reliability/workflow-guardrails`) against a real workspace. So the gates protect their own code via unit tests but are not themselves run as a CI gate — they fire only when an agent/flow invokes them. | Confirm the orchestrator/CI invokes `validate-project-harness` on a workspace before promotion; document the boundary. |
| [INTEGRATION] | flow.py:1635-1676 vs workflow_guard_harness.py:589-763 | `workspace-flow` records only `workflow_step` events, almost always with `status="ok"`, and never `tool_start`/`tool_result`/failed statuses. The reliability checks that key off `tool_start` pairing, `status in {failed,error}`, or nonzero exit_code therefore see nothing on the flow path; they rely on `cli_runner.py` (which does record proper pairs) or external command logs. Two recording paths with different fidelity. | Route flow steps through the same `tool_start/tool_result` recording as `cli_runner`, or document that reliability monitoring requires the cli_runner path. |
| [NOT-PROD] | reliability_suite.py (no `run-reliability-suite` script) | Despite `generated_by="run-reliability-suite"` and `RECOVERY_COMMAND_TOKENS` referencing `run-reliability-suite`, pyproject exposes it only via `harness reliability`; there is no `run-reliability-suite` console script. The recovery-token match and the next-command string `uv run harness reliability ...` are inconsistent with the advertised tool name. | Either register `run-reliability-suite` or fix the advertised name/tokens to `harness reliability`. |
| [BUG] | project_harness.py:373-374,397-400 (`_score`) | `workflow_score`/`graph_score` use `.get("ok", True)` defaulting to True. If the workflow-guard or evidence-graph sub-check raised and returned `{"status":"failed","ok":False}` that is handled, but a check that returns no `ok` key at all scores 100. Combined with `_hard_blockers` also using `.get(...,True)`, an unexpected/missing-shape sub-result silently passes. | Default missing `ok` to `False` (fail-closed) for gate-critical checks. |
| [NOT-PROD] | pipeline_execution_harness.py:47-51 | Records are hardcoded fixtures (`row_count=2`, fixed columns) regardless of actual generated SQL — it only checks the file exists and PHI-redacts a sample. The "execution harness" does not execute or count anything. | Rename to reflect presence/redaction check, or wire real execution + row counts. |
| [BUG] | layered_pipeline_harness.py:30-49 | `_load_json` here (and in pipeline_execution / ai_cli `_load_jsonl`) does NOT guard `json.JSONDecodeError` — `json.loads` on a corrupt `pipeline_plan.json` raises and crashes the harness (project_harness wraps some but not this one). project_harness `_run_layered_pipeline` only checks file presence, so the crash surfaces via `harness layered-pipeline`. | Use the defensive `_load_json` pattern (catch `JSONDecodeError`) consistently. |
| [BUG] | ai_app_harness.py:472-481; ai_cli_harness.py:542-551 | `_load_jsonl` calls bare `json.loads(line)`; a single malformed dataset line raises `JSONDecodeError` and aborts the whole run (no per-line tolerance, unlike `trajectory_recorder.load_trajectory`). | Catch per-line parse errors and surface as an error record/finding. |
| [DUP] | _rel / _load_json / _load_jsonl / _key across all 10 files | Each module re-implements `_rel`, `_load_json`, `_load_jsonl`, `_key`, `_finding` with subtly different error handling (some catch `JSONDecodeError`, some don't; `_rel` variants differ on resolve/slash handling). | Extract a shared `harness/_io.py` (or reuse `core.paths`) to unify behavior and close the inconsistent-except gaps above. |
| [NOT-PROD] | workflow_guard_harness.py:683-718 (`_check_unsupported_commands`) | When no tool roster is discoverable (`_allowed_tool_names` empty) the check returns `[]` (degrade-open). On a sandboxed `repo_root` without `.agents/tools.json` or pyproject, every unsupported command is silently allowed. The module-relative pyproject fallback (1318) mitigates but depends on package layout `parents[3]`. | Log/emit a warning finding when the roster cannot be resolved rather than silently passing. |
| [BUG] | workflow_guard_harness.py:330,1198-1203 (`_reads_raw_dataset`) | Raw-data-read detection requires the literal `/datasets/` path fragment AND a data suffix AND first token in a small set. A raw read via an absolute path, a workspace-relative path without `/datasets/`, or PowerShell `Import-Csv` with different casing/spacing slips through. | Broaden to any data-suffix path under a workspace inputs dir; treat the check as best-effort and document coverage. |
| [DEAD] | trajectory_recorder.py:299-308 (`_metadata`) | Helper is only used by `main`; fine. No dead code of consequence, noted for completeness. | None. |

## Cross-package coupling
- **project_harness** is the hub: imports `WorkspaceEvidenceGraphBuilder` (onboarding.evidence_graph),
  `AICLIHarness`, `WorkflowGuardHarness`, `AgentBenchmarkScorecardBuilder` (onboarding.benchmark),
  `KPIExecutionHarness` (onboarding.kpi), `WorkspaceArtifactValidator` (onboarding.workspace.validation),
  `WorkspaceLayout` (storage), and `tools.git_hygiene`. A failure in any of those propagates into the gate
  (most are try/except-wrapped to a `{status:"failed"}` shape).
- **reliability_suite** depends on project_harness + workflow_guard + evidence_graph.
- **workflow_guard_harness** depends on `trajectory_recorder.load_trajectory`, `console_tables`,
  `WorkspaceLayout`; reads `.agents/tools.json` / `pyproject.toml` for the tool roster, and lazily imports
  `core.dashboard.screener.vision_review_pending`.
- **trajectory_recorder** is the shared data source; written by `cli_runner.py` (full
  tool_start/tool_result/failed fidelity) and `flow.py` (workflow_step/ok only) — the two producers do not
  agree on event vocabulary, which is the root of the flow-path detection gaps above.
- **Consumers/registration**: `harness_cli.py` (`harness <suite>`) routes 9 suites; pyproject registers
  `validate-project-harness`, `validate-kpi-intent-coverage`, `validate-engine-generation`,
  `record-workspace-trajectory`, `harness`. `dashboard_services.py` surfaces the reliability_suite artifact.
  `green_gate.py` gates the *tests*, not the CLIs. No `[DEAD]` modules — all are reachable via harness_cli or
  a console script and have dedicated tests.

## Verdict
Architecturally sound and well-tested at the unit level; the gate wiring (blockers vs warnings, hard-blocker
set, score weighting) is deliberate and mostly fail-aware. The **enforcement is genuine** on the
`cli_runner` path and via `project_harness` hard blockers (workspace artifacts, KPI execution, cross-engine
parity, git hygiene, workflow-guard errors, pipeline/data-quality/AI-CLI, critical release gates all hard-fail
the gate). However several reliability checks are **warn-only or effectively dormant in production**:
`slow_step` never gets a duration on the live path; `stalled_step` mis-pairs on repeated commands;
`required_specialist_not_fired` is defeated by free-text "fired" matching; roster/specialist findings default
to warning; and the `workspace-flow` recording path emits no failure/tool-pair events, so the
stall/failed/completion checks largely depend on `cli_runner` or externally supplied logs. The green gate
runs the harnesses' *tests*, not the harness CLIs, so nothing forces a workspace through `validate-project-
harness` before promotion — that enforcement lives in the orchestrator/flow, which should be confirmed.
Net: **production-capable as a gate, but with meaningful detection blind spots and two recording paths of
unequal fidelity** that should be unified before relying on the reliability findings for promotion decisions.
Error handling is defensive in the big modules but inconsistent in the small ones (`_load_json`/`_load_jsonl`
crash on corrupt input in 3 files).

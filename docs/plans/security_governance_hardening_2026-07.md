# Security/Governance Hardening — core/ (2026-07, completed)

**Status: COMPLETE.** All 4 phases shipped and committed on
`feature/dashboard-powerbi-live` as `5a6fe3a`. Kept as a reference record of
what was found and why, not an active/pending plan — the working copy of
this plan lived in a CLI-specific location during execution
(`~/.claude/plans/`); it's migrated here afterward specifically because that
location is not readable by every agentic CLI this platform supports, and a
completed multi-phase remediation record belongs in the repo, not in one
tool's private config directory. See `docs/README.md`'s plan-location
convention note for the going-forward rule this migration established.

## Context

The prior 8-phase "lingering issues" plan (Q1-Q8, `feature/dashboard-powerbi-live`
commit `c68ffda`) closed a first pass of audit-found bugs across `core/`. This
plan is a separate, follow-up effort: a live governance/security question
("what guarantees do we have against destructive commands, permission overrides,
prompt injection, security vulnerabilities... how does it adhere to an
enterprise's own guardrails... what is the meta-harness actually checking...
how do we guarantee dashboard/report correctness") produced a live,
evidence-based answer (not a docs read) which found one confirmed bug on the
spot: `core/observability/cost_ledger.py`'s `CostLedger.append()` had no lock
and **lost 31% of writes under real concurrent subprocess load** (verified via
live reproduction, not theory — 10 subprocesses x 100 writes each -> only
687/1000 lines survived, 2 corrupted).

A widened, deep-level plan followed. Three parallel research passes did
full-repo audits: (1) every other unlocked shared-write site in `core/`/`tools/`,
(2) every gap in `injection_guard.py` coverage across untrusted-content
ingestion, (3) current state of secrets/sandboxing/RBAC/deploy-gate-coverage/the
"meta harness". Their findings became the four phases below (risk-ordered,
regression test per fix, `file:line` cited).

**Areas already covered by existing docs, confirmed accurate at the time, NOT
re-derived here**: secrets management (`docs/core_audit/PROD_SECURITY_GAPS.md`
Gap 3 + `SECURITY_RECHECK.md` Surface 1/5), sandboxed execution
(`PROD_SECURITY_GAPS.md` Gap 5 — `IsolatedDuckDBBackend` exists at
`core/execution/backend.py:205-256` but is dead code, never wired into
`build_execution_backend`), RBAC/multi-tenant isolation (`PROD_SECURITY_GAPS.md`
Gap 2 — no user/role concept anywhere in `core/`). All three require real
infrastructure investment (Vault/KMS, container sandbox, an identity provider)
outside this repo's own code — out of scope here, restated at the end.

---

## S1 — Close the dbt/Airflow production-execution authorization gap (most severe, done first)

The medallion Databricks deploy path has 5 real gates
(`core/onboarding/databricks/deploy_gates.py`), and G5 in particular is a
genuine guarantee: `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` must be set by a
human's own shell, and nothing in the codebase ever sets it programmatically
(verified by grep — zero hits). The SECOND production path — dbt project
generation + Airflow/Cosmos orchestration — had **no equivalent gate at all**:

- `core/orchestration/dbt_backfill.py:90-116` — the only existing check
  (`would_refuse = over_bound and source != "human"`, line 93) guarded
  *date-range span* (default max 31 days), not *authorization*. A within-bound
  backfill executed `dbt build` with **zero** human confirmation of any kind.
- `core/orchestration/cosmos_dag.py:69-133` (`build_dbt_tasks`) wired a
  `DbtBuildLocalOperator` against `target_name="prod"` (line 120) with zero
  gate check of any kind.
- A THIRD, independently-triggerable surface not caught by the initial grep:
  `core/orchestration/pipeline_stages.py`'s `DBT_BUILD_STAGE.command` is a raw
  shell string shared by plain `pipeline-run`, Dagster, AND the Airflow
  BashOperator fallback all at once (`airflow_dag.py` turned out to have no
  independent construction of its own — it just delegates to `cosmos_dag.py`
  or this shared stage command).

**Fix shipped**: `check_remote_approval()` (the exact G5 check) is now reused
across all three surfaces. `dbt_backfill.py` checks it as an additional,
independent condition alongside the existing span-bound check. `cosmos_dag.py`'s
`build_dbt_tasks()` checks it FIRST, before even attempting the optional
airflow/cosmos import — deliberately reordered so the gate is provable by
tests regardless of whether `astronomer-cosmos` happens to be installed, and
because it's the cheaper check anyway. A new tiny CLI,
`check-remote-execution-gate` (thin wrapper reusing `check_remote_approval()`
with zero duplicated logic, registered as a real console script), lets
`pipeline_stages.py`'s shell-command surface gate on it via `&&`.

**Verify**: `tests/regressions/test_security_s1_dbt_execution_gate.py` — 12
tests, including a real subprocess end-to-end CLI test. 64 existing tests
across `test_deploy_gates`/`test_pipeline_orchestration`/`test_cost_ledger`/
`test_cosmos_dag`/`test_dbt_backfill` stayed green (2 existing tests in the
latter two files needed updating to explicitly authorize remote execution via
env var, since they exercised other behavior and were incidentally relying on
execution proceeding unauthorized — isolated, not weakened).

---

## S2 — Fix the unlocked-shared-write pattern (root cause, 7 confirmed sites)

`CostLedger.append()`'s bug was one instance of an architectural pattern:
observability/audit writes fire **outside** the per-command `workspace_lock()`
scope that `run_workspace_command()` (`core/onboarding/workspace/cli_runner.py`)
wraps around the actual workflow function. The identical unprotected pattern
was found replicated across every file that fires on *every* CLI command:

- `core/governance/audit_chain.py:95` `append_audit_record()` — **worst
  finding**: unlocked tail-read THEN unlocked append. Two racing processes
  could read the same `(prev_hash, seq)` and both append, producing a chain
  `verify_chain()` would flag as **tampered** (seq discontinuity) when it was
  actually just a benign race — a false-positive tamper signal on a file
  whose entire purpose is being tamper-evident.
- `core/onboarding/harness/trajectory_recorder.py:91` `record()` — unlocked
  append to `trajectory.jsonl`, called both before AND after the nested
  `workspace_lock` block. `render()` also did plain non-atomic `write_text`.
  A second unlocked writer to the same file existed at
  `core/onboarding/workspace/delegation.py:425` (`_append_trajectory`).
- `core/observability/events.py:79` `emit_event()` — unlocked append to
  `events.jsonl`, fires on every command via `time_command`.
- `core/onboarding/workspace/idempotency.py:126` `record_op()` — **worst
  finding #2**: check-then-act race. `is_duplicate_op()` (read) +
  `record_op()` (append) were two separate unlocked calls — two concurrent
  calls with the same `op_id` could both pass the duplicate check before
  either recorded, undermining the idempotency guarantee every apply-*/
  finalize-* command relies on.
- `core/onboarding/memory/user_decisions.py` `apply_user_decision()` —
  unlocked read-modify-write, reachable via a legacy direct-apply path that
  never routed through `run_workspace_command`/`workspace_lock`.
- `core/onboarding/memory/wiki_memory.py` — unlocked read-modify-write on a
  file shared **across all workspaces**, not scoped to one (a per-workspace
  lock couldn't protect it even if applied).
- `core/onboarding/workspace/flow.py:1781` `WorkspaceFlow._write_state()` —
  read-modify-write on session state, `flow.py` never imported
  `workspace_lock` at all.

**Fix shipped** (root-cause, not per-caller): 6 of the 7 files now acquire
`workspace_lock(workspace_path)` around their own critical section (for
`audit_chain.py` and `idempotency.py` this means the *read* and the *write*
both happen inside one lock acquisition, not just the write).
`wiki_memory.py`'s cross-workspace file needed a genuinely different
mechanism — `core/storage/workspace_lock.py` was refactored to extract its
core mechanics (same-fd stale-retry, no-unlink-on-release, reentrancy) into a
new `named_lock(path)` function, so `wiki_memory.py` reuses the exact proven
mechanism on a fixed cross-workspace sentinel path instead of a second
hand-rolled lock. `flow.py`'s fix is explicitly a PARTIAL close: locking
`_write_state`/`_load_state` closes the torn-write hazard but does not make
the ~6 separate load-mutate-write call sites scattered through the file
atomic end-to-end — flagged as a lower-priority follow-up rather than falsely
claimed fully closed (this was independently rated the lowest-plausibility of
the 7 findings).

**Verify**: `tests/regressions/test_security_s2_concurrent_writes.py` — 8
tests, all real-subprocess-based (not threads, not mocks — matching the
reproduction rigor used to find the original bug). 92 of 93 existing tests
across the touched modules stayed green (1 pre-existing, unrelated failure:
`test_delegation_pipeline` needs `pytest`, not installed in this environment).

---

## S3 — Close the injection-guard coverage gap in the KPI blocker panel

`injection_guard.py` was credited with 4 wired call sites, but within
`blocker_question_panel.py` itself, only one narrow function
(`_execute_option_preview`) was actually guarded. The panel's own declared
`primary_artifact` (`current.md`, read verbatim by the orchestrating CLI
agent) rendered raw KPI business-question prose, raw sample/observed values,
a human-typed wiki "why" note, blocked-KPI prose excerpts, and the full
CLI-agent evidence pack (including PDF/DOCX-extracted data-dictionary
excerpts) completely unguarded.
`core/onboarding/kpi/kpi_confirmation_panel.py`'s `render_kpi_confirmation_markdown`
had the same gap for raw workbook cell values.

**Fix shipped**: every render point wrapped in `neutralize_text`/
`neutralize_rows`. For the nested CLI-agent evidence pack (a dict of dicts/
lists, not flat rows), added a new `neutralize_json(value)` recursive
primitive to `injection_guard.py` itself — applied BEFORE `json.dumps`, not
to the dumped text afterward, to avoid a match spanning JSON syntax and
corrupting structure. Fixed at the SOURCE too:
`onboarding.py::_extract_data_model_documents` now neutralizes raw PDF/DOCX
text before writing `interns/generated/data_dictionary/*.txt`, so every
current and future reader is protected, not only the one reader originally
traced. A lower-confidence finding (`source_catalog.py::_normalize_catalog_entry`)
was left as a documented follow-up, not force-fixed, since it wasn't
confirmed as a real LLM-facing sink.

**Verify**: `tests/regressions/test_security_s3_blocker_panel_injection.py` —
12 tests. 151 existing tests across 14 touched-module suites stayed green.

---

## S4 — Document the meta-harness's real scope

`project_harness.py` + `workflow_guard_harness.py` (the "meta harness") were
confirmed to have **zero** security/injection/destructive-action awareness —
purely a data-quality/evidence-completeness/agent-reliability scorer. That
scope limit wasn't written down anywhere, for a component whose green status
could otherwise be mistaken for "this release is safe."

**Fix shipped**: explicit scope note added to both modules' docstrings.
`docs/core_audit/PROD_SECURITY_GAPS.md`'s Gap 8 section — which had gone
materially stale relative to this pass's own S3 fixes (it still credited
`blocker_question_panel.py` with coverage S3 found was misleading, and still
listed an already-fixed dashboard chat-context item as open) — was corrected
to reflect verified current reality, with the meta-harness-is-not-a-security-
control note added to its roadmap and executive-summary sections.

**Verify**: doc-only change; all 80 existing tests across
`test_project_harness*`/`test_workflow_guard_*`/`test_reliability_suite`
stayed green.

---

## Out of scope (infra/deployment, reconfirmed at the time)

- **Secrets management** — `PROD_SECURITY_GAPS.md` Gap 3. Secrets are
  `.env`-only, no Vault/KMS. Requires real infra, not a code change here.
- **Sandboxed execution** — `PROD_SECURITY_GAPS.md` Gap 5. `IsolatedDuckDBBackend`
  exists but is dead code — never wired into `build_execution_backend`.
  Wiring it in behind a config flag would be a genuine, cheap partial step if
  ever prioritized.
- **RBAC / multi-tenant isolation** — `PROD_SECURITY_GAPS.md` Gap 2. No
  user/role concept anywhere in `core/`. Requires an identity provider.

## Sequencing rationale

S1 first: an unauthorized production `dbt build` executing against real data
with zero human gate is a more severe failure mode than data loss —
safety-of-action before safety-of-observability. S2 next: the concurrency
pattern was confirmed across 7 sites sharing one root cause, high value per
line changed. S3: closes the clearest actual data-exfiltration-style risk but
requires a hostile document already present in a workspace. S4 is cheap
documentation, included last only because it depended on nothing else.

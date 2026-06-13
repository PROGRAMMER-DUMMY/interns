# core/ Remediation Plan

Derived from the full-read audit (`SUMMARY.md` + 29 per-unit `*.md`). This is the execution
plan: phased, risk-ordered, one PR per phase (or per sub-bullet for the big ones). Every item
cites the unit doc and `file:line` so a fresh session can act without re-reading code first.

## Goal
Take `core/` from "functionally complete, many systemic defects" to "production-ready for the
governed single-user path, then concurrent/multi-user". Fix **themes, not symptoms** — most
individual findings collapse into the 12 themes in `SUMMARY.md`.

## Non-goals
- No feature work. No refactor-for-its-own-sake (flow.py decomposition is P8, optional).
- Do **not** hand-edit generated workspace contracts (`kpi_registry.json`, etc.) to mask a bug —
  fix the upstream producer and regenerate (AGENTS.md rule).
- Keep everything **workspace-agnostic** (no Healthcare-RCM/domain words) and **local-safe by
  default** (no remote calls without `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`).

## Working conventions (read before any edit)
- Branch per phase off `main`: `fix/core-p1-pii`, `fix/core-p2-gates`, … One phase = one PR.
- Audit branch `core-audit` holds the docs only; rebase fix branches on latest `main`.
- For every BUG fixed, **add a regression test** under `tests/` that fails before, passes after.
- Run the gate after each phase: `uv run validate-workspace-artifacts --workspace workspaces/Healthcare-RCM-Data-Platform` and `python -m pytest tests/ -q` (plus `core/dev/green_gate`).
- ASCII status markers only (`[ok] [~] [x] [blocked]`), no emojis, in any generated text.
- Secret hygiene: never print `.env`/tokens/salts; report redacted key names only.
- After a phase lands, update the **Progress ledger** at the bottom of this file.

---

## P0 — Safety net + hygiene (do first, ~half day)
Small, unblocks everything else.
- [ ] `.gitignore`: add `mlruns/` and `mlflow.db` (untracked at repo root today). Confirm with user first — see HANDOFF open question.
- [ ] Regenerate the stale `interns/MANIFEST.md` (claims `Present 17/29` but kpi_results exist; predates the 2026-06-12 run). Wire MANIFEST regen into `workspace-flow complete` if not already (artifact-inventory producer). Ref: discovery finding #1.
- [ ] Establish a green baseline: run full `pytest`, record pass/fail count in the ledger so regressions are visible.
- [ ] Add a `tests/regressions/` folder + naming convention `test_core_p<N>_<slug>.py` for the fixes below.

## P1 — PII/PHI fails green  *(THEME T2 + T5-phi — HIGHEST RISK)*
A sensitive column can reach Gold, the results packet, and a non-covered target unmasked while
every gate reports green. Fix the whole chain in one PR so no half-state ships.
- [ ] **Masking parity**: `kpi/polars_generator` + `kpi/pyspark_generator` must apply the same
  `hash()`/`sha2()` masking that `kpi/sql_generator` does, before writing Gold. Ref: `ob-kpi-b.md`.
- [ ] **Results packet redaction**: route `kpi/result_view_builder` + `kpi/verify_kpi_output`
  sample tables through `kpi/pii_redaction` (currently only the blocker panel uses it). Ref: `ob-kpi-d.md`.
- [ ] **Sensitivity field unification**: onboarder writes `columns.<name>.is_sensitive` but
  validator + `medallion/design.py` read `datasets[].columns[].pii`. Pick one shape, migrate the
  other reader, so the anti-PII-in-Silver invariant actually collects columns. Ref: `ob-workspace-b.md`.
- [ ] **PCI pattern single-source**: make `kpi/pii_redaction` import `phi_gate.PCI_IDENTIFIER_PATTERNS`
  instead of a drifted copy; add a test asserting full-set equality (current test spot-checks ~7).
  Ref: `governance.md` + `ob-kpi-d.md`.
- [ ] **phi_gate freshness**: `governance/phi_gate.py` must check `profile_index.json` freshness
  (mtime/fingerprint vs datasets) before trusting it; stale → re-profile or fail closed. Ref: `governance.md`.
- [ ] **phi_gate fail-closed**: `execution/backend.py:587` `_phi_gate_failure_for_task` swallows all
  exceptions and returns None (fails open). Make it fail closed. Ref: `execution.md`.
- **Acceptance**: a workspace with a name/SSN/DOB/PAN column → masked in all 3 engines, redacted in
  current.md + verifier tables, flagged by the Silver invariant; phi_gate refuses on stale profile.
- **Verify**: new `tests/regressions/test_core_p1_pii.py` covering each bullet.

## P2 — Gate / approval / SSRF bypass  *(THEME T7 + T8)*
Before any remote or external-source rollout.
- [ ] **Genie-lane gate**: `databricks/workspace_deployer.py:509` `run_deployment(apply=True)` skips
  `run_deploy_gates` (G1/G2 + G3 human-provenance never run). Make the Genie lane run the same gate
  set as the medallion UC lane (the correct reference). Ref: `ob-databricks.md`.
- [ ] **Remote target gate**: `onboarding-root/pipeline_deployment_plan.py:37` — remote-approval check
  only fires for `external`/`warehouse`; a `databricks` target in apply mode bypasses it. Switch to a
  local allow-list (fail closed for unknown targets). Ref: `onboarding-root.md`.
- [ ] **No network on local bootstrap**: `workspace/…check_databricks_readiness` fires a live
  `health_check()` on every bootstrap. Gate behind remote-approval env. Ref: `ob-workspace-b.md`.
- [ ] **External-root allowlist**: `sources/external_discovery._validate:284` and
  `catalog._plan/_apply_local_source:607/646` must call `storage/external_data.load_external_data_policy`
  / `is_external_path` and refuse paths outside configured roots. Ref: `ob-sources.md`.
- [ ] **SSRF egress control**: `sources/catalog.py:1148` API fetch must block loopback/RFC1918/
  link-local (`169.254.169.254`) unless explicitly allowlisted. Ref: `ob-sources.md`.
- **Acceptance**: Genie apply refuses without G3 human provenance; `databricks` target refuses
  without approval env; discovery refuses an out-of-allowlist absolute path; API fetch refuses a
  metadata-IP URL.
- **Verify**: `tests/regressions/test_core_p2_gates.py`.

## P3 — Injection into emitted artifacts  *(THEME T4)*
Build one shared escaping/identifier-quoting + parameterization layer, then apply it.
- [ ] `execution/databricks_client.py:142` `write_delta` — stop interpolating telemetry JSON into the
  INSERT f-string; parameterize. Ref: `execution.md`.
- [ ] `medallion/delta_emitter` + `merge_emitter` — quote/escape PII expressions, PK and column names
  before they go into `F.expr("…")` / SQL MERGE conditions. Ref: `medallion-b.md`.
- [ ] `kpi/sql_generator` + polars/pyspark generators — sanitize derived-formula bodies and filter
  values/ops before inlining; reject formulas that fail a token allowlist. Ref: `ob-kpi-b.md`.
- **Acceptance**: a column/formula containing a quote/semicolon/`F.expr` payload is escaped or
  rejected, never executed. **Verify**: `tests/regressions/test_core_p3_injection.py`.

## P4 — Concurrency & durability  *(THEME T1 + T6)*
Blockers for concurrent/multi-user use.
- [ ] **Kill `os.chdir`**: add `core/storage/…resolve_under_repo(path)` (or reuse `core.paths`) and
  replace process-global `os.chdir(repo_root)` in: `dashboard/renderer.py:31`, `dashboard/profile.py:317`,
  `kpi/panel_preview_executor`, `kpi/verify_kpi_output`, `workspace/flow.py:1321`, and the cwd-relative
  upload in `databricks/workspace_deployer.py:483`. Pass absolute paths to DuckDB/Polars. Ref: `SUMMARY.md` T1.
- [ ] **Atomic writes**: temp-file + `os.replace` for all JSON stores — `memory/workspace_definitions.py`
  (104/110/448), `user_decisions.py`, `flow.py:_save_panel` (1560/1568), `sources` allowlist.
- [ ] **Fail-loud on corrupt store**: `memory/workspace_definitions.load_workspace_definitions:212`
  silently resets a corrupt store to `[]` then overwrites it — erasing all reusable human definitions.
  Rename to `*.corrupt-<ts>` + raise instead. Same pattern in `user_decisions` + requirements mirrors. Ref: `ob-memory.md`.
- [ ] **Take the lock**: `kpi/cli_agent_confirm_cli` (confirm-cli-agent-proposal) and
  `sources/_register_external_allowlist:692` rewrite JSON with no `workspace_lock`. Route through the
  envelope / acquire the lock. Ref: `ob-kpi-c.md`, `ob-sources.md`.
- [ ] **SQLite concurrency**: `storage/workspace.py` — single connection, `check_same_thread=True`,
  no WAL/timeout, shared across loop+telemetry+dashboard. Enable WAL + busy_timeout, or per-thread
  connections. Ref: `storage.md`.
- [ ] **Metadata upsert**: `storage/metadata_store.py:294` Delta "upsert" is append-only → duplicate
  stale rows. Implement real merge/dedup by `document_id`. Ref: `storage.md`.
- [ ] **workspace_lock POSIX race**: `storage/workspace_lock.py` flock+unlink reclaim permits
  double-acquire; add PID-liveness check, stop unlinking on release. Also `medallion/run_state.py`
  stale-lock reclaim. Ref: `storage.md`, `medallion-b.md`.
- **Acceptance**: concurrent apply/prepare don't lose writes; an interrupted write never erases a
  store; no `os.chdir` remains in library code (`grep -rn "os.chdir" core/` is empty or test-only).
- **Verify**: `tests/regressions/test_core_p4_concurrency.py` (incl. a threaded write-race test).

## P5 — Result correctness  *(THEME T3 + T9 + standalone high-value BUGs)*
These silently produce wrong numbers/labels.
- [ ] **Parity coverage (T3)**: either execute PySpark in the live parity path or stop
  `engine_recommender` from routing to pyspark/hybrid until it's certified. Fix `_normalize_cell`
  blanket 2dp rounding + int→float coercion (precision loss > 2^53). Ref: `ob-kpi-b.md`.
- [ ] **Substring→token matching (T9)**: `result_view_builder` dedupe (`age` ⊂ `age_band`),
  `intent_coverage:752` (`LIMIT 5` masks age filter), `base_source_selector:283` (`id`⊂`paid`),
  `features` 2-char hints `:64`, `external_discovery` `.jsonl` precedence `:20/159`. Replace with
  anchored/token/equality matching.
- [ ] **Databricks success enum**: `execution/backend.py:290,431` compares `result_state == "SUCCESS"`
  which never matches `RunResultState.SUCCESS` → successful jobs recorded exit_code=1 (corrupts
  governance). Ref: `execution.md`.
- [ ] **Optimization never converges**: `optimization/strategy.py:13` reads `task["direction"]` with no
  default while loop/memory default `"higher"` → every candidate discarded. Align defaults. Ref: `optimization.md`.
- [ ] **Medallion no-ops**: `medallion/build.py:652` KPI-regen byte-copies v1→v2 then compares to v1
  (always "equal", never rebuilds); `build.py:792` failing assertion statements write non-`FAIL` keys
  so broken assertions look green; `design.py:884` type-cast/null policies are comment-only (never
  executed). Ref: `medallion-a.md`.
- [ ] **Un-gated dedup**: `onboarding-root/pipeline_sql_generator.py:56` emits `SELECT DISTINCT *`
  contradicting `deduplication: approval_gated`. Ref: `onboarding-root.md`.
- [ ] **Free-text join executable**: `relationships/contracts.py:374` marks prose "joins … on COL"
  edges executable at 0.92 with no uniqueness/RI gate; add the gate + plan-level fan-trap detection.
  Ref: `ob-relationships.md`.
- [ ] **Wiki data loss**: regeneration drops human sections with non-canonical headings; `_split_sections`
  isn't fenced-code-aware. Preserve unknown sections. Ref: `wiki.md`.
- **Acceptance**: parity badge only appears for executed engines; no substring false-match; Databricks
  success recorded correctly; loop converges on a `direction`-less task; medallion regen/assertions
  behave; wiki regen preserves human edits.
- **Verify**: `tests/regressions/test_core_p5_correctness.py`.

## P6 — Silent-except hardening (remainder of T5)
- [ ] `onboarding/data_quality.py:281/330/352` — three silent excepts → DQ gate `ok=True` on
  unreadable data. Narrow to specific exceptions; count read failures as findings.
- [ ] `documents` accepted_candidates broad except → discards human-confirmed decisions; narrow it.
- [ ] `sources/catalog.py:1028/1262` parse failure → silent "fetched"/empty rows; quarantine instead.
- [ ] Sweep: `grep -rn "except Exception" core/` → triage each; either narrow or add logging+finding.
- **Acceptance**: no governance/DQ gate can report green purely because an error was swallowed.

## P7 — Integration wiring (THEME selected INTEGRATION findings)
Decide per-item: **wire it** or **delete it** (don't leave half-built).
- [ ] `orchestration/governor.py` (Governor/decide_routing/run_specialist) + `runner.py`
  (ExperimentRunner) — unused by the loop. Wire the error-routing step or remove. Ref: `orchestration.md`.
- [ ] `agents` — no Anthropic/Claude API engine exists; `registry.get_intern` picks engine from
  `google_api_key`, ignoring `main_agent`. Implement the Claude engine or make routing honor
  `main_agent`. Ref: `agents.md`. (Confirm with user which provider is intended.)
- [ ] `apply-kpi-panel-answer` hand-rolls the envelope → misses the reliability tripwire/op-signal
  hooks. Route through `run_workspace_command`. Ref: `ob-workspace-c.md`.
- [ ] `harness` trajectory: flow.py records only `workflow_step status=ok`; unify with `cli_runner`'s
  tool_start/tool_result so stall/failed checks see the flow path. Fix `_fired_specialists` substring
  match + `stalled_step` command-string pairing. Ref: `ob-harness.md`.
- [ ] `context/doc_retrieval` orphaned + `refresh` flag no-op; `vocabulary_panel` unwired (no script,
  no lock). Wire or remove. Ref: `context.md`, `ob-workspace-c.md`.
- [ ] `data_model` governance-language contradiction: sidecars say `executable_usage_allowed=False`
  but `relationships/contracts.py` auto-promotes profile-matched edges to executable. Reconcile the
  user-facing promise with the behavior. Ref: `ob-data_model.md`.
- [ ] `flow.py` intents `full_kpi_sql` vs `usual_workflow` run identically — make them differ or
  collapse to one. Ref: `ob-workspace-a.md`.

## P8 — Cleanup (THEME T10 + T11 + T12) — lowest risk
- [ ] **Dead code**: `contracts/versioning.migrate` (test-only — add a real v2 or document as future),
  `medallion` budget/prompt_strategies/tier_router (unwired), `dashboard` non-SQL render path,
  `agents.StubLLMEngine`, `databricks/post_api`, orphaned storage read APIs, `context_pages.jsonl`.
- [ ] **Dup helpers (T11)**: consolidate `_rel`/`_load_json`/`_norm`/`_slug`/`_now` (diverging across
  onboarding-root/sources/memory/relationships) into one shared util; pick one `_norm` semantics.
  Also `_CLI_DISPATCH` triplicated across agents.
- [ ] **Agnostic leaks (T12)**: `kpi/feature_resolver.py:1219` hardcoded healthcare
  procedure/description/code branch; `source_family_contracts.json` hardcoded
  `partition_columns:["report_year"]`. Derive from evidence. Lock with `tests/test_genericity_audit.py`.
- [ ] **(Optional) flow.py decomposition**: extract results layer first (fixes the chdir hazard while
  splitting); then per-intent handlers behind a registry. Only after P1–P7 are green.

---

## Test & verification strategy
- Each phase ships its regression test(s) under `tests/regressions/`.
- Phase gate (must pass before merge): `python -m pytest tests/ -q` + `uv run validate-workspace-artifacts --workspace workspaces/Healthcare-RCM-Data-Platform` + green_gate.
- End-to-end sanity after P1/P3/P4/P5: re-run the KPI pipeline on Healthcare-RCM and on
  Hostile_Synthetic; diff `current.md` — numbers must match the pre-fix baseline except where a bug
  was correcting a wrong value (document those).
- Re-run the relevant audit unit after its phase to confirm findings are closed (spawn the same
  per-unit auditor prompt, or read the file and re-grep the `file:line`s).

## Sequencing rationale
P1→P2→P3 are safety/security (green-but-leaking, bypasses, injection). P4 unblocks multi-user.
P5 fixes silently-wrong outputs. P6/P7 harden and connect. P8 is cosmetic/debt. Each phase is
independently shippable; P0 first.

---

## Resolved open decisions (confirmed by user 2026-06-13)
1. **gitignore mlruns/ + mlflow.db** -> YES, ignore both. (`mlflow.db` already matched by `*.db`;
   explicit MLflow section added.) [DONE in P0]
2. **LLM provider (P7 agents)** -> Make routing **honor `main_agent`** and use the orchestrating
   CLI agent's LLM (per "LLM via CLI agent, not SDK"); do **not** build a direct Claude SDK engine.
3. **PySpark parity (P5/T3)** -> **Stop recommending** pyspark/hybrid until certified:
   `engine_recommender` must not route to pyspark/hybrid, and the parity badge only appears for
   engines actually executed. (Smaller P5 footprint — no live PySpark wiring.)
4. **flow.py decomposition (P8)** -> **Defer** until P1-P7 are green.

## Progress ledger (update as phases land)
| Phase | Branch | PR | Status | Tests added | Notes |
| --- | --- | --- | --- | --- | --- |
| P0 hygiene | fix/core-p0-hygiene | (pending) | done (code) | tests/regressions/ scaffold + P0_BASELINE.md | gitignore mlruns/+mlflow.db; MANIFEST regen wired into `complete` (17->23/29); baseline 24 failed / 1552 passed (pre-existing, see P0_BASELINE.md) |
| P1 PII | fix/core-p1-pii | (pending) | done (code) | tests/regressions/test_core_p1_pii.py (20) + test_pii_redaction full-set | masking parity (SHA-256 all 3 engines, new sensitive_masking module, line-126 fix); packet+verifier redaction; sensitivity-shape unified (validator+medallion read flat is_sensitive); PCI single-sourced from phi_gate; phi_gate stale fails closed; backend phi-gate fail-closed. Net: 24->23 baseline failures (PreviewRowCap now passes; no new failures) |
| P2 gates | - | - | not started | - | |
| P3 injection | - | - | not started | - | |
| P4 concurrency | - | - | not started | - | |
| P5 correctness | - | - | not started | - | |
| P6 silent-except | - | - | not started | - | |
| P7 wiring | - | - | not started | - | provider decision pending |
| P8 cleanup | - | - | not started | - | flow.py split optional |

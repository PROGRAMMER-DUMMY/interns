# core/ Full-Read Audit — Aggregate Summary

All **227 `.py` files / ~86.9k lines** of `core/` were read in full, package-by-package,
across 29 units (5 waves of background auditors). Per-unit detail is in the sibling
`*.md` files; this is the cross-cutting roll-up.

## Headline totals (approx)

| Tag | Count | Meaning |
| --- | --- | --- |
| [BUG] | ~110 | incorrect behavior / crash / wrong result |
| [NOT-PROD] | ~75 | works but not production-ready |
| [INTEGRATION] | ~30 | built but not wired / parts don't connect |
| [MISSING] | ~22 | stub / incomplete / gap |
| [DEAD] | ~22 | unused / unreachable |
| [DUP] | ~22 | duplicated logic |

The platform is **functionally impressive and largely wired** — the KPI pipeline,
medallion build, governance gates, deploy boundary, and human-gate provenance are real,
not aspirational. The defects cluster into a small number of **recurring systemic themes**
that cut across many packages. Fixing the themes fixes dozens of individual findings.

---

## Cross-cutting themes (fix these first — each spans multiple packages)

### T1. `os.chdir` for relative-path resolution — thread-unsafe, repo-wide
Process-global `os.chdir(repo_root)` is used to resolve relative dataset paths in at least
**six** places: `dashboard/renderer.py:31`, `dashboard/profile.py:317`,
`kpi/panel_preview_executor`, `kpi/verify_kpi_output`, `workspace/flow.py:1321`
(`_write_result_preview`), and effectively in `databricks/workspace_deployer.py:483` (cwd-relative
upload). This races against the platform's own advertised parallel KPI fan-out and concurrent
Dash callbacks. **Fix once:** a shared `resolve_under_repo(path)` helper + pass absolute paths to
DuckDB/Polars; ban `os.chdir` in library code.

### T2. PII/PHI masking is SQL-only and the safety gates can't see it
- `kpi/sql_generator` emits `hash()`/`sha2()` masking, but `polars_generator`/`pyspark_generator`
  **write raw unmasked values to Gold** (ob-kpi-B) — and mismatch parity on every sensitive metric.
- The canonical `kpi_results/current.md` packet + verifier sample tables render **raw rows with no
  redaction** (ob-kpi-D) — `pii_redaction` is wired only into the blocker panel.
- The onboarder writes `columns.<name>.is_sensitive` but the validator + `medallion/design.py`
  read `datasets[].columns[].pii` (ob-workspace-B) — so the **anti-PII-in-Silver invariant collects
  zero columns and can never fire**.
- `phi_gate` trusts a possibly-stale `profile_index.json` with no freshness check (governance).
- `pii_redaction` PCI patterns have **drifted** from `phi_gate.PCI_IDENTIFIER_PATTERNS`; the sync
  test only spot-checks ~7 (ob-kpi-D + governance [DUP]).
**Net effect:** a sensitive column can reach Gold, the results packet, and a non-covered target
unmasked, while every automated gate reports green. This is the single highest-risk theme.

### T3. Engine parity certifies Polars only; PySpark ships unverified
The "engine parity" badge and result packet compare **SQL vs Polars only**; PySpark is never
executed in the live results path, yet `engine_recommender` can route a KPI to pyspark/hybrid
(ob-kpi-B). A pyspark-recommended KPI ships labeled "verified" with zero cross-engine evidence.
Compounded by `_normalize_cell` blanket-rounding to 2 dp (absorbs real sub-0.005 divergence) and
int→float coercion (precision loss above 2^53).

### T4. Code/SQL injection into generated artifacts
Workspace-owned formula templates, filter values, PII expressions, PK/column names are
interpolated **unescaped** into emitted SQL and `F.expr("...")` PySpark, then executed via
subprocess: `medallion/delta_emitter` + `merge_emitter`, `kpi/sql_generator` + the Polars/PySpark
generators, and `execution/databricks_client.py:142` (`write_delta` INSERT f-string). Expression
*evaluation* itself is safe (no `eval`/`exec` anywhere — confirmed in ob-features), but the emitted
text is an injection surface.

### T5. Silent excepts that turn failure into a false "green"
Recurring `except Exception: pass`/`-> {}`/`-> []` that converts errors into clean passes:
- `governance` PHI-gate failure helper fails **open** (execution/backend.py:587).
- `data_quality.py` three silent excepts → DQ gate reports `ok=True` on unreadable data.
- `documents` + `memory` broad excepts can **discard human-confirmed decisions**.
- `sources/catalog.py` parse failures silently downgrade to "fetched" / empty rows.
- `harness` records `status="ok"` on the flow path regardless of real outcome.

### T6. Non-atomic, unlocked JSON writes → durability + concurrency loss
Decision stores, requirements mirrors, session.json, workspace_settings.json, and the metadata
store are written with bare `write_text` (no temp+`os.replace`) and often **without the workspace
lock**: `memory/workspace_definitions` (can permanently erase all reusable human definitions on one
partial write), `kpi/confirm-cli-agent-proposal` (no lock), `sources/_register_external_allowlist`
(no lock, last-writer-wins), `flow.py` `_save_panel` (double non-atomic write), plus
`storage/workspace.py` single SQLite connection with `check_same_thread=True` and no WAL/timeout.
The platform is **not safe for concurrent/multi-user operation** until T1+T6 are addressed.

### T7. Remote-approval / gate bypasses
- `databricks/workspace_deployer.py:509` — **Genie-lane apply skips `run_deploy_gates`** entirely
  (G1/G2 and G3 human-provenance never run) while doing real remote mutation.
- `onboarding-root/pipeline_deployment_plan.py:37` — remote gate only fires for `external`/`warehouse`
  targets; a `databricks` target in apply mode bypasses the env check.
- `workspace-B/check_databricks_readiness` — live `health_check()` network call on every local-safe
  bootstrap, before any approval.
The medallion UC lane is the **correct reference** (dry-run-first, plan-hash bound, consume-once,
G4/G5 re-verified, human-attributed) — the other lanes should adopt its contract.

### T8. External-root / SSRF governance not enforced (sources)
`external_discovery._validate` never consults the `external_data_roots` allowlist, and
`catalog._plan/_apply_local_source` accept any absolute host path; `api` fetch has no SSRF egress
control (no block on loopback/RFC1918/`169.254.169.254`). Once a selection is approved, arbitrary
host files can be enumerated/copied into a governed workspace. The allowlist helper exists in
`storage/external_data` but is simply not called on these paths.

### T9. Substring matching used where token/equality matching is required
Repeated class of false-positive bug: `alias in term` / bare-integer-in-SQL / unanchored substring:
- `result_view_builder` drops `age` because it's a substring of `age_band` (ob-kpi-D).
- `intent_coverage` — `LIMIT 5` masks a missing age filter (ob-kpi-A).
- `base_source_selector` — `id` matches `paid`/`district`, flips fact-table pick (relationships).
- `features` start/stop hints — 2-char `in`/`to`/`out` misclassify `claim`/`total`/`account`.
- `harness` `_fired_specialists` + `stalled_step` — substring/command-string matching defeats the check.
- `external_discovery` — `.jsonl` precedence misclassifies NDJSON logs as datasets.

### T10. Dead / test-only machinery presented as production capability
- `contracts/versioning.migrate()` + `register_migration` — **entirely test-only**; zero migrations
  registered, every contract v1.
- `orchestration/runner.ExperimentRunner` + `governor.Governor` — exported, never invoked by the loop.
- `medallion` P4 budget + prompt-strategy/tier-router — never reaches `intern.design()`.
- `dashboard` non-SQL-dialect render path — polars/pyspark KPIs silently render "blocked".
- `flow.py` intents `full_kpi_sql` vs `usual_workflow` — recorded but run identically.
- `context` `doc_retrieval` + `context_pages.jsonl` — orphaned; `refresh` flag is a no-op.
- `agents` `StubLLMEngine`, `databricks/post_api`, several storage read APIs — unused.

### T11. `_rel`/`_load_json`/`_norm`/`_slug`/`_now` duplicated and diverging
Path/id/text helpers are re-implemented in 6-8 modules (onboarding-root, sources, memory, relationships)
with **subtly different rules** (e.g. `_norm` strips `[a-z0-9]` in one place, `[a-z0-9_]` in another),
risking divergent dedup/reuse keys. Consolidate into one shared util.

### T12. Workspace-agnosticism leaks
- `kpi/feature_resolver.py:1219` — hardcoded healthcare `procedure`/`description`/`code` scoring branch.
- `onboarding-root/source_family_contracts.json` — hardcodes `partition_columns: ["report_year"]`.
(The lexicon itself is clean — confirmed workspace-derived; the only sanctioned curated vocabulary is
the generic finance/time/identifier seed in `workspace/research.py`, which is universal, not domain.)

---

## Confirmed-healthy (don't "fix" these)
- Local-safe-by-default + remote-needs-approval gating in `build_execution_backend` (env opt-in,
  strict fail-closed, DuckDB fallback, lazy imports).
- Human-gate provenance (BUG-014: `source: human` vs `agent`) genuinely enforced in relationships
  and flow.py review.
- Medallion UC deploy lane: dry-run-first, plan-hash bound, consume-once, G4/G5 re-verified.
- Telemetry dual-write (Local always + Databricks when configured) is correct.
- STAGE_ROUTING covers all 10 agents + 17 skills, test-locked.
- Optimization planner/memory/classifier genuinely wired into the loop.
- Expression evaluation is `eval`/`exec`-free (pure regex tokenization).
- Diagram-edge hallucination guard (both endpoints must profile_match + pass RI gates).

---

## Suggested remediation order
1. **T2 + T5 (PII fail-open / fail-green)** — safety gates that report green while leaking. Highest risk.
2. **T7 + T8 (gate/approval/SSRF bypass)** — before any remote or external-source rollout.
3. **T4 (injection into emitted artifacts)** — shared escaping/parameterization layer.
4. **T1 + T6 (chdir + non-atomic/unlocked writes)** — blockers for concurrent/multi-user use.
5. **T3 (parity coverage)** — either execute PySpark in the parity path or stop recommending it.
6. **T9 (substring→token matching)** — mechanical but corrupts KPI results silently.
7. **T10 + T11 + T12 (dead code / dup helpers / domain leaks)** — cleanup; lower risk.

See per-unit `*.md` files for exact `file:line` and suggested fixes for every finding.

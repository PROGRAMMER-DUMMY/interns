# HANDOFF — autoresearch / KPI platform

---

## Session 2026-06-02 — current state (⚠️ ALL UNCOMMITTED)

Everything in this section is **in the working tree, green, but NOT committed** on branch
`kpi-multi-runtime-engine`. Green gate: **`.venv\Scripts\python.exe -m core.dev.green_gate --sweep`
→ 304 tests, 0 failing, 0 regressions, 3 known-baseline** (the 3 pre-existing `test_pipeline_sql_generator`
×2 + `test_kpi_proof_packet` ×1). Tests run with `.venv\Scripts\python.exe -m unittest …` — **never
`uv run`** (hook-blocked).

### What landed (working tree)
1. **BUG-024 — percentage-share grain fix** (`result_view_builder.py`): declared cuts now SUBDIVIDE
   the share (grain = all cuts), numerator `PARTITION BY` full grain, denominator stays grand-total.
   ⚠️ **This REVERSES the 2026-06-01 "kpi_002 group-only grain" decision below** — the KPI's stated
   cuts (gender/age/visit-type/department) must all appear. Do not "fix" it back.
2. **KPI intent-coverage harness** (`core/onboarding/kpi/intent_coverage.py` + `validate-kpi-intent-coverage`):
   independent (NOT via `parse_kpi`) checks that the generated result-view SQL realizes the KPI's
   grain / metric / explicit filters / **JOINs (must match a proven relationship)** / prose filters.
   Enforced inside `execution_harness._semantic_errors` (hard gate).
3. **Denominator-scope fix** (`pipeline_decisions.percentage_denominator_scopes`): `within_<group>`
   now actually emits `OVER (PARTITION BY <group>)` (was silently ignored → grand-total); the harness
   hard-fails `denominator_scope_not_realized` if a recorded scope isn't applied. (`result_view_builder.py`,
   `sql_generator.py`, `intent_coverage.py`, `execution_harness.py`.)
4. **Assurance/quality batch (BUG-025):** gate-provenance assurance banner + `--require-human-gates`
   (`flow.py`); **low-cardinality diagram-join confidence cap** (dimension < 50 rows → flagged,
   capped at 0.75) (`contracts.py`); **dashboard spec fidelity** — chart x/y/color resolve from the
   emitted result-view columns + a `CAST(x AS DATE)`-alias parser fix (`core/dashboard/inference.py`,
   `spec.py`); **KPI-extraction determinism** — `workbook_structure` now reads `worksheets[0]` not
   `wb.active` (fixed the 3-vs-42 non-determinism); `ready_marker` stub → `ValueError`
   (`sql_generator.py`); **result-packet staleness guard** + `/solutions/` hand-edit = workflow-guard
   ERROR.
5. **KPI Intent Contract artifact (Phase 3)** (`core/onboarding/kpi/intent_contract.py` +
   `build-intent-contract`): 7 facets (metric/grain/filters/denominator_scope/temporal_anchor/
   output_shape/null_zero) each with `{value, confidence, source, evidence, alternatives}`;
   `low_confidence_facets()` returns panel-ready questions. Writes `kpi_intent_contract.json`.
6. **PDF / document ingestion (`opendataloader-pdf`) — full loop, proven end-to-end:**
   `core/onboarding/documents/document_loader.py` (`scan-document`, free-mode default, Java-11+
   preflight, graceful degrade if lib/Java absent, PHI redaction via existing `pii_redaction`,
   review-gated sidecar, deterministic), `classifier.py` (walks the REAL opendataloader `kids` tree;
   routes tables→KPI/lexicon candidates, text→ERD/prose), `candidate_review.py`
   (`prepare-document-candidate-review`) + `candidate_apply.py` (`apply-document-candidate`:
   human-confirmed, refuses without `--confirmed-by`, durable `accepted_candidates.json`). Onboarding
   wiring: `WorkspaceOnboarder._scan_documents()` (no-op without PDFs) + `_accepted_document_kpis()`
   (a human-confirmed PDF KPI candidate now MERGES into `kpi_registry.json` as a proposal →
   flows downstream). Verified: scan→review→apply→onboard→KPI in registry.
7. **Design docs:** `docs/design/kpi_intent_contract.md`, `docs/design/pdf_ingestion.md`.

### Dependency / setup
- `opendataloader-pdf==2.4.7` is installed in `.venv` (via `uv pip install`); **Java 24 present**
  (needs ≥11; smoke-tested OK). It is **NOT in `pyproject` deps yet** — add to a `documents` extra
  when ready. PDF code degrades gracefully if it/Java is absent.
- **New `[project.scripts]`:** `validate-kpi-intent-coverage`, `scan-document`,
  `prepare-document-candidate-review`, `apply-document-candidate`, `build-intent-contract`. Run
  **`uv sync`** once so `uv run <cmd>` resolves them (Python APIs + tests already work without it).

### NOW WIRED (2026-06-03 session — was "NOT wired yet")
- **Non-KPI candidate consumption — DONE.** `onboarding._accepted_document_open_questions()` appends
  human-confirmed prose/SLA candidates to `open_questions.md`; `_accepted_document_relationship_notes()`
  surfaces data_model candidates as **NON-EXECUTABLE** relationship notes (profile RI proof still
  required — never auto-executable); `lexicon/builder._harvest_from_document_candidates()` attaches
  confirmed glossary terms as `from_dictionary` aliases **only to columns that already exist** (never
  invents a column). Tests: `tests/test_document_candidate_consumption.py`.
- **Intent-contract routing — DONE.** `intent_contract.intent_facet_panel_questions()` builds
  contract-conformant blocker-panel questions for unanswered low-confidence facets; the blocker panel
  appends them to the panel SET (index.json) but NOT to `current` (so the KPI flow's stop semantics
  are unchanged — intent ambiguity is surfaced, not hard-blocking). `record_intent_answer()` persists
  to `kpi_intent_answers.json` and **mirrors denominator_scope into pipeline_decisions.json** so it
  actually changes generated SQL; answered facets are skipped on re-prepare. `_apply_option` gained an
  `intent_facet` branch. `flow._collect_gate_provenance` now surfaces low-confidence facets as
  agent-asserted gates (human once answered) → enforceable with `--require-human-gates`. Also fixed a
  latent bug: `_load_registry_with_features` now reads the real `{"kpis": [...]}` shape, not only a
  bare list. Tests: `tests/test_intent_contract_routing.py`.
- **PHI gate — DONE.** New `core/governance/phi_gate.py`: HIPAA-18 identifier detection (column-name
  based; reuses/extends the pii patterns), `PHIAssessment` tier, `databricks_phi_covered` (reads new
  `[databricks].phi_covered` lock.toml flag, default False), and `enforce_remote_phi_gate` which
  BLOCKS non-covered remote upload/exec of identifiable PHI unless de-identified/BAA. Wired into the
  upload path (`run_deployment(apply=True)`) and the remote execute backends (Strict jobs/warehouse).
  Local DuckDB is always allowed. CLI: `assess-workspace-phi`. Tests: `tests/test_phi_gate.py`.
- **Enforcer (advisory→enforced) — DONE (partial, the runtime lever).**
  `workflow_guard_harness._check_required_specialists_fired()` flags a COMPLETED stage that listed
  `required_specialists` but in which none fired (no hand-off note / recorded review / trajectory
  step naming them) — the runtime complement to `roster_not_routed`. Severity follows `roster_severity`
  (default warning; set "error" to hard-gate). Tests: `tests/test_workflow_guard_specialist_firing.py`.
  STILL TODO: a true PreToolUse hook-level enforcer + the format-confirmation record-back gate.

### How to test now
- KPI flow: `uv run run-kpi-pipeline --workspace workspaces/Healthcare-RCM-Data-Platform --domain healthcare`
  (stops at the kpi-analyst review human gate — resolve with `workspace-flow review --session <id>
  --verdict ok --confirmed-by <you>`).
- PDF: drop a `.pdf` in `workspaces/<ws>/docs/` (RCM has none → PDF path is a no-op there), then
  `scan-document` → `prepare-document-candidate-review` → `apply-document-candidate … --confirmed-by <you>`
  → re-onboard → KPI appears in registry.
- RCM workspace currently holds a fresh `interns/` parked at review gate `wf_20260602T160757Z`.

### Gotchas
- BUG-024 reverses the old kpi_002 grain decision (see §2 below) — intentional.
- `accepted_candidates.json` lives under `interns/generated/documents/` and **survives re-onboarding**
  (not in `_clear_onboarding_artifacts`'s clear-list) — verified.
- New console scripts need `uv sync` to be callable via `uv run`.

### Recommended next steps
1. **Commit this set** (it's large + green) with a changelog/bug entry.
2. Wire the 3 remaining pieces (risk order): open_question consumption → lexicon+data_model
   consumption → intent-contract facet→blocker routing + provenance surfacing.
3. Then the still-open **PHI gate** (§ below, the prior-session #1).

---

State (2026-06-01): This session **committed the entire previously-uncommitted batch** (prior
session's BUG-001..012 + this session's new work) in **11 logical commits** on branch
`kpi-multi-runtime-engine`, added a `.gitignore` (the committed one was empty), built a **KPI file
format-detection + confirmation layer**, added a **kpi-analyst hard review gate** + **centralized
stage routing**, added the **kpi-clarification** skill, and — via `/grill-me` — surfaced a **live PHI
exposure** that is now the #1 priority. This doc captures only what a fresh session can't re-derive:
current state, decisions, the PHI situation, agreed priorities, and non-obvious gotchas. Durable
architecture facts live in the memory index
(`C:\Users\shubh\.claude\projects\C--Users-shubh-OneDrive-Desktop-interns\memory\MEMORY.md`).

## 🔴 #1 — ACTIVE PHI EXPOSURE (unresolved, user action)

Real, maximally-identifiable PHI was uploaded straight into a **Databricks 14-day premium trial**
Unity Catalog (`config/lock.toml`: `enabled=true`, `execution=warehouse`, `catalog=healthdata`).
A trial has **no BAA / no compliance security profile → not HIPAA-covered**. Schema (column names
only) confirms identifiability: `patients.csv` has `SSN, FirstName, LastName, MiddleName, DOB,
Address, PhoneNumber`; `transactions.csv` has `MedicaidID, MedicareID`. Treat as breached.

**Agreed remediation (NOT done — only you can run the remote parts):**
- A. `DROP` the PHI tables/schema in UC (`patients, encounters, transactions, providers,
  departments`; or `DROP SCHEMA healthdata.default CASCADE`).
- B. Purge leaked copies: SQL **query history**, **DBFS `/FileStore`**, **MLflow** artifacts,
  **notebook revision outputs**.
- C. Rotate `DATABRICKS_TOKEN` in `.env` (gitignored); consider `lock.toml enabled=false`.
- Continue dev on **local DuckDB** (safe — already proven) or synthetic data.

## Agreed priority order (from the /grill-me session)

1. **PHI purge + rotate + synthetic data** (the incident above).
2. **PHI gate (systemic):** wire identifier-detection → a `PHI` data-understanding tier → BLOCK
   non-covered remote upload/exec unless de-identified/BAA. **Reuse the HIPAA-identifier check and
   the new `kpi_format_detector`/profiler.** Not built.
3. **Enforcement layer:** the format-confirmation gate's record-back + a **hook-level enforcer** so
   `required_specialists`/`suggested_skills` actually FIRE, not just get listed. Not built.
- Deferred: overengineering pass (~90 CLIs / 15 agents / 19 skills), push+CI+PR, enterprise
  auth/RBAC.

**Throughline:** every gap this session was the same failure — *advisory ≠ enforced* (kpi-analyst
didn't fire; routing was advisory; the PHI guardrail was advisory). Partial fixes landed (gate +
routing); the hook-level enforcer is the general fix.

## What changed this session (so you don't redo it) — all COMMITTED

11 commits on `kpi-multi-runtime-engine` (run `git log --oneline -12`). Highlights:

1. **BUG-012** — windowed-only result views emit `SELECT DISTINCT` (one row per grain).
   `result_view_builder.py`.
2. **kpi_002 reinterpretation (REVERSES BUG-002):** metric `% of count(distinct PatientID) /
   count(distinct PatientID) for department` is **share-of-total BY group** — numerator
   `PARTITION BY group`, denominator grand-total `OVER ()`, **grain = group only**. User confirmed
   (KPI name "share of lives" + business desc). The `4297` denominator BUG-002 called a "broken
   constant" is the correct grand total. Shares sum to ~221% (patients span departments — expected).
3. **Semantic gate** accepts `COUNT(DISTINCT)` as a faithful rendering of `sum(distinct X)`, derived
   from `parse_kpi` (no metric-spelling literals in the verifier). `execution_harness.py`.
4. **kpi-analyst hard review gate:** completion is blocked until a verdict is recorded via
   `workspace-flow review --session <id> --verdict ok|blocked --summary "..."`. Verdict is bound to
   a KPI-**intent** signature (metric/cuts/filters), so regen that changes intent re-gates. New
   `review` subcommand + `_kpi_review_signature`. `flow.py`.
5. **Centralized stage routing:** `_save_panel` now attaches `routing_for(stage)` so every panel
   presents its full agent+skill roster (was ~3 of ~13 panels). `flow.py` + `delegation.py`.
6. **NEW skill `kpi-clarification`** (decompose ambiguous KPI → structured definition); routed in
   `STAGE_ROUTING["kpi_definition"]`; adapters regenerated (`.agents/*`).
7. **NEW KPI format-detection layer** (3 modules, ~30 tests):
   - `core/onboarding/kpi/kpi_format_detector.py` — header+content+confidence column-role detection;
     handles 3–4+ cols, header-less, and **misleading headers via content-override for required
     roles**; flags nesting + missing-required + read-back.
   - `core/onboarding/kpi/workbook_structure.py` — openpyxl merged-cell reader (nested-KPI signal).
   - `core/onboarding/kpi/kpi_confirmation_panel.py` — mapping + real-row read-back + nesting tree +
     ambiguity, JSON + markdown.
   - Wired into `onboarding._read_tabular_kpis` (detector primary, legacy synonym fallback); every
     onboarding writes `interns/reports/kpi_format/current.{json,md}`. Verified live on the real
     `docs/Sample KPI.xlsx` (22 rows, nesting flagged, messy `col_2/col_3` headers).
8. **`.gitignore` created** (was empty → `.env`/secrets/runtime/caches were stageable). NOTE: the
   user subsequently **commented out `workspaces/**/interns/`** in `.gitignore` — interns/ is now
   trackable again, intentionally.
9. **`ci.yml`** runs the 5 new/ungated KPI test modules; same modules added to
   `green_gate.SWEEP_MODULES`.

Tests: **green-gate 187/0/0/3** (0 regressions, 3 known-baseline). **NOT pushed; CI never run**
(user declined push). `origin/main` unchanged.

## RCM workspace is WIPED to raw inputs (intentional)

The user deleted `workspaces/Healthcare-RCM-Data-Platform/interns/` (governed cleanup) to test it
themselves. `datasets/`, `docs/`, `workspace_settings.json` intact. A clean local run was verified
this session and reproduces **3-for-3 on DuckDB**: kpi_001 1169 rows, kpi_002 **20 department rows**
(share-of-total), kpi_003 10 rows — but only **after approving the 5 non-provider FK joins** (the 2
`ProviderID→providers` joins stay candidate; `H1-` namespace mismatch, correctly non-executable; the
3 KPIs don't need them).

## How to work in this repo (non-obvious / learned this session)

- **Tests/pyspark/engine-gen via `.venv\Scripts\python.exe`**, never `uv run` (PreToolUse hook
  `guard_uv_run.py` blocks it). Governed `uv run` wrappers (onboard, resolve, apply-*) are fine.
- **`apply-relationship-answer` is a SEPARATE console script** (`contracts:apply_main`), NOT
  `contracts:main --apply`. Calling the module directly runs the *builder*. Use
  `.venv\Scripts\apply-relationship-answer.exe` or `uv run apply-relationship-answer`.
- **From a clean wipe, multi-table SQL won't generate until joins are approved** (governance gate).
  Local clean-run sequence: `onboard-workspace` → `resolve-kpi-features` (RCM auto-resolves, 0
  blockers) → approve the 5 non-provider joins → `generate-kpi-sql kpi_001/002/003` →
  `run-kpi-execution-harness --quiet`.
- **Secret guard hook** (`guard_secrets.py`) blocks Read/print of `.env`/`.databrickscfg`/keys and
  even Bash commands that merely reference `.env`. Keep PHI/secret VALUES out of output; column
  names and counts are fine.
- **No emojis** in generated/committed text — `[ok]/[~]/[x]/[blocked]`.
- **green-gate sweep** has 3 known-baseline failures (`test_pipeline_sql_generator` x2,
  `test_kpi_proof_packet` x1) — NOT regressions; they are intentionally NOT in `ci.yml` (CI has no
  known-baseline allowance).

## Suggested next steps (priority order)
1. **PHI remediation** (runbook A–C above) — your action; remote.
2. **Build the PHI gate** (#2): profiler/format-detector flags identifier columns → `PHI` tier →
   `prepare-databricks-assets`/remote-exec refuses non-covered PHI upload unless de-identified. + 1 test.
3. **Build the enforcement layer** (#3): hook-level enforcer + format-confirmation record-back gate.
4. **Push `kpi-multi-runtime-engine` + open a PR**; confirm CI green (retires the standing
   "CI never run" risk).
5. Overengineering pass on the ~90 CLIs / agent+skill roster (merge candidates identified:
   kpi-clarification↔kpi-analyst, workspace-kpi-query-optimizer↔data-engineering-pipeline-design,
   domain-model↔data-model-creation).

## Suggested skills
`gitagent`/`git-guardrails` (push + PR safely), `databricks-access-gates` + a new PHI gate (the #2
work), `diagnose` (if local run misbehaves), `overengineering-auditor` (CLI complexity),
`architect`/`planner` (PHI gate + enforcement design).

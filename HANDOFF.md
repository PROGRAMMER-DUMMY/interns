# HANDOFF — autoresearch / KPI platform

State (2026-05-31, updated): a Gemini local-test session on `workspaces/Healthcare-RCM-Data-Platform`
(Hospital A EMR) exposed a batch of real engine bugs. This session logged them in
`docs/bugs/BUG_SESSION_REPORT.md`, fixed them (parallel subagents), added a quiet-output/dedupe
layer + a data-understanding gate + an internal-doc-retrieval helper, reorganized `docs/`, and
**ran a fresh end-to-end dry run on the RCM workspace** that verified the fixes in the field and
surfaced two more bugs. This doc captures only what a fresh session can't re-derive — the
**current state, weak points, and how to continue**. Durable architecture facts live in the memory
index (`C:\Users\shubh\.claude\projects\C--Users-shubh-OneDrive-Desktop-interns\memory\MEMORY.md`).

## Current state

- **Branch `kpi-multi-runtime-engine`** — prior 14 commits PLUS this session's large body of work
  (12+ bug fixes, docs reorg, new features) is **ALL UNCOMMITTED**. `origin/main` unchanged, CI
  never run. Committing this batch is the single highest-consequence open item.
- **Tests green:** `green-gate --sweep` = **187 tests, 0 failing, 0 regressions**, 3 known-baseline.
- **Fresh RCM dry run result (3 KPIs, local DuckDB):** kpi_001 ✅ (1169 rows, age now as-of-service),
  kpi_003 ✅ (10 rows), kpi_002 ✅ numerically correct (per-department denominator VARIES:
  Psychiatry 524, Pulmonology 496, Surgery 492… — no longer the broken constant 4297) **but emits
  duplicate grain rows (BUG-012, open).** Provider edges correctly held non-executable by the RI
  gate (`H1-` namespace mismatch, 0% resolution).

## What changed this session (so you don't redo it)

All in `docs/bugs/BUG_SESSION_REPORT.md` (read it — per-bug Fix paragraphs with files+tests):

1. **BUG-001** (feature dedup) — same-physical-column features for one KPI collapse; killed the
   phantom `departement` blocker. NOTE: first fix was incomplete (only proven-vs-proven); the dry
   run caught it; a second pass handles candidate-vs-proven. `feature_resolver.py`.
2. **BUG-002** (kpi_002 denominator) — per-`departement` `PARTITION BY`, not global total.
   `result_view_builder.py`. Verified correct in the dry run.
3. **BUG-003** (join key) — picks the dimension-side UNIQUE key (ProviderID over DeptID), flags
   non-unique keys non-executable. `relationships/contracts.py`.
4. **BUG-004** (RI + diagram) — referential-integrity gate blocks 0%-resolution edges (the `H1-`
   provider mismatch); consumes `DataModel.png` sidecar as ranked evidence. `contracts.py`.
5. **BUG-005** (age basis) — age computed as-of event/service date, not CURRENT_DATE.
   `result_view_builder.py`.
6. **BUG-006** (token bloat) — `--quiet` on project-harness/execution-harness/list-workspace-files/
   workspace-flow + `(xN)` warning dedupe. Wired into AGENTS.md/TOOLS.md/.agents/tools.json.
7. **BUG-007** (`runs/` snapshot) — dated snapshot now written by the executor (mirrors final
   results), not the generator. `flow.py` + `sql_generator.py`.
8. **BUG-008** (agent thrash) — WorkflowGuard checks: `repeated_identical_command`,
   `generated_artifact_hand_edited`, `throwaway_reader_script`. `workflow_guard_harness.py`.
9. **BUG-009** — restored `.gemini/settings.json` `summarizeToolOutput` to object form (an agent
   had corrupted it to a boolean).
10. **BUG-010** (data-understanding gate) — `data_understanding.py` classifier (quality tier +
    schema type), wired into `flow.py` after onboarding, + standalone `understand-data` CLI.
11. **BUG-011** (found by dry run) — BUG-001/002 interaction: the result view referenced the
    dropped `departement` column. Group token now resolves to the surviving physical column.
    `result_view_builder.py`.
12. **Docs reorg** — `docs/reference/`, `docs/plans/`, `docs/enterprise/`, each with `index.md`;
    `docs/README.md` is the map (internal-docs-vs-workspace-`interns/` scope stated). New
    **`retrieve-docs`** CLI (`core/context/doc_retrieval.py`) + `clarify-ambiguity` skill wired to
    consult internal docs before asking the user.

## Weak points / risks (the important part)

1. **HUGE uncommitted batch.** This session's 12+ fixes + docs reorg + 2 new features sit
   local-only on top of the prior 14 commits. CI never run. Commit in logical chunks ASAP — this
   is the biggest risk now.
2. **BUG-012 OPEN (found this dry run, not yet logged/fixed):** kpi_002 result view emits ~10,000
   rows — one per source record, not one per grain combo (exact duplicates). Numbers correct, output
   not deduped. Needs `DISTINCT`/group at grain in `result_view_builder.py`. Also BUG-011 + BUG-012
   are not yet written into `BUG_SESSION_REPORT.md`.
3. **Fixes are tested + dry-run-verified, NOT CI-verified.** green-gate is unit tests; the dry run
   was manual. No push has triggered `.github/workflows/ci.yml`.
4. **Advisory ≠ enforced.** `--quiet`, guard checks, `retrieve-docs`, the ambiguity wiring only help
   if the orchestrating agent uses them. The observed Gemini run did NOT use `--quiet` despite the
   guidance — which is what burned quota and forced a model fallback. The guard now DETECTS thrash
   but does not PREVENT it. Enforcement is the next real lever.
5. **Silent-wrong is structural.** The harness validates executability + schema, not business
   correctness against the workbook. A subtle spec misread still passes as `ok` unless a human
   compares to the source. (kpi_002 was exactly this class — only caught by reading the numbers.)
6. **3 pre-existing failures still open** (outside the gate, in `green_gate.KNOWN_BASELINE`):
   `test_pipeline_sql_generator` x2, `test_kpi_proof_packet` x1. Plus a pre-existing
   `test_data_model_image_parser` failure (`dept`≠`department` root match) noted during BUG-004.
7. **PHI exposure.** Real patient records in a non-HIPAA Databricks trial (`healthdata`). De-identify
   or move before real use. (Headline risk in the roadmap.)
8. **Enterprise foundation absent** (no API/auth/RBAC/real tenancy; one shared PAT) and **complexity
   high** (~90 CLIs). Roadmap + overengineering pass still pending.

## How to work in this repo (non-obvious)

- **Run tests via `green-gate`** (now on PATH) or `.venv\Scripts\python.exe -m core.dev.green_gate
  --sweep`. NEVER `uv run` for tests/pyspark/engine-gen — it resyncs pre-release pyspark 4.1.1
  (no Delta) and breaks them. The Claude hook + Gemini policy now block this; AGENTS.md documents it.
- **Secrets:** real values go in `.env` (gitignored) or `.claude/settings.local.json` `env` — never
  `.env.example` or chat. The secret guard hook blocks displaying `.env`/`.databrickscfg`/keys
  (templates like `.env.example` are allowed).
- **Cross-CLI env:** launch any CLI via `scripts\with-env.ps1 <claude|gemini|green-gate>` so `.env`
  loads once for the app + that CLI's MCP servers.
- **Databricks:** `config/lock.toml` (local) drives it; secrets via env-var names. Validate with
  `scripts\with-env.ps1 .venv\Scripts\python.exe -m tools.databricks_setup` (masks the token, prints
  the config-tailored required scopes, names a missing scope on permission errors). Token scopes for
  the current warehouse config: `sql, unity-catalog, mlflow, settings`. Remote KPI execution also
  needs `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`. Genie is spec-only (no `genie` scope needed).
- **PySpark local run** needs JDK 8/11/17 + winutils (box has Java 24; JDK17 + `C:\hadoop`). SQL/Polars
  need no JVM. DuckDB is the default local engine.
- **No emojis** in generated/committed text — use `[ok]/[~]/[x]/[blocked]`.
- **Agent/skill routing** is `delegation.STAGE_ROUTING`, locked by `tests/test_agent_skill_routing.py`
  (every `.claude/agents/*` and `skills/*` must be routed). `.claude/` is gitignored EXCEPT the
  committed `settings.json` + `hooks/`.

## Suggested next steps (priority order)
1. **Fix BUG-012** (kpi_002 duplicate grain rows — `DISTINCT`/group at grain in
   `result_view_builder.py`), then re-run the RCM dry run to confirm a clean 3-for-3.
2. **Log BUG-011 + BUG-012** into `docs/bugs/BUG_SESSION_REPORT.md` (BUG-011 fixed-but-undocumented;
   BUG-012 open).
3. **Commit the whole batch** in logical chunks (bug-fixes / quiet+guard infra / docs+retrieval),
   then push `kpi-multi-runtime-engine` + open a PR; confirm CI runs green. Retires risk #1.
4. **Enforcement, not just detection** for the advisory layer (risk #4): a hook/guard that injects
   `--quiet` or blocks the Nth identical command, so the token savings happen regardless of agent.
5. Triage/quarantine the known-baseline failures (incl. the image-parser `dept`≠`department` one).
6. Resume `docs/plans/PRODUCTION_ROADMAP.md` Phase 0 (PHI + tenancy) once local is proven.
7. Overengineering pass on the ~90 CLIs.

## Suggested skills
`gitagent`/`git-guardrails` (commit/push the big uncommitted batch safely — do this first),
`diagnose` (BUG-012 grain dedup), `auditor` (validate state vs the bug report),
`overengineering-auditor` (CLI complexity), `architect`/`planner` (enterprise Phase 1).

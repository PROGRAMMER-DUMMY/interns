# HANDOFF — autoresearch / KPI platform

State (2026-05-30, updated): the data/KPI engine works, is tested, and proven across
SQL/Polars/PySpark, workspace-agnostic, with reliability gates and a clean panel contract.
This session committed the previously-uncommitted work, fixed the failing tests, added a
portable test gate + cross-CLI guardrails, wired the Databricks config + token-scope
suggestion, and parked an enterprise production roadmap. This doc captures only what a fresh
session can't re-derive — the **current state, weak points, and how to continue**. Durable
architecture facts live in the memory index
(`C:\Users\shubh\.claude\projects\C--Users-shubh-OneDrive-Desktop-interns\memory\MEMORY.md`).

## Current state

- **Branch `kpi-multi-runtime-engine`** holds 14 commits. **NOT pushed** — `origin/main`
  unchanged. Push + open a PR when ready (no remote work done yet).
- **Tests green:** curated CI suite (99) + `test_enterprise_optimization` (78). The 4
  enterprise failures from the prior handoff are FIXED.
- **User is validating the local flow now** (Databricks connection + end-to-end). Enterprise
  work is parked until local works as intended — see `docs/PRODUCTION_ROADMAP.md`.

## What changed this session (so you don't redo it)

1. Committed the large working tree (commit `74e3523`) — the uncommitted risk is resolved.
2. Fixed the 4 `test_enterprise_optimization` failures (`3fb60e8`): metadata store defaults to
   local (updated stale tests), resolver exact-column hits stay `proven_direct`, proof-packet
   wired into the presentation exporter.
3. Added **`green-gate`** console script + skill (`core/dev/green_gate.py`): runs curated +
   enterprise suites; `--sweep` classifies new regressions vs a known baseline. Also a
   `regression-sweep` subagent and a `regression_review` STAGE_ROUTING stage.
4. Cross-CLI guardrails: Claude PreToolUse hooks (`.claude/hooks/guard_uv_run.py`,
   `guard_secrets.py`) — now committed (un-ignored) — plus Gemini policy deny rules and a
   corrected AGENTS.md Verification section (it used to tell agents to use `uv run` for tests).
5. **`with-env`** loader (`scripts/with-env.ps1`/`.sh`): loads `.env` into the process env so a
   single `.env` feeds the app and every CLI's MCP layer.
6. `.mcp.json` scaffold (context7 ready; github + databricks need env tokens).
7. Config: `config/README.md` index, `config/lock.toml` (local, gitignored) +
   `lock.toml.example`, deprecated the unwired `domain_packs/` (inference is the derived
   workspace lexicon, not curated packs).
8. Databricks: `docs/databricks_token_scopes.md` + config-aware scope suggestion wired into
   `tools/databricks_setup.py` (`required_scopes_for_config`), and `config/lock.toml` set to
   `healthdata`/`default`, warehouse mode.
9. `docs/PRODUCTION_ROADMAP.md` — enterprise gap analysis, PARKED pending local validation.

## Weak points / risks (the important part)

1. **Branch not pushed; CI never run.** 14 commits sit local-only on `kpi-multi-runtime-engine`.
   `.github/workflows/ci.yml` has still never executed — "proven in CI" is aspirational until a
   push triggers it.
2. **3 pre-existing failures still open** (outside the curated gate, recorded in
   `green_gate.KNOWN_BASELINE`): `test_pipeline_sql_generator` x2 (catalog bootstrap SQL) and
   `test_kpi_proof_packet` x1 (`data_engineering_evidence` KeyError). Not regressions; triage
   when convenient.
3. **PHI exposure.** Real patient records were uploaded to a Databricks trial workspace
   (`healthdata` catalog). A trial tenant is not HIPAA-covered — de-identify or move to compliant
   infra before real use. (Headline risk #1 in the roadmap.)
4. **Enterprise foundation absent.** No service/API layer, no auth/RBAC, no real multi-tenancy
   (workspaces are folders), secrets only in `.env`, one shared Databricks PAT. See roadmap.
5. **Complexity still high** (~90 CLIs, dormant `.example` harness configs). Overengineering
   pass still pending; `overengineering-auditor` findings stand.
6. **Unexplained workspace wipe (unreproduced, from prior session).** Root cause unknown; watch
   for it.

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
1. Validate the local end-to-end flow (user is doing this now).
2. Push `kpi-multi-runtime-engine` + open a PR; confirm CI runs green.
3. Triage or formally quarantine the 3 known-baseline failures.
4. Resume `docs/PRODUCTION_ROADMAP.md` at Phase 0 (PHI decision + tenancy model) once local is proven.
5. Overengineering pass on the ~90 CLIs + dormant harness configs.

## Suggested skills
`overengineering-auditor` (complexity pass), `gitagent`/`git-guardrails` (push/PR safely),
`auditor` (validate state vs intent), `architect`/`planner` (enterprise Phase 1 design).

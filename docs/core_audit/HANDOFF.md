# Handoff — core/ audit complete, remediation not started

**For a fresh session.** Read this first, then `SUMMARY.md`, then `REMEDIATION_PLAN.md`. You do
**not** need to re-read `core/` — it has already been read in full and the findings are durable.

## Situation (one paragraph)
A full read of the entire `core/` package (227 `.py` files, ~86.9k lines) is done. Every file was
read in full by package-scoped auditors; findings are recorded per-unit under `docs/core_audit/`
and rolled up into 12 cross-cutting themes in `SUMMARY.md`. A phased, risk-ordered fix plan is in
`REMEDIATION_PLAN.md`. **No source code has been changed** — only audit + plan docs exist. The next
job is to execute the plan, P0 first.

## Repo state
- Branch with all audit work: **`core-audit`** (6 commits, off `main`). Docs only; source untouched.
- `main` is unchanged. `git log --oneline main..core-audit` shows Waves 1–5b + SUMMARY + (this) plan/handoff.
- Untracked: `mlruns/` (and `mlflow.db` at repo root) — neither gitignored yet (P0 decision).

## Where everything lives — `docs/core_audit/`
- `README.md` — index of all 29 audit units with per-unit tag counts + status.
- `SUMMARY.md` — **start here**: headline totals, the 12 themes (T1–T12), confirmed-healthy list,
  remediation order.
- `REMEDIATION_PLAN.md` — **the plan to execute**: phases P0–P8, each with `file:line` targets,
  fix approach, acceptance criteria, verify command, and a progress ledger to update.
- 29 unit files (e.g. `governance.md`, `ob-kpi-b.md`, `ob-sources.md`) — exact findings with
  `[TAG] file:line → suggested fix`.

## What's done vs not
- [ok] Full read of `core/` — complete.
- [ok] Per-unit findings + aggregate themes + execution plan — written and committed.
- [x] Any code fix — **not started**. Progress ledger in the plan is all "not started".
- [x] PR — none opened. Branch unpushed.

## How to resume (cold start, exact steps)
1. `git checkout main && git pull` (if remote moved), then read `SUMMARY.md` + `REMEDIATION_PLAN.md`.
2. Resolve the two **open decisions** below with the user (they gate P0/P1/P2/P7).
3. Start **P0** (hygiene + safety net): branch `fix/core-p0-hygiene`, do the gitignore + MANIFEST
   regen + baseline `pytest` capture, record the baseline pass/fail in the ledger.
4. Then **P1 (PII)** — highest risk; ship the whole chain in one PR. Branch `fix/core-p1-pii`.
5. After each phase: run the phase gate (pytest + `validate-workspace-artifacts` + green_gate),
   update the Progress ledger row, open one PR.
6. Re-run the matching audit unit to confirm the phase's findings are closed before moving on.

First command for a fresh session (after reading the two docs):
```
git checkout -b fix/core-p0-hygiene
python -m pytest tests/ -q   # capture the baseline first
```

## Conventions to honor (from CLAUDE.md / AGENTS.md)
- One phase = one branch/PR off `main`. Add a regression test per BUG (fails before, passes after).
- **Never** hand-edit generated contracts to hide a bug — fix the producer and regenerate.
- Workspace-agnostic always (no domain words); local-safe by default (no remote without
  `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`).
- ASCII status markers `[ok] [~] [x] [blocked]`, no emojis.
- Secret hygiene: never print `.env`/tokens/salts; redacted key names only.
- Don't spawn sub-agents unless the user asks; the audit's done, fixes are normal edits.

## Open decisions for the user (resolve before P0/P1/P2/P7)
1. **gitignore `mlruns/` + `mlflow.db`?** (P0) — assumed yes, but confirm they're not intentionally tracked.
2. **Which LLM provider is canonical?** (P7 agents) — config loads `anthropic_api_key` + defaults
   `main_agent="claude-code"`, but the only API engine is Gemini and routing picks from
   `google_api_key`. Need to know whether to implement the Claude engine or make routing honor
   `main_agent`.
3. **PySpark parity (P5/T3)** — execute PySpark in the live parity path, or stop recommending it
   until certified? (Affects how much P5 touches.)
4. **flow.py decomposition (P8)** — do it now (optional) or defer? Recommended: defer until P1–P7 green.

## Highest-risk items if you only have time for three
1. P1 — PII/PHI fails green (masking SQL-only, packet unredacted, invariant can't fire). `ob-kpi-b/d.md`, `ob-workspace-b.md`, `governance.md`.
2. P2 — Genie deploy skips human-provenance gate; external-root/SSRF unenforced. `ob-databricks.md`, `ob-sources.md`.
3. P3 — injection into emitted SQL/PySpark/Delta. `medallion-b.md`, `ob-kpi-b.md`, `execution.md`.

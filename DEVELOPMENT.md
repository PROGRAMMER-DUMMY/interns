<!--
  ===================================================================
  !!  MODE: DEVELOPMENT  (NOT PRODUCTION)  !!
  ===================================================================
  If you are reading this file, you are DEVELOPING THE PLATFORM ITSELF
  (core/, tools/, tests/) -- not operating an end-user KPI workspace.

  This file is PRIVATE to the maintainer + the AI pair. It is not a
  user-facing doc. When pointed here, switch into development posture:
  follow develop_spec/guidelines.md, run the harness/tests/tracing the
  way we run them, and log what you change.

  This is a ROUTER. Keep it lean. Heavy content lives in develop_spec/.
  Load ONLY the spec file you need for the task in front of you.
  ===================================================================
-->

# DEVELOPMENT.md — dev-mode context (router)

**You are in development mode.** We are building and improving this platform
together. Before writing code, read `develop_spec/guidelines.md`. After you
change anything, append to `develop_spec/changelog.md` and update
`develop_spec/follow_ups.md`.

## What this is (and is not)

- **Is:** the entry point that tells an agent "we develop the product here," and
  routes to the right spec. Private to us.
- **Is not:** a replacement for the operating docs. Those stay authoritative:
  - `AGENTS.md` — canonical operating guide (workflow, selection rules, gates).
  - `CONTEXT.md` — domain language + architecture.
  - `README.md` — repo purpose, layout, verification commands.
  - `TOOLS.md` + `.agents/tools.json` — tool catalog, routing, safety.
  - `CLAUDE.md` — per-CLI init + token discipline.

## Route table — load only what the task needs

| If you are about to... | Read |
| --- | --- |
| Write/modify any platform code | `develop_spec/guidelines.md` |
| Run onboarding / KPI pipeline / a wrapper | `develop_spec/harness.md` |
| Add or run tests, run the gate | `develop_spec/testing.md` |
| Inspect events / telemetry / traces | `develop_spec/tracing.md` |
| Decide if work is ready to commit/merge | `develop_spec/path_to_production.md` |
| See what changed recently / why | `develop_spec/changelog.md` |
| Pick up the next open item | `develop_spec/follow_ups.md` |

## The five non-negotiables (full text in guidelines.md)

1. **Generic, never workspace-specific.** No domain words baked into logic; the
   genericity guard test must pass.
2. **Tests run on the venv interpreter, not `uv run`** (a hook blocks `uv run`
   for tests/pyspark/engine-gen). `green-gate` is the portable gate.
3. **Never hand-edit generated contracts** (`interns/generated/**`). Fix the
   generator and re-run.
4. **No emojis** in code/output/docs — ASCII markers `[ok]` `[~]` `[x]`.
5. **Log your change** in `develop_spec/changelog.md`; record loose ends in
   `develop_spec/follow_ups.md`.

## Working loop

`read guidelines` -> `change generically` -> `add/extend tests` ->
`green-gate + targeted unittest (.venv)` -> `validate if a workspace touched` ->
`update changelog.md + follow_ups.md` -> `path_to_production.md gates` -> commit.

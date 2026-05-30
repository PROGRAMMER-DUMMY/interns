# config/ — index

What each configuration file is, who reads it, and whether it is active.

Status legend: `[active]` loaded as-is · `[template]` copy to the non-`.example`
name to activate · `[empty]` present but unpopulated · `[missing]` expected by code
but not yet created.

## Files

| File | Purpose | Read by | Status |
|------|---------|---------|--------|
| `lock.toml` | Runtime lock: `[databricks]` (enabled, execution, catalog, schema, fallback) and other typed settings. Secrets are read from env vars **named** here, not stored here. | `core/config.py` | `[missing]` — without it, Databricks stays off (DuckDB only) |
| `agents.toml` | Agent roster per domain (`[interns.<domain>]`) + call/timeout/token limits. | `core/config.py` agent loader | `[active]` |
| `tasks.json` | Task registry for workspace selection (`active_task`, `tasks[]`). | workspace selection / `CLAUDE.md` flow | `[empty]` |
| `databricks_scopes.json` | Reference: all Databricks OAuth scopes (`_meta` + `scopes`) with relevance notes. | docs / scope planning | `[active]` (reference) |
| `external_data_roots.example.json` | Policy for huge external data roots (bounded listing, allowlist required before reads/profiling). Root path from `CMS_COLD_STORAGE_ROOT`. | external-source intake | `[template]` |
| `source_catalogs/cms_public.json` | A source catalog definition (CMS public data). | source-catalog commands | `[active]` |
| `domain_packs/*.json` | Domain inference rules (metric rules, cut rules, column aliases). `--domain <name>` loads `<name>.json` + `generic.json`. See `domain_packs/_README.md`. | `core/onboarding/domain_packs.py` | `[missing]` — only `_README.md` present; no packs |
| `ai_cli_harness.example.json` | AI-CLI eval harness config (which CLI, args, system-prompt file, pass threshold). | `run-ai-cli-harness` | `[template]` |
| `ai_cli_system_prompt.example.txt` | System prompt used by the AI-CLI harness. | `run-ai-cli-harness` | `[template]` |
| `ai_cli_harness.governed_suite.example.jsonl` | Governed command-policy eval scenarios (tool-use, raw-read rejection, artifact/workflow-guard checks). | `run-ai-cli-harness` | `[template]` |
| `ai_cli_harness.workflow_scenarios.example.jsonl` | Workflow eval scenarios for the AI-CLI harness. | `run-ai-cli-harness` | `[template]` |
| `ai_harness.example.json` | AI-app (HTTP API) eval harness config. API key from `AI_APP_API_KEY`. | `run-ai-app-harness` | `[template]` |
| `ai_harness.kpi_suite.example.jsonl` | KPI eval scenarios (mapping / sql_semantic / result_table / adversarial). | `run-ai-app-harness` | `[template]` |

## Conventions

- **`.example` files are templates.** Copy to the non-`.example` name to activate,
  e.g. `cp ai_cli_harness.example.json ai_cli_harness.json`. Same pattern as
  `.env.example` -> `.env`.
- **Secrets never live in `config/`.** `lock.toml` stores the *names* of env vars
  (`host_env`, `token_env`, `http_path_env`); the values live in `.env` (gitignored).
- **No emojis** in generated/committed text — use `[ok] / [~] / [x] / [blocked]`.

## Environment variables referenced by config

| Var | Used by | Where set |
|-----|---------|-----------|
| `DATABRICKS_HOST` / `DATABRICKS_TOKEN` | Databricks connection | `.env` (names in `lock.toml`) |
| `DATABRICKS_HTTP_PATH` | warehouse-mode SQL execution | `.env` |
| `AI_APP_API_KEY` | AI-app harness | `.env` |
| `CMS_COLD_STORAGE_ROOT` | external data root | `.env` |

## Activation gaps (as of this writing)

1. `lock.toml` is absent -> Databricks is disabled; the platform runs DuckDB locally.
2. All harness configs are `.example` only -> the AI/KPI eval harnesses are dormant
   until activated.
3. `domain_packs/` has only `_README.md` -> domain inference has no packs to load
   (add `generic.json` and a per-domain pack such as `healthcare.json`).

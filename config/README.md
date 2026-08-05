# config/ — index

What each configuration file is, who reads it, and whether it is active.

Status legend: `[active]` loaded as-is · `[template]` copy to the non-`.example`
name to activate · `[empty]` present but unpopulated · `[missing]` expected by code
but not yet created.

## Files

| File | Purpose | Read by | Status |
|------|---------|---------|--------|
| `lock.toml` | Runtime lock: `[databricks]` (enabled, execution, catalog, schema, fallback) + typed settings. Real, machine-local; **gitignored**. Secrets read from env vars **named** here, not stored here. | `core/config.py` | `[local]` — copy from `.example` |
| `lock.toml.example` | Committed template for `lock.toml` (placeholders, `enabled=false`). | — | `[template]` |
| `agents.toml` | Agent roster per domain (`[interns.<domain>]`) + call/timeout/token limits. | `core/config.py` agent loader | `[active]` |
| `tasks.json` | Task registry for workspace selection (`active_task`, `tasks[]`). | workspace selection / `CLAUDE.md` flow | `[empty]` |
| `databricks_scopes.json` | Reference: all Databricks OAuth scopes (`_meta` + `scopes`) with relevance notes. | docs / scope planning | `[active]` (reference) |
| `external_data_roots.example.json` | Policy for huge external data roots (bounded listing, allowlist required before reads/profiling). Root path from `CMS_COLD_STORAGE_ROOT`. | external-source intake | `[template]` |
| `source_catalogs/cms_public.json` | A source catalog definition (CMS public data). | source-catalog commands | `[active]` |
| `optimization_playbook.yaml` | Symptom -> detect -> cheapest-first remedies rules (Spark/AQE, Delta clustering, Photon, dbt/warehouse, Polars) + Q5 revisit triggers, every threshold cited to a vendor doc. | `core/blueprint/playbook.py` | `[active]` |
| `domain_packs/` | Curated domain-inference packs. **[deprecated]** — never wired (no loader); superseded by the derived workspace lexicon in `text_parser.py`. Do not add packs. See `domain_packs/_README.md`. | nothing | `[deprecated]` |
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

## Local configuration layer (gitignored — real values per machine)

Configuration lives in two layers: committed templates/defaults, and a local layer
holding real values that must never be committed. Each local file has a committed
counterpart so a fresh clone knows what to create.

| Local file (gitignored) | Committed counterpart | Holds | Consumed by |
|-------------------------|-----------------------|-------|-------------|
| `config/lock.toml` | `config/lock.toml.example` | Databricks enable/exec/catalog/schema; env-var names | `core/config.py` |
| `.env` | `.env.example` | Real secret/connection values (Databricks, LLM, GitHub) | the app (`os.environ`) + every CLI MCP layer (`.mcp.json ${VAR}`) via `scripts/with-env` |
| `.claude/settings.local.json` | `.claude/settings.json` | Personal permission allow-list + `env` block; the committed file holds the project hooks | Claude Code |
| `~/.gemini/policies/autoresearch.toml` (user-level copy) | `.gemini/policies/autoresearch.toml` | Same guardrail policy, installed at user level (workspace tier is non-functional) | Gemini CLI |
| `.mcp.json` is committed | — | MCP server defs; secret values resolved from the process env at startup | any MCP-capable CLI |

Load rule: the app and all CLIs read the **process environment**, not `.env`
directly. Use `scripts/with-env.ps1 <cli>` (or `.sh`) to load `.env` once, then a
single `.env` feeds the app and every CLI. See repo root for `scripts/with-env.*`.

## Environment variables referenced by config

| Var | Used by | Where set |
|-----|---------|-----------|
| `DATABRICKS_HOST` / `DATABRICKS_TOKEN` | Databricks connection | `.env` (names in `lock.toml`) |
| `DATABRICKS_HTTP_PATH` | warehouse-mode SQL execution | `.env` |
| `AI_APP_API_KEY` | AI-app harness | `.env` |
| `CMS_COLD_STORAGE_ROOT` | external data root | `.env` |

## Activation gaps (as of this writing)

1. Create `config/lock.toml` from the template and set `enabled = true` to connect
   Databricks; otherwise the platform runs DuckDB locally.
2. All harness configs are `.example` only -> the AI/KPI eval harnesses are dormant
   until you copy them to active names.
3. `domain_packs/` is **deprecated** (never wired). Do NOT add packs — domain
   inference is derived from the workspace lexicon (`text_parser.py`). Not a gap to
   fill; left documented so no one re-adds dead config.

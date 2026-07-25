# Agent Operating Guide

This repo is a governed optimization control plane for scoreable data-engineering work.
Agents may run through CLI, API, or terminal tools, but they should follow the same operating
rules.

## Read First

0. Identify the active workflow before doing project work:
   - Ask what the user wants to do in this session.
   - Ask the user to point to the current active workflow/project if it is not already clear.
   - Scan likely files and folders.
   - Present the likely file set.
   - Ask for confirmation that these are the files/workspace to use.
   - Continue only after confirmation, unless the user explicitly asks for a best-effort scan.
1. `README.md` for repo purpose, core layout, workspace output layout, and verification commands.
2. `CONTEXT.md` for domain language and architecture.
3. `config/tasks.json` for active task, workspace path, commands, contracts, and policy.
4. The Stage index in `Tool And Evidence Discovery` (below) for available project tools; read a
   command's `TOOLS.md` section on demand only. Do not read `TOOLS.md` / `.agents/tools.json`
   whole.
5. `program.md` only when the active benchmark/task refers to it (optional, task-supplied file; not checked in to this repo).
6. Relevant files in `core/`, `tools/`, `interns/`, `tests/`, or `workspaces/<project>/`.

## Local-Native vs. Cloud-Native

This platform's original, still-default shape is local-native: source data as local files under
`workspaces/<project>/datasets/`, profiled and executed against local DuckDB. `databricks_source`
mode is the concrete embodiment of moving a workspace to cloud-native: source data lives and is
processed in a real Databricks Unity Catalog account instead, with the same onboarding →
KPI-resolution → dbt-generation → orchestration pipeline running against it. Neither mode is more
"correct" than the other — local-native is the right default for a smoke test, a POC, or any
workspace without a real Databricks account behind it; `databricks_source: exclusive` is the right
mode once a workspace has real, governed data landed in Unity Catalog. A workspace is one or the
other by explicit declaration (see "Where a workspace's data actually lives", below), never by
guessing from what files happen to exist.

One invariant holds regardless of mode: **all generated onboarding output always lands in the same
place**, `workspaces/<project>/interns/` (contracts, profiles, evidence, reports — see "Workspace
Rule" below). Going cloud-native changes *where source data is read from* and *where a workspace's
dbt project/gold marts are built* (a real Unity Catalog schema, not a local Delta table) — it never
changes where this platform's own artifacts are written. A cloud-native workspace's generated dbt
project itself (`workspaces/<project>/dbt/`) is also local, git-tracked repo content — only the data
it reads and the tables it builds are remote.

| | local-native (default) | cloud-native (`databricks_source`) |
|---|---|---|
| Source data | `workspaces/<project>/datasets/` | Unity Catalog (real catalog/schema) |
| Profiling | Local file scan | `SHOW TABLES` + SQL-warehouse sampling |
| KPI execution | Local DuckDB | dbt project → Databricks (Cosmos/Airflow) |
| Generated artifacts | `workspaces/<project>/interns/` | same path, unchanged |
| Credentials needed | None | Databricks CLI auth (see readiness check, below) |

## Step 0: Active Workflow Setup

Before onboarding, optimizing, refactoring, or writing outputs, establish the active workflow.

Use one of these startup paths:

1. If the user gives a clear task and workspace, treat that as the likely active workflow, scan the
   likely files, summarize the file set, and ask for confirmation before writing outputs.
2. If the user gives a task but no workspace, ask:

```text
What do you want to do with this project right now, and which workspace/files should I treat as active?
```

3. If the user says only `set <workspace>` or names a workspace/project, treat that as a workspace
   selection command. During the selection turn, the agent MUST NOT call Edit, Write, or any
   file-creating/deleting tool on any file — including `.gitignore`, `.geminiignore`, settings
   files, generated artifacts, or any repo file — until the user has confirmed the workspace AND
   explicitly authorized continuing past selection. Allowed actions during selection: read-only
   listing via `list-workspace-files`, bounded PowerShell fallback, `git status --short`, and
   reading `config/tasks.json`. Recursively list file paths from the workspace root only, summarize
   what will be active, ask for confirmation, and then continue from the highest-priority blocker.
   For KPI/query workspaces, if feature mappings or business definitions are blocked, start the
   automatic blocker grilling session after confirmation.

Then scan likely sources with bounded listing only:

```powershell
git status --short
uv run list-workspace-files --workspace workspaces/<project>
```

The preferred workspace listing tool is `list-workspace-files`. It lists all file paths up to the
cap and also performs basic classification. The full file list is the confirmation boundary: ask the
user whether this is the file set they want to use, not only whether the similar-looking KPI/model
files are correct. It must not read file contents, parse Excel, profile datasets, onboard, delete, or
write files. If the tool is unavailable, use this bounded PowerShell fallback:

```powershell
Get-ChildItem -LiteralPath workspaces/<project> -Force -File -Recurse |
  Select-Object -First 200 -ExpandProperty FullName
```

Workspace discovery must bypass gitignore rules. Some valid customer inputs are intentionally
ignored by git, including raw data, PDFs, and whole local workspace folders. If a folder reader
reports ignored items or an apparently empty workspace, immediately rescan with `rg --files -uu
workspaces/<project>` or the bounded PowerShell fallback before concluding that KPI registries,
data models, source artifacts, datasets, or docs are missing.

Workspace selection scans must be bounded. For `set <workspace>`:

- Recursively list file paths from the workspace root only.
- List all found paths up to the cap, then classify possible KPI registry files, possible data model files,
  dataset roots, docs, source artifacts, evaluator/runner files, task config references, and
  `interns/` state. The confirmation question must refer to the full listed file set.
- Do not read raw dataset contents.
- Do not profile datasets.
- Do not parse Excel deeply beyond identifying likely KPI registry files.
- Do not run onboarding before confirmation.
- Prefer `uv run list-workspace-files --workspace workspaces/<project>`; if unavailable, use the
  bounded PowerShell fallback with `Select-Object -First 200`.
- Stop after 200 file paths or 30 seconds, whichever comes first.
- After the listing command returns, do not run more scans or perform extended reasoning. Respond
  within 10 seconds using only the returned path list and `config/tasks.json`.
- The response must contain only the likely active file-set summary and the confirmation question.
- HARD STOP: do not call Edit, Write, or any mutation tool on any file during the selection turn.
  This includes `.gitignore`, `.geminiignore`, `settings.json`, generated artifacts, and all other
  repo or workspace files. Any mutation before explicit post-confirmation authorization is forbidden.

Summarize the likely active set:

```text
I found this likely workflow:
- Workspace: workspaces/<project>
- Possible KPI files: ...
- Possible data model files: ...
- Source artifact: ...
- Evaluator/runner: ...
- Task config: ...

Should I use these files for this workflow?
```

After the user confirms, continue with the appropriate skill flow. Do not ask a generic
"what would you like to do next?" question when the next workflow step is discoverable from the
workspace state.

### Where a workspace's data actually lives

The file-set scan above answers "what's in the repo for this workspace" — it does not answer
"where does this workspace's *source data* actually live." That is a separate, explicit
declaration, not something inferred from which local files happen to exist:

- Read `workspace_settings.json`'s `databricks_source` block (catalog, schema, `mode`, optional
  `enterprise_id`). No block at all → `local_files` (every workspace's default; nothing changes for
  it). A block with `mode: additive` → local files AND the declared Unity Catalog catalog/schema are
  both profiled. A block with `mode: exclusive` → local dataset discovery is skipped entirely; the
  declared catalog/schema is the only source.
- If the mode is not yet explicitly declared for a workspace that looks cloud-native (the user
  mentions Databricks, a catalog name, or an existing Unity Catalog data estate), do not guess or
  silently default. Run `uv run prepare-data-source-panel --workspace workspaces/<project>` and ask
  from that panel — the same "generate a panel, ask from it, never freehand" rule as every other
  blocker in this guide. Record the answer with
  `uv run apply-data-source-answer --workspace workspaces/<project> --answer <local_files|databricks_additive|databricks_exclusive> --catalog <c> --schema <s> --confirmed-by "<name>"`.
  This is asked once per workspace, not once per session — it is durable workspace state.
- `enterprise_id` (explicit, or the catalog name as a fallback) is what selects *which* Databricks
  account/credentials a cloud-native workspace actually connects to
  (`core.config.resolve_databricks_config`): if `config/enterprises/<enterprise_id>/lock.toml`
  exists, that workspace's onboarding, KPI execution, dbt generation, and dashboard reads all use
  it; otherwise they fall back to the single global `config/lock.toml`. This is the multi-tenant
  seam — a second real enterprise is a matter of dropping in that one file, not touching any
  workspace's own code path.

### Platform readiness check

Before attempting any step that touches Databricks (profiling a `databricks_source` workspace,
resolving KPI features against it, `generate-dbt-project`, orchestrating via Airflow/Cosmos), and
proactively near the start of a session where the user signals cloud-native intent, run:

```powershell
uv run check-platform-readiness --workspace workspaces/<project>
```

(Pass `--enterprise-id <id>` once the workspace's enterprise is known, to check the credentials that
workspace will actually use rather than the global default.) This is read-only — it never mutates
anything, only reports:

- **Databricks**: `not_configured` (fine — the workspace may be local-native), `blocked`
  (configured but unreachable/unauthenticated — a real stop, route to the `databricks-access-gates`
  skill; the fix is almost always `databricks auth login` / `databricks configure` on the user's
  machine, never a token pasted into chat), or `ready`.
- **dbt** / **Airflow**: `not_installed`, `partial` (installed but missing the Databricks
  adapter / astronomer-cosmos), or `ready`. Neither is ever a blocker on its own — the local-DuckDB
  KPI flow works with neither installed; they are only required once a workspace actually declares
  `databricks_source` and needs `generate-dbt-project` / scheduled orchestration.

Only a `blocked` Databricks status should stop the agent and prompt the user; report `not_installed`
dbt/Airflow status plainly (what it means, the install command) without treating it as broken.

For KPI/query workspaces, always show the user two post-confirmation paths before onboarding unless
they already gave a narrower command:

```powershell
uv run prepare-kpi-generation --workspace workspaces/<project>
```

Read `workspaces/<project>/interns/reports/kpi_generation/current.md` or `current.json`, then ask
from that panel. The panel must always include:

1. KPI generation / BA-product interview for creating, revising, challenging, scoring, and proving
   KPIs.
2. Usual workflow / onboard existing KPI + data model.

The recommendation should be based on the KPI quality/readiness score, but the user must always be
able to choose KPI generation even when an existing KPI registry is present. Optional stakeholder
context such as meeting transcripts, product notes, PRDs, data dictionaries, screenshots, and policy
docs can be passed with `--context-file`. Draft KPI registries are generated under
`interns/generated/requirements/`; user-facing KPI registries are written only by
`finalize-kpi-generation --approve-final-preview` after the final preview is explicitly approved.

For fresh KPI/query workspaces, the standard onboarding command is:

```powershell
uv run onboard-workspace --workspace workspaces/<project>
```

If a confirmed workspace has KPI registry/data model/datasets but no `interns/` artifacts or task
config entry, treat it as a fresh KPI/query workspace. The next step is to propose local-safe
onboarding with the exact command above, or run it if the user's confirmation explicitly allowed
continuing. The response should say that onboarding will generate profiles, contracts, normalized
KPI registry, feature mapping, open questions, and evidence artifacts under
`workspaces/<project>/interns/`.

When the user answers `yes` to the workspace confirmation and the workspace is fresh, do not spend
time rechecking the same files. Within 10 seconds, either run the standard onboarding command if the
prior prompt allowed continuing, or ask one direct approval sentence that names the exact command.

After a shell command completes successfully, do not spend minutes narrating or re-reading generated
artifacts. Summarize the command result in under 30 seconds from the returned output, list the key
artifact paths, and provide the next deterministic command. For `onboard-workspace`, the next
command is usually:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

(`resolve-kpi-features` is deprecated; its default invocation now redirects to
`prepare-kpi-blocker-panel`.) The wrapper writes `question_panel_path` and
`question_panel_markdown_path` in its JSON output. If `blocked_kpi_count` is nonzero, the next step
is to read `question_panel_markdown_path`; do not inspect generated contracts and invent a separate
interview.

After any onboarding, feature-resolution, derived-feature markdown, or blocker-question-panel write,
run the read-only validator before relying on generated artifacts:

```powershell
uv run validate-workspace-artifacts --workspace workspaces/<project>
```

Treat validator errors as blockers. Do not manually edit generated contracts such as
`kpi_registry.json` to make a blocker disappear; fix the upstream parser/resolver or regenerate.

For KPI blocker preparation, prefer the deterministic wrapper:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

It owns the standard sequence: missing onboarding, feature resolution with candidates,
derived-feature Markdown, blocker question panel, and validation. Use lower-level commands only when
debugging a specific stage.

The main loop auto-runs local-safe bootstrap when required `interns/` artifacts
are missing or stale. It reuses existing generated artifacts when the workspace
input fingerprint is current.

Databricks or other remote execution must not run only because credentials are
present. Remote execution requires explicit approval. In this repo that approval
is represented by `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`; otherwise the backend
falls back to local DuckDB after any health check.

## Repo Map

- `core/orchestration/`: experiment loop and runner.
- `core/execution/`: local and Databricks execution backends.
- `core/governance/`: policies, contracts, semantic rules, approval gates.
- `core/contracts/versioning.py`: per-artifact `ContractVersion` + `migrate(...)` registry.
- `core/optimization/`: planning, memory, diff classification, decision strategy.
- `core/profiling/`: data model profiling and downcast diagnostics.
- `core/agents/`: intern routing and LLM engine abstractions.
- `core/observability/`: metric parsing, telemetry, structured event emitter
  (`events.emit_event` / `time_command`) writing to `interns/state/events.jsonl`.
- `core/storage/`: SQLite/Git workspace state, workspace layout, and
  `workspace_lock.workspace_lock` (cross-platform per-workspace mutex).
- `core/onboarding/lexicon/`: workspace-derived vocabulary (metric phrases, cut phrases,
  column aliases) replacing the old curated keyword ladder.
- `core/onboarding/workspace/cli_runner.py`: shared envelope for every `apply-*` /
  `finalize-*` / `prepare-*` CLI (lock + event + idempotency + trajectory recording).
- `core/onboarding/workspace/idempotency.py`: deterministic op-ids + `applied_ops.jsonl`
  ledger so repeated apply calls don't duplicate decisions.
- `tools/`: CLI utilities.
- `interns/`: built-in intern agents.
- `tests/`: unit and benchmark harnesses.
- `workspaces/<project>/`: user/project input.

### Governed CLI envelope

Every `apply-*` / `finalize-*` / `prepare-*` command in `pyproject.toml` funnels through
`core.onboarding.workspace.cli_runner.run_workspace_command(...)`. The envelope provides:

* `workspace_lock` — fails fast with `WorkspaceLockTimeout` (exit code 2) if another command
  is mutating the same workspace.
* `time_command` — appends a JSONL event with `command`, `status`, `duration_ms`, and any
  per-command details to `workspaces/<ws>/interns/state/events.jsonl`.
* `compute_op_id` / `record_op` — for apply/finalize commands (passed `record_idempotent=True`),
  derives a deterministic op id from the arguments and writes an `AppliedOp` row to
  `workspaces/<ws>/interns/state/applied_ops.jsonl`. A second call with the same arguments
  returns the prior payload as `{"status": "idempotent_replay", ...}` instead of re-running.
  Pass `--allow-replay` to force re-execution.
* `record_trajectory_event_safe` — tool_start / tool_result entries for the workflow guard
  and `prepare-workspace-bug-report`.

New CLI authors should add the standard envelope by calling `run_workspace_command(...)` rather
than re-implementing the boilerplate.

### CLI-agent two-step proposal flow

When the blocker question panel emits a `cli_agent_proposal_needed` panel (no scored options
remain), the orchestrating CLI agent reads the bounded `cli_agent_evidence_pack`, proposes a
JSON mapping, then runs `apply-kpi-panel-answer ... --via-cli-agent`. That records the mapping
as `cli_agent_proposed` (NOT `user_confirmed`); the KPI stays blocked until the user runs
`confirm-cli-agent-proposal --decision confirm` (flips to `user_confirmed`) or
`--decision reject` (flips to `cli_agent_rejected` and reverts the affected rows). The agent
must never run the confirm step on the user's behalf without explicit direction.

## Workspace Rule

Treat `workspaces/<project>/` as the project/customer input area. Do not scatter generated output
directly in the project root. All optimizer output for a project belongs under:

```text
workspaces/<project>/interns/
  state/        # workspace.db, run.log
  runs/         # per-run artifacts
  generated/    # requirements, contracts, profiles, evidence, memory
  reports/      # human-readable reports
```

`workspaces/**/interns/` is ignored by git.

For a fresh workspace restart, use the cleanup tool before regenerating artifacts:

```powershell
uv run cleanup-workspace-references --workspace workspaces/<project> --all-references
```

This is a dry run by default. Apply only after reviewing the plan:

```powershell
uv run cleanup-workspace-references --workspace workspaces/<project> --all-references --apply --confirm-delete workspaces/<project>
```

The cleanup boundary is generated workspace output and stale repo-level references. It must not
delete workspace `docs/` or `datasets/`.

Any delete operation requires a hard permission block: stop, show the exact delete plan, and require
an explicit user confirmation before deleting files or folders. Do not infer delete approval from a
general workflow request.

Requirement discovery, grill-me interviews, stakeholder conversations, task choices,
accepted decisions, rejected assumptions, and accepted recommendations are project artifacts. Save them under the
active workspace's `interns/` folder, typically:

```text
workspaces/<project>/interns/generated/requirements/
workspaces/<project>/interns/generated/memory/
workspaces/<project>/interns/reports/
```

Structured JSON artifacts are also written through the metadata store. Local mode
stores them as Delta tables under `workspaces/<project>/interns/state/delta_metadata/`,
with JSON fallback under `workspaces/<project>/interns/state/metadata_store/`.
Enterprise Databricks deployments should map the same collections to Delta tables
in Unity Catalog. MongoDB is optional when `AUTORESEARCH_METADATA_BACKEND=mongo`
and `AUTORESEARCH_MONGO_URI` are configured. Keep executable artifacts and
human-readable reports as files under `interns/`.

## DataFrame Rule

Use Polars for dataframe work by default. Do not introduce pandas for profiling, schema
inspection, sampling, KPI preparation, CSV/parquet processing, or generated workspace
utilities. If a third-party API requires pandas, keep the conversion at the boundary,
document the reason in the code or report, and convert back to Polars as soon as possible.

## Data Model Driven Generation Rule

When generating SQL, Polars, PySpark, ETL/ELT, or medallion-layer loading logic, use the KPI
requirements and the data model together. The KPI registry defines the business question; the data
model and profiles decide which datasets are eligible, how they join, what grain is valid, and which
columns are safe to use.

Before writing executable logic:

- map KPI terms to facts, dimensions, source datasets, columns, joins, filters, and grain;
- verify selected datasets and joins against `domain_model.json`, profile evidence, data model docs,
  dictionaries, catalog metadata, or accepted workspace definitions;
- reject or block code generation when a source table, join key, temporal anchor, aggregation grain,
  or medallion layer is unproven;
- record source-to-target assumptions for bronze/raw, silver/conformed, and gold/KPI outputs when
  ETL or medallion loading is requested;
- ask the user which query/runtime language to use when it is not already specified. Acceptable
  answers include `sql`, `polars`, `pyspark`, `sql+polars`, or another supported hybrid requested
  by the user. Generate only the requested language/output, not parallel SQL, Polars, and PySpark
  variants by default;
- honor the requested target engine (`sql`, `polars`, `pyspark`, or hybrid) only when the repo has a
  supported generator or a reviewed solution brief for that target.

Do not generate SQL/Polars/PySpark merely from column-name similarity. Treat data model mismatch,
missing join proof, and grain mismatch as blockers.

Use the source-to-target planner before executable generation:

```powershell
uv run plan-source-to-target --workspace workspaces/<project> --target-engine sql
```

For any executable generation path that uses more than one dataset, build and enforce relationship
contracts first:

```powershell
uv run build-relationship-contracts --workspace workspaces/<project>
```

Trusted SQL, Polars, PySpark, ETL/ELT, medallion loading, and production KPI proof may use only
relationships whose contract allows executable usage, currently `proven_data_model` or
`user_confirmed`. Profile-only relationship candidates are advisory and must trigger blocker
grilling or user confirmation before executable logic is generated.

Review `interns/generated/contracts/source_to_target_plan.json` and
`interns/reports/source_to_target_plan.md`. If any KPI in the plan is blocked, resolve that blocker
before generating SQL, Polars, PySpark, or medallion pipeline code.

## Workspace Data Policy (user-authored)

A workspace owner may place `data_policy.json` at the workspace root (or under `docs/`) to declare
their own data-protection rules on top of the built-in HIPAA/PCI detection:
`sensitive_columns` (exact names), `sensitive_column_patterns` (regexes), `categories`
(named custom categories), `not_sensitive_columns` (reviewed false-positive allowlist), and
`tier_override` (`"phi"` forces the PHI tier). Honored by the PHI gate (`assess_workspace_phi`),
display redaction (blocker-panel samples and previews), and the semantic contract's
`columns.<name>.is_sensitive` map that the SQL generator masks from. The policy can only WIDEN
display redaction; the allowlist suppresses tier findings but never un-redacts rendered surfaces.
This file is user input like datasets: agents must never write or edit it; a malformed policy
surfaces as `errors` in its summary and must be reported to the owner, not auto-fixed.

## Human-Gate Provenance Rule

When a human answers an approval or review gate — a relationship-join approval or the kpi-analyst
review — the agent MUST pass `--confirmed-by <name>` to the relevant CLI:

```powershell
uv run apply-relationship-answer --workspace workspaces/<project> --confirmed-by "<reviewer>"
uv run workspace-flow review      --workspace workspaces/<project> --confirmed-by "<reviewer>"
```

An empty `--confirmed-by` records the decision as agent-asserted (`source: agent`). A human "yes"
in an Ask-User prompt must be recorded as `source: human`. Do not clear a human gate while
recording it as agent-asserted. If `--confirmed-by` is not available from context, ask for the
reviewer name before applying the decision.

(Residual from BUG-014; the harness now stores `source`/`confirmed_by` but relies on the agent
passing the flag correctly.)

## KPI Result Packet Forwarding Rule

When presenting KPI results, forward the canonical artifact verbatim:

```powershell
# Read and display — do not retype or paraphrase
workspaces/<project>/interns/reports/kpi_results/current.md
```

The agent MUST NOT re-author, re-type, or reconstruct the generated SQL or result tables from
memory or session context. Emitting the packet from memory caused a fabricated data-source render
(BUG-015: `read_csv_auto` shown when the on-disk SQL used `delta_scan`). Show the emitted packet
once; do not paraphrase the SQL or table rows.

Present results automatically on completion — do not wait to be asked. When the pipeline reaches
the `complete` or `results` stage, that stage's panel markdown already renders each KPI's
definition + generated SQL + result table inline (`render_kpi_block`), and `kpi_results/current.md`
holds the same packet. Forward it in the same turn the run finishes. The operator should never have
to type "show results" / "show me the results" to see the tables — if they do, the completion turn
under-presented and should be treated as a bug, not a normal step.

(Residual from BUG-015; the completion path now auto-emits this packet, but any explicit "show
results" turn must still forward the file, not reconstruct from memory.)

### Compact vs full results

Two packet variants exist; pick by what the user asked for and forward the file verbatim either way:

- Default ("results", "show results", or pipeline completion): forward the COMPACT packet —
  `interns/reports/kpi_results/current.md` (same content as `interns/runs/<date>/results.md`).
  SQL is linked per KPI, not inlined.
- "full results" / "entire results": forward the FULL packet —
  `interns/reports/kpi_results/current_full.md` (same content as
  `interns/runs/<date>/results_full.md`). SQL is inlined per KPI.

Never answer a "full results" request with the compact packet, a hand-built summary table, or a
re-authored excerpt. Even the full packet caps result previews; the complete row set lives in
`interns/generated/evidence/kpi_results/current.json` (machine-only — query it, do not dump it).

### Results read discipline (token/quota guardrail)

Reading the packet must be ONE cheap read. Re-reading the results in many forms in a single turn
has burned ~7% of a model quota in one go — do not repeat that. These rules apply in every CLI
(Claude, Gemini, Antigravity, or other agent frontends):

- Read the packet with the agent's NATIVE file-read tool, not a shell command
  (`Get-Content` / `cat` / `type`). Shell output is summarized or capped by the CLI harness, which
  truncates long reads and is exactly what starts the re-read loop. Native reads return the whole
  file once.
- Do NOT re-read the same file with `-TotalCount`, `-Head`, `-Tail`, `-Raw`, `-Encoding`,
  `Select-String`, or `workspace-flow results --preview-rows N` back-to-back to "see more" — they
  all return the same packet. One native read is the whole thing.
- For many KPIs, forward the per-KPI files `interns/runs/<date>/kpi_<id>.md` (one read each, each
  self-contained) instead of the combined file — this never exceeds a read cap.
- NEVER use `-Wait` or any follow/stream flag on these files — it hangs until cancelled.
- If the CLI display shows "... first N lines hidden ...", the read SUCCEEDED — that is a UI
  truncation, not a failure. Forward what was read; do not retry with another command.

## Dataset Isolation Rule

When the operator scopes a workspace to a subset of its datasets (a source system, a site, a
partner, a date range of files — any subset), persist that scope as a `dataset_allowlist` in
`workspaces/<project>/interns/state/workspace_settings.json` BEFORE running onboarding, profiling,
or generation:

```json
{
  "dataset_allowlist": ["datasets/<subset-path>"]
}
```

- All downstream stages (profiling, contracts, feature mapping, medallion ingestion, generated
  SQL/engine code) must read only from allowlisted paths.
- The scope is workspace state, not prose: do not rely on the conversation to remember it. If the
  allowlist file exists, honor it in every session and every CLI without being re-told.
- When verifying isolation, compare VALUES, not keys or row counts — sibling datasets can share
  identical ID sets and row counts while differing in measures. A provenance check that only joins
  on keys can silently pass for the wrong source.

## Grain-Bucketing Blocker Rule

When the execution harness blocks a share/percentage KPI on a grain-bucketing decision (a raw
continuous cut fragmenting the denominator — e.g. an exact-valued numeric or date-derived
dimension), the blocker question panel shows NO options — this is a pipeline decision, not a
feature blocker. Do NOT loop on `apply-kpi-panel-answer` or `workspace-flow answer` (they error
with "current panel has no options" / "not waiting for a supported answer"). Apply it
deterministically, then re-run generation:

```powershell
uv run apply-pipeline-decision --kpi-id <kpi_id> --grain-bucketing band_continuous_cuts
uv run workspace-flow start --workspace workspaces/<project> --intent full_kpi_sql --domain <domain>
```

Use `band_continuous_cuts:<width>` for a non-default band width (default 10), or
`exact_value_grain` only if exact-value rows are genuinely wanted. (The panel route for this facet
is a known open bug — see `develop_spec/follow_ups.md`.)

## Token Discipline

Per-run token cost is currently ~44 pp of model quota. These habits reduce it materially:

- Use `run-kpi-pipeline` for the deterministic KPI chain instead of issuing each step
  (onboard / blocker / contracts / start / results) as a separate LLM-driven call. The wrapper
  stops only at genuine human gates and emits the result packet once:

  ```powershell
  uv run run-kpi-pipeline --workspace workspaces/<project> --domain <domain>
  ```

- Never read large JSON audit files whole. The following are machine audit trails (thousands of
  lines); read the paired `.md` summary instead and treat the JSON as machine-only:
  - `interns/state/**/session.json`
  - `**/trajectory*.json`
  - workflow `current.json`
  - `kpi_feature_mapping.json`

- Pass `--quiet` on workspace-flow subcommands (accepted per-subcommand since BUG-019) so panels
  are not dumped in full each call.

- Use a cheaper model tier for mechanical/deterministic pipeline steps (profiling, contract
  building, validation, execution harness). Reserve the top tier for genuine semantic decisions:
  KPI clarification, derived-feature judgment, and the kpi-analyst review. See the `control-pane`
  skill for model-tier routing.

## Quiet Execution Rule

Keep main-chat workflow output concise. Show only the stage, key result, blocker or risk,
recommendation, and next deterministic command. Do not paste long shell output, full JSON contracts,
raw logs, or validation traces unless the user asks to inspect them. Save details under
`workspaces/<project>/interns/generated/` and `workspaces/<project>/interns/reports/`, then point to
the artifact path.

Pass `--quiet` to high-volume CLIs so they emit a compact summary instead of the full JSON. These
commands write the full result to disk regardless; quiet mode prints a pass/fail line, counts,
and the artifact path to read when detail is actually needed. Use it by default for status,
validation, listing, and execution checks:

```powershell
uv run validate-project-harness --workspace workspaces/<project> --domain <domain> --quiet
uv run run-kpi-execution-harness --workspace workspaces/<project> --quiet
uv run list-workspace-files --workspace workspaces/<project> --quiet
uv run workspace-flow status --diff --workspace workspaces/<project> --quiet
```

Quiet-mode discipline:

- Default to `--quiet` for any command run to check state or progress (harness, execution, diff,
  listing). Reach for the full JSON (`--json` or no flag) only when a specific field is needed that
  the quiet summary does not surface, and say which field.
- Run each deterministic command once. Do not re-run `validate-project-harness`, `workspace-flow
  start`, or `list-workspace-files` repeatedly in one turn; if a command resumed an existing session
  or already produced an artifact, read the artifact path it printed instead of re-running.
- Do not write throwaway reader scripts (`read_*.py`) to view an artifact. Read the file directly,
  or re-run the producing command with `--quiet` and read the `detail:` path it prints.
- Repeated-identical warnings and blockers are already collapsed to one line with an `(xN)` count
  suffix; do not expand them back out.

## Tool And Evidence Discovery

Before inventing a helper script, manually scanning large files, or choosing an ad hoc workflow,
inspect the project tool registry:

```text
TOOLS.md
.agents/tools.json
pyproject.toml [project.scripts]
tools/
```

Prefer existing project tools and generated artifacts over custom one-off scripts. Use the
machine-readable registry in `.agents/tools.json` for routing, safety level, inputs, and expected
outputs.

### Stage index (registry summary)

This index satisfies the registry-read gate. `TOOLS.md` (~16k tokens) and `.agents/tools.json`
(~23k tokens) must NOT be read whole as session preamble — find the stage below, then read only
the matching `### <command>` section of `TOOLS.md` for the one command you are about to run.
`tools.json` is machine-only (programmatic routing); never page through it.

| Stage | Commands (each is a `###` section in TOOLS.md) |
| --- | --- |
| Workspace selection | `list-workspace-files`, `prepare-workspace-selection`, `session-snapshot`, `prepare-data-source-panel`, `apply-data-source-answer` |
| Onboarding | `onboard-workspace`, `kickstart-workspace`, `understand-data` |
| Cloud-native (dbt/Airflow) | `check-platform-readiness`, `generate-dbt-project`, `prepare-data-quality-panel`, `apply-data-quality-answer`, `run-dbt-backfill` |
| KPI definition + blockers | `prepare-kpi-blocker-panel`, `apply-kpi-panel-answer`, `apply-kpi-definition`, `confirm-cli-agent-proposal`, `prepare-kpi-generation`, `apply-kpi-generation-answer`, `finalize-kpi-generation` (deprecated redirects: `resolve-kpi-features`, `blocker-question-panel`, `derived-feature-markdown`) |
| Data model | `prepare-data-model-generation`, `apply-data-model-answer`, `finalize-data-model-generation`, `prepare-data-model-blocker-panel`, `apply-data-model-blocker-answer`, `parse-data-model-images`, `export-data-model-diagram` |
| Relationships + source-to-target | `build-relationship-contracts`, `apply-relationship-answer`, `plan-source-to-target` |
| Source catalog + external intake | `prepare-source-catalog`, `build-catalog-contract`, `build-source-family-contracts`, `discover-external-sources`, `prepare-external-source-intake`, `ingest-source-catalog` |
| Engineering route + pipeline | `prepare-data-engineering-route`, `prepare-pipeline-plan`, `prepare-pipeline-format-panel`, `prepare-pipeline-deployment-plan`, `apply-pipeline-decision`, `generate-pipeline-sql` |
| Generation + execution | `run-kpi-pipeline`, `workspace-flow`, `generate-kpi-sql`, `generate-kpi-engines`, `kpi-proof-packet` |
| Validation + QA | `validate-workspace-artifacts`, `validate-project-harness`, `harness reliability`, `harness workflow-guardrails`, `harness data-quality`, `prepare-duplicate-review-panel`, `apply-duplicate-review-answer`, `harness layered-pipeline`, `harness pipeline-execution`, `validate-git-hygiene`, `validate-memory-health` |
| Evidence + reporting | `record-workspace-trajectory`, `build-workspace-evidence-graph`, `export-kpi-registry-excel`, `export-workspace-presentation`, `prepare-wiki-memory`, `prepare-workspace-bug-report`, `record-engine-evolution` |
| Dashboard | `workspace-dashboard` (start `--live` / `--stop` / `--refresh` / `--refresh-seconds` / `--screen`), `dashboard-verify` (single-page DOM/color gate) — see "Dashboard server lifecycle" below |
| Context + budget | `context-router`, `resource-preflight`, `cleanup-workspace-references` |
| Dev + harness | `prepare-agent-benchmark`, `harness ai-app`, `harness ai-cli`, `prepare-workspace-workflow`, `profiler.py`, `optimizer_finder.py`, `methodology_parser.py`, `generate-skill-adapters`, Databricks tools |

Dashboard server lifecycle:

The per-workspace dashboard is the **live MinusAnalyst app** (served process), NOT a static
file. The standalone static export was removed; any `dashboard/exports/*.html` still on disk are
legacy leftover artifacts — stale, never updated by current code, and not the dashboard. Always
serve the live app. The live app builds from the
workspace's `interns/` bronze/gold (gitignored), so a fresh clone must run the pipeline before it
can serve. Commands (all `uv run workspace-dashboard --workspace workspaces/<project> ...`):

- **start** — `--live` (default). Regenerates the model (DQ-gated) and serves at
  `http://127.0.0.1:8060`. Writes a PID file so it can be stopped cleanly.
- **stop / kill** — `--stop`. Terminates the running server via its PID file, with a fallback
  scan of `--port` for orphans. Idempotent (no-op if nothing is running). Cross-platform.
- **on-demand refresh (new data landed today)** — `--refresh`: regenerate-and-exit. DQ-gated, so
  a bad/new load that fails certification leaves the last-good snapshot in place. Intended for a
  scheduler to call after the KPI pipeline re-ingests new data (new data flow:
  `new raw data -> re-run pipeline (bronze/gold) -> --refresh -> view updates`).
- **live auto-pickup** — `--live --refresh-seconds <N>`: the running server re-reads data every N
  seconds, so a `--refresh` write appears without a restart.

Operating rule: a **data** refresh needs no restart (config-watcher + atomic spec writes handle
it); a **code/schema change** (e.g. a new spec field) needs a restart, because a long-running
process keeps its old Python classes in memory (cause of stale `data_through: Extra inputs are not
permitted`-style config errors — fix is `--stop` then `--live`).

Hard registry-read gate:

- Before choosing any workflow route or next command, the active agent must have read the Stage
  index above (or the specific command's `###` section in `TOOLS.md`, or its generated adapter
  under `.agents/<tool>/SKILLS.md`) in the current session.
- If the agent has not read any of those, it must stop, read the Stage index, and restart route
  selection instead of guessing from memory.
- Reading `TOOLS.md` or `.agents/tools.json` end-to-end as session preamble is a token-discipline
  violation, not diligence: drill into single sections on demand.
- For an external profiled workspace with no KPI registry entries, do not run KPI feature
  resolution first. Run `uv run build-source-family-contracts --workspace workspaces/<project>` and
  review schema drift before `prepare-data-engineering-route` or medallion planning.

For dataset questions, use profile-first evidence:

1. Read `workspaces/<project>/interns/generated/profiles/profile_index.json`.
2. Read the relevant `*.profile.json` files.
3. Use bounded samples only when profile evidence cannot answer the question.
4. Read full raw datasets only with a concrete reason.

Do not paste raw datasets into prompts. If bounded sampling is needed, state why the existing
profile evidence was insufficient and save the result under the active workspace's `interns/`
artifacts when it affects decisions.

## Secret And Environment Display Guardrail

Never print, paste, summarize verbatim, or screenshot secret-bearing values. This is a hard stop,
like destructive deletion approval.

Do not display contents of:

- `.env`, `.env.*`, `.databrickscfg`, private keys, tokens, secret stores, credential files, or
  shell environment dumps.
- Command output that includes access tokens, API keys, passwords, connection strings, signed URLs,
  bearer headers, cookies, private endpoints, or cloud credentials.
- AST/tree/config dumps if they include secret values or full environment values.

Allowed safe output:

- Whether a variable/file exists.
- Redacted key names such as `OPENAI_API_KEY=<redacted>` or `DATABRICKS_HOST=<set>`.
- A count of configured variables, missing variables, or validation status.

If a command may expose secrets, use a filtered command that prints only names/status, or stop and
ask for permission to run a safer diagnostic. If secret output is accidentally produced, do not
repeat it in the response; state that sensitive output was suppressed.

## Never Push

- `.env`, secrets, tokens, `.databrickscfg`, private keys.
- `state/`, logs, SQLite/DuckDB databases.
- Raw datasets, CSV/PDF/parquet data dumps, profile outputs.
- `workspaces/<project>/interns/`.
- Nested workspace repositories unless the user explicitly asks to add a submodule.
- `config/lock.toml` unless the user explicitly asks; it is human-owned.

## Skill Routing

Use the repo skills in `skills/` as operating policies:

- `grill-requirements`: interview users/teams to discover goals, constraints, and guardrails;
  includes the merged "Clarify Ambiguity" mode (ask one targeted question only when ambiguity
  materially matters).
- `stakeholder-memory`: store user/team preferences and decision style.
- `domain-model`: align terms with KPI registry, data model, and `CONTEXT.md`.
- `data-engineering-pipeline-design`: design source-to-target, ETL/ELT, medallion, SQL, Polars,
  and PySpark implementation plans from KPI requirements and the data model.
- `feature-derivation-library`: propose reusable derived-feature patterns without treating candidates as proof.
- `to-solution-brief`: turn interview decisions into a concrete implementation brief.
- `task-onboarding`: convert project inputs into task config, contracts, profiles, and baseline plan.
- `workspace-kpi-query-optimizer`: build, validate, baseline, and optimize KPI/query logic for any workspace.
- `workspace-governance`: keep outputs, data, and git staging safe.
- `databricks-access-gates`: stop on missing Databricks scopes, grants, workspace permissions, compute/storage policies, or remote mutation approvals and ask the user/admin for the exact unblocker before retrying.
- `evolution`: record accepted decisions, rejected assumptions, lessons, and future optimization hints.

## Blocker Grilling Rule

If an agent finds something wrong, missing, contradictory, or unsafe enough to block correct
progress, it should switch into an automatic grilling session instead of guessing or stopping with a
generic status update.

Use this rule for blockers such as:

- KPI terms that cannot be mapped to proven tables, columns, formulas, taxonomies, or accepted
  user decisions.
- Conflicting business definitions, grains, filters, temporal anchors, payer/member/provider
  attribution rules, or lifecycle states.
- Missing dictionaries, metadata exports, catalog paths, SLA files, policies, contracts, access
  grants, or remote execution approvals.
- Evaluation failures where the next action depends on whether correctness, performance, cost, or
  governance should take priority.

Before asking, inspect available files, configs, profiles, registries, code, logs, and metadata.
Then ask exactly one high-leverage question at a time. The question should name the blocker,
offer concrete options when possible, include a recommended answer, and explain why that answer is
the safest or most useful default.

For KPI/query blocker questions, do not ask from freehand prose or a custom terminal prompt.
Generate the standardized question packet first:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

(`blocker-question-panel` is deprecated; its default invocation now redirects to
`prepare-kpi-blocker-panel`.) Ask from
`workspaces/<project>/interns/reports/blocker_question_panel/current.json` or `current.md`
only. If those files are missing, generate them before asking. This applies to direct mappings,
source-of-truth choices, aliases, reusable workspace definitions, and derived-feature questions.
Run `uv run validate-workspace-artifacts --workspace workspaces/<project>` after generating the
panel; if it fails, fix the artifact writer before asking the user.

When the user answers a panel option, apply it with the supported panel-answer wrapper:

```powershell
uv run apply-kpi-panel-answer --workspace workspaces/<project> --domain <domain> --answer option_a
```

The answer must resolve against `current.json`. Do not invent flags such as `--accept-option`, and
do not hand-edit generated contracts to apply a choice.

KPI blocker UI behavior:

- Render `current.md` verbatim as the human-facing blocker card. Do not summarize it, collapse it,
  or replace it with a tool-native generic picker.
- Do not use generic `ask_user`, `Ask User`, or `Answer Questions` UI for KPI blocker panels.
- Use `current.json` only for exact option ids, button labels, answer application, and automation.
  Preserve the option ids, labels, order, recommended option, and business summaries from that
  artifact.
- Do not invent, rename, reorder, or simplify blocker options outside the panel artifact.
- Do not ask from truncated terminal output. Read the relevant `current.md` or `current.json` file
  explicitly first.
- If `validate-workspace-artifacts` fails, stop and report the validation errors instead of asking
  the user to choose from a malformed panel.

For KPI/query work, do not grill KPI-by-KPI when the same unresolved feature appears across many
KPIs. First cluster unresolved terms across the active workspace, rank blockers by reuse and
semantic risk, and ask about the highest-reuse workspace definition first. Save accepted answers as
workspace-level definitions when they apply broadly, then reuse them automatically for every KPI
that needs the same feature. Ask a KPI-specific question only when the definition, grain, filter,
temporal anchor, or exception is truly unique to that KPI.

Every derived-feature option shown during blocker grilling must have strict provenance. Do not ask
the user to choose a formula unless the option includes structured evidence with:

- `derived_column_name`
- `formula` and formula templates when available
- input columns and roles
- observed/profiled values for each input column, such as bounded sample values, min/max, null
  count, and profile source
- semantic meaning sources for each input column, such as data dictionaries, metadata, profile
  inference, or an empty but explicit candidate evidence note
- per-input reason explaining why that column was used
- a worked synthetic example using those input columns
- evidence sources with file paths and evidence type
- `derivation_reasoning` with `why_this_formula`, `why_not_ground_truth`, and `remaining_risk`
- `evidence_state`, `confidence`, and `needs_user_confirmation`

Treat derivation-pattern output as `candidate_derivation_not_ground_truth` until it is proven by
source artifacts or accepted by the user.

Do not offer semantically mismatched derivation patterns as selectable options. If a feature such as
`FacilityID` receives unrelated candidate formulas such as age, AR aging, CPT family, or provider
specialty, reject those candidates in the question and ask for a direct physical mapping, source
origin rule, dictionary entry, or accepted business definition instead.

Derived-column blocker options must be JSON-backed. Prose-only options are invalid. If derived
feature options exist, run the wrapper, which owns derived-feature markdown, panel generation, and
validation in one pass (`derived-feature-markdown` and `blocker-question-panel` are deprecated and
redirect here):

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

Then validate:

```powershell
uv run validate-workspace-artifacts --workspace workspaces/<project>
```

Ask the user from `interns/reports/blocker_question_panel/current.json` or `current.md`, not from a
freehand terminal prompt. If no valid JSON-backed derived option exists, the panel must ask for a
direct mapping, source-origin rule, data dictionary evidence, or workspace business definition
instead of offering formula choices.

When reusing an accepted definition, say so briefly:

```text
Reusing accepted workspace definition for `DeniedAmount` from prior blocker grilling.
```

Use this shape:

```markdown
Blocker: ...

Question: ...

Options:
- Option A: ...
- Option B: ...

Recommended answer: ...

Why: ...
```

After the user answers, record the accepted decision, rejected option, or remaining question under
the active workspace's `interns/` artifacts before relying on it for executable logic. Resume the
workflow from the unblocked point.

## Cross-Tool Skill Adapters

Repo skills are canonical in `skills/*/SKILL.md`. Do not duplicate skill bodies
manually for Claude Code, Gemini CLI, Codex, or other agent tools. Generate
tool-agnostic adapters instead:

```powershell
uv run generate-skill-adapters
```

This writes `.agents/skills_index.json` and `.agents/<tool>/SKILLS.md` for the
configured tools. Use `--embed-full` only for tools or hosted environments that
cannot read local `SKILL.md` files. Otherwise adapters should remain lightweight:
name, description, path, and routing rules.

For KPI/query work, apply the skill chain in this order:

```text
workspace-governance
  -> domain-model
  -> data-engineering-pipeline-design for SQL/Polars/PySpark or medallion/ETL requests
  -> feature-derivation-library for reusable derived-feature candidates
  -> task-onboarding / workspace-kpi-query-optimizer
  -> databricks-access-gates before or after Databricks remote execution/mutation
  -> grill-requirements (clarify-ambiguity mode) only for unresolved high-impact mappings
  -> grill-requirements when business interpretation must be chosen
  -> stakeholder-memory for accepted preferences and definitions
  -> to-solution-brief for implementation direction
  -> evolution after runs
```

Resolve KPI terms from registry, data model, profiles, data dictionaries, metadata files, catalog
metadata, then user clarification. Do not generate executable KPI logic from unproven assumptions.
If a required dictionary, metadata file, catalog path, SLA, policy, contract, or derivation rule is
missing, ask for it and save the request under the active workspace's `interns/reports/open_questions.md`.

## Multi-Phase Plan Persistence

This repo is driven by multiple interchangeable agentic CLIs (`claude-code`, `codex`,
`gemini-cli` — see `core/agents/llm_engine.py`'s `_CLI_DISPATCH`), never assume only one
of them is in use. When a task spans multiple phases/sessions (a remediation plan, a
multi-step build-out, anything with its own progress ledger), the plan file belongs
under `docs/plans/` in this repo, never in a CLI's own private config/state directory
(`~/.claude/plans/`, `~/.codex/`, `~/.gemini/`, or any future tool's equivalent). A plan
tracked in a CLI-private location is invisible to a teammate driving the same repo with
a different tool, and is lost entirely if that tool's local config is ever wiped —
whether or not the CLI you're currently using happens to offer its own plan-mode
feature, write the actual plan file into the repo. Update it live as phases land, the
same way any other governed artifact in this repo is updated, not only after the work
is finished. `docs/plans/*` is gitignored by default (keeps in-progress scratch plans
out of history) -- once a plan is worth keeping as a shared record (done, or a
teammate/another CLI needs to see it), `git add -f` it to promote it to tracked, the
same way every currently-committed file under `docs/plans/` got there. See
`docs/plans/index.md` for the existing convention and an example
(`security_governance_hardening_2026-07.md`, the first plan migrated here for exactly
this reason).

## Verification

Run the portable green gate before claiming done or committing. It runs the curated
CI suite plus the enterprise suite the same way `.github/workflows/ci.yml` does:

```powershell
green-gate            # curated + enterprise suites (strict gate)
green-gate --sweep    # also sweep blast-radius modules; flag NEW vs known failures
```

Hard rule: run tests with the venv interpreter, NOT `uv run`. `uv run` resyncs and
reinstalls pre-release pyspark 4.1.1 (no Delta), which breaks the pyspark-backed
tests. If `green-gate` is not on PATH, call it via the venv interpreter:

```powershell
.venv\Scripts\python.exe -m core.dev.green_gate --sweep
.venv\Scripts\python.exe -m unittest tests.test_enterprise_optimization
.venv\Scripts\python.exe -m compileall core interns tools tests dashboard.py
.venv\Scripts\python.exe -m ruff check core
```

Governed `uv run` wrappers (onboard-workspace, resolve-kpi-features, ...) are still
the right entry points for workspace flows -- the venv rule applies to tests, pyspark,
and engine generation only. Use broader lint only if you are ready to clean legacy
tools too.

## Git

Stage only intended files. Check before commit:

```powershell
git status --short
git diff --cached --stat
git diff --cached --name-only
```

Commit after verification. Push only the intended branch/target requested by the user.

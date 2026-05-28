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
4. `TOOLS.md` and `.agents/tools.json` for available project tools, routing, safety, and
   evidence-order policy.
5. `program.md` only when the active benchmark/task refers to it.
6. Relevant files in `core/`, `tools/`, `interns/`, `tests/`, or `workspaces/<project>/`.

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
   selection command. Recursively list file paths from the workspace root only, summarize what will
   be active, ask for confirmation, and then continue from the highest-priority blocker. For KPI/query
   workspaces, if feature mappings or business definitions are blocked, start the automatic blocker
   grilling session after confirmation.

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
uv run resolve-kpi-features --workspace workspaces/<project> --domain <domain> --include-candidates
```

The `resolve-kpi-features` command writes `question_panel_path` and
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

## Quiet Execution Rule

Keep main-chat workflow output concise. Show only the stage, key result, blocker or risk,
recommendation, and next deterministic command. Do not paste long shell output, full JSON contracts,
raw logs, or validation traces unless the user asks to inspect them. Save details under
`workspaces/<project>/interns/generated/` and `workspaces/<project>/interns/reports/`, then point to
the artifact path.

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

Hard registry-read gate:

- Before choosing any workflow route or next command, the active agent must have read
  `.agents/tools.json`, `TOOLS.md`, or its generated adapter under `.agents/<tool>/SKILLS.md` in the
  current session.
- If the agent has not read the registry/adapter, it must stop, reread it, and restart route
  selection instead of guessing from memory.
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

- `clarify-ambiguity`: ask one targeted question only when ambiguity materially matters.
- `grill-requirements`: interview users/teams to discover goals, constraints, and guardrails.
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

For KPI/query blocker questions, do not ask from freehand prose or a custom terminal prompt. After
feature resolution, generate the standardized question packet first:

```powershell
uv run blocker-question-panel --workspace workspaces/<project>
```

Ask from `workspaces/<project>/interns/reports/blocker_question_panel/current.json` or `current.md`
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
feature options exist, run:

```powershell
uv run derived-feature-markdown --workspace workspaces/<project>
```

Then run:

```powershell
uv run blocker-question-panel --workspace workspaces/<project>
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
  -> clarify-ambiguity only for unresolved high-impact mappings
  -> grill-requirements when business interpretation must be chosen
  -> stakeholder-memory for accepted preferences and definitions
  -> to-solution-brief for implementation direction
  -> evolution after runs
```

Resolve KPI terms from registry, data model, profiles, data dictionaries, metadata files, catalog
metadata, then user clarification. Do not generate executable KPI logic from unproven assumptions.
If a required dictionary, metadata file, catalog path, SLA, policy, contract, or derivation rule is
missing, ask for it and save the request under the active workspace's `interns/reports/open_questions.md`.

## Verification

Run focused checks before commit:

```powershell
uv run python -m unittest tests.test_enterprise_optimization
uv run python -m compileall core interns tools tests dashboard.py
uv run ruff check core interns\base.py interns\insights.py tools\databricks_setup.py tools\methodology_parser.py tests\test_enterprise_optimization.py dashboard.py
```

Use broader lint only if you are ready to clean legacy tools too.

## Git

Stage only intended files. Check before commit:

```powershell
git status --short
git diff --cached --stat
git diff --cached --name-only
```

Commit after verification. Push only the intended branch/target requested by the user.

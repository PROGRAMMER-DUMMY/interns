# Project Tools

This repo has first-class project tools. Agents should inspect this file and
`.agents/tools.json` before writing ad hoc scripts or reading large inputs.

## Evidence Order

For dataset questions, use profile artifacts before raw data:

1. `workspaces/<project>/interns/generated/profiles/profile_index.json`
2. `workspaces/<project>/interns/generated/profiles/*.profile.json`
3. bounded samples only when profile evidence is insufficient
4. full raw dataset reads only with a concrete reason

Do not paste raw datasets into prompts. Prefer Polars for bounded data inspection.

## Secret Display Guardrail

Never print `.env`, `.databrickscfg`, private keys, tokens, shell environment dumps, connection
strings, bearer headers, cookies, or config/AST/tree output that includes secret values. Tool output
should report only existence/status or redacted key names, for example
`DATABRICKS_TOKEN=<redacted>`.

## Governance Modules

Shared infrastructure every governed CLI in this repo plugs into. New apply / finalize / prepare
commands should reuse these modules rather than re-implementing the envelope.

### core.onboarding.workspace.cli_runner

`run_workspace_command(...)` is the single entry point every `apply-*` / `finalize-*` /
`prepare-*` CLI funnels through. It wraps four concerns into one call:

1. `core.storage.workspace_lock.workspace_lock` — process-level mutex on
   `workspaces/<ws>/interns/state/workspace.lock` so two concurrent commands cannot corrupt
   workspace state. Cross-platform (`msvcrt` on Windows, `fcntl` on POSIX). Times out after
   30 seconds with `WorkspaceLockTimeout` and a non-zero exit code.
2. `core.observability.events.time_command` — appends a structured JSONL event with duration,
   status, and per-command details to `workspaces/<ws>/interns/state/events.jsonl`. Never raises.
3. `core.onboarding.workspace.idempotency.compute_op_id` / `record_op` — deterministic 16-char op
   id from the user-visible arguments. Repeats with the same arguments return the prior payload
   from `workspaces/<ws>/interns/state/applied_ops.jsonl` instead of re-running.
4. `core.onboarding.harness.trajectory_recorder.record_trajectory_event_safe` — high-level
   workflow events that feed `prepare-workspace-bug-report` and the workspace flow guard.

Set `record_idempotent=True` for commands that mutate accepted decisions; leave it false for
prepare/inspect commands.

### core.onboarding.lexicon

`build_workspace_lexicon(...)` derives a per-workspace vocabulary from authored evidence (KPI
registry cells, profile column names, accepted `workspace_feature_definitions` entries,
`kpi_feature_mapping` source columns, and `data_dictionary` excerpts). The lexicon is consumed by
`core.onboarding.kpi.text_parser.infer_metric_and_cuts` and
`core.onboarding.relationships.schema_alias_matching.alias_index` so that semantic inference is
sourced from the workspace's own evidence rather than a curated keyword ladder. The old
`BUSINESS_COLUMN_ALIASES` dictionary and the healthcare-RCM keyword ladder were removed.

### core.contracts.versioning

Generated contracts (`workspace_lexicon`, `kpi_registry`, `kpi_feature_mapping`,
`relationship_contracts`, `source_to_target_plan`, `pipeline_plan`, `blocker_question_panel/current`)
each register a `ContractVersion` and an optional `migrate(old, target_version)` callable.
Readers can call `migrate(...)` before parsing so older artifacts continue to load.

## Tools

### list-workspace-files

Command:

```powershell
uv run list-workspace-files --workspace workspaces/<project>
uv run list-workspace-files --workspace workspaces/<project> --quiet   # counts + hints, omits the full file dump
```

Use first for `set workspace` and active-workspace selection requests. It lists all workspace file
paths up to the cap and adds basic hint groups: possible KPI files, possible data model files,
dataset roots, docs, and `interns` state. The possible KPI/model groups are not ground truth. The
full `All files` section is the user confirmation boundary. It does not read file contents, parse
Excel, profile datasets, onboard, delete, or write files. Use `--quiet` for progress/state checks
where the full file dump is not needed (it still prints the file count and classified hints); use
the default (full `All files`) when you must present the confirmation boundary to the user.

### prepare-workspace-selection

Command:

```powershell
uv run prepare-workspace-selection --workspace <workspace-name-or-path>
```

Use for `set workspace` flows when the target may be empty, missing, or backed by external raw
data. It wraps the bounded workspace listing and returns a guarded selection panel. If the workspace
has no files, it must stop with an empty-workspace blocker, list available workspaces, and offer the
external `dataset_allowlist` setup pattern instead of scanning unrelated folders or borrowing files
from another workspace.

### session-snapshot

Command:

```powershell
uv run session-snapshot start --name gemini-hospital --workspace workspaces/<project> --tool gemini
uv run session-snapshot append --name gemini-hospital --role user --content "set working directory as patients record"
uv run session-snapshot command --name gemini-hospital --command "uv run list-workspace-files --workspace workspaces/<project>" --status ok --summary "Workspace listed."
uv run session-snapshot file-change --name gemini-hospital --path workspaces/<project>/interns/reports/current.md --action create --summary "Generated stakeholder report."
uv run session-snapshot decision --name gemini-hospital --decision "User approved cleanup dry run." --status accepted
uv run session-snapshot verify --name gemini-hospital
uv run session-snapshot finish --name gemini-hospital
```

Use when an operator wants an exact end-user conversation snapshot across CLI tools. It writes a
JSONL event log plus Markdown views under `.agents/sessions/current/` by default:

```text
.agents/sessions/<timestamp>-<name>/compact.md
.agents/sessions/<timestamp>-<name>/intent_verification.md
.agents/sessions/<timestamp>-<name>/intent_verification.json
.agents/sessions/<timestamp>-<name>/events.jsonl
.agents/sessions/<timestamp>-<name>/transcript.md
.agents/sessions/<timestamp>-<name>/commands.md
.agents/sessions/<timestamp>-<name>/file_changes.md
.agents/sessions/<timestamp>-<name>/decisions.md
.agents/sessions/<timestamp>-<name>/snapshot.json
```

The snapshot directory is ignored by git. The tool redacts common secret patterns before writing,
but users should still avoid pasting credentials, tokens, connection strings, or `.env` content.
Agents should read `compact.md` first and run `session-snapshot verify` at checkpoints such as
after edits, after command failures, after deletes, after user corrections, and before final
answers.

### prepare-data-source-panel

Command:

```powershell
uv run prepare-data-source-panel --workspace workspaces/<project>
```

Asks once, at workspace level, where this workspace's data actually lives, instead of letting the
`databricks_source` key in `workspace_settings.json` silently decide it. Writes:

```text
workspaces/<project>/interns/reports/data_source_panel/current.json
workspaces/<project>/interns/reports/data_source_panel/current.md
```

Three options: `local_files` (scan/profile local `datasets/` only), `databricks_additive` (local
`datasets/` AND the declared Unity Catalog catalog/schema, merged into one profile index), and
`databricks_exclusive` (the declared catalog/schema only, local discovery skipped). Status is
`needs_user_answer` until a `mode` has been declared explicitly; the currently effective mode is
reported either way. Runs `validate-workspace-artifacts` as its validation step.

### apply-data-source-answer

Command:

```powershell
uv run apply-data-source-answer --workspace workspaces/<project> --answer <option_id> `
  [--catalog <name> --schema <name>] --confirmed-by "<name>"
```

Records the durable decision into the workspace's `workspace_settings.json` as
`databricks_source` (`catalog`, `schema`, `mode`, `source`, `confirmed_by`, `confirmed_at`), then
rebuilds the panel so a re-run reflects it. `--answer` maps `local_files` -> mode `local_files`,
`databricks_additive` -> `additive`, `databricks_exclusive` -> `exclusive`.

Refuses any other `--answer` value, and refuses either Databricks answer unless `--catalog` and
`--schema` are supplied here or already declared in `workspace_settings.json`. An empty
`--confirmed-by` records the decision as agent-asserted (`source: agent`). `--allow-replay` re-runs
an already-recorded identical answer.

### doctor

Command:

```powershell
uv run doctor
uv run doctor --json
uv run doctor --workspace workspaces/<project>
```

Read-only, one pass over everything that can make the platform unusable before you even get to a
workspace: Python version, `uv`/`.venv` presence, whether the local Java toolchain is PySpark-
compatible (Spark 3.5 needs Java 8/11/17 -- a newer JDK reports `blocked`, not just a vague crash
later), Databricks/dbt/Airflow readiness (delegates to `check-platform-readiness`), and git hygiene
(delegates to `validate-git-hygiene --all`). No new checking logic beyond Python/uv/Java -- it
reuses the same primitives those other commands already call.

Run this first in any new session, or whenever something that used to work suddenly doesn't --
`[ok]`/`[x]`/`[~]` per check, exit code 1 iff there is a real `[x]` blocker. `not_installed` /
`partial` (dbt, Airflow) are never blockers -- they only matter for the cloud-native/orchestration
path, and are reported so you know the gap, not to alarm you.

### onboard-workspace

Command:

```powershell
uv run onboard-workspace --workspace workspaces/<project>
```

Use when a workspace needs generated `interns/` artifacts: profiles, contracts,
reports, baseline runner/evaluator, and generated solution scaffolding.

### kickstart-workspace

Command:

```powershell
uv run kickstart-workspace --workspace workspaces/<project> --domain <domain>
```

Use when setting up or refreshing a governed enterprise workspace from raw
project inputs. It updates task config, discovery artifacts, feature mapping,
and open questions.

### prepare-kpi-generation

Command:

```powershell
uv run prepare-kpi-generation --workspace workspaces/<project>
```

Use after workspace confirmation when the user should choose between KPI generation and the usual
onboarding/mapping workflow. The tool always writes a two-choice route panel with a smart
recommendation based on detected KPI files, data models, datasets, optional context, and a combined
KPI quality/readiness score.

Outputs:

```text
workspaces/<project>/interns/generated/requirements/kpi_generation_session.json
workspaces/<project>/interns/reports/kpi_generation/current.json
workspaces/<project>/interns/reports/kpi_generation/current.md
```

Optional stakeholder context can be provided with repeated `--context-file` values. Context files
must stay inside the workspace.

### apply-kpi-generation-answer

Command:

```powershell
uv run apply-kpi-generation-answer --workspace workspaces/<project> --answer option_a
```

Use after the user answers the current KPI generation panel. It records the accepted option,
advances the deterministic interview, saves decisions, and eventually writes a draft KPI registry
preview with competitive advisor notes and per-KPI evidence-proof requirements under
`interns/generated/requirements/kpi_registry_draft.json`.

### finalize-kpi-generation

Command:

```powershell
uv run finalize-kpi-generation --workspace workspaces/<project> --approve-final-preview
```

Use only after the final KPI draft preview has been shown to the user and explicitly approved. It
writes the user-facing KPI registry, production-readiness proof, workspace memory, and team-level
preference memory. Without `--approve-final-preview`, it must fail. Existing registry outputs require
`--replace-existing`.

### prepare-workspace-workflow

Command:

```powershell
uv run prepare-workspace-workflow --workspace workspaces/<project> --mode local-safe --domain healthcare
```

Use after workspace confirmation when the user wants one governed checkpoint for the whole workflow.
It writes `interns/reports/workflow/current.json` and `current.md`, runs local-safe preparation
steps, and shows manual/local-safe/autopilot options. Modes:

- `plan`: inspect and write the checkpoint without generating missing artifacts.
- `local-safe`: run missing local-safe preparation, validation, and presentation export steps.
- `autopilot`: apply only bounded low-risk recommended answers, while still stopping before final
  approval, deletes, remote execution, relationship approval, docs promotion, and executable
  DDL/dbt/SQL generation.

### workspace-flow

Command:

```powershell
uv run workspace-flow start --workspace workspaces/<project> --intent kpi_generation --domain <domain>
uv run workspace-flow status --session <session-id>
uv run workspace-flow --quiet status --diff --workspace workspaces/<project>   # compact KPI-readiness summary
uv run workspace-flow answer --session <session-id> --answer option_a
uv run workspace-flow results --session <session-id>
uv run workspace-flow results --workspace workspaces/<project>   # latest session auto-resolved
uv run workspace-flow results --workspace workspaces/<project> --full --kpi kpi_002
```

`--quiet` and `--json` are accepted both before and after the subcommand. For `status --diff`,
`--quiet` prints a compact ready/blocked/gap summary plus the manifest path instead of the full
diff JSON. `status`/`answer`/`results`/`review` accept `--workspace` in place of `--session` and
resolve that workspace's most recent session. `start` resumes an existing open session when one
exists; do not re-run `start` in a loop — read the artifact paths it prints. `results` emits the
result packet itself (compact by default, `--full` inlines SQL, `--kpi <id>` forwards one KPI's
file) — do not re-run it with different flags to "see more".

Use as the quiet front door for agent-led workspace workflows. It persists session state under
`interns/state/workflow_sessions/<session-id>/`, runs existing governed tools in-process, hides
lower-level command noise, and returns a compact current panel with instruction, question, options,
suggested default, and artifact paths. `full_kpi_sql` runs the local-safe KPI path through
onboarding, blocker preparation, relationship contracts, source-to-target planning, SQL generation,
validation, and KPI result previews.

Outputs:

```text
workspaces/<project>/interns/state/workflow_sessions/<session-id>/session.json
workspaces/<project>/interns/state/workflow_sessions/<session-id>/current.json
workspaces/<project>/interns/state/workflow_sessions/<session-id>/current.md
workspaces/<project>/interns/reports/kpi_results/current.md
workspaces/<project>/interns/generated/evidence/kpi_results/current.json
```

### run-kpi-pipeline

Command:

```powershell
uv run run-kpi-pipeline --workspace workspaces/<project> --domain <domain>
uv run run-kpi-pipeline --workspace workspaces/<project> --domain <domain> --quiet
uv run run-kpi-pipeline --workspace workspaces/<project> --domain <domain> --new-session
```

The preferred single entry point for the deterministic KPI chain (see CLAUDE.md Token
Discipline). Runs onboard-workspace -> prepare-kpi-blocker-panel -> [human KPI-blocker gate] ->
build-relationship-contracts -> [human relationship-approval gate] -> workspace-flow start
--intent full_kpi_sql -> [human kpi-analyst review gate] -> results, stopping only at genuine
human gates with the exact resolving command. Idempotent and resumable: re-invoke after each gate
is resolved. Never auto-approves relationships or review verdicts (BUG-014).

Outputs: same session/panel/result artifacts as `workspace-flow` (it drives the same flow).

### generate-kpi-engines

Command:

```powershell
uv run generate-kpi-engines --workspace workspaces/<project> --engine recommended
uv run generate-kpi-engines --workspace workspaces/<project> --engine sql,polars --kpi-id kpi_002
```

Generates KPI code for the recommended engine (or `all`, or a comma list; SQL is always the
baseline) from the same ready feature mappings the SQL generator uses, preserving cross-engine
parity. PySpark execution requires JDK 8/11/17 (parity gates env-skip Spark when the JVM is
unsuitable); SQL/Polars need no JVM. The `guard_uv_run` hook blocks `uv run` for engine
generation in test contexts — use the venv interpreter (`.venv\Scripts\python.exe -m
core.onboarding.kpi.generate_kpi_engines ...`) or `green-gate` there.

Outputs:

```text
workspaces/<project>/interns/generated/solutions/kpi_*_polars.py   (and *_pyspark.py)
workspaces/<project>/interns/reports/engine_generation/current.md  (+ current.json)
```

Command:

```powershell
uv run prepare-wiki-memory --workspace workspaces/<project> --domain <domain>
```

Use when repeated KPI terms, data-model entities, grains, relationships, or workflow decisions
should be converted into governed reuse cards. V1 scans structured KPI/data-model/session artifacts
only and writes scoped memory under the workspace plus a repo-level team memory index.

Outputs:

```text
state/team_memory/wiki_memory_index.json
workspaces/<project>/interns/generated/memory/wiki_memory_candidates.json
workspaces/<project>/interns/reports/wiki_memory/current.json
workspaces/<project>/interns/reports/wiki_memory/current.md
```

Automation policy: exact approved matches may be used as draft prefill candidates, but executable
generation, final promotion, and relationship approval remain blocked until current-workspace
evidence or user approval exists.

### validate-memory-health

Command:

```powershell
uv run validate-memory-health --workspace workspaces/<project>
```

Use when workspace or shared team memory needs confidence-scored lifecycle health. It scans
`workspaces/<project>/interns/generated/memory/*.json` and, by default, `state/team_memory/*.json`,
normalizes memory entries with status, confidence, last verification, expiration condition, and
evidence fields when present, then writes:

```text
workspaces/<project>/interns/reports/memory_health/current.json
workspaces/<project>/interns/reports/memory_health/current.md
workspaces/<project>/interns/generated/evidence/memory_health/current.json
```

The command is local-safe and does not read raw datasets.

### prepare-agent-benchmark

Command:

```powershell
uv run prepare-agent-benchmark --workspace workspaces/<project> --domain <domain>
```

Use when the workspace needs a project-native readiness proof and release gate before SQL, ETL,
medallion, autopilot, or production promotion. V1 scores existing governed artifacts rather than
running external TPC/Spider/BIRD suites.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/agent_benchmark_scorecard.json
workspaces/<project>/interns/generated/contracts/release_gate_status.json
workspaces/<project>/interns/reports/benchmarks/current.json
workspaces/<project>/interns/reports/benchmarks/current.md
```

The scorecard separates core readiness from product maturity. Core readiness weights business
correctness first: KPI definitions, grain, filters, data-model readiness, relationship proof,
source-to-target readiness, and validation. Product maturity tracks presentation exports, wiki reuse,
workflow checkpoint status, and autopilot safety. Blockers route back to existing deterministic
tools such as KPI blocker panels, data-model blocker panels, relationship contracts, source-to-target
planning, validation, and wiki memory.

### validate-project-harness

Command:

```powershell
uv run validate-project-harness --workspace workspaces/<project> --domain <domain>
uv run validate-project-harness --workspace workspaces/<project> --domain <domain> --quiet   # score + collapsed blockers/warnings + paths
```

Use when a workspace needs one top-level local-safe score before completion is claimed. It runs
artifact validation, the KPI execution harness, the project-native benchmark, workflow guardrails,
and staged-file git hygiene, then writes one scoreable proof packet. Workflow guardrails include
trajectory health when `interns/state/trajectory.jsonl` is present. Pass `--quiet` to print the
score, collapsed blockers/warnings (identical findings are deduped with an `(xN)` suffix), and the
artifact paths instead of the full ~700-line JSON; the JSON is still written to disk.

Outputs:

```text
workspaces/<project>/interns/generated/evidence/project_harness.json
workspaces/<project>/interns/reports/project_harness.md
```

### harness reliability  (formerly run-reliability-suite)

Command:

```powershell
uv run harness reliability --workspace workspaces/<project> --domain <domain>
uv run harness reliability --workspace workspaces/<project> --project-harness skip
uv run harness reliability --workspace workspaces/<project> --project-harness run
```

Use for scheduled or local-safe reliability checks that should compose existing workspace harnesses
without shelling out. It runs workflow guardrails, builds the evidence graph when available, and
runs the project harness in `auto` mode only when the required generated artifacts exist. Use
`--project-harness run` to force that check or `--project-harness skip` for fresh workspaces.

Outputs:

```text
workspaces/<project>/interns/reports/reliability_suite/current.json
workspaces/<project>/interns/reports/reliability_suite/current.md
workspaces/<project>/interns/generated/evidence/reliability_suite/current.json
```

### harness ai-app  (formerly run-ai-app-harness)

Command:

```powershell
uv run harness ai-app --workspace workspaces/<project> --dataset workspaces/<project>/interns/ai_harness/datasets/happy_path.jsonl
```

Use when a workspace needs dependency-free AI application tests from JSONL cases. The harness
supports `local_stub` and `http_ai` targets, exact-match/schema/keyword evaluators, KPI mapping
assertions, SQL semantic assertions, result-table assertions, tag filtering, append-only run
outputs, `current.json`/`current.md`, pass-threshold gating, reliability coverage reporting, and
baseline regression checks. Result-table baselines compare columns, row count, and pinned metric
values, so a case can fail even when the current assertions still pass. Remote HTTP AI cases are
blocked unless `--allow-remote-ai` is passed. Config must store an env var name such as
`api_key_env`, not an API key value.

KPI/SQL-specific eval types:

- `kpi_mapping`: output must be JSON with `mappings`, `mapping_rows`, `features`, or KPI-level
  mapping rows. Configure `expected_mappings` with fields such as `feature`, `column`, `dataset`,
  `state`, `join_key`, `grain`, and `filter`.
- `sql_semantic`: output is SQL text checked for required clauses, tables, columns, joins, filters,
  and forbidden patterns.
- `result_table`: output must be JSON with `columns`, `rows`, and optional `row_count`; configure
  expected columns, row bounds, and pinned values for metric regression.

Example KPI suite rows live at `config/ai_harness.kpi_suite.example.jsonl`; copy those shapes into
`workspaces/<project>/interns/ai_harness/datasets/` and replace the stub outputs with calls to the
application boundary you want to test.

Outputs:

```text
workspaces/<project>/interns/ai_harness/runs/<run_id>/outputs.jsonl
workspaces/<project>/interns/ai_harness/runs/<run_id>/report.json
workspaces/<project>/interns/reports/ai_app_harness/current.json
workspaces/<project>/interns/reports/ai_app_harness/current.md
workspaces/<project>/interns/generated/evidence/ai_app_harness/current.json
```

### harness ai-cli  (formerly run-ai-cli-harness)

Command:

```powershell
uv run harness ai-cli --workspace workspaces/<project> --dataset workspaces/<project>/interns/ai_cli_harness/datasets/governed_suite.jsonl
uv run harness ai-cli --workspace workspaces/<project> --dataset workspaces/<project>/interns/ai_cli_harness/datasets/governed_suite.jsonl --config config/ai_cli_harness.example.json --allow-cli-exec
```

Use when a CLI agent such as Claude, Gemini, Codex, or a custom CLI needs to be tested against the
governed workflow. It is local-safe by default: real subprocess CLI execution is blocked unless
`--allow-cli-exec` is passed. Stub mode validates command transcripts, project-tool usage,
artifact existence, JSON artifact values, and workflow guardrail results without spending tokens.
Config must not contain secret-bearing keys or tokens.

Supported eval types:

- `command_policy`: validates captured commands, required/forbidden command fragments, project-tool
  use, raw-data read avoidance, and non-portable shell markers.
- `artifact_exists`: asserts generated files exist.
- `artifact_json_path`: checks JSON artifact fields such as `feature` or `status`.
- `workflow_guard`: runs `harness workflow-guardrails` against the case transcript.
- `cli_text`: checks required keywords in final CLI output.

Example CLI suite rows live at `config/ai_cli_harness.governed_suite.example.jsonl`.

Outputs:

```text
workspaces/<project>/interns/ai_cli_harness/runs/<run_id>/outputs.jsonl
workspaces/<project>/interns/ai_cli_harness/runs/<run_id>/report.json
workspaces/<project>/interns/reports/ai_cli_harness/current.json
workspaces/<project>/interns/reports/ai_cli_harness/current.md
workspaces/<project>/interns/generated/evidence/ai_cli_harness/current.json
```

### harness workflow-guardrails  (formerly validate-workflow-guardrails)

Command:

```powershell
uv run harness workflow-guardrails --workspace workspaces/<project>
uv run harness workflow-guardrails --workspace workspaces/<project> --command-log workspaces/<project>/interns/state/commands.jsonl
```

Use when the workflow itself needs a reliability gate. It checks for invented generic KPI features
such as `created_at` when no matching profile column exists, blocker panels that ask about
non-source-backed features, raw dataset reads that bypass generated profiles, non-portable shell
commands such as `cat ... | head` on Windows, and failed commands that were not followed by an
obvious retry or safer project tool.

Outputs:

```text
workspaces/<project>/interns/reports/workflow_guard_harness/current.json
workspaces/<project>/interns/reports/workflow_guard_harness/current.md
workspaces/<project>/interns/generated/evidence/workflow_guard_harness/current.json
```

### harness layered-pipeline  (formerly run-layered-pipeline-harness)

Command:

```powershell
uv run harness layered-pipeline --workspace workspaces/<project>
```

Use after `build-catalog-contract`, `prepare-data-engineering-route`, and
`prepare-pipeline-plan` when catalog, route, and pipeline contracts need a layered
data-engineering validation gate before code generation or production proof. The harness checks
catalog object shape, route remote-mutation policy, pipeline quality gates, layer objects, grain,
duplicate behavior, and approval-gated deduplication. It is read-only and does not read raw
datasets.

Outputs:

```text
workspaces/<project>/interns/reports/layered_pipeline_harness/current.json
workspaces/<project>/interns/reports/layered_pipeline_harness/current.md
workspaces/<project>/interns/generated/evidence/layered_pipeline_harness/current.json
```

### harness pipeline-execution  (formerly run-pipeline-execution-harness)

Command:

```powershell
uv run harness pipeline-execution --workspace workspaces/<project>
```

Use after `generate-pipeline-sql` to execute `pipeline_layers.sql` locally in DuckDB and verify
the expected bronze, silver, and gold views. The harness writes row counts, columns, and bounded
sample tables with sensitive healthcare identifiers redacted. It does not write remote tables.

Outputs:

```text
workspaces/<project>/interns/reports/pipeline_execution_harness/current.json
workspaces/<project>/interns/reports/pipeline_execution_harness/current.md
workspaces/<project>/interns/generated/evidence/pipeline_execution_harness/current.json
```

### harness data-quality  (formerly run-data-quality-harness)

Command:

```powershell
uv run harness data-quality --workspace workspaces/<project>
```

Use after catalog, profile, and pipeline contracts exist when a workspace needs a local-safe data
quality review focused on duplicate evidence. The harness is profile/catalog/pipeline contract
driven, uses bounded duplicate evidence, writes redacted samples only, and in milestone 1 does not
perform automatic deduplication, quarantine SQL generation, quarantine SQL execution, or remote
mutation.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/data_quality_contract.json
workspaces/<project>/interns/generated/evidence/data_quality_harness/current.json
workspaces/<project>/interns/reports/data_quality/current.json
workspaces/<project>/interns/reports/data_quality/current.md
```

### prepare-data-quality-panel

Command:

```powershell
uv run prepare-data-quality-panel --workspace workspaces/<project>
```

Asks what "valid" means for one (dataset, column) actually used by a KPI, in the same JSON-backed
option shape as the KPI blocker panel -- so dbt schema tests come from a recorded human answer, not
hand-maintained YAML. It only fires where profiling evidence makes the rule genuinely ambiguous:
nulls observed, or a string-typed column with a small (1-8) observed distinct-value set. A clean,
unambiguous column is never asked about, and a column shared by two KPIs is asked once. Candidates
come from `interns/generated/contracts/kpi_feature_mapping.json` (only KPIs whose features are all
in a ready state) joined to `interns/generated/profiles/profile_index.json`. Writes:

```text
workspaces/<project>/interns/reports/data_quality_panel/current.json
workspaces/<project>/interns/reports/data_quality_panel/current.md
```

Status is `needs_user_answer` (with `remaining_count`) or `no_pending_checks`. One question at a
time. Runs `validate-workspace-artifacts` as its validation step.

### apply-data-quality-answer

Command:

```powershell
uv run apply-data-quality-answer --workspace workspaces/<project> --answer <option_id> `
  --confirmed-by "<name>"
```

Appends the answered check to the durable contract
`workspaces/<project>/interns/generated/contracts/data_quality_decisions.json` (`check_type`,
`check_config`, `severity`, `source`, `confirmed_by`, `confirmed_at`), then rebuilds the panel with
the next pending column. `dbt_project_generator` reads that file to emit real `not_null` /
`accepted_values` tests; `skip` records that no test is wanted. Severity comes from the chosen
option (`error` blocks the build, `warn` surfaces drift), never from a separate setting.

Refuses when there is no `data_quality_panel/current.json` (run `prepare-data-quality-panel`
first), when the panel has no pending question, or when `--answer` is neither an `option_id` nor an
option label. An empty `--confirmed-by` records the decision as agent-asserted (`source: agent`).

### prepare-duplicate-review-panel

Command:

```powershell
uv run prepare-duplicate-review-panel --workspace workspaces/<project>
```

Use after `harness data-quality` when duplicate findings need a JSON-backed stakeholder review
panel. It prepares bounded, redacted duplicate evidence and options from profile/catalog/pipeline
contracts. It does not apply a duplicate decision and does not generate or run deduplication or
quarantine SQL in milestone 1.

Outputs:

```text
workspaces/<project>/interns/reports/duplicate_review/current.json
workspaces/<project>/interns/reports/duplicate_review/current.md
workspaces/<project>/interns/generated/contracts/data_quality_contract.json
```

### apply-duplicate-review-answer

Command:

```powershell
uv run apply-duplicate-review-answer --workspace workspaces/<project> --answer option_a
```

Use only after the user answers the current duplicate review panel from
`interns/reports/duplicate_review/current.json` or `current.md`. It resolves option ids or labels
against the current panel and records the accepted duplicate handling decision. Milestone 1 remains
decision-only: no automatic deduplication, quarantine SQL mutation, or remote mutation is performed.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/duplicate_decisions.json
workspaces/<project>/interns/reports/duplicate_review/current.json
workspaces/<project>/interns/reports/duplicate_review/current.md
```

### record-workspace-trajectory

Command:

```powershell
uv run record-workspace-trajectory --workspace workspaces/<project> --event-type command --status ok --summary "Listed workspace." --command "uv run list-workspace-files --workspace workspaces/<project>"
uv run record-workspace-trajectory --workspace workspaces/<project> --event-type validation --status ok --summary "Workspace artifacts validated." --validation validate-workspace-artifacts
uv run record-workspace-trajectory --workspace workspaces/<project> --render-only
```

Use when an agent, CLI, or workflow wrapper needs a workspace-scoped replay log. It appends
secret-redacted JSONL events to the active workspace and writes current JSON/Markdown summaries.
`harness workflow-guardrails` reads this trajectory by default when present, so unsupported
commands, raw dataset reads, and failed steps without nearby recovery become scoreable findings.
Controlled tools such as `workspace-flow`, `prepare-kpi-blocker-panel`, and
`apply-kpi-panel-answer` record best-effort trajectory events automatically; this command remains
available for external CLIs and manual transcript capture.

Outputs:

```text
workspaces/<project>/interns/state/trajectory.jsonl
workspaces/<project>/interns/reports/trajectory/current.json
workspaces/<project>/interns/reports/trajectory/current.md
workspaces/<project>/interns/generated/evidence/trajectory/current.json
```

### build-workspace-evidence-graph

Command:

```powershell
uv run build-workspace-evidence-graph --workspace workspaces/<project>
uv run query-workspace-evidence-graph --workspace workspaces/<project> --term Payer
uv run query-workspace-evidence-graph --workspace workspaces/<project> --impact-feature Payer
```

Use when a reviewer or agent needs one graph connecting KPI rows, terms, features, columns,
datasets, profiles, mappings, blocker panels, generated SQL, trajectory events, and harness
findings. The first version is local-safe and builds from existing generated artifacts only; it does
not run onboarding, read raw datasets, or execute SQL.

Outputs:

```text
workspaces/<project>/interns/generated/evidence_graph/graph.json
workspaces/<project>/interns/reports/evidence_graph/current.md
```

The query command reads the graph, rebuilding it first if needed, and returns JSON for term lookup,
feature impact, or column impact.

### kpi-proof-packet

Command:

```powershell
uv run kpi-proof-packet --workspace workspaces/<project> --domain <domain>
```

Use when stakeholders or operators need a read-only all-KPI recommendation and proof packet before
bulk approval or execution. The first version supports `recommend` mode only. It does not apply
mapping decisions or run SQL. It summarizes the source KPI row, normalized engine fields, current
mapping recommendations, reliability gates, generated SQL when present, execution output previews
when present, profile-backed sample values, and the next deterministic command for each KPI.

Outputs:

```text
workspaces/<project>/interns/reports/kpi_proof_packet/current.md
workspaces/<project>/interns/reports/kpi_proof_packet/current.json
workspaces/<project>/interns/generated/evidence/kpi_proof_packet/current.json
```

### check-kpi-anomalies

Command:

```powershell
uv run check-kpi-anomalies --workspace workspaces/<project>
```

Post-results check: median-absolute-deviation over the workspace's trailing KPI headline history, so
a KPI number lurching for no known reason is flagged before it reaches the dashboard silently. Reads
`interns/reports/kpi_results/current.json` and the headline history in
`interns/generated/evidence/kpi_headline_history.json`, writes
`interns/reports/kpi_alerts/current.md` (printing that path), then appends this run to the history.

Optionally posts to a webhook when the alert webhook environment variable is set. Never raises on a
missing or malformed artifact -- absent evidence means no findings this run, not a crashed task --
so it is safe as a post-results task in the pipeline DAG.

### prepare-data-model-generation

Command:

```powershell
uv run prepare-data-model-generation --workspace workspaces/<project>
```

Use after onboarding/profiling when data model docs are missing, weak, image-only, or need to be
converted into governed relationship proof. It writes a route panel under
`interns/reports/data_model_generation/` and does not finalize user-facing docs.

### apply-data-model-answer

Command:

```powershell
uv run apply-data-model-answer --workspace workspaces/<project> --answer option_b
```

Use after the user answers the current data-model panel. It writes a draft core model pack under
`interns/generated/requirements/` and readable draft reports under
`interns/reports/data_model_generation/`.

### finalize-data-model-generation

Command:

```powershell
uv run finalize-data-model-generation --workspace workspaces/<project> --approve-final-preview
```

Use only after the draft data model preview is reviewed and explicitly approved. It writes
user-facing `docs/data-model.md`, `docs/erd.md`, `docs/relationships.md`, and finalized
`interns/generated/contracts/data_model_contract.json`. Approved relationships can then be promoted
by `build-relationship-contracts` for executable SQL planning.

### prepare-data-model-blocker-panel

Command:

```powershell
uv run prepare-data-model-blocker-panel --workspace workspaces/<project>
```

Use after a data-model draft exists and the next unresolved model decision should be asked from a
JSON-backed panel. It ranks grain, primary-key, relationship, temporal-anchor, and SCD blockers and
writes `interns/reports/data_model_blocker_panel/current.json` and `current.md`.

### apply-data-model-blocker-answer

Command:

```powershell
uv run apply-data-model-blocker-answer --workspace workspaces/<project> --answer option_a
```

Use after the user answers the current data-model blocker panel. It resolves the option against
`current.json`, applies the structured operation to `data_model_draft.json`, writes the next blocker
panel, and keeps unresolved decisions blocked.

### parse-data-model-images

Command:

```powershell
uv run parse-data-model-images --workspace workspaces/<project>
```

Use when a workspace contains image-only or image-backed data model evidence such as ERDs,
star-schema diagrams, or medallion diagrams. The command is local-safe by default: it creates
review-gated sidecars under `interns/generated/data_model_images/` and review panels under
`interns/reports/data_model_images/`. Local OCR runs only if a configured local engine is available.
If OCR is missing and the operator wants the tool to resolve it, pass `--auto-install-ocr`; the
command attempts a supported local package-manager install for Tesseract and records the attempt in
the sidecar.
Remote/multimodal vision is not called unless explicit remote flags are added, and healthcare or
customer diagrams require a separate sensitivity confirmation. Image-derived relationships remain
non-executable until matched to profile/catalog/schema evidence or explicitly approved and then
validated by the normal relationship-contract workflow.

Outputs:

```text
workspaces/<project>/interns/generated/data_model_images/<image>.model.json
workspaces/<project>/interns/reports/data_model_images/<image>.model.md
workspaces/<project>/interns/reports/data_model_images/current.json
workspaces/<project>/interns/reports/data_model_images/current.md
```

### understand-data

Command:

```powershell
uv run understand-data --workspace workspaces/<project>
uv run understand-data --workspace workspaces/<project> --quiet   # one-line tier + schema + option count + artifact path
```

Use to run the BUG-010 data-understanding gate standalone: it classifies the workspace data-quality
tier (raw/bronze, silver, gold) and schema type (star, snowflake, galaxy, flat, OBT, 3NF,
hierarchical) from generated profiles plus relationship contracts, surfaces tier-scoped
data-processing options, and writes `interns/reports/data_understanding/current.json` and
`current.md`. It reads generated profiles only (no raw dataset reads) and reuses the same classifier
the workspace-flow gate uses. `--quiet` prints a compact summary line while still writing the full
JSON and Markdown to disk.

### prepare-document-candidate-review

Command:

```powershell
uv run prepare-document-candidate-review --workspace workspaces/<project>
```

Reads the document-derived candidates `onboard-workspace` classified into
`interns/generated/documents/candidates.json` and writes the human-review panel:

```text
workspaces/<project>/interns/reports/documents/candidates.md
workspaces/<project>/interns/reports/documents/candidates.json
```

Display-only: it promotes nothing and mutates no contract. Each candidate gets a stable
`candidate_id` derived from its type + source document + page + content hash, so ids survive
re-runs and can be quoted back to `apply-document-candidate`. Ask from this panel; the panel's own
interaction contract sets `generic_answer_picker_allowed: false`. With no `candidates.json` on disk
it returns `status: no_candidates` and points at `onboard-workspace`.

### apply-document-candidate

Command:

```powershell
uv run apply-document-candidate --workspace workspaces/<project> --candidate-id <id> `
  --confirmed-by "<name>" [--reject] [--note "<why>"]
```

Accepts or rejects ONE candidate from that panel and appends the decision to the durable artifact
`workspaces/<project>/interns/generated/documents/accepted_candidates.json`. That file lives under
`generated/documents/`, not `generated/contracts/`, precisely so an onboarding re-run (which clears
and regenerates `contracts/`) cannot destroy accepted decisions. Rejections are appended too, with
`decision: rejected` and the `--note`, so they stay auditable.

It touches `kpi_registry.json`, `workspace_lexicon.json`, `relationship_contracts.json` and every
other generated contract not at all -- merging accepted entries is a separate wired-in step.

Refuses acceptance when `--confirmed-by` is empty (`status: refused`, nonzero exit); errors when
`candidates.json` is missing or the `--candidate-id` is not in it. A `data_model_candidate` is
recorded `executable: false` even after human acceptance -- profile RI proof is still required
before any join derived from it may run.

### export-data-model-diagram

Command:

```powershell
uv run export-data-model-diagram --workspace workspaces/<project>
```

Use when stakeholders need a presentable data-model diagram artifact. It reads finalized
`data_model_contract.json` when available, otherwise the draft data model or onboarded
`domain_model.json`, and writes native SVG plus Mermaid Markdown under
`interns/reports/presentation/`.

### export-kpi-registry-excel

Command:

```powershell
uv run export-kpi-registry-excel --workspace workspaces/<project>
```

Use when stakeholders need an Excel workbook for KPI review. It uses finalized KPI registry JSON
when present, otherwise the KPI generation draft, otherwise onboarded `kpi_registry.json`, and
writes a multi-sheet workbook under `interns/reports/presentation/kpi_registry.xlsx`.

### export-workspace-presentation

Command:

```powershell
uv run export-workspace-presentation --workspace workspaces/<project>
```

Use for a stakeholder-ready presentation bundle. It produces the data-model SVG/Mermaid export,
KPI Excel workbook, and `presentation_manifest.json` under `interns/reports/presentation/`.

### prepare-source-catalog

Command:

```powershell
uv run source-catalog plan --workspace workspaces/<project>
uv run prepare-source-catalog --workspace workspaces/<project>
```

Use when external sources should be selected before ingestion. Reusable source templates live under
`config/source_catalogs/`; workspace-approved selections live at
`workspaces/<project>/docs/source_selection.json`. The command writes a dry-run plan and report
under `workspaces/<project>/interns/` without fetching rows, copying files, or calling remote
catalog APIs.

Supported source types:

- `api`: HTTP/JSON dataset or document endpoint with bounded pagination.
- `local`: approved local/workspace file source, copied into `datasets/` or `docs/` or registered
  as an external allowlist entry.
- `databricks_uc`: Unity Catalog table metadata source. It plans by default; remote metadata export
  requires explicit remote approval.

API sources support conservative runtime controls through the selection or template:

```json
{
  "fetch_policy": {
    "qps": 1.0,
    "attempts": 4,
    "timeout_seconds": 30,
    "backoff_initial_seconds": 1,
    "backoff_max_seconds": 30,
    "max_bytes": 50000000
  },
  "auth": {
    "type": "header",
    "header_name": "Authorization",
    "header_prefix": "Bearer",
    "header_env": "VENDOR_API_TOKEN"
  }
}
```

Only the environment variable name is stored in artifacts; secret values are never written. Runtime
checkpoints are written under `interns/state/source_catalog/checkpoints/`, and failed pages are
quarantined under `interns/generated/evidence/source_catalog/quarantine/`.

Outputs:

```text
workspaces/<project>/interns/generated/requirements/source_catalog_plan.json
workspaces/<project>/interns/reports/source_catalog_plan.md
```

The canonical controllable CLI uses subcommands:

```powershell
uv run source-catalog plan --workspace workspaces/<project>
uv run source-catalog preflight --workspace workspaces/<project>
uv run source-catalog api-fetch --workspace workspaces/<project> --source <source-id>
uv run source-catalog local-stage --workspace workspaces/<project> --source <source-id>
uv run source-catalog uc-inspect --workspace workspaces/<project> --source <source-id>
uv run source-catalog discover-docs --workspace workspaces/<project>
uv run source-catalog index-catalog --workspace workspaces/<project> --source <catalog-source-id>
uv run source-catalog match-catalog --workspace workspaces/<project> --source <catalog-source-id> --keyword claims
uv run source-catalog draft-selection --workspace workspaces/<project> --source <catalog-source-id>
uv run source-catalog finalize-selection --workspace workspaces/<project> --source <catalog-source-id> --approve-final-preview
uv run source-catalog process --workspace workspaces/<project>
uv run source-catalog validate --workspace workspaces/<project> --strict
uv run source-catalog run --workspace workspaces/<project>
```

Use the subcommands when debugging or controlling a source type independently. The `prepare-*` and
`ingest-*` commands remain compatibility wrappers for the dry-run and all-source apply paths.
`preflight` checks target boundaries, resource budget, URLs, rate-limit policy, auth environment variable presence,
local file existence, and Databricks remote approval state. `api-fetch` uses a concurrent scheduler
for multiple API sources, shares QPS throttling per host, resumes row APIs from checkpoints when
possible, enforces expected columns when configured, streams declared file/document responses through
`.part` files, and quarantines failed pages. `process` classifies materialized
outputs, stages CSV/JSON datasets to Parquet evidence, writes profile JSON, and records a basic
drift report against the previous profile. `validate --strict` treats partial fetches and fetch
failures as errors for production-style runs.

For large catalog payloads, do not paste the full JSON into chat or prompts. Use
`index-catalog` to write compact JSONL entries under
`interns/generated/requirements/source_catalog/`, `match-catalog` to score the index against
workspace dataset/doc names and optional keywords, then `draft-selection` to create
`docs/source_selection.generated.json`. JSONL/NDJSON and streamable JSON arrays are indexed without
loading the whole catalog. Draft selections use `approval: needs_approval`; promote a reviewed draft
with `finalize-selection --approve-final-preview`, which writes a backup of the previous
`docs/source_selection.json`.

### build-catalog-contract

Command:

```powershell
uv run build-catalog-contract --workspace workspaces/<project>
```

Use after onboarding/profile generation when downstream data-engineering plans need a stable
logical source interface. It builds catalog objects from profile evidence, keeps raw physical paths
limited to ingestion bootstrap and local smoke-test adapters, and does not fetch remote data or
mutate external systems.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/catalog_contract.json
workspaces/<project>/interns/reports/catalog_contract.md
```

### build-source-family-contracts

Command:

```powershell
uv run build-source-family-contracts --workspace workspaces/<project>
```

Use after onboarding/profile generation for external raw folders that contain repeated dated CSV
releases. It groups profile-backed files into logical source families, detects compact schema
versions and drift, extracts release/year/quarter tokens from filenames, and writes bronze planning
hints before medallion or ETL route planning. It reads generated profile metadata and approved
source selection only; it does not read raw datasets or duplicate full profile payloads.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/source_family_contracts.json
workspaces/<project>/interns/reports/source_family_contracts.md
```

### discover-external-sources

Command:

```powershell
uv run discover-external-sources --workspace workspaces/<project> --external-root D:\Cold_Storage
```

Use after a user points to a large external folder. Keep the repo workspace under
`workspaces/<project>` and treat the external folder as a source root. The command performs
metadata/path-only classification, groups related datasets and documents, detects raw files, docs,
Delta tables, DuckDB/SQLite files, logs, specs, system/session state, and writes:

```text
workspaces/<project>/interns/generated/requirements/external_source_discovery.json
workspaces/<project>/interns/reports/external_source_discovery.md
workspaces/<project>/docs/source_selection.generated.json
```

It recommends data-engineering strategies such as raw CSV medallion intake with dictionaries,
metadata-first profiling when docs are missing, Delta external-table inspection, database metadata
inspection, or exclusion for logs/runtime state. The generated source selection is review-gated with
`approval: needs_approval`; promote it only after review.

### prepare-external-source-intake

Command:

```powershell
uv run prepare-external-source-intake --external-root D:\Cold_Storage --proposed-workspace workspaces/cms
uv run apply-external-source-intake --external-root D:\Cold_Storage --proposed-workspace workspaces/cms --answer option_a
```

Use when the user gives an external path before choosing whether it belongs to an existing workspace
or a new workspace. The workflow writes a deterministic route panel, remembers repo-level defaults,
records per-workspace intake memory, runs metadata-only discovery after routing, then asks outcome
and source-group questions. Current panel files live at:

```text
workspaces/<project>/interns/reports/external_source_intake/current.json
workspaces/<project>/interns/reports/external_source_intake/current.md
```

The session and memory are written to:

```text
workspaces/<project>/interns/generated/requirements/external_source_intake_session.json
workspaces/<project>/interns/generated/memory/external_source_intake_memory.json
state/team_memory/external_source_intake_preferences.json
```

If a saved routing default exists and the user chooses a different route, the workflow asks for a
change reason before continuing. A one-off change does not update the default unless
`--save-as-default` is used.

### apply-external-source-intake

Command:

```powershell
uv run apply-external-source-intake --external-root D:\Cold_Storage `
  --proposed-workspace workspaces/<project> --answer <option_id> `
  [--existing-workspace workspaces/<other>] [--workspace-name <name>] `
  [--change-reason "<why>"] [--save-as-default]
```

Answers whichever stage the intake session is currently on (`route_selection`,
`route_change_reason`, `outcome_selection`, `source_group_selection`), appends the answer to
`external_source_intake_session.json`, and rewrites `external_source_intake/current.{json,md}` with
the next stage. `--max-files` (default `2000`) and `--max-seconds` (default `30.0`) bound the
metadata-only discovery that runs after routing. `--allow-replay` re-runs an already-recorded
identical answer.

Refuses when `--answer` matches no option in the current panel, when there is no active stage, when
the chosen route is `attach_existing` and no `--existing-workspace` is resolvable, and when a custom
new workspace is chosen without `--workspace-name`. Choosing a route that differs from a saved
team default is held at a `route_change_reason` stage until `--change-reason` is given; the default
itself only changes with `--save-as-default`.

### declare-source

Command:

```powershell
uv run declare-source --workspace workspaces/<project> --type <source-type> --location <uri> `
  [--format-hint parquet] [--credential-ref <credential-name>] `
  [--schema-registry-url <https-endpoint>] [--one-shot] [--declared-by "<name>"]
```

Records where this workspace's data actually lives, into `workspace_settings.json`
(`source_declaration`). `--type` is constrained to the registered source types
(`core.intake.declaration.SOURCE_TYPES`); `--location` is a bucket/URI, JDBC url, broker list,
`<catalog>.<schema>`, or a path. `--credential-ref` is the NAME of a credential (secret scope/key,
cloud profile, env-var name, UC storage credential) and must never be a secret value. `--one-shot`
marks a source that is loaded once as a historical backfill rather than on a schedule.

First command of the new cloud-native spine; see AGENTS.md > "New spine (built, not yet default)".

### discover-source

Command:

```powershell
uv run discover-source --workspace workspaces/<project> [--max-items 2000] [--max-seconds 30]
```

Read-only scan of the declared source. Bounded by `--max-items` / `--max-seconds` so a huge bucket
cannot stall the session. Writes:

```text
workspaces/<project>/interns/generated/intake/discovery.json
```

Requires a prior `declare-source`. Re-run it after the source changes -- `prepare-drift-panel`
diffs consecutive discoveries.

### prepare-intake-panel

Command:

```powershell
uv run prepare-intake-panel --workspace workspaces/<project>
```

Builds the merged intake interview from discovery plus prior answers, and writes the understanding
playback -- the restatement of what the platform believes it was told, with each line tagged
`(measured)` / `(you said)` / `(default)`:

```text
workspaces/<project>/interns/reports/intake_panel/current.json
workspaces/<project>/interns/reports/intake_panel/current.md
workspaces/<project>/interns/reports/intake_playback/current.md
```

Ask from the panel files; do not invent freehand intake questions. Runs
`validate-workspace-artifacts` as its validation step.

### apply-intake-answer

Command:

```powershell
uv run apply-intake-answer --workspace workspaces/<project> --question <question_id> `
  --answer <option_id_or_text> --answered-by "<name>"
uv run apply-intake-answer --workspace workspaces/<project> `
  --question playback_confirm --answer confirmed --answered-by "<name>"
```

Records one answer from the intake panel into
`workspaces/<project>/interns/generated/intake/intake_answers.json`. `--answer` takes an option id,
a comma-separated list of option ids, or free text. An empty `--answered-by` records the answer as
agent-asserted.

The second form is the alignment gate `prepare-blueprint` refuses without: confirm the playback
only after reading `interns/reports/intake_playback/current.md`. If a line there is wrong,
re-answer that question first. Re-answering any question after a blueprint was confirmed clears the
confirmation again by design -- the previous confirmation covered the previous requirement.

### prepare-solution-blueprint

RETIRED (Task D1) -- this is now a deprecation redirect to `prepare-blueprint`. It writes no
blueprint of its own.

```powershell
uv run prepare-solution-blueprint --workspace workspaces/<project> [--catalog <name>] `
  [--bronze-schema bronze] [--silver-schema silver] [--gold-schema gold]
```

Forwards `--workspace`, `--repo-root`, `--catalog` and the three schema flags to
`prepare-blueprint` and returns its exit code. `--source-root` and `--ingestion-mode` are DROPPED
with a named reason on stderr: the source now comes from `declare-source`
(`workspace_settings.source_declaration`), and ingestion is emitted by `generate-ingestion` and
executed by the separately-gated `run-ingestion`. Any other flag is dropped the same way, never
silently.

Use `prepare-blueprint` directly. What follows describes the behaviour this command had BEFORE the
redirect, kept because `apply-blueprint-answer` can still edit artifacts it produced.

Turns an external-source discovery listing into a per-group plan -- what becomes a table, what
becomes a Unity Catalog volume, what is not ingested at all -- and states it in plain English before
anything exists. `--ingestion-mode system` means we run the bootstrap once approved; `manual` means
we only emit the commands. Writes:

```text
workspaces/<project>/interns/reports/solution_blueprint/current.json
workspaces/<project>/interns/reports/solution_blueprint/current.md
```

Defaults by discovered class: `dataset` / `delta_table` -> external table (zero copy, registered in
place), `document` -> volume, `log_or_state` / `database` / `other` -> excluded and must be opted IN
rather than out. The Unity Catalog bootstrap chain is EMITTED into the artifact, never executed, and
`status` stays `draft` until an approval is recorded.

Refuses when `interns/generated/requirements/external_source_discovery.json` does not exist -- run
`discover-external-sources --workspace <ws> --external-root <root>` first, because a blueprint
without a listing is a guess.

Strangler overlap, still live: the legacy producer (`build_blueprint`) is no longer reachable
through this command, but `apply-blueprint-answer` still calls it and still stamps
`generated_by: prepare-solution-blueprint`. So the new renderer's preservation of
`current.legacy.{json,md}` remains load bearing and must NOT be deleted until
`apply-blueprint-answer` is retired too -- which needs an answer for what replaces blueprint EDITS
(exclude/include/as_volume/as_managed) in the new spine, where `prepare-blueprint` has no
equivalent. Use `prepare-blueprint` + `confirm-blueprint` for all new work.

### apply-blueprint-answer

Command:

```powershell
uv run apply-blueprint-answer --workspace workspaces/<project> --exclude <group>
uv run apply-blueprint-answer --workspace workspaces/<project> --include <group>
uv run apply-blueprint-answer --workspace workspaces/<project> --as-volume <group>
uv run apply-blueprint-answer --workspace workspaces/<project> --as-managed <group>
uv run apply-blueprint-answer --workspace workspaces/<project> --approve --confirmed-by "<real name>"
```

The edit + approval gate for the legacy `prepare-solution-blueprint` artifact. Edits are typed
flags, never parsed from prose: the agent translates the user's English into `--exclude` /
`--include` / `--as-volume` / `--as-managed` (each repeatable) and the platform records the flags,
so the decision can be replayed and audited. Edits persist across re-discovery, and a group named in
a new instruction is dropped from every other bucket first. `--as-managed` is the one disposition
that COPIES data.

Any edit clears a prior approval and stamps `approval_invalidated` -- approving plan A must never
carry over onto plan B.

`--approve` refuses unless `--confirmed-by` resolves to a human (Human-Gate Provenance Rule): this
approval authorises creating catalogs, schemas, volumes and tables. Both paths refuse when no
blueprint exists yet.

### apply-uc-intake

Command:

```powershell
uv run apply-uc-intake --workspace workspaces/<project>                                  # dry run
uv run apply-uc-intake --workspace workspaces/<project> --role-arn <aws-iam-role> --apply
```

Executes an APPROVED solution blueprint against Unity Catalog: storage credential, external
location, catalog, schemas, volumes, external tables. It creates governance objects and moves no
bytes. Writes:

```text
workspaces/<project>/interns/reports/uc_intake/current.json
workspaces/<project>/interns/reports/uc_intake/current.md
```

Dry run is the default -- without `--apply` it reports exactly what would happen and creates
nothing. Every operation is idempotent: existence is checked first and an already-present object is
`skipped`, so a re-run after a partial failure resumes. Execution stops at the first failure,
because later objects depend on earlier ones.

Refusals, in order: no blueprint (`prepare-solution-blueprint` first); the blueprint is not
`approved` (`apply-blueprint-answer --approve --confirmed-by "<name>"`); `--apply` without the
remote-execution approval (`AUTORESEARCH_ALLOW_REMOTE_EXECUTION`, set by a human's own shell --
`status: refused_no_remote_approval`); `--apply` without `--role-arn`, the AWS IAM role Unity
Catalog assumes to reach the bucket, which has no safe default (`refused_no_role_arn`). A
`managed_table` disposition is reported as `requires_copy` and deliberately NOT executed here --
`COPY INTO` / Auto Loader move real bytes and need their own decision.

### prepare-blueprint

Command:

```powershell
uv run prepare-blueprint --workspace workspaces/<project> [--catalog <name>] `
  [--bronze-schema bronze] [--silver-schema silver] [--gold-schema gold]
```

Evaluates the blueprint decision tables against discovery + intake answers and renders the pipeline
blueprint (each choice shown with the rule that fired). `--catalog` defaults to the workspace name.
Writes the decision record next to the contracts it drives, plus:

```text
workspaces/<project>/interns/reports/solution_blueprint/current.json
workspaces/<project>/interns/reports/solution_blueprint/current.md
```

If an existing `current.json` was written by the legacy `prepare-solution-blueprint`, it is
preserved as `current.legacy.{json,md}` rather than overwritten.

Refuses -- cleanly, with `status: refused` and a nonzero exit, creating nothing -- until the intake
playback is confirmed (`apply-intake-answer --question playback_confirm`). That refusal is the
alignment gate working, not a bug.

### confirm-blueprint

Command:

```powershell
uv run confirm-blueprint --workspace workspaces/<project> --confirmed-by "<real name>"
```

The ONE human gate of the cloud-native spine. Writes
`interns/reports/solution_blueprint/current.confirmed.json` and stamps `status: confirmed` with
human provenance.

It refuses (structured payload, nonzero exit) when: there is no blueprint yet; the blueprint on
disk was written by a different command than `prepare-blueprint` (re-run `prepare-blueprint` so the
confirmation covers the current decisions); `--confirmed-by` resolves to an agent rather than a
human (Human-Gate Provenance Rule -- this confirmation authorises creating catalogs, schemas,
external locations, tables and DAGs); or any decision is still blocked on a missing fact, in which
case it names the missing facts to measure in discovery or answer in the interview.

### plan-provisioning

Command:

```powershell
uv run plan-provisioning --workspace workspaces/<project> [--catalog <base-name>] [--env dev|prod] `
  [--schema bronze --schema silver --schema gold] [--grant-principal <principal>]
```

Plans additive-only Unity Catalog provisioning from the discovery + blueprint evidence. `--env`
drives catalog-per-env naming (the env suffix is added to `--catalog`). `--schema` is repeatable
and defaults to bronze/silver/gold. `--grant-principal` is repeatable and only grants read on a
NEWLY created catalog. Writes:

```text
workspaces/<project>/interns/generated/contracts/provision_plan.json
```

The plan records each step's kind (catalog / schema / volume / external location / grant) and marks
anything destructive as `blocked_destructive` -- planned so a human can decide, never executed.
Runs `validate-workspace-artifacts` as its validation step.

### apply-provisioning

Command:

```powershell
uv run apply-provisioning --workspace workspaces/<project> [--dry-run | --no-dry-run]
```

Executes the provision plan against Unity Catalog, additively and idempotently. Every step checks
existence first: an object already there is recorded `existing` and skipped, so a re-run after a
partial failure resumes. `blocked_destructive` steps are refused by kind.

Refusals happen before anything is created, and are structured payloads with a nonzero exit, never
tracebacks:

1. No confirmed blueprint (`interns/reports/solution_blueprint/current.confirmed.json`) -- this is a
   dry run and says so. `--dry-run` defaults ON without that confirmation and OFF with it.
2. `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=0` -- the human kill-switch stops it even for a confirmed
   workspace.
3. Databricks unreachable -- structured failure pointing at `check-platform-readiness`.

### generate-ingestion

Command:

```powershell
uv run generate-ingestion --workspace workspaces/<project>
```

Generates Databricks-native ingestion code per discovered table (Auto Loader / `COPY INTO` per the
connector) into `workspaces/<project>/ingestion/` -- git-tracked repo content, like `dbt/` -- plus
`ingestion/jobs_manifest.json`. It generates; it runs nothing and mutates no remote object. A
connector with no generated ingestion is reported as a `[~]` note rather than silently skipped.
Edit the generator, not the generated files.

### prepare-drift-panel

Command:

```powershell
uv run prepare-drift-panel --workspace workspaces/<project>
```

Snapshots the current `interns/generated/intake/discovery.json`, diffs it against the previous
snapshot, and opens a panel when a finding needs a decision:

```text
workspaces/<project>/interns/reports/schema_drift_panel/current.json
workspaces/<project>/interns/reports/schema_drift_panel/current.md
```

Run it after re-running `discover-source` on a live source. Runs `validate-workspace-artifacts` as
its validation step.

### apply-drift-answer

Command:

```powershell
uv run apply-drift-answer --workspace workspaces/<project> --finding <finding_id> `
  --answer propagate|quarantine_column|block_pipeline --confirmed-by "<real name>"
```

Records one answer from the schema drift panel. For `quarantine_column` it also writes the
`interns/generated/contracts/schema_exclusions.json` contract.

`--confirmed-by` must be a real human name for `quarantine_column` and `block_pipeline`; an empty
value or an agent identity records the decision as agent-asserted, which those two options refuse.
An unknown finding, an unsupported option, or an agent-asserted human gate returns a structured
refusal with a nonzero exit, not a traceback. The confirmer is part of the op identity, so a
refused agent attempt does not make the human's retry look like a replay.

### resource-preflight

Command:

```powershell
uv run resource-preflight --workspace workspaces/<project>
```

Writes local CPU, memory, disk, budget, recommended worker/API-concurrency, and resource mode
evidence under `interns/generated/evidence/resource_preflight.json` and
`interns/reports/resource_preflight.md`. Use it before heavyweight ingestion, profiling,
transformation, or local loading. `source-catalog preflight` calls the same resource layer and marks
disk/RAM budget blockers before fetching or staging data. `onboard-workspace` applies resource
profile settings by reducing sample rows and disabling expensive checks under pressure.
`build-medallion` uses strict local resource gating and returns a remote-execution recommendation
when the local build is unsafe. `plan-source-to-target` writes `resource_transform_settings` into
the generated plan, `generate-kpi-sql` includes the resource mode/strategy in SQL and blocks local
DuckDB generation when the plan requires remote execution, and local DuckDB execution records or
enforces the resource decision before subprocess launch.

### medallion apply-deploy

Command:

```powershell
uv run medallion apply-deploy --workspace workspaces/<project> --confirmed-by "<name>" [--dry-run]
```

Evaluates the five Databricks deployment gates from
`docs/prd/databricks_deployment.md` section 7 (G1 local-green, G2 design
ratified, G3 human provenance, G4 plan freshness, G5 remote approval env) and
prints a per-gate verdict table. NO remote call is ever made: on all-green it
records `interns/state/medallion/deploy_approval.json` (gate evidence +
provenance + plan hash) and stops at the approval boundary; any failing gate
exits nonzero with the blocking reasons. `--dry-run` never records. An empty
`--confirmed-by` fails G3 by design (Human-Gate Provenance Rule); agents must
never set `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` to satisfy G5.

### apply-design-panel-answer

Command:

```powershell
uv run apply-design-panel-answer --workspace workspaces/<project> `
  --item fact:<name>|dim:<name>|"rel:<from_table>.<from_col>-><to_table>.<to_col>" `
  --answer ratify --confirmed-by "<real name>" --reasoning "<why this is right>"
```

`medallion design` proposes a star schema with every fact, dimension and relationship marked
`needs_user_confirmation: true`; this is the human step that clears ONE of them, without
hand-editing `star_schema.json` or blanket-overriding the gate with `--force-with-blockers`. It
rewrites `interns/generated/medallion/star_schema.json` (+ `star_schema.md`) with `confirmed_by`,
`confirmed_at` and `confirmation_reasoning`, regenerates the design panel, and prints
`remaining_open_count`.

`ratify` is the only valid `--answer`. It refuses (nonzero exit, nothing written) when
`--confirmed-by` is agent-asserted, when `--reasoning` is empty -- a name-only ratification is
indistinguishable from a rubber stamp in an audit trail -- when the `--item` id does not match a
fact, dimension or relationship in the schema, and when there is no `star_schema.json` yet (run
`medallion design` first).

This is deliberately not a full dimensional-modeling review: it does not ask about SCD type, grain,
or history requirements. `confirmation_reasoning` is free text, and it is what the audit trail
records as having actually been reviewed.

### context-router

Command:

```powershell
uv run context-router build --workspace workspaces/<project> --task plan-source-to-target --budget standard
```

Builds a bounded context pack from canonical workspace artifacts without loading raw datasets into
chat or prompts. It writes a page index, JSONL page store, task manifest, and human wiki under:

```text
workspaces/<project>/interns/generated/context/context_index.json
workspaces/<project>/interns/generated/context/context_pages.jsonl
workspaces/<project>/interns/generated/context/manifests/<task>_<budget>.json
workspaces/<project>/interns/reports/context/<task>_<budget>.md
```

Use named budgets `small`, `standard`, or `deep`, optionally bounded further with
`--max-sections`, `--max-bytes`, and `--max-estimated-tokens`. The context layer is derived: source
artifacts such as profile indexes, KPI mappings, relationship contracts, source catalog selections,
resource evidence, and engine memory remain authoritative. `plan-source-to-target` now builds and
records a context manifest automatically.

### record-engine-evolution

Command:

```powershell
uv run record-engine-evolution --workspace workspaces/<project> --stage gold_kpi --engine polars --workload-signature csv_groupby --resource-mode local_streaming --elapsed-seconds 1.2
```

Records validated SQL/Polars/PySpark stage outcomes under
`interns/generated/memory/engine_evolution.json` and appends human-readable lessons to
`interns/generated/memory/evolution.md`. `plan-source-to-target --target-engine hybrid` reads these
lessons and records the current engine recommendation in the generated source-to-target plan.
Use the `--workload-shape-json`, `--decision-analysis-json`, `--bottlenecks-json`,
`--alternatives-json`, `--validation-json`, `--promotion-json`, and `--next-experiment-json` options
to store detailed learning evidence. The derived lesson keeps compact routing signals such as
workload family, common bottlenecks, rejected alternatives, confidence, promotion state, and next
experiment.

### prepare-wiki-memory

Command:

```powershell
uv run prepare-wiki-memory --workspace workspaces/<project> [--domain <domain>]
```

Collects this workspace's governed definitions (accepted workspace definitions, KPI definitions and
the like) into reviewable reuse cards, and merges them into the CROSS-workspace shared index. Every
workspace's run read-modify-writes that shared file, so it is taken under a named lock keyed on a
fixed sentinel rather than a per-workspace lock. Outputs:

```text
state/team_memory/wiki_memory_index.json
workspaces/<project>/interns/generated/memory/wiki_memory_candidates.json
workspaces/<project>/interns/reports/wiki_memory/current.json
workspaces/<project>/interns/reports/wiki_memory/current.md
```

Cards are SUGGESTIONS for review, not applied decisions: the result reports `card_count`,
`conflict_count` (a shared definition that disagrees with this workspace's) and `auto_fill_count`.
Errors when the workspace path does not exist, is the repo root itself, or is outside the repo root.

### ingest-source-catalog

Command:

```powershell
uv run source-catalog run --workspace workspaces/<project>
uv run ingest-source-catalog --workspace workspaces/<project>
```

Use after reviewing the source catalog plan and approving the workspace selection. API and local
sources write only under the workspace `datasets/` or `docs/` tree and create sidecar provenance
files with hashes. Databricks UC remains metadata-only and returns `planned_only` unless
`AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` is set; it does not mutate remote data.

### resolve-kpi-features

[deprecated] The default invocation now redirects to `prepare-kpi-blocker-panel`
(same workspace/repo-root/domain arguments). Only the `--apply-decision` and
`--apply-workspace-definition` debug modes still run stage logic directly;
prefer `apply-kpi-panel-answer` for those as well.

Command:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

Use when KPI/query features must be mapped to schema/profile evidence,
workspace definitions, derivation candidates, or user-confirmed blockers.
Derived-feature options must be emitted as strict JSON evidence objects with
formula, input columns, observed/profiled column values, worked example, source
files, semantic meaning sources, per-column reasons, derivation reasoning,
evidence state, confidence, and confirmation status. Prose-only derived-column
options are invalid. Semantically mismatched candidates must be rejected instead
of offered as selectable options.
The command also writes the standardized blocker question panel and includes
`question_panel_path` and `question_panel_markdown_path` in its JSON output. If
`blocked_kpi_count` is nonzero, read the markdown panel before asking the user.

Apply one KPI-specific decision:

```powershell
uv run resolve-kpi-features --workspace workspaces/<project> --apply-decision --kpi-id kpi_001 --feature FeatureName --evidence-note "..."
```

Apply one reusable workspace definition:

```powershell
uv run resolve-kpi-features --workspace workspaces/<project> --apply-workspace-definition --feature FeatureName --definition "..." --evidence-note "..."
```

### prepare-kpi-blocker-panel

Command:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

Preferred wrapper for KPI blocker preparation. It runs missing onboarding when needed, resolves KPI
features with candidates, renders derived-feature markdown, regenerates the blocker question panel,
and runs `validate-workspace-artifacts`. Agents should use this wrapper instead of hand-chaining the
lower-level commands when the next action is to ask a KPI blocker question. If validation fails, do
not ask the user; fix the parser/resolver or malformed artifact first.

### apply-kpi-panel-answer

Command:

```powershell
uv run apply-kpi-panel-answer --workspace workspaces/<project> --domain <domain> --answer option_a
uv run apply-kpi-panel-answer --workspace workspaces/<project> --domain <domain> --answer custom --custom-definition '<JSON>' --via-cli-agent
```

Use after the user answers a blocker question from
`interns/reports/blocker_question_panel/current.json` or `current.md`. It resolves friendly answers
such as `option_a`, `Option A: PaidAmount`, an exact label, or an unambiguous recommended answer
against `current.json`, applies the selected physical-column or derived-formula definition through
supported resolver APIs, then prepares and validates the next panel. Do not invent unsupported flags
such as `--accept-option`.

Pass `--via-cli-agent` when the orchestrating CLI agent is applying an answer derived from the
`cli_agent_evidence_pack` (the panel emitted when scored options ran out). With that flag the
mapping is recorded as `cli_agent_proposed` rather than `user_confirmed`; the user must then run
`confirm-cli-agent-proposal` to finalize the decision.

The command is idempotent: a deterministic op id is derived from the arguments, and a second call
with the same arguments returns the prior result instead of duplicating decision history. Pass
`--allow-replay` to force re-execution.

### apply-kpi-definition

Command:

```powershell
uv run apply-kpi-definition --workspace workspaces/<project> --kpi-id kpi_004 --metric "count(distinct Id)" --cuts "PAYER_COVERAGE = 0" --confirmed-by "<reviewer>"
uv run apply-kpi-definition --workspace workspaces/<project> --business-question "<question text>" --metric "avg(BASE_COST)" --cuts "DESCRIPTION" --confirmed-by "<reviewer>"
```

Governed write-back for a human-confirmed KPI definition when the source row
left `metric`/`cuts` empty and the blocker panel reports "definition help"
(blocked KPIs with no answerable feature question). Records the decision in
`interns/generated/decisions/kpi_definitions.json` keyed by the business
question; onboarding re-applies accepted decisions on every registry rebuild.
An empty `--confirmed-by` records the entry as agent-asserted per the
human-gate provenance rule. Re-run `prepare-kpi-blocker-panel` afterwards.

Outputs:

```text
workspaces/<project>/interns/generated/decisions/kpi_definitions.json
```

### confirm-cli-agent-proposal

Command:

```powershell
uv run confirm-cli-agent-proposal --workspace workspaces/<project> --feature <feature> --decision confirm
uv run confirm-cli-agent-proposal --workspace workspaces/<project> --feature <feature> --decision reject --note "<why>"
```

Use as the second step of the CLI-agent proposal flow. When `apply-kpi-panel-answer --via-cli-agent`
records a mapping as `cli_agent_proposed`, the KPI stays in the blocked state until the user
confirms or rejects. `--decision confirm` flips the recorded mapping to `user_confirmed`;
`--decision reject` flips it to `cli_agent_rejected` and reverts the affected KPI rows to
`blocked_missing_evidence` so the next `prepare-kpi-blocker-panel` re-asks. The CLI agent should
never run this command on the user's behalf without explicit direction.

### derived-feature-markdown

[deprecated] The default invocation now redirects to `prepare-kpi-blocker-panel`,
which regenerates derived-feature markdown as part of the panel build. Stage-only
flags (`--mapping`, `--out`, `--no-strict`) keep the legacy single-stage behavior
for debugging.

Command:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

Use after feature resolution when business analysts,
product leads, or stakeholders need readable Markdown review files for strict
derived-feature JSON options. The converter validates required fields by
default and writes separated `.md` and `.json` files under:

```text
workspaces/<project>/interns/reports/derived_feature_reviews/md/
workspaces/<project>/interns/reports/derived_feature_reviews/json/
```

Multiple options for the same KPI feature are kept together in one Markdown file
and one JSON file.

### blocker-question-panel

[deprecated] The default invocation now redirects to `prepare-kpi-blocker-panel`,
which runs feature resolution, panel build, and validation atomically. Stage-only
flags (`--mapping`, `--out`) keep the legacy single-stage behavior for debugging.

Command:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

Use whenever an agent needs to
ask a stakeholder a KPI blocker question. This is mandatory for direct mappings,
source-of-truth choices, aliases, reusable workspace definitions, and
derived-feature questions. The tool writes a stable question panel with the
blocker, reuse scope, recommended answer, reason, answer shape, and JSON-backed
derived-feature option when one is valid. If no valid derived option exists, the
panel asks for a direct mapping, source-origin rule, data dictionary evidence, or
workspace business definition instead of inventing formula choices.

Outputs:

```text
workspaces/<project>/interns/reports/blocker_question_panel/current.json
workspaces/<project>/interns/reports/blocker_question_panel/current.md
workspaces/<project>/interns/reports/blocker_question_panel/index.json
```

### prepare-phi-review-panel

Command:

```powershell
uv run prepare-phi-review-panel --workspace workspaces/<project>
```

PHI/PII detection is otherwise automatic and silent -- a column gets `is_sensitive` in
`semantic_contract.json` with no human step in between. This is that missing step. It lists every
column flagged sensitive in `interns/generated/contracts/semantic_contract.json`, minus columns
already dispositioned in `interns/generated/contracts/phi_disposition.json` and columns already in
`data_policy.json`'s `not_sensitive_columns` allowlist (consulted directly, so an answer takes
effect immediately even though the contract snapshot is stale). Writes:

```text
workspaces/<project>/interns/reports/phi_review_panel/current.json
workspaces/<project>/interns/reports/phi_review_panel/current.md
```

Status is `needs_user_answer` or `no_pending_columns`.

### apply-phi-review-answer

Command:

```powershell
uv run apply-phi-review-answer --workspace workspaces/<project> --column <name> `
  --answer hash_to_key|pass_through_and_tag|bronze_only|not_sensitive --confirmed-by "<real name>"
```

Records one human answer for one column. `not_sensitive` appends the column to `data_policy.json`'s
`not_sensitive_columns` allowlist (plus a `not_sensitive_columns_confirmed_by` provenance entry) --
written on the human's behalf only after an explicit answer, never silently. The three dispositions
are recorded in `interns/generated/contracts/phi_disposition.json`: `hash_to_key` (irreversible
hash, join-key use only), `pass_through_and_tag` (kept readable, governed by catalog-layer masking
downstream), `bronze_only` (never selected past bronze).

Refuses (nonzero exit, nothing written) when `--confirmed-by` is blank or resolves to an agent
identity. PHI classification is high-sensitivity enough that this gate does NOT accept a persisted
default identity -- every answer needs a real name on the command itself.

### validate-workspace-artifacts

Command:

```powershell
uv run validate-workspace-artifacts --workspace workspaces/<project>
```

Use after `onboard-workspace`, `resolve-kpi-features`, `derived-feature-markdown`, or
`blocker-question-panel` before an agent relies on generated contracts. It validates generated JSON
shape, KPI registry provenance, feature-mapping summary fields, strict derived-feature evidence,
profile-backed physical-column option evidence, and whether blocked KPIs have a current question
panel. It also gates on blocking workspace product bugs detected by the shared bug detector. It is
read-only and exits nonzero on schema/format errors or Critical/High workspace bugs.

### prepare-workspace-bug-report

Command:

```powershell
uv run prepare-workspace-bug-report --workspace workspaces/<project>
```

Use when workspace selection, onboarding, validation, or kickstart behavior contradicts the evidence
available in the workspace. It writes a structured JSON bug report plus a human-readable Markdown
report. The first detector rule catches the dangerous case where `list-workspace-files` finds
dataset/KPI/data-model evidence but onboarding generates empty input, profile, or KPI artifacts.

Outputs:

```text
workspaces/<project>/interns/generated/evidence/bug_report.json
workspaces/<project>/interns/reports/bugs/current.md
```

### validate-git-hygiene

Command:

```powershell
uv run validate-git-hygiene
```

Use before commits. By default it checks staged files and blocks raw data extensions, oversized
files, generated workspace output under `workspaces/**/interns/`, runtime state, logs, and local
databases. Use `--all` for a broader tracked/untracked audit and `--max-mb` to override the default
25 MB file-size threshold.

### generate-kpi-sql

Command:

```powershell
uv run generate-kpi-sql --workspace workspaces/<project> --kpi-id kpi_001
```

Use only after required KPI features are proven or user-confirmed and the selected source datasets,
joins, grain, filters, and date anchors match the data model/profile evidence. If the user asks for
Polars, PySpark, ETL/ELT, or medallion-layer loading, first produce or inspect a data-model-backed
source-to-target plan; do not translate KPI text directly into executable code.

### plan-source-to-target

Command:

```powershell
uv run plan-source-to-target --workspace workspaces/<project> --target-engine sql
```

Use before generating SQL, Polars, PySpark, or medallion/ETL logic. It reads the KPI feature mapping,
domain model, and profiles, then writes:

```text
workspaces/<project>/interns/generated/contracts/source_to_target_plan.json
workspaces/<project>/interns/reports/source_to_target_plan.md
workspaces/<project>/interns/generated/context/manifests/plan-source-to-target_standard.json
workspaces/<project>/interns/reports/context/plan-source-to-target_standard.md
```

The plan records selected and rejected datasets, feature-to-column mappings, join candidates, grain,
temporal anchors, medallion layers, validation checks, resource settings, context manifest, and
blockers. Treat blockers as hard stops before executable code generation.

### prepare-data-engineering-route

Command:

```powershell
uv run prepare-data-engineering-route --workspace workspaces/<project> --track auto --target-engine sql
```

Use before pipeline planning when a workspace needs a governed route choice across KPI-only, ETL,
ELT, medallion, OLTP ingestion, or existing-gold validation workflows. It ensures the catalog
contract exists, inspects trusted local layer contracts, records a local-first remote policy, and
writes the next deterministic `prepare-pipeline-plan` command. It is local-safe and does not execute
pipeline code or remote mutations.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/data_engineering_route.json
workspaces/<project>/interns/reports/data_engineering_route.md
```

### prepare-pipeline-plan

Command:

```powershell
uv run prepare-pipeline-plan --workspace workspaces/<project> --track auto --target-engine sql --table-format auto
```

Use after `prepare-data-engineering-route` and source-to-target planning when executable SQL,
Polars, PySpark, ETL/ELT, medallion, or existing-layer validation work needs a governed pipeline
contract before code generation. It ensures the catalog contract exists, reuses the route contract,
records layer definitions, quality gates, approval-gated transformations, source-to-target blockers,
and remote-write approval policy. For ETL/ELT/medallion/ingestion tracks, `--table-format auto`
blocks and writes the pipeline format panel until the user chooses a storage format. It is
local-safe and writes planning artifacts only.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/pipeline_plan.json
workspaces/<project>/interns/reports/pipeline_plan.md
```

### prepare-pipeline-format-panel

Command:

```powershell
uv run prepare-pipeline-format-panel --workspace workspaces/<project>
uv run apply-pipeline-format-answer --workspace workspaces/<project> --answer option_a
```

Use before `prepare-pipeline-plan` for ETL, ELT, medallion, or ingestion tracks when the target
table/file format is not already approved. The panel asks whether medallion outputs should be stored
as Delta (`option_a`, recommended) or local Parquet (`option_b`). The panel is JSON-backed; agents must read
`interns/reports/pipeline_format/current.md` or `current.json` before asking the user and must apply
answers through `apply-pipeline-format-answer`.

Outputs:

```text
workspaces/<project>/interns/reports/pipeline_format/current.json
workspaces/<project>/interns/reports/pipeline_format/current.md
workspaces/<project>/interns/generated/contracts/pipeline_decisions.json
```

### apply-pipeline-format-answer

Command:

```powershell
uv run apply-pipeline-format-answer --workspace workspaces/<project> --answer option_a
```

Records the chosen table format into
`workspaces/<project>/interns/generated/contracts/pipeline_decisions.json` with the reason
`Accepted <answer>`. `option_a` / `delta` / `Delta` record `delta`; every other value records
`local_parquet` -- there is no third format and no rejection path, so pass an option id from the
panel rather than free text. `--allow-replay` re-runs an already-recorded identical answer.

This command takes no `--confirmed-by`: the format decision is recorded without human provenance.
If the choice needs to be attributable, capture it in the workspace decision record separately.

### prepare-bronze-silver-standards

Command:

```powershell
uv run prepare-bronze-silver-standards --workspace workspaces/<project> [--domain <domain>]
```

Writes the layer-responsibility baseline the medallion harnesses check against -- what each layer
may and may not do (bronze preserves source fidelity; silver does type/timestamp/null/naming
normalization plus approved conformance; gold owns KPI formulas and business aggregation) -- plus
the cross-engine parity policy (`sql`, `polars`, `pyspark`, blocking on an unsupported rule) and the
workflow reroute rules. Deterministic; asks nothing. Outputs:

```text
workspaces/<project>/interns/generated/contracts/bronze_silver_standards.json
workspaces/<project>/interns/generated/contracts/transformation_manifest.json
workspaces/<project>/interns/generated/contracts/workflow_reroute_policy.json
workspaces/<project>/interns/reports/bronze_silver_standards.md
```

### prepare-pipeline-deployment-plan

Command:

```powershell
uv run prepare-pipeline-deployment-plan --workspace workspaces/<project> --target warehouse --mode dry-run
```

Use after `prepare-pipeline-plan` when generated pipeline outputs need a deployment dry-run or an
approval-backed apply contract. The command reads `catalog_contract.json` and `pipeline_plan.json`,
writes planning artifacts only, records `remote_writes_require_explicit_approval=true`, and never
performs remote mutation. `--mode` defaults to `dry-run`; `--mode apply` fails for `external` and
`warehouse` targets unless `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` is set.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/pipeline_deployment_plan.json
workspaces/<project>/interns/reports/pipeline_deployment_plan.md
```

### apply-pipeline-decision

Command:

```powershell
uv run apply-pipeline-decision --workspace workspaces/<project> --kpi-id kpi_002 --percentage-denominator-scope global_total
```

Use only after the user or source truth approves a percentage or ratio denominator scope. The
command writes `pipeline_decisions.json`, which `prepare-pipeline-plan` uses to unblock denominator
scope blockers. Supported scopes are `global_total`, `within_department`, `within_gender`,
`within_visit_type`, and `selected_population`.

Output:

```text
workspaces/<project>/interns/generated/contracts/pipeline_decisions.json
```

### generate-pipeline-sql

Command:

```powershell
uv run generate-pipeline-sql --workspace workspaces/<project>
```

Use after catalog, route, and pipeline contracts are ready. It emits local DuckDB SQL for
bronze/silver/gold layer scaffolds from `pipeline_plan.json`. Raw file paths are allowed only in
the generated catalog bootstrap section; downstream layer logic reads catalog-bound views. The
command generates code only and does not execute it or write remote tables.

Output:

```text
workspaces/<project>/interns/generated/pipeline/pipeline_layers.sql
```

### build-relationship-contracts

Command:

```powershell
uv run build-relationship-contracts --workspace workspaces/<project>
```

Use before multi-dataset SQL, Polars, PySpark, ETL/ELT, medallion generation, or production KPI
proof. It writes a production-grade FK/relationship contract with data-model evidence, profile
evidence, confidence, approval state, cardinality/null/uniqueness/referential-integrity checks,
grain impact, source-system scope, lifecycle review dates, promotion policy, and executable usage
policy.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/relationship_contracts.json
workspaces/<project>/interns/reports/relationship_contracts.md
```

Only relationships with executable-approved states such as `proven_data_model` or `user_confirmed`
may be used by trusted executable generation. Profile-only relationships remain advisory
`profile_validated` candidates and should trigger blocker grilling before SQL/code generation.

### workspace-dashboard

Commands (server lifecycle):

```powershell
# START (default = --live): regenerate (DQ-gated) + serve; writes a PID file.
uv run workspace-dashboard --workspace workspaces/<project> --live              # http://127.0.0.1:8060
# STOP / KILL: terminate the running server (PID file, fallback scan of --port). Idempotent, cross-platform.
uv run workspace-dashboard --workspace workspaces/<project> --stop
# ON-DEMAND REFRESH (new data landed): regenerate-and-exit, DQ-gated (bad load -> last-good kept).
uv run workspace-dashboard --workspace workspaces/<project> --refresh
# LIVE AUTO-PICKUP: running server re-reads data every N seconds.
uv run workspace-dashboard --workspace workspaces/<project> --live --refresh-seconds 300
# QA visual screener: headless screenshot of every page + deterministic checks.
uv run workspace-dashboard --workspace workspaces/<project> --screen
```

**Live vs static — read first.** The dashboard IS the live `--live` app (a served process). The
standalone `--export` flag was removed; the live app is the single deliverable. Any
`dashboard/exports/*.html` still on disk are **legacy leftover artifacts** from the old static
renderer — stale, never updated by current code, and NOT the dashboard. Always serve `--live`; do
not open those HTML files. The live app builds from the workspace's `interns/` bronze/gold
(gitignored), so a fresh clone must run the pipeline before it can serve.

**New-data flow:** `new raw data -> re-run KPI pipeline (refresh bronze/gold) -> --refresh (or
--refresh-seconds picks it up) -> view updates`. A **data** refresh needs no restart (config-watcher
+ atomic spec writes); a **code/schema change** needs `--stop` then `--live` (a running process
keeps its old Python classes in memory — the cause of stale `Extra inputs are not permitted`
config errors).

The per-workspace BI dashboard: clickable KPI tile strip with status badges,
per-panel view toggles, charts chosen by the data-to-viz knowledge base
(`core/dashboard/chart_knowledge.py`; every panel spec records
`selection_reason`/`selection_source`), and a display-redacted Data table per
KPI. KPI completion exports and opens it automatically.

`--screen` is the visual screener: exports, screenshots every page (headless
Edge/Chrome), runs deterministic checks (render failures, blank pages, missing
or unredacted data viewer, palette delta-E / contrast), writes
`interns/reports/dashboard_screener/current.{json,md}`, and stages the
screenshots under `.../dashboard_screener/shots/` for the agent's vision
review (misalignment, color mismatch, visual defects). Exits nonzero on
findings. KPI completion runs it automatically (skip with
`AUTORESEARCH_SCREEN_DASHBOARD=0`).

### dashboard-verify

```powershell
uv run dashboard-verify <url-or-file> --screenshot out.png
```

Single-page DOM gate via agent-browser: chart render counts, container
overflow, legend presence, perceptual color-clash (delta-E) and contrast
checks. The screener supersedes it for whole-board sweeps; keep it for
one-page debugging.

### apply-relationship-answer

Command:

```powershell
uv run apply-relationship-answer --workspace workspaces/<project> --relationship-id <relationship_id> --answer approve
```

Use after a user approves, rejects, or keeps blocked a relationship from
`interns/reports/relationship_contracts.md`. This is the supported lock-aware path for relationship
approval. Do not edit `relationship_contracts.json` by hand. The command appends decision history,
updates approval state, recomputes executable/candidate summary counts, and keeps portable
repo-relative paths.

Supported answers:

```text
approve
reject
keep_blocked
```

### cleanup-workspace-references

Dry run:

```powershell
uv run cleanup-workspace-references --workspace workspaces/<project> --all-references
```

Apply:

```powershell
uv run cleanup-workspace-references --workspace workspaces/<project> --all-references --apply --confirm-delete workspaces/<project>
```

Use when a workspace needs a fresh start and stale generated references must be
removed from `workspaces/<project>/interns`, repo runtime state, Databricks
deployment state, and task config. It must not remove workspace `docs/` or
`datasets/`. Any deletion path requires `--confirm-delete` with the exact
workspace path after reviewing the dry run.

### loop

Command:

```powershell
uv run loop --task <task-id>
```

Use to run the governed optimization loop. Remote execution is approval-gated.

### profiler.py

Command:

```powershell
uv run python tools/profiler.py --input <path> --pct 5 --engine auto --out <dir>
```

Use for sampling, profiling, representation checks, null audits, distribution
checks, and model-transfer diagnostics. Prefer generated profile artifacts when
they already answer the question.

### optimizer_finder.py

Command:

```powershell
uv run python tools/optimizer_finder.py --target <file.sql|file.py> --mode auto
```

Use when SQL or Python is slow, timing out, or needs hotspot evidence.

### methodology_parser.py

Command:

```powershell
uv run python tools/methodology_parser.py --doc <file> --out <schema.json>
```

Use when a methodology document, data dictionary, or contract must be converted
into semantic schema JSON.

### prepare-databricks-assets

Command:

```powershell
uv run prepare-databricks-assets --workspace workspaces/<project> `
  [--environment dev|stage|prod] [--domain <domain>] [--catalog <name>] [--schema <name>] `
  [--workspace-root /Workspace/Autoresearch]
```

Turns `interns/generated/profiles/profile_index.json` into a registration manifest at
`workspaces/<project>/interns/generated/requirements/databricks_asset_manifest.json`: one entry per
profiled dataset with its source path/format and target `catalog.schema.table` FQN, plus the
workspace asset layout under `<workspace-root>/<environment>/<domain>`. `--schema` defaults to
`--domain`; duplicate table stems are disambiguated.

It registers nothing and uploads nothing. Every asset is written with
`registration_state: requires_user_approved_upload_or_table_registration` and the manifest carries
`approval_required: true` -- remote registration and workspace deployment stay behind explicit
approval, and local/DuckDB execution is a developer smoke test, not enterprise evidence.

### prepare-genie-workspace

Command:

```powershell
uv run prepare-genie-workspace --workspace workspaces/<project> `
  [--manifest-path <path>] [--environment dev] [--domain <domain>] [--catalog <name>] `
  [--schema <name>] [--workspace-root /Workspace/Autoresearch]
```

Turns the asset manifest into a reviewable Genie deployment spec, an operator runbook, and
evolution memory. It builds the manifest first (via `prepare-databricks-assets`) when
`--manifest-path` is absent and none exists yet. Mutates no Databricks workspace. Outputs:

```text
workspaces/<project>/interns/generated/requirements/genie_workspace_spec.json
workspaces/<project>/interns/reports/genie_operator_runbook.md
workspaces/<project>/interns/generated/memory/genie_workspace_decisions.json
workspaces/<project>/interns/generated/memory/evolution.md
workspaces/<project>/interns/generated/memory/lessons.json
```

The spec covers workspace folders, Genie spaces and their starter prompts, and the role/action
permission matrix; the result reports a count for each. Errors when the workspace path does not
exist, is the repo root itself, or is outside the repo root.

### Databricks Tools

Commands:

```powershell
uv run prepare-databricks-assets --workspace workspaces/<project>
uv run prepare-genie-workspace --workspace workspaces/<project>
uv run deploy-databricks-workspace --workspace workspaces/<project>
uv run python tools/databricks_setup.py
```

Use Databricks setup/deployment tools only when the user explicitly asks for
Databricks validation, planning, or approved remote mutation. Do not run remote
execution just because credentials exist.

### generate-skill-adapters

Command:

```powershell
uv run generate-skill-adapters
```

Use after changing `skills/*/SKILL.md` or cross-tool skill routing.

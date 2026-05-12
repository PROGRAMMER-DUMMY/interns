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
4. `program.md` only when the active benchmark/task refers to it.
5. Relevant files in `core/`, `tools/`, `interns/`, `tests/`, or `workspaces/<project>/`.

## Step 0: Active Workflow Setup

Before onboarding, optimizing, refactoring, or writing outputs, establish the active workflow.

Ask:

```text
What do you want to do with this project right now, and which workspace/files should I treat as active?
```

Then scan likely sources:

```powershell
git status --short
rg --files workspaces config core tools tests
```

Summarize the likely active set:

```text
I found this likely workflow:
- Workspace: workspaces/<project>
- KPI registry: ...
- Data model: ...
- Source artifact: ...
- Evaluator/runner: ...
- Task config: ...

Should I use these files for this workflow?
```

After the user confirms, continue with the appropriate skill flow.

For fresh KPI/query workspaces, the standard onboarding command is:

```powershell
uv run onboard-workspace --workspace workspaces/<project>
```

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
- `core/optimization/`: planning, memory, diff classification, decision strategy.
- `core/profiling/`: data model profiling and downcast diagnostics.
- `core/agents/`: intern routing and LLM engine abstractions.
- `core/observability/`: metric parsing and telemetry.
- `core/storage/`: SQLite/Git workspace state and workspace layout.
- `tools/`: CLI utilities.
- `interns/`: built-in intern agents.
- `tests/`: unit and benchmark harnesses.
- `workspaces/<project>/`: user/project input.

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

Requirement discovery, grill-me interviews, stakeholder conversations, task choices,
assumptions, and accepted recommendations are project artifacts. Save them under the
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
- `to-solution-brief`: turn interview decisions into a concrete implementation brief.
- `task-onboarding`: convert project inputs into task config, contracts, profiles, and baseline plan.
- `workspace-kpi-query-optimizer`: build, validate, baseline, and optimize KPI/query logic for any workspace.
- `workspace-governance`: keep outputs, data, and git staging safe.
- `evolution`: record accepted decisions, rejected assumptions, lessons, and future optimization hints.

For KPI/query work, apply the skill chain in this order:

```text
workspace-governance
  -> domain-model
  -> task-onboarding / workspace-kpi-query-optimizer
  -> clarify-ambiguity only for unresolved high-impact mappings
  -> grill-requirements when business interpretation must be chosen
  -> stakeholder-memory for accepted preferences and definitions
  -> to-solution-brief for implementation direction
  -> evolution after runs
```

Resolve KPI terms from registry, data model, profiles, data dictionaries, metadata files, catalog
metadata, then user clarification. If a required dictionary or metadata file is missing, ask for it
and save the request under the active workspace's `interns/reports/open_questions.md`.

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

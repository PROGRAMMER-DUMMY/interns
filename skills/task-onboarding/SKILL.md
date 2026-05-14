---
name: task-onboarding
description: >
  Turn a workspace project with data, KPI registry, data model, and source artifacts into a runnable
  optimization task. Use when adding a new project under workspaces/ or refreshing task config,
  contracts, profiles, and baseline setup.
---

# Task Onboarding

Convert project inputs into the artifacts the optimization loop needs.

## Step 0: Active Workflow Setup

Do not assume the active project from file names alone.

1. Ask what the user wants to do.
2. Ask them to point to the active workspace/project if it is not clear.
3. Scan likely files:

```powershell
rg --files workspaces config tests tools core
```

4. Identify likely:
   - workspace root
   - KPI registry
   - data model
   - source artifact to optimize
   - evaluator/runner
   - task config
5. Ask the user to confirm the selected files.

Continue only after confirmation, unless the user explicitly asks for best-effort onboarding.

## Steps

1. Identify project root under `workspaces/<project>/`.
2. Ensure all generated outputs go under `workspaces/<project>/interns/`.
3. Read KPI registry and data model.
4. Run or prepare data profiling.
5. Generate semantic/domain contract drafts.
6. Create or update `config/tasks.json`.
7. Define experiment and evaluator commands.
8. Document missing blockers in `interns/reports/open_questions.md` and, when useful, structured
   metadata.
9. Keep executable KPI/query logic blocked until mappings and derivations are backed by evidence or
   explicit user decisions.

Use Polars for dataframe/file inspection during onboarding. Do not introduce
pandas-based profiling or helpers unless a third-party API explicitly requires
pandas; document that exception if it happens.

## Outputs

```text
workspaces/<project>/interns/generated/contracts/
workspaces/<project>/interns/generated/profiles/
workspaces/<project>/interns/generated/requirements/
workspaces/<project>/interns/reports/open_questions.md
```

Do not commit raw workspace data or generated intern outputs.

# Workspace Conversation Scenarios

This guide describes how agents should handle the local RCM workspace currently used in this repo.
It is conversation-oriented: the agent runs the local-safe commands, reads the generated panels,
summarizes the result, and asks the user for the next business decision.

Agents should not ask the user to run commands manually unless tool access is unavailable.

## Common Conversation Rules

1. Confirm the active workspace from a bounded file listing.
2. Treat the listed file set as the confirmation boundary.
3. For fresh KPI/query workspaces, run onboarding or the relevant preparation wrapper after
   confirmation.
4. Ask from generated panels, not from freehand guesses.
5. Record accepted answers through the supported apply command.
6. Validate generated artifacts after onboarding, blocker preparation, or generated artifact writes.
7. Do not generate SQL, Polars, PySpark, ETL, or medallion logic until feature mappings,
   relationship contracts, grain, and source-to-target assumptions are proven or user-confirmed.
8. Treat huge external data roots, such as CMS cold storage, as bounded-listing-only until the user
   confirms a narrow folder or file allowlist with a business reason.
9. Before committing, run `uv run validate-git-hygiene` to block raw data, generated workspace
   outputs, state directories, logs, local databases, and oversized files.

## Huge External Data Roots

External roots such as `D:/Cold_Storage` are not workspaces and must not be copied into this repo.
Use `config/external_data_roots.example.json` as the tracked policy template and keep actual
machine-specific roots in ignored local config or environment variables.

Default behavior for huge external roots:

1. Allow only bounded metadata listing before user confirmation.
2. Do not recursively scan the whole root.
3. Do not read file contents, profile, sample, copy, move, delete, or commit raw files.
4. Ask the user to approve a specific folder or file allowlist.
5. Store workspace-approved dataset allowlists under
   `workspaces/<project>/interns/state/workspace_settings.json`.

Example workspace setting:

```json
{
  "dataset_allowlist": [
    {
      "type": "external_absolute",
      "path": "D:/Cold_Storage/<approved-folder>",
      "reason": "Approved CMS source slice for the current KPI workflow"
    }
  ]
}
```

## Workspace: Healthcare-RCM-Data-Platform

Expected source inputs:

```text
workspaces/Healthcare-RCM-Data-Platform/docs/Sample KPI.xlsx
workspaces/Healthcare-RCM-Data-Platform/docs/DataModel.png
workspaces/Healthcare-RCM-Data-Platform/datasets/
```

Generated state such as `interns/` and workspace `wiki/` may be absent after a fresh cleanup.
When absent, treat this as a fresh KPI/query workspace with existing KPI and data model inputs.

### Scenario A: User Selects The RCM Workspace

User intent examples:

```text
set Healthcare-RCM-Data-Platform
use the RCM workspace
start fresh with healthcare rcm
```

Agent behavior:

1. Run a bounded workspace listing.
2. Summarize the full active file set:
   - workspace path
   - possible KPI file
   - possible data model file
   - dataset roots
   - existing generated state, if any
3. Ask:

```text
Should I use these files for this workflow?
```

If the user confirms and generated artifacts are missing, continue with onboarding. The agent should
say that onboarding will generate profiles, contracts, normalized KPI registry, feature mapping,
open questions, evaluation scaffolding, and evidence artifacts under `interns/`.

### Scenario B: Existing KPI + Data Model Workflow

Use when the user wants to work from `Sample KPI.xlsx` and `DataModel.png`.

Agent behavior:

1. Run onboarding.
2. Run artifact validation.
3. Run KPI generation preparation so the user can still choose between KPI generation and usual
   workflow.
4. Read the generated KPI generation panel.
5. Ask from the panel:

```text
Blocker/choice: The workspace has existing KPI and data model inputs.

Question: Do you want to use the existing KPI/data-model workflow or revise the KPIs first?

Options:
- KPI generation / BA-product interview
- Usual workflow / onboard existing KPI + data model

Recommended answer: Use the recommendation from the generated panel.
```

If the user chooses usual workflow, proceed to blocker preparation. Ask only from
`interns/reports/blocker_question_panel/current.md` or `current.json`.

### Scenario C: KPI Generation / BA Product Interview

Use when the user says they want better KPIs, wants to challenge the Excel, wants product/BA
questions, or asks for a stakeholder interview.

Agent behavior:

1. Prepare KPI generation.
2. Read the generated panel.
3. Ask the next panel question in business language.
4. Apply the user answer through the KPI generation apply command.
5. Continue until the generated workflow produces a final preview.
6. Finalize only after explicit user approval of the final preview.

Do not overwrite user-facing KPI docs from a draft.

### Scenario D: Missing Or Weak Data Model

Use if `DataModel.png` is insufficient, image-only, contradictory, or cannot prove relationships.

Agent behavior:

1. Prepare data-model generation.
2. Read the generated data-model panel.
3. Ask from that panel.
4. Apply the answer through the data-model apply command.
5. Finalize only after explicit preview approval.
6. Build relationship contracts after the data model is accepted.

If relationships remain profile-only, treat them as advisory and ask for user confirmation before
executable generation.

### Scenario E: User Asks For SQL, ETL, Or Medallion Output

Agent behavior:

1. Check onboarding artifacts and validate them.
2. Prepare blocker panel if mappings are unresolved.
3. Build relationship contracts.
4. Plan source-to-target for the requested target engine.
5. If the plan has blockers, ask from the generated report or panel.
6. Generate executable logic only after blockers are resolved.

The agent should not generate executable logic from column-name similarity alone.

## Fresh Cleanup Conversation

If the user asks to start over:

1. Run the cleanup tool in dry-run mode.
2. Show the exact delete plan.
3. Ask for explicit approval before deletion.
4. Apply only the approved cleanup boundary.
5. Confirm what remains.

Cleanup may remove generated workspace output such as `interns/`. It must not delete `docs/` or
`datasets/` unless the user explicitly requests those paths.

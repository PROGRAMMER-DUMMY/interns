# Workspace Conversation Scenarios

This guide describes how agents should handle the two local workspaces currently used in this
repo. It is conversation-oriented: the agent runs the local-safe commands, reads the generated
panels, summarizes the result, and asks the user for the next business decision.

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

## Workspace: Medicare Part D Prescribers - by Provider

Expected source inputs:

```text
workspaces/Medicare Part D Prescribers - by Provider/MUP_DPR_RY25_20250401_DD_PRV_508.pdf
workspaces/Medicare Part D Prescribers - by Provider/MUP_DPR_RY25_20250401_Methodology_508.pdf
workspaces/Medicare Part D Prescribers - by Provider/MUP_DPR_RY25_P04_V10_DY23_NPI.csv
```

This workspace starts differently from the RCM workspace. It has a CSV plus public documentation,
but no obvious KPI registry and no explicit data model file.

### Scenario F: User Selects The Medicare Workspace

User intent examples:

```text
set Medicare Part D
use the Medicare prescribers workspace
start the provider workspace
```

Agent behavior:

1. Run a bounded workspace listing.
2. Summarize that the workspace has PDF methodology/data-dictionary inputs and one CSV dataset.
3. State that no existing KPI registry or data model file was found.
4. Ask for confirmation that this is the active file set.

After confirmation, treat it as a fresh workspace that likely needs KPI generation and data-model
generation rather than the usual existing-KPI workflow.

### Scenario G: KPI Discovery From Methodology Docs

Use when the user wants KPIs, measures, prompts, or analytical questions from the Medicare files.

Agent behavior:

1. Onboard the workspace.
2. Validate artifacts.
3. Prepare KPI generation with the methodology/data-dictionary PDFs as context when supported.
4. Ask from the generated KPI generation panel.
5. Apply answers through the KPI generation workflow.
6. Finalize only after explicit final-preview approval.

The expected conversation is exploratory: the user may need to choose business goals such as cost,
prescribing behavior, provider comparison, drug utilization, geography, specialty, or outlier
detection before executable KPI logic exists.

### Scenario H: Data Model Discovery From CSV And PDF Dictionary

Use when the user asks for schema, relationships, feature definitions, or governed modeling.

Agent behavior:

1. Prepare data-model generation.
2. Ask from the generated data-model panel.
3. Apply the selected answer.
4. Finalize only after preview approval.

Because this workspace appears to have a single primary CSV, relationship contracts may be simple or
empty. The agent should still validate grain, identifiers, time period, provider identity, and any
derived dimensions before generating SQL.

### Scenario I: User Asks For Analysis Or SQL Immediately

Agent behavior:

1. Explain that the workspace lacks an accepted KPI registry and data model.
2. Run the preparation workflow instead of writing SQL immediately.
3. Ask the first generated business question.
4. After accepted KPI/data-model decisions, plan source-to-target.
5. Generate SQL only if the plan has no blockers.

## Fresh Cleanup Conversation

If the user asks to start over:

1. Run the cleanup tool in dry-run mode.
2. Show the exact delete plan.
3. Ask for explicit approval before deletion.
4. Apply only the approved cleanup boundary.
5. Confirm what remains.

Cleanup may remove generated workspace output such as `interns/`. It must not delete `docs/` or
`datasets/` unless the user explicitly requests those paths.

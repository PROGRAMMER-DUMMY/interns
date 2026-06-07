# Workspace Onboarding Bug Log

Date: 2026-05-19

Scope: Fresh client-style workflow for `workspaces/Hospital_Patient_Records`.

## Summary

The workspace selection screen now correctly detects root-level client files, but downstream onboarding still does not consume those same files. This creates a broken handoff: the user sees the right KPI/data-model evidence at selection time, then onboarding generates empty artifacts with `kpi_count: 0` and `profile_count: 0`.

## BUG-001: Onboarding Ignores Root-Level Workspace Inputs

Severity: Critical

Status: Open

Finding:
`list-workspace-files` detects root-level files correctly:

- KPI input: `hospital_analytics_questions.sql`
- Data model inputs: `create_hospital_db.sql`, `data_dictionary.csv`
- Dataset root: `workspaces/Hospital_Patient_Records`
- Dataset files: `patients.csv`, `encounters.csv`, `organizations.csv`, `payers.csv`, `procedures.csv`

But `onboard-workspace` reports:

```json
{
  "data_files": [],
  "kpi_registries": [],
  "data_models": [],
  "kpi_count": 0,
  "profile_count": 0
}
```

How It Is Created:

1. Start fresh by deleting `workspaces/Hospital_Patient_Records/interns`.
2. Run:

```powershell
uv run list-workspace-files --workspace workspaces/Hospital_Patient_Records
```

3. Confirm the workspace.
4. Run:

```powershell
uv run onboard-workspace --workspace workspaces/Hospital_Patient_Records
```

5. Observe that onboarding outputs empty discovered inputs even though the listing found valid root-level files.

Expected Behavior:
`onboard-workspace` should reuse the same discovery/classification logic as `list-workspace-files`:

- Root CSV files should become `data_files`.
- `hospital_analytics_questions.sql` should become KPI/context input.
- `create_hospital_db.sql` and `data_dictionary.csv` should become data model/context inputs.
- Profiles should be generated for root CSV datasets.

Impact:
Fresh client workspaces that use a flat folder layout appear valid during selection but become empty during onboarding. This blocks KPI generation, profiling, feature mapping, data-model proof, and downstream SQL/Polars/PySpark generation.

Suspected Cause:
`list-workspace-files` has newer root-level classification logic, while `WorkspaceOnboarder` still appears to rely on older `docs/` and `datasets/` folder assumptions.

Fix Direction:
Centralize workspace input discovery so `list-workspace-files`, `onboard-workspace`, `prepare-kpi-generation`, and validation share the same classifier.

Acceptance Criteria:

- `onboard-workspace` returns non-empty `data_files` for the Hospital Patient Records root CSV files.
- `profile_count` is greater than zero.
- KPI/context discovery includes `hospital_analytics_questions.sql`.
- Data-model/context discovery includes `create_hospital_db.sql` and `data_dictionary.csv`.
- No generated artifacts are written outside `workspaces/Hospital_Patient_Records/interns/`.

## BUG-002: Validator Warns About Missing `docs/` and `datasets/` Despite Valid Root-Level Evidence

Severity: High

Status: Open

Finding:
After onboarding, validation returns `ok: true` but still warns:

```text
workspace: docs/ was not found under the workspace
workspace: datasets/ was not found under the workspace
```

This is misleading when root-level files have already been classified as docs/context and dataset evidence.

How It Is Created:

1. Use a flat workspace with root CSV/SQL/context files.
2. Run onboarding and validation:

```powershell
uv run validate-workspace-artifacts --workspace workspaces/Hospital_Patient_Records
```

3. Observe warnings for missing folders even though equivalent evidence exists at the root.

Expected Behavior:
Validator should accept either:

- Traditional layout: `docs/` and `datasets/`
- Flat client layout: root-level dataset files plus root-level docs/context files

Impact:
The system looks less trustworthy to a client because it warns about missing evidence that was already detected. It also encourages agents to over-correct by creating folders or moving files unnecessarily.

Suspected Cause:
Validator checks for physical folder names instead of checking normalized input classification/evidence.

Fix Direction:
Make validation depend on classified evidence roles, not only folder structure.

Acceptance Criteria:

- No `docs/ missing` warning when root-level context docs exist.
- No `datasets/ missing` warning when root-level dataset evidence exists.
- Warnings remain when neither folder-based nor root-level evidence exists.

## BUG-003: Kickstart Generates an Empty Bootstrap From an Empty Input Fingerprint

Severity: High

Status: Open

Finding:
`kickstart-workspace` generated task config and bootstrap artifacts after onboarding, but the bootstrap fingerprint was:

```text
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

That is the SHA-256 hash of empty content, which strongly suggests the bootstrap saw no effective workspace inputs.

How It Is Created:

1. Run onboarding on the flat Hospital Patient Records workspace.
2. Run:

```powershell
uv run kickstart-workspace --workspace workspaces/Hospital_Patient_Records --domain healthcare
```

3. Observe generated task artifacts with `kpi_count: 0`, no feature rules, and an empty-input fingerprint.

Expected Behavior:
Kickstart should refuse to treat the workspace as ready when onboarding found zero data files, zero KPI inputs, and zero profiles despite the selection classifier seeing valid source files.

Impact:
The system can move into a "ready_for_sql" or task-configured state with no real evidence. That is dangerous because later tools may assume the workspace is ready for executable KPI generation.

Suspected Cause:
Kickstart trusts generated onboarding artifacts without checking whether discovery was materially empty or inconsistent with workspace listing.

Fix Direction:
Add a consistency gate between workspace listing and onboarding/kickstart:

- If listing has dataset/KPI/model evidence but onboarding has none, block kickstart.
- Route to a discovery bug/open question instead of generating a ready task.

Acceptance Criteria:

- Kickstart blocks or warns hard when input discovery is empty but file classification is non-empty.
- `feature_resolution.status` must not become `ready_for_sql` when `kpi_count = 0` and `profile_count = 0`.
- Empty fingerprint is treated as a blocker for non-empty workspaces.

## BUG-004: Workspace Summary Labels Dataset Root as `Source artifact: None`

Severity: Medium

Status: Open

Finding:
The Gemini summary after file listing showed:

```text
Source artifact: None
```

while the listing had a valid dataset root:

```text
Dataset roots:
- workspaces/Hospital_Patient_Records
```

How It Is Created:

1. Run workspace selection.
2. Let the agent summarize `list-workspace-files` output.
3. Observe that the summary omits or mislabels the dataset root.

Expected Behavior:
The summary should display either:

```text
Dataset root: workspaces/Hospital_Patient_Records
```

or:

```text
Source artifact: workspaces/Hospital_Patient_Records
```

Impact:
This is mostly presentation-level, but it can confuse users because the file list shows datasets while the summary says no source artifact exists.

Suspected Cause:
The agent summary template does not map `dataset_roots` into `Source artifact`.

Fix Direction:
Update agent/tool adapter guidance to summarize `dataset_roots` explicitly.

Acceptance Criteria:

- Workspace-selection summaries include dataset roots when present.
- `Source artifact: None` is not shown when `dataset_roots` is non-empty.

## BUG-005: Gemini Monitor Can Simulate Workflow Panels When Required Project Tools Are Unavailable

Severity: High

Status: Open

Finding:
During a monitored headless Gemini CLI run for `workspaces/Healthcare-RCM-Data-Platform`,
Gemini displayed reasonable workspace-selection and KPI route panels, but it did not reliably use
the deterministic repo tools required by `AGENTS.md`.

Observed behavior:

- Gemini attempted to call `run_shell_command`, but that tool was not registered in its session.
- On a later workspace-selection turn, Gemini reused previous context and made no listing tool calls.
- Gemini manually wrote a KPI generation route panel instead of running:

```powershell
uv run prepare-kpi-generation --workspace workspaces/Healthcare-RCM-Data-Platform
```

- Gemini repeatedly claimed a required `interns/state/workspace_settings.json` /
  `dataset_allowlist` mechanism without proving that this is a supported repo contract.

How It Is Created:

1. Start a monitored Gemini session from the repo root:

```powershell
gemini --skip-trust --session-id <id> --output-format stream-json -p "<workspace setup prompt>"
```

2. Ask it to perform Step 0 for `workspaces/Healthcare-RCM-Data-Platform`.
3. Confirm scope: use only EMR Hospital A.
4. Select Option 2: usual workflow / onboard existing KPI + data model.
5. Observe that it reports shell tooling is unavailable, but still invents or manually presents some workflow details.

Expected Behavior:

- Step 0 must call `uv run list-workspace-files --workspace workspaces/Healthcare-RCM-Data-Platform`,
  or explicitly state that shell execution is unavailable and stop.
- The KPI generation route must come from:

```powershell
uv run prepare-kpi-generation --workspace workspaces/Healthcare-RCM-Data-Platform
```

- The usual workflow must run:

```powershell
uv run onboard-workspace --workspace workspaces/Healthcare-RCM-Data-Platform
```

  or stop with a tooling blocker.
- Gemini must not invent unsupported scope config files or simulate generated artifacts.
- Chat output should show the full panel content, not only compact JSON or artifact paths.

Impact:
The user can see a plausible panel while the governed workflow has not actually advanced. This can
hide missing tool calls, unsupported scope enforcement, stale context reuse, and fake generated
panel state.

Suspected Cause:
The monitored Gemini headless environment does not expose the repo shell command tool, and there is
no stream-json monitor harness that fails when required deterministic commands are skipped.

Fix Direction:

- Add a Gemini monitor harness that validates stream-json logs.
- Treat unavailable shell execution as a hard blocker.
- Fail the monitor if Gemini claims unsupported files such as `workspace_settings.json`.
- Fail the monitor if required project commands are absent from the stream.
- Keep the full current panel markdown visible in CLI/chat output by default, with JSON summaries
  behind an explicit `--json` flag.

Acceptance Criteria:

- A monitored Gemini run cannot pass if it skips required deterministic commands.
- A monitored Gemini run cannot pass if it invents unsupported scope files.
- Workspace-selection, KPI generation route, and KPI resolution review panels are visible in chat.
- Hospital A-only scope is preserved in generated inventory and validated before KPI mapping or SQL generation.

## Priority Order

1. BUG-001: Fix onboarding discovery first.
2. BUG-005: Add Gemini monitor harness checks for deterministic tool usage and unsupported claims.
3. BUG-003: Add kickstart consistency gate.
4. BUG-002: Update validator to respect flat workspace evidence.
5. BUG-004: Clean up summary wording.

## Related Project-Level Hardening Report

The RCM end-to-end session also produced broader data-engineering control-plane
findings that go beyond onboarding discovery. See:

```text
docs/bugs/data_engineering_control_plane_hardening.md
```

That report tracks catalog-first generation, ETL/ELT/medallion route gates,
bronze/silver/gold contracts, raw-path restrictions, layered harnesses, full
review panels, cross-agent evidence harnesses, and evidence-order/time-budget
guardrails.

## Product Risk

Current risk is high for fresh client demos using flat folders. The first screen looks correct, but onboarding can silently produce empty contracts. The system should fail closed when the discovery handoff is inconsistent.

## Recommended Next Fix

Refactor input discovery into one shared source of truth:

```text
workspace file listing/classification
  -> onboarding input discovery
  -> validator evidence checks
  -> KPI generation context intake
  -> kickstart readiness gate
```

This keeps the client-facing flow consistent and prevents later stages from contradicting the selection screen.

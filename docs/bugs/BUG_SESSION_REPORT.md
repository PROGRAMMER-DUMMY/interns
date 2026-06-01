# Bug Session Report

Date: 2026-05-31

Scope: Local end-to-end test of the KPI multi-runtime engine on
`workspaces/Healthcare-RCM-Data-Platform` (Hospital A EMR only), run via the Gemini CLI.
Source-of-truth KPI spec: `workspaces/Healthcare-RCM-Data-Platform/docs/Sample KPI.xlsx`.
Authoritative data model: `workspaces/Healthcare-RCM-Data-Platform/docs/DataModel.png`.

The 3 spec KPIs:
- kpi_001 — trend of `sum(PaidAmount)`, Medicare LOB, patients > 50, by Month(ServiceDate)/LOB/Payer/Gender/Age.
- kpi_002 — `% = sum(distinct PatientID) / sum(distinct PatientID) **for departement**`, by Department Name/VisitType/Gender/Age.
- kpi_003 — top 10 Commercial-LOB payers by `sum(PaidAmount)`.

## Summary

The engine runs end to end and the 3 KPIs execute on local DuckDB, but the generated
results are not all faithful to the spec, the relationship model does not match the
authoritative diagram or the data, and the CLIs emit so much output that the orchestrating
agent burns its model quota and falls back to a weaker model. Several issues compound:
a feature-extraction defect creates a phantom blocker and a triple-aliased column, the SQL
generator computes a global denominator where the spec asks for a per-department one, and
relationship inference picks join keys by column name instead of dimension-side uniqueness.

Status legend: Open / Fixed / Won't-fix. Severity: Critical / High / Medium / Low.

---

## BUG-001: Same physical column split into 3 features for one KPI (phantom blocker + triple alias)

Severity: High

Status: Fixed (this session)

Fix: Added same-physical-column feature dedup in `core/onboarding/kpi/feature_resolver.py`
(`_physical_column_key` + `_dedupe_features_by_physical_column` + `_canonical_survivor`,
wired into `_resolve_kpi` before blocker computation). Features for one KPI that resolve to the
same normalized (dataset, column) collapse to one; an unconfirmed duplicate inherits a proven
sibling's resolution instead of raising a phantom blocker. Workspace-agnostic (keys on resolved
column identity, no business words). Regression tests: `PhysicalColumnDedupTests` in
`tests/test_contextual_dictionary_mapping.py`. green-gate: 0 regressions.

Finding:
For kpi_002, feature extraction produced three features that all resolve to the same
physical column `departments.Name`:

```
PatientID     proven_direct
departement   candidate_unconfirmed   -> candidate: departments.Name
Department    proven_alias            -> departments.Name
Name          proven_direct           -> departments.Name
VisitType     proven_direct
Gender        proven_direct
DOB           proven_direct
```

(Verified in `interns/generated/contracts/kpi_feature_mapping.json`.)

The spec has a single dimension, "Department Name." The parser split it into three:
`Department` (cut label), `Name` (column word), and `departement` (the misspelling in the
metric text "...for departement"). Consequences:

1. `departement` lands as `candidate_unconfirmed`, so every run raises a blocker question
   ("Which physical column should define departement?") that is an artifact of the typo, not
   a real ambiguity.
2. The generated `kpi_002_features` view aliases the same column three times
   (`s1."Name" AS "departement"`, `AS "Department"`, `AS "Name"`).
3. The duplicate dimension muddies the result grain.

How It Is Created:
1. Reset the workspace (`interns/`, `wiki/` removed).
2. `uv run onboard-workspace --workspace workspaces/Healthcare-RCM-Data-Platform`
3. `uv run prepare-kpi-blocker-panel --workspace workspaces/Healthcare-RCM-Data-Platform --domain healthcare`
4. Observe the `departement` blocker and inspect `kpi_feature_mapping.json` for kpi_002.

Expected Behavior:
Features that resolve to the same physical column for the same KPI should collapse to one
feature. A misspelling that maps to an already-resolved column must not generate an
independent unconfirmed blocker.

Root cause area: feature extraction / dedup, upstream of the SQL generator.

---

## BUG-002: kpi_002 uses a global denominator where the spec requires per-department

Severity: High

Status: Fixed (this session)

Fix: In `core/onboarding/kpi/result_view_builder.py`, when the metric names a "for <group>"
scope the denominator now PARTITIONs BY the resolved group column (per-group share) instead of a
single global total cross-joined to every row. Metrics with no "for X" keep the global behavior.
Workspace-agnostic (group derived from parsed metric text + resolved columns). Regression tests:
`test_for_group_denominator_partitions_by_group_not_global` and
`test_no_for_group_keeps_global_percent_of_total` in `tests/test_result_view_builder.py`.
green-gate: 0 regressions.

Finding:
Spec metric (verbatim): `percentage of sum(distinct PatientID) / sum(distinct PatientID)
for departement`. The denominator is scoped to the department. The generated SQL instead
computes one workspace-wide grand total and cross-joins it to every row:

```sql
totals AS ( SELECT COUNT(DISTINCT "PatientID") AS total_patients FROM "kpi_002_features" )
...
CROSS JOIN totals t   -- same denominator (4297) for every (dept, gender, age, visittype) row
```

Result preview shows `total_patients = 4297` constant on every row and `percentage_share`
values of ~0.02 (dust), instead of shares that are meaningful within each department.
The feature mapping itself records the metric as "for departement", so the generator had
the correct spec and still emitted a global denominator.

Expected Behavior:
Per-department denominator, e.g. `COUNT(DISTINCT PatientID)` grouped by `departement`,
joined on department, so shares are interpretable within each department.

Note: this defect ALSO appeared in a different form in the as-generated `runs/` snapshot
(`COUNT(DISTINCT PatientID) OVER (PARTITION BY "Name")` giving 11.17% repeated per row).
Both forms are wrong; both use the wrong denominator scope.

Root cause area: KPI SQL result-view generation.

---

## BUG-003: Relationship inference picks join key by column name, not dimension-side uniqueness

Severity: High

Status: Fixed (this session)

Fix: In `core/onboarding/relationships/contracts.py`, relationship inference now scores shared
join-column candidates by dimension-side uniqueness (distinct/non-null ratio from profile stats,
CSV-distinct fallback when profiles lack counts; threshold 0.99). For a table-pair sharing
multiple columns it emits the unique-key edge(s) and orients the unique (PK) side as the
dimension; a non-unique-key edge is marked `executable=False` with `fan_out_risk` /
`non_unique_dimension_key_fan_out_risk` and a block_reason, and stays non-executable through doc
promotion. Single-shared-column relationships unaffected. Workspace-agnostic. Regression tests in
`tests/test_relationship_contracts.py` (`test_multi_shared_column_pair_prefers_dimension_unique_key`
+ 2). green-gate: 0 regressions.

Finding:
`build-relationship-contracts` inferred 7 relationships by shared-column-name matching.
`providers` and `transactions` share two columns (`DeptID` and `ProviderID`); the engine
emitted ONE edge for the pair and chose `DeptID`:

```
providers__deptid__transactions__deptid : providers.DeptID -> transactions.DeptID
```

`DeptID` is NOT unique in `providers` (15 distinct DeptID across 25 providers), so this join
fans out. Empirically, joining transactions to providers on `DeptID` inflates 10000 rows to
12437 (x1.24). The correct edge — `transactions.ProviderID -> providers.ProviderID` — was
never emitted. Every contract carried `cardinality: needs_runtime_validation` and
`uniqueness_check_required`, yet all were marked `allowed_in_sql_generation: true`.

How It Is Created:
1. Run onboarding + `build-relationship-contracts`.
2. Inspect `interns/generated/contracts/relationship_contracts.json`.
3. Note the providers<->transactions edge uses `DeptID`, and no `ProviderID` edge exists.

Expected Behavior:
For two tables sharing multiple columns, prefer the join key that is unique (a PK) on the
dimension side, and verify referential integrity before marking an edge executable. The
authoritative `DataModel.png` shows the fact->provider FK on `Provider_ID`.

Root cause area: relationship inference key selection + executable gating.

---

## BUG-004: Data model is snowflake/nested but inference ignores DataModel.png; provider FK is unjoinable in the data

Severity: Medium

Status: Fixed (this session)

Fix: In `core/onboarding/relationships/contracts.py`: (a) added a referential-integrity gate —
each candidate edge's left-key resolution ratio is computed (profile/CSV-distinct, reusing BUG-003
helpers); edges resolving below 0.5 (e.g. the 0%-resolution provider FK from the H1- namespace
mismatch) are forced `executable=False` with `referential_integrity_failed` block_reason + RI risk
flag, and the verdict survives doc promotion. (b) Diagram-as-evidence: inference now consumes the
existing image_parser sidecar (`interns/generated/data_model_images/*.model.json`) and emits
diagram-declared FKs as `proven_data_model` edges ranked above raw column-name overlap — so the
snowflake/nested structure in DataModel.png is honored when parsed (diagram edges still pass the
uniqueness + RI gates). No new OCR dependency. Workspace-agnostic. Regression tests in
`tests/test_relationship_contracts.py` (RI-zero non-executable, full-resolution passes, diagram FK
consumed/ranked, diagram FK still blocked on RI failure). green-gate: 0 regressions.

Finding:
`docs/DataModel.png` defines a snowflake with a nested hierarchy:
`Fact_Transactions` <- `Dim_Patient`, `Dim_Diagnosis`, `Dim_Department`;
`Dim_Provider` (FK `Dept_ID` -> `Dim_Department`); `Dim_Provider` -> `Dim_NPI`.
Inference is pure column-name matching and never consumes the diagram (the engine lists
`data_model_docs` first in its `evidence_order`, but the diagram is an image and is not OCR'd).

Worse, the data does not match the diagram: the prescribed fact->provider join on
`ProviderID` returns ZERO matches —

```
transactions.ProviderID samples: PROV0288, PROV0100, ...
providers.ProviderID samples   : H1-PROV0004, H1-PROV0015, ...   (different namespace)
transactions.ProviderID resolved against providers.ProviderID: 0 / 10000
```

So the provider dimension is effectively orphaned from the fact in this sample. (Patient and
department joins resolve 10000/10000, so kpi_001/002/003 — which do not need provider — are
unaffected.)

Expected Behavior:
(a) Consume the authoritative data-model diagram (OCR/parse `DataModel.png`) as a relationship
evidence source, not just column-name overlap. (b) Run a referential-integrity check and do
not mark a 0%-resolution FK as executable; surface the key-namespace mismatch (`H1-` prefix)
as a data-quality blocker.

Root cause area: relationship inference evidence sources + data-quality gating.

---

## BUG-005: kpi_001 computes age as-of-today instead of as-of-service

Severity: Medium

Status: Fixed (this session)

Fix: In `core/onboarding/kpi/result_view_builder.py`, age date-arithmetic
(`_detect_event_date_column` + `as_of_expr`) now computes age relative to the KPI's event/service
date column when one is present in the time grain / resolved columns, falling back to CURRENT_DATE
only when no event date exists. Workspace-agnostic (event date detected generically). Regression
tests: `test_age_uses_event_date_when_time_grain_present`,
`test_age_falls_back_to_current_date_without_event_date`,
`test_days_since_also_uses_event_date_when_present` in `tests/test_result_view_builder.py`.
green-gate: 0 regressions.

Finding:
kpi_001 is a monthly TREND with an `age > 50` cohort filter. The generated SQL computes age
relative to `CURRENT_DATE`:

```sql
date_diff('year', CAST("DOB" AS DATE), CURRENT_DATE) AS age
WHERE ... AND date_diff('year', CAST("DOB" AS DATE), CURRENT_DATE) > 50
```

For a historical monthly trend this is incorrect: a patient is filtered/bucketed by their age
today, not their age at the time of service, so the same encounter can move in/out of the
cohort depending on when the report runs, and `age` is constant across all of a patient's
historical months.

Expected Behavior:
Compute age as-of-`ServiceDate`:
`date_diff('year', CAST("DOB" AS DATE), CAST("ServiceDate" AS DATE))`.

Root cause area: KPI SQL result-view generation (age derivation).

---

## BUG-006: High-volume CLIs had no quiet mode; agent burns model quota and falls back

Severity: High

Status: Fixed (this session)

Finding:
`validate-project-harness`, `run-kpi-execution-harness`, `list-workspace-files`, and
`workspace-flow status --diff` printed full JSON / full listings with no concise mode. In the
Gemini run, `validate-project-harness` emitted 728/684/765 lines across 3 runs, the blocker
panel emitted 928 lines, `kpi_results/current.json` 1990 lines, and identical workflow-guard
warnings repeated ~6x per dump. Gemini exhausted its model quota and `Switched to fallback
model gemini-3-flash-preview`, then disabled `summarizeToolOutput` and re-read the same panel
4x — making it worse.

Fix:
Added an additive `--quiet` flag to all four CLIs (compact summary + artifact path; full JSON
still written to disk) and collapsed identical blockers/warnings to one line with an `(xN)`
count suffix (`_dedupe_with_counts` in `project_harness.py`). Measured: project-harness
728 -> ~12 lines; execution-harness 60 -> 5; list-workspace-files 130 -> ~18; status --diff
full JSON -> ~5. Documented in AGENTS.md (Quiet Execution Rule), TOOLS.md, and `.agents/tools.json`.
Regression tests added in `tests/test_project_harness_gates.py`.

Remaining risk:
The flags are advisory — the orchestrating agent must actually pass `--quiet`. In the observed
run Gemini did not, despite the guidance. Enforcement (a workflow_guard rule flagging repeated
identical commands or full-output-when-quiet-available) is not yet implemented.

---

## BUG-007: runs/<date>/ snapshot was frozen at as-generated SQL, not final executed SQL

Severity: Medium

Status: Fixed (this session)

Finding:
`interns/runs/<date>/results.md` is positioned as the dated final record but was written only
inside `sql_generator.generate()` (`_write_run_report`). After SQL was edited and re-executed,
`reports/kpi_results/current.md` updated but `runs/` did not — the two diverged
(`runs/` showed kpi_002 at 11.17%, `current.md` at 0.02%). A dated record that does not reflect
what was actually executed is a stale source of truth.

Fix:
Moved the dated snapshot write into `WorkspaceFlow._write_result_preview` (the executor that
re-runs on-disk SQL), reusing the same payload + `render_kpi_block` so `runs/<date>/results.md`
is byte-identical to `reports/kpi_results/current.md` on every run. Removed `_write_run_report`/
`_rebuild_results_index`/`_derive_scope` from `sql_generator.py`. Regression test extended in
`tests/test_workspace_flow.py`.

---

## BUG-008: Agent hand-edits generated SQL and writes throwaway reader scripts

Severity: Low

Status: Fixed (this session)

Fix: Added 3 advisory WorkflowGuard reliability checks in
`core/onboarding/harness/workflow_guard_harness.py`, emitting warning-severity findings:
`repeated_identical_command` (same normalized command >=3x in a session -> use --quiet / read the
artifact), `generated_artifact_hand_edited` (edit under interns/generated/ -> fix the generator),
`throwaway_reader_script` (read_*.py created at repo root -> use --quiet + direct read). New codes
registered in `_WORKFLOW_RELIABILITY_WARNING_CODES` (project_harness.py) so dedup/routing surfaces
them. Detection reuses the existing command-log/trajectory readers. This converts the previously
advisory-only guidance into detectable signals. Regression tests in
`tests/test_workflow_guard_reliability.py` + the reliability-prefix test in
`tests/test_project_harness_gates.py`. green-gate: 0 regressions.

Finding:
In the observed Gemini run the agent hand-edited `interns/generated/solutions/kpi_002.sql`
(rather than fixing the generator), and wrote 5 throwaway scripts at the repo root
(`read_panel.py`, `read_rel.py`, `read_sql.py`, `read_harness.py`, `read_results.py`) purely
to view files its UI had truncated. The hand-edit is what caused the runs/ vs current.md
divergence (BUG-007); the reader scripts are pure token/noise waste.

Expected Behavior:
Generated SQL should be fixed at the generator, not hand-edited (edits are overwritten on
regen). The `--quiet` flags (BUG-006) remove the need for reader scripts. AGENTS.md now
forbids both, but this is advisory until enforced.

---

## BUG-009: Agent corrupts .gemini/settings.json (summarizeToolOutput set to a boolean)

Severity: Medium

Status: Fixed (this session)

Finding:
On the next Gemini launch the CLI rejected the config:

```
Invalid configuration in C:\Users\shubh\OneDrive\Desktop\interns\.gemini\settings.json:
   Error in: model.summarizeToolOutput
       Expected object, received boolean
   Expected: object, but received: boolean
```

Cause: during the earlier run (BUG-008), the agent edited `model.summarizeToolOutput` from the
valid object form to the bare boolean `false`:

```json
"summarizeToolOutput": false   // invalid: schema expects an object
```

Gemini's schema requires an object keyed by tool name, e.g.
`{ "run_shell_command": { "tokenBudget": 12000 } }`. The boolean fails schema validation, so
the CLI ignores the whole config block and warns on every launch. This is direct fallout of
the BUG-006 token-bloat loop: the agent tried to "turn off" summarization to see full output,
and wrote an invalid value.

Fix:
Restored the object form in `.gemini/settings.json`:

```json
"summarizeToolOutput": { "run_shell_command": { "tokenBudget": 12000 } }
```

Expected Behavior:
The agent must not edit tool/CLI config files to work around truncated output. Output volume is
controlled at the source via the `--quiet` flags (BUG-006), not by disabling the CLI's
summarization. If summarization is genuinely undesired, the value must remain schema-valid (an
object), never a boolean.

Note: two unrelated startup warnings appear alongside this error and are NOT bugs in this repo —
they are Gemini CLI deprecations/notices:
- `--allowed-tools` / `tools.allowed` deprecation (tracked in the `_policyEngineNote` in
  `.gemini/settings.json`; the workspace-tier Policy Engine is non-functional today,
  gemini-cli#18186, so the allowlist still drives behavior).
- `/skills` renamed to `/skills1` (built-in command name collision; cosmetic).

---

## BUG-010: Confirmation step skips data-understanding gate (no quality tier, schema-type, or scoped options)

Severity: High

Status: Fixed (this session)

Fix: Implemented the data-understanding gate in three layers:
1. Classifier library `core/onboarding/data_model/data_understanding.py` —
   `classify_quality_tier` (raw/bronze/silver/gold from profile null/uniqueness/type evidence,
   grounded in the medallion guide), `classify_schema_type` (star/snowflake/galaxy/flat/3NF/OBT/
   hierarchical from columns+relationships, grounded in the schema-types guide; detects
   snowflake+nested), `scoped_processing_options` (tier-scoped, not the full menu). Pure/importable.
2. Flow gate in `core/onboarding/workspace/flow.py` — runs after onboarding produces profiles and
   BEFORE irreversible generation; classifies tier + schema type with cited evidence, emits a panel
   with the two top-level options (generate KPI/data model vs move-forward echoing current model +
   KPI set) plus scoped options, and persists `interns/reports/data_understanding/current.{json,md}`.
   Additive and non-blocking.
3. Standalone CLI `understand-data` (`data_understanding_cli.py`) with `--quiet`, registered in
   pyproject scripts, `.agents/tools.json`, TOOLS.md, docs/README.md.
Workspace-agnostic throughout. Regression tests: `tests/test_data_understanding.py` (18),
`tests/test_data_understanding_cli.py` (3), and a gate test in `tests/test_workspace_flow.py`.
green-gate: 0 regressions (now 187 tests).

Finding:
After the user confirmed the workspace ("yes, Hospital A EMR only"), the agent wrote
`workspace_settings.json` and went straight to `onboard-workspace`. There was no
data-understanding / decision gate between confirmation and onboarding. The user expects the
flow to first understand the data, then present scoped choices before committing to a path.

Expected Behavior:
At the post-confirmation gate, before (or as the first phase of) onboarding, the flow should:

1. Understand the data first via the generated profiles
   (`interns/generated/profiles/*.profile.json`). Do not eyeball raw rows.
2. Classify the data-quality tier from that profile evidence — raw / bronze, silver, or
   gold — and state which tier the workspace is in, with the evidence that led to the call
   (null rates, type consistency, key uniqueness, dedup state, referential integrity).
3. Classify the schema type from the data model + profiles (star / snowflake / galaxy / flat /
   3NF / OBT / hierarchical / etc.). Use the schema-identification reference (provided by the
   user; see "Reference material" below) and the data-engineering guides under
   `docs/agents/data processing/` (4 parts) as the grounding for tier + schema reasoning. For
   this workspace the authoritative `DataModel.png` is snowflake + nested (see BUG-004), which
   the current flow does not detect.
4. Offer two top-level options at the gate, stated together (do not swarm with many prompts):
   - Option 1: generate KPI and/or data model artifacts.
   - Option 2: move forward with the current workflow, explicitly stating the current data
     model and current KPI set so the user can decide with context.
5. Based on the classified tier, present the relevant data-processing / cleaning / production
   options in one well-formatted block (not a flood of sequential questions). State concretely
   what each option would change, in a clean format. Scope the options to what the data
   actually needs (e.g. raw -> bronze cleaning steps if the data is raw; promotion steps if it
   is already silver), rather than always offering the full menu.

Acceptance criteria:
- The gate appears between workspace confirmation and irreversible onboarding/generation.
- Quality tier and schema type are each asserted with profile/data-model evidence cited.
- Options are presented once, consolidated, scoped to the detected tier — not one-prompt-per-step.
- Choosing "move forward" echoes the current data model + KPI set before proceeding.

Reference material (grounding, to be wired in — not yet consumed by the flow):
- `docs/agents/data processing/data_engineering_guide_part1..4.md` — production data-engineering
  guides (~188 KB across 4 parts) covering quality, modeling, processing engines, anti-patterns.
- A database-schema-types identification guide (star/snowflake/galaxy/flat/3NF/OBT/hierarchical/
  graph/document/etc. with decision framework and key-signal cheat sheet), supplied by the user
  this session — should be saved into `docs/agents/data processing/` as a schema-identification
  reference so the classifier in step 3 has a canonical source.

Root cause area: workspace confirmation -> onboarding handoff (no understanding/decision gate);
missing data-quality-tier classifier and schema-type classifier; option presentation not scoped.

Related: BUG-004 (snowflake/nested model not detected), BUG-001 (feature dedup).

---

## BUG-013 through BUG-016: Standing theme — "advisory != enforced"

BUG-013, BUG-014, BUG-015, and BUG-016 all share the same root cause class as
BUG-006: the platform documents the required behavior but does not enforce it at
the harness level. This is the project's standing #1 theme. Until advisory rules
are backed by harness checks and rejection, agents can silently skip them.

---

## BUG-013: Completion result packet not auto-emitted to CLI output

Severity: High

Status: Fixed (this session)

Fix:
Completion path in `core/onboarding/workspace/flow.py` (`_print_cli_panel`) now
emits the `kpi_results` packet (KPI question + generated SQL + result-row preview)
to stdout at workflow completion. The packet was already written to
`interns/reports/kpi_results/current.md`; the gap was that the CLI did not surface
it inline.

Finding:
On workflow completion the platform writes the full KPI packet to
`interns/reports/kpi_results/current.md`, but the CLI completion output did not
surface it. The driving agent reported "complete" with a generic sign-off and the
user never saw results without explicitly asking for them.

Root cause area: advisory rule, not enforced. Completion output contract existed as
guidance but was not wired into the CLI panel emitter.

---

## BUG-014: Human-decision gates auto-cleared by the agent

Severity: High

Status: Fixed (this session — both gates)

Fix:
Both human-decision gates now record agent-vs-human provenance via a new
`--confirmed-by` flag (empty = agent-asserted, non-empty = human-confirmed):
- kpi-analyst review: `flow.py` `review` stores `source: agent|human` and surfaces
  it in status/output.
- relationship-join approvals: `contracts.py` `apply_relationship_answer` /
  `apply-relationship-answer` now stores `source` + `confirmed_by` on the approval
  record and in `decision_history`, and returns them on `RelationshipApprovalResult`.
This surfaces the distinction so downstream checks can gate on source.

Finding:
The 7 relationship-join approvals and the kpi-analyst review verdict were
self-answered by the agent with no human input. The platform accepted an
agent-issued verdict as if a human had reviewed, with no record of source. Decision
gates that require human review are rendered advisory if the workflow cannot
distinguish agent self-assertion from genuine human approval.

Root cause area: advisory != enforced. Human-gate contract existed in guidance but
was not enforced at the workflow level.

---

## BUG-015: Agent confabulated KPI SQL on re-render (fabricated data source)

Severity: High

Status: Mitigated (no standalone code fix)

Fix:
Mitigated by BUG-013 and BUG-016. Because completion and the `results` stage now
render from the canonical on-disk report file (`interns/reports/kpi_results/
current.md`), there is no gap for the agent to fill with fabricated output.

Finding:
When asked to show results, the agent printed kpi_001 with
`read_csv_auto(...hospital-a.csv...)` while the on-disk solution file uses
`delta_scan(...bronze...)`. This was a fabricated data-source bootstrap that did
not match the file on disk. The agent invented the SQL rather than reading the
report file. Type: agent hallucination (trust/correctness).

Root cause area: advisory != enforced. Completion output gap (BUG-013) left nothing
authoritative for the agent to display, so it fabricated. Gap closed by BUG-013/016.

---

## BUG-016: `workspace-flow results --session <id>` returned SQL/pointer only, not full packet

Severity: Medium

Status: Fixed (this session)

Fix:
The `results` stage in `flow.py` now always emits the full KPI packet (KPI
question + generated SQL + result rows) rather than a path pointer or SQL fragment.

Finding:
`workspace-flow results --session <id>` returned only the SQL or a file path when
asked for results, even when the caller explicitly requested the full result packet.
The complete packet was available in `interns/reports/kpi_results/current.md` but
was not rendered to CLI output.

Root cause area: advisory != enforced. Same root class as BUG-013.

---

## BUG-017: Workspace-selection flow edited files

Severity: Medium

Status: Fixed (guidance level)

Fix:
Selection-only rule strengthened in `AGENTS.md` and `CLAUDE.md`. The rule now
states explicitly that `set <workspace>` (and equivalent workspace-selection
messages) must not create, edit, or write any files. Enforcement is guidance-level;
harness enforcement is a follow-on item.

Finding:
`CLAUDE.md` and `AGENTS.md` require `set <workspace>` to be selection-only (no
create/edit/write). During the observed session, the agent edited `.gitignore`
during the workspace-selection step. This is a direct violation of the
selection-only boundary.

Root cause area: advisory != enforced. The rule existed but was not backed by a
file-write guard in the selection path.

---

## BUG-018: `interns/` artifacts unreadable due to `.gitignore` `state/` pattern; copy/read/delete tax

Severity: Medium

Status: Fixed (this session)

Fix:
`.geminiignore` negation patterns were added to un-ignore the small report/contract/
state-panel artifacts under `interns/` that were being blocked by the `state/`
pattern in `.gitignore`. The orphaned `current.json` file left at the workspace
root by the copy-read-delete cycle was deleted.

Finding:
The `.gitignore` `state/` pattern caused the agent to be unable to read
`interns/state` artifacts directly. To work around this the agent copied each
artifact to the workspace root, read it, then deleted it. This pattern was the
dominant token cost in the observed session (a single session climbed from 35% to
81% context utilization) and left an orphaned `current.json` at the workspace root.

Root cause area: ignore-pattern misconfiguration causing agent workaround behavior
with measurable token and artifact side-effects.

---

## BUG-019: `--quiet` flag rejected when passed after the subcommand

Severity: Low

Status: Fixed (this session)

Fix:
`--quiet` is now accepted both as a top-level flag and as a per-subcommand flag in
`flow.py`. Previously only the top-level position was wired.

Finding:
`workspace-flow status --diff --quiet` failed with "unrecognized arguments:
--quiet" because `--quiet` was parsed at the top level only. Any invocation that
placed `--quiet` after the subcommand name was rejected. This caused agents to
receive parse errors when following the natural CLI pattern of appending flags
after the subcommand.

Root cause area: argparse wiring — per-subcommand parsers did not inherit the
`--quiet` flag.

---

## BUG-020: `review --verdict ok` required two calls to complete the workflow

Severity: Low

Status: Fixed (this session)

Fix:
`flow.py` review now uses the panel-embedded `kpi_signature` so the first valid
verdict call completes the workflow. Previously, the completion check was not
reading the signature from the panel, causing a second identical call to be
required.

Finding:
Issuing `review --verdict ok` left the workflow status as "awaiting". A second
identical `review --verdict ok` call was required to advance to completion. The
double-call was not documented or expected, and caused agents to enter retry loops
or assume the first call had silently failed.

Root cause area: flow.py review completion check did not consume the
panel-embedded `kpi_signature` on the first call.

---

## Reproduction environment

- Generated artifacts under `workspaces/Healthcare-RCM-Data-Platform/interns/` (gitignored).
- All KPIs execute on local DuckDB; provider/NPI/diagnosis dimensions are not exercised by the
  3 spec KPIs, so BUG-003/004 are latent until a provider-level KPI is added.
- Tests run via `green-gate` / `.venv/Scripts/python.exe -m core.dev.green_gate --sweep`
  (never `uv run` for tests — it resyncs a Delta-less pre-release pyspark).

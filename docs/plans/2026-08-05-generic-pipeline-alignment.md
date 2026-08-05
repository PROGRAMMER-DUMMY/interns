# Generic Pipeline Lanes + User Alignment Implementation Plan

> **For agentic workers:** dispatched as five parallel slices with disjoint file ownership;
> each slice is TDD (venv unittest, never `uv run`), workspace-agnostic, ASCII-only output.

**Goal:** Make the platform generic across velocity (batch / micro-batch / streaming / realtime-serving),
better at first-setup requirement gathering and at absorbing changed requirements, and close the
audit's correctness gaps in schema evolution, data modeling, ingestion idempotency, and operations.

**Relationship to other plans:** independent of `2026-08-05-finish-cloud-first-restructure.md`
Phases 0/B (which need the operator); overlaps none of its tasks. Audit source:
`docs/reference/voltagent_platform_audit_2026-08-05.md` (issue ids A1-A11, missing M1-M17).

**Architecture:** velocity becomes a per-source routed decision (a `velocity.yaml` decision table,
like engine/compute/modeling); requirement understanding becomes an explicit playback-and-confirm
step plus a re-intake diff flow; schema evolution becomes a detected, panel-decided event, not an
implicit setting; the dbt generator finally emits the incremental/materialization behavior its own
validators guard.

## Global constraints

Same as the finish-plan: venv unittest only; ASCII markers; no hardcoded workspace names in core/;
secrets as reference names; additive-only remote ops; green gate stays 0-failing; no pyproject.toml
edits by slice agents (orchestrator wires CLIs + tool index after).

## Slice ownership map (hard boundaries)

| Slice | Owns (only these) | Model |
|---|---|---|
| S1 Intake + lanes + alignment | `core/intake/*`, `core/blueprint/*` (incl. `tables/velocity.yaml`, R10 rule in `tables/modeling.yaml`) | opus |
| S2 Schema evolution | new `core/evolution/*` (reads intake/blueprint artifacts, writes its own) | opus |
| S3 dbt generator: modeling + marts | `core/onboarding/kpi/dbt_project_generator.py` + its tests | opus |
| S4 Ingestion correctness | `core/provisioning/ingestion.py` + its tests | opus |
| S5 Orchestration ops | `core/orchestration/*`, new `core/observability/kpi_anomaly_check.py` | sonnet |

## S1 — Velocity lanes + requirement understanding (intake/blueprint)

1. **Arrival-pattern measurement**: `discovery.py` stops hardcoding `is_streaming: false`; for
   object-store sources, infer arrival pattern from file mtimes/naming cadence when listable
   (`arrival_pattern: continuous|periodic|one_shot|unknown` + evidence note); `unknown` stays
   honest (drives the intake question, never a guess).
2. **Velocity intake question** (skippable when measured): target latency class per source ->
   `maps_to: ["lane.velocity"]`, options batch/micro_batch/streaming/realtime_serving.
3. **`tables/velocity.yaml`**: lane decision per source from `arrival_pattern` + `freshness_sla`
   + velocity answer; constraints: realtime_serving requires an online-store serving edge
   (blocked with named missing fact until M4 exists — honest block, cite audit M4); streaming lane
   requires checkpointable source. Rendered in the blueprint per source with rule-that-fired.
4. **Understanding playback ("did I get you right")**: after intake answers, before blueprint,
   emit `interns/reports/intake_playback/current.md` — plain-English restatement: consumers,
   decisions driven, grain sentence, SLA per source, lane per source, SCD2 yes/no, retention —
   each line tagged (measured)/(you said)/(default). `prepare-blueprint` refuses until playback
   is confirmed (`apply-intake-answer --question playback_confirm --answer confirmed`), giving
   requirement gathering an explicit alignment gate.
5. **Change absorption (re-intake diff)**: re-answering any intake question after a confirmed
   blueprint re-runs decisions and renders the blueprint DIFF (existing diff mode) plus a
   `changed_decisions` section naming which answers changed which decisions; confirmed blueprint
   stays live until re-confirmed.
6. Add `schema_registry_url` optional field to `source_declaration` (S4 consumes defensively).
7. `tables/modeling.yaml`: add R10 modifier `late_arriving_dimensions` (fires when lane is
   streaming/micro_batch OR intake `calendar_and_restatement` indicates restatement) -> choice
   `inferred_member_dimensions`; S3 implements the macro.

Tests: measurement honest-unknown; lane rules incl. blocked-realtime; playback gate refusal;
re-intake diff contains changed_decisions; drift guard vs `interview.QUESTIONS_BY_ID` extended.

## S2 — Schema evolution as a governed event (`core/evolution/`)

1. `snapshot.py`: on every `discover-source` completion (called by S1's discovery via a thin hook
   S1 exposes — see interface below), persist `interns/generated/intake/discovery_history/<utc>.json`.
2. `drift.py`: `detect_drift(prev, curr) -> DriftReport` — added/removed columns, type changes,
   new/removed tables; severity: additive=info, removal/type-change=action-needed.
3. `panel.py`: action-needed drift renders `interns/reports/schema_drift_panel/current.{md,json}`
   (blocker-panel conventions: option ids, recommended option, ASCII): options per finding —
   `propagate` (allow downstream: bronze mergeSchema + silver on_schema_change already emit),
   `quarantine_column` (exclude from silver select-lists via an exclusions contract
   `interns/generated/contracts/schema_exclusions.json` that S3's generator honors), or
   `block_pipeline`. Applied via `apply-drift-answer` (cli.py, envelope, idempotent, --confirmed-by).
4. Interface produced: `schema_exclusions.json` = `{"<table>": {"excluded_columns": [...],
   "decided_by": ..., "at": ...}}`. S3 reads it defensively (absent file = no exclusions).

Tests: snapshot rotation; drift detection matrix (add/remove/retype/new-table); panel renders
JSON-backed options; apply records provenance; exclusions contract shape.

## S3 — dbt generator: real incrementals, late dims, freshness, retention

1. **Incremental marts (audit A1)**: when the KPI's grain has a temporal anchor and the lane is
   micro_batch/streaming, emit `materialized='incremental'`, `incremental_strategy='merge'` with
   `unique_key` = grain hash key, `on_schema_change='append_new_columns'`, `event_time` +
   `lookback` per spec Section 6 — making the currently-dead incremental validators live. Batch
   lane keeps `table`. Publish step for incremental marts becomes metadata swap (RENAME pair),
   not a second full copy.
2. **Late-arriving dimensions (audit missing #1)**: when modeling decision includes R10, emit
   `macros/inferred_member.sql` (insert unknown-member row per dimension: hash key of natural key,
   `is_inferred=true`, type-1 overwrite on real arrival) and LEFT JOIN + COALESCE-to-unknown in
   fact builds so facts are never dropped.
3. **Freshness (A6)**: `sources.yml` emits `freshness: {warn_after, error_after}` + `loaded_at_field`
   when a temporal anchor column resolves for the source; unresolvable -> writes an open question
   line into the generation report instead of silence. `validate_generated_project` requires one
   or the other.
4. **Retention (A4)**: mart/silver configs emit `tblproperties` with
   `delta.deletedFileRetentionDuration`/`delta.logRetentionDuration` from the declared retention
   policy (default silver 365d gold 730d); validator asserts presence.
5. Honor S2's `schema_exclusions.json` in silver select-list emission.

Tests: incremental emitted for temporal+micro_batch fixture and validators now exercised on a
REACHABLE path; inferred-member macro + fact join text; freshness-or-open-question invariant;
tblproperties presence; exclusions honored.

## S4 — Ingestion correctness (`core/provisioning/ingestion.py`)

1. **JDBC (A2)**: emit watermark-bounded incremental pull + MERGE on discovered/declared key by
   default; no resolvable watermark column AND no key -> REFUSE to emit the job (structured
   `blocked_no_idempotency_key` in the manifest naming what to declare), matching the dbt
   refuse-over-unsafe convention. Full-table one-shot allowed only with explicit
   `one_shot: true` in the declaration.
2. **Kafka (A9/M10)**: emit `maxOffsetsPerTrigger` default; honor `schema_registry_url` when
   declared (from_avro with registry) else keep raw-string cast but write a `schema_unverified`
   note into the manifest.
3. Manifest schema gains `idempotency: {mode: merge|append_once|refused, key: [...], watermark: ...}`
   per job; tests grep emitted code for unconditional `.mode("append")` on JDBC (must be absent).

## S5 — Orchestration ops (`core/orchestration/`, `core/observability/`)

1. **Maintenance task**: DAG emits weekly `OPTIMIZE` (playbook-gated comment) + `VACUUM` (retention-
   aligned) task per generated schema, and schedules the ghost-reconcile report (A11) monthly —
   all report/maintenance, nothing destructive beyond VACUUM's declared retention.
2. **On-call wiring (A8)**: DAG `owner` + failure-callback payload carry intake's
   `ownership.on_call` answer (read from `intake_answers.json`, defensive default "unassigned");
   alert payload includes severity + workspace + task.
3. **Threshold alerting**: new `core/observability/kpi_anomaly_check.py` — after results land,
   compare each KPI headline vs trailing history in `interns/runs/` (median absolute deviation,
   flag > 3 MAD); writes `interns/reports/kpi_alerts/current.md` + optional webhook post; wired
   as a post-results DAG task. No invented thresholds beyond MAD-3 (documented as default,
   overridable per workspace settings key `kpi_alert_mad_threshold`).

Tests: DAG contains maintenance + reconcile tasks with schedules; owner threading; MAD math on
fixture history; no-history = no alert (never alarm on first run).

## Acceptance

All five slices: suites green + green gate 0-failing + tool index refreshed; cross-slice smoke
extended: micro_batch lane fixture produces incremental mart + inferred-member macro + freshness
block + drift panel on a mutated re-discovery. Orchestrator wires new CLIs (`apply-drift-answer`)
into pyproject + STAGE index afterward.

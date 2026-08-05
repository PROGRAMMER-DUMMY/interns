# CLI-First Integration: Decision, Status and Handoff

Date: 2026-08-06
Branch: `fix/close-built-to-wired-gap` (5 commits landed, gate green 1706/0)
Supersedes the tooling approach in: `docs/plans/2026-08-05-finish-cloud-first-restructure.md` (phases still valid; Phase A/C task shapes revised here)

---

## 1. The decision: CLI or SDK, per surface

**The rule:** CLI for anything a human would run by hand. SDK only where a governed
artifact needs to branch on *which* failure occurred.

Reason the rule is drawn there and not elsewhere, from real probing (see
`docs/reference/databricks_cli_reference.md`):

- `--help` cannot drift. This session cost hours fixing 36% doc drift and a skill
  whose MANDATORY GATE led with a flag that never existed. A CLI-first design makes
  that class of bug structurally impossible.
- BUT the Databricks CLI returns exit 0 or 1 for *everything* -- missing catalog,
  bad group, ambiguous auth profile are indistinguishable. `-o json` is unenveloped
  (bare arrays, prose errors on stderr, inconsistent pagination). Our additive-only
  refusals must record WHY they refused; prose parsing cannot carry that.
- The CLI is also more fragile to environment state than the SDK: two same-host auth
  profiles break every `bundle` command while `WorkspaceClient(profile=...)` works.

| Surface | Use | Why |
|---|---|---|
| dbt: build/test/parse/docs/retry/backfill/clone | **CLI** | dbt's Python API is explicitly unstable; the CLI is the supported interface. Exit codes are meaningful (0 clean / 1 handled failure / 2 unhandled). |
| dbt: project authoring | **Codegen** (ours) | No CLI equivalent; this is the product. |
| Airflow: DAG authoring | **Codegen** (ours) | DAGs are Python by definition. |
| Airflow: verification + dev loop | **CLI** | `astro dev parse`, `dags list-import-errors`, `tasks render`, `dags test` -- catches a broken emitted DAG before deploy, no scheduler needed. |
| Airflow: backfill | **CLI** | `backfill create` (Airflow 3; `dags backfill` was REMOVED). |
| Airflow: health/monitoring | **REST API** | CLI requires co-location; REST works against managed Airflow. JWT via `POST /auth/token`, `/api/v2/`. |
| Databricks: file/code delivery | **CLI** | `databricks sync` -- incremental, preserves extensions. NEVER `workspace import-dir` (strips `.py`/`.sql`). |
| Databricks: job/pipeline deployment | **CLI (bundles)** | Desired-state with `targets: {dev,prod}`, diffing, `bundle validate --strict`. Beats hand-rolled Jobs-API JSON. |
| Databricks: interactive auth | **CLI** | `auth login` only. |
| Databricks: UC provisioning (our additive gate) | **SDK** | Typed exceptions -> governed refusal reasons -> audit trail. This is the one place binary exit codes are disqualifying. |
| Databricks: query execution + remote gates | **SDK** | Structured results become evidence artifacts. |
| Cost/telemetry | **SQL over system tables** (SDK) | `system.billing.usage`, `system.query_history` joined to dbt `query_tags`. No CLI equivalent. |

**Net effect:** we are currently SDK-heavy where we should be CLI-heavy (deployment,
sync, validation) and that costs us code. The provisioner staying SDK is correct and
should not be "simplified" into bundles -- bundles' core value is reconcile/destroy,
which is exactly what the additive-only safety model exists to forbid.

---

## 2. How much is done

### Landed and verified (green gate 1706 tests, 0 failing)

| Area | State |
|---|---|
| Source intake | `declare-source`, `discover-source` (S3/UC/local scan; adls/gcs/jdbc/sftp/kafka honest `unsupported_yet`), 17-question interview, measured arrival patterns |
| Requirement alignment | Understanding-playback gate (blueprint refuses until confirmed); re-intake diff naming which answer moved which decision |
| Decisions | Decision tables as data (engine/compute/modeling/dq/velocity) with cited thresholds; unknown fact BLOCKS and names itself |
| Blueprint | Mermaid render + rule-that-fired callouts + ONE human confirmation (agent identities refused) |
| Provisioning | Additive-only planner (no destructive step kind exists), idempotent apply, refuses without confirmed blueprint |
| Ingestion codegen | Auto Loader / COPY INTO / JDBC watermark+MERGE-or-refuse / throttled Kafka + registry |
| Schema evolution | Snapshots, drift matrix, governed quarantine panel -> `schema_exclusions.json` |
| dbt generator | Real incremental marts, inferred-member late dims, freshness, retention TBLPROPERTIES, hash keys, liquid clustering, ghost reconcile |
| Orchestration | Hashed schedules, backfill seam, WAP gold swap, OPTIMIZE/VACUUM maintenance, on-call wiring, MAD anomaly alerts |
| Optimization | `config/optimization_playbook.yaml` (29 cited rules) + `performance-optimizer` agent that owns it |
| Agent/instruction integrity | AGENTS.md drift 47->0 with a CI-blocking guard; STAGE_ROUTING 17->25; adapters fixed (were listing 40/130 CLIs); secret+dataset guardrails restored to all adapters |

### NOT done -- and the honest headline

**Nothing has run against a real Databricks account.** Every gain above is
test-and-smoke level. The replay is the only thing that converts "built" into
"works".

---

## 3. How far accuracy actually moved

Measured, before -> after:

| Dimension | Before | After |
|---|---|---|
| Green gate | 4 failing (2 pre-existing, unfixed) | **0 failing**, 1684 -> 1706 tests |
| CLIs visible to an agent | 83 / 130 (36% invisible) | **130 / 130**, enforced by a CI test |
| CLIs in generated skill adapters | 40 / 130 | **130 / 130** |
| Routed pipeline stages | 17 (none covering the new spine) | **25** + reverse-coverage test |
| Fabricated values in decisions | size = `rows*cols*16` when unmeasured; null counts from a LIMIT-ed sample; lane guessed | **deleted**: absent measurement BLOCKS with a named fact; null counts exact via full-scan aggregate; arrival pattern measured from mtimes |
| Silent-wrong-data classes open | merge-without-unique_key, implicit on_schema_change, positional replace_where, identity keys, JDBC append duplication, late-arriving dims dropped, nested-dtype profile abort | **all closed and enforced against emitted text** |
| Stale instructions that produce hallucinated flags | >=3 (incl. a MANDATORY GATE leading with a nonexistent `--export`) | **0 found in a full sweep** |
| Guardrails in non-Claude adapters | secret + raw-dataset rules silently missing | **restored, grep-verified, guard test** |

The theme: the platform moved from *guessing plausibly* to *refusing honestly*. That is
the accuracy gain -- not more features, but fewer places where a wrong answer could be
produced confidently.

---

## 4. What is left

### Phase A (revised CLI-first) -- blocks the replay

- **A1 readiness diagnosis (SDK)**: count auth profiles per host and report the
  same-host conflict that breaks every `bundle` command; report warehouse state
  (STOPPED = cold start, not an error); name which credential source was used.
- **A2 code delivery (CLI)**: wrap `databricks sync` -- NOT a custom uploader, NOT
  `workspace import-dir`. Ships `workspaces/<ws>/ingestion/` and `dbt/`.
- **A3 generation gate (CLI)**: `dbt parse` after generation and inside
  `verify-dbt-project`; fail on exit 2. Also promote `NoNodesForSelectionCriteria`
  to an error (an empty selection currently exits 0).
- **A4 DAG verification (CLI)**: `astro dev parse` + `airflow dags list-import-errors`
  + `airflow tasks render` on the emitted DAG, so a broken DAG is caught before deploy.

### Phase 0 -- needs the human (AWS console)

Resolve the duplicate auth profile; create + validate the UC storage credential for
the S3 source. Assistant generates the IAM trust/permissions JSON and the
`storage-credentials create/validate` commands.

### Phase B -- the acceptance test

Replay the original failed session on `workspaces/rcm` against real Databricks:
declare -> discover -> intake -> playback -> ONE confirm -> provision -> land ->
`dbt build` -> Airflow leg. Every refusal or manual step is a FINDING written to
`docs/plans/rcm_replay_findings.md`. Findings re-rank Phase C.

### Phase C -- post-replay, CLI-first where possible

- `dbt docs generate` -> `manifest.json`/`catalog.json` as the lineage + ghost-reconcile
  source (replaces our alias-diff heuristic with `relation_name`).
- Publish dbt `target/` state to a UC volume -> unlocks `state:modified`, `--defer`,
  `dbt retry`, `dbt clone`.
- Airflow pools (a backfill can currently starve the nightly run) + `is_paused`
  monitoring via REST.
- Playbook telemetry collector: `system.query_history` + table detail -> `consult()`.
  The 29 rules still have no production caller.
- Dashboard read path: pushdown + version-keyed cache (full `delta_scan` per callback today).

### Phase D -- the flip

AGENTS.md/CLAUDE.md inversion, `--local` demotion, retire legacy
`prepare-solution-blueprint` (collides with `prepare-blueprint` on the same artifact path).

### Roadmap (own brainstorm, not this plan)

Semantic layer (`metrics.yml`) first -- cheapest high-leverage item, serves self-serve
analysts and stops metric divergence. Then: scheduled report delivery, ML feature
tables, reverse-ETL, Delta Sharing, right-to-be-forgotten erasure, bitemporal gold,
ADLS/GCS discovery scanners (ingestion codegen already handles those schemes -- the
platform can generate ingestion for a source it cannot yet discover).

---

## 5. Handoff notes (environment facts a fresh session needs)

- Tests: `.venv\Scripts\python.exe -m unittest <module>`. **Never `uv run` for tests** --
  it resyncs pre-release pyspark and breaks Delta tests. A PreToolUse hook enforces this.
- Full gate: `.venv\Scripts\python.exe -m core.dev.green_gate`. Scary-looking
  SECURITY REFUSAL / "gate exploded" lines in its output are tests exercising refusal
  paths; they appear in passing runs.
- `astro` and `docker` are on PATH; Airflow is deliberately NOT in `.venv` (installing it
  downgrades shared deps).
- A secrets PreToolUse hook blocks commands whose text names credential files -- it will
  block a commit message that mentions one. Reword, do not disable.
- `.agents/skill_route_manifest.json` is still unguarded by the drift test (only
  `tools.json` is covered).
- `skills/kpi-analyst/agents/openai.yaml` is vendor-named in a provider-agnostic repo and
  is the only sidecar on the legacy `interface:` schema. Rename is safe; left as a human call.
- Uncommitted and untouched by this session: `tools/profiler.py`, `core/config.py`,
  `dashboard.py`, `uv.lock`, `.gitignore` -- pre-existing WIP, deliberately not staged.
- `pytest` is absent from `.venv`, so `tests/test_delegation_pipeline.py` cannot import
  (pre-existing).

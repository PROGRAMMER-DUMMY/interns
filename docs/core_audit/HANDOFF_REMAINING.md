# Handoff — core/ remediation: the last 4 baseline test failures

**For a fresh session.** The core/ audit + remediation P0–P8 are done. The full suite is at
**4 failed / 1640 passed** (was 24 failed at the P0 baseline). This doc is the contract for
finishing those 4. Read this, then the per-item detail below. You do **not** need to re-read the
audit; `docs/core_audit/REMEDIATION_PLAN.md` (ledger + "Finishing pass" section) and `SUMMARY.md`
have the context.

## Repo state
- All fix work is committed and **pushed** across stacked branches off `main`:
  `fix/core-p0-hygiene` → `…-p1-pii` → `…-p2-gates` → `…-p3-injection` → `…-p4-concurrency` →
  `…-p5-correctness` → `…-p6-silent-except` → `…-p7-wiring` → `…-p8-cleanup` → `fix/core-finish`.
- `fix/core-finish` (HEAD `c469472`) is the tip and contains every prior phase. **Start the new
  work on a branch off `fix/core-finish`**: `git checkout fix/core-finish && git checkout -b fix/core-finish-2`.
- Regression tests live in `tests/regressions/test_core_p<N>_*.py`. Add new ones there.

## Conventions (unchanged, honor them)
- One logical fix = one commit; commit+push incrementally so nothing is lost mid-session.
- Workspace-agnostic (no domain words). Local-safe by default. ASCII markers only (`[ok] [~] [x] [blocked]`), no emojis.
- **Never** hand-edit generated contracts to mask a bug — fix the producer.
- Per fix, run the targeted test(s), then the owning test file(s) to confirm no regression. The
  phase gate is `python -m pytest tests/ -q` (~2 min) → expect the failed count to only go DOWN
  from 4. Baseline is **4 failed / 1640 passed**; any NEW failure is yours to fix before commit.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Parallel background agents: the only config that works here is **non-isolated, main-tree,
  disjoint files, NO agent git** (the agent edits one file and you do all git/commits). Worktree
  isolation is broken in this env (wrong base + can't run git). Items 2/3/4 below touch disjoint
  files, so they can be farmed to 3 agents at once if you want — but each is a real feature-build,
  so budget for ~1 agent session each, and they may hit session limits mid-task (checkpoint by
  committing partials yourself).

---

## The 4 remaining

### 1. `test_kpi_pipeline_wrapper` relationship gate — **DECISION REQUIRED, likely WON'T-FIX**
- Test: `tests/test_kpi_pipeline_wrapper.py:371`
  `PipelineWrapperTests::test_pipeline_main_relationship_gate_fires_for_candidate_relationships`
- It wants `pipeline_main` (`core/onboarding/workspace/flow.py`, around the STEP-3 relationship
  block ~line 3847) to **exit 1 + print `[blocked]` + list `rel_test_001`** whenever any candidate
  (non-executable) relationship exists.
- **Why it's deferred / probably should stay failing:** the existing code deliberately does NOT
  hard-block on candidate count (read the reasoned comment right there). Blocking on "any candidate
  exists" would **block legitimate single-dataset KPIs that need no join** — including the test's own
  "count encounters" single-CSV workspace. I implemented the gate, proved it over-blocks, and
  reverted it. Also: the test pre-seeds `relationship_contracts.json`, but blocker-panel prep
  (`core/onboarding/kpi/blocker_workflow.py:89`) runs the **real** `RelationshipContractBuilder.build()`
  which rewrites it to `relationships: []` for a single-dataset workspace (`contracts.py:142`), so a
  "read ids from disk" gate can't see the pre-seeded id anyway.
- **Action for the new session:** make a product call WITH THE USER. Options:
  (a) **Won't-fix** — delete/replace the test (the per-KPI join-proof gate inside `start` is the
      correct stop, and it already works). Recommended.
  (b) If a wrapper-level gate is truly wanted, gate **only** when an in-scope KPI actually needs an
      unapproved join (not on raw candidate count) and source the ids from the builder RESULT, not
      the disk file — and fix the `blocker_workflow` build clobbering a populated contracts file with
      empty (that overwrite is itself a latent data-loss bug worth fixing regardless).

### 2. `test_data_model_image_parser` — OCR text → schema candidates
- Test: `tests/test_data_model_image_parser.py:18`
  `test_parses_ocr_text_into_schema_candidates` — asserts table names `Fact`, `Dim_Patient`,
  `Dim_Department` are extracted from OCR text.
- Producer: `core/onboarding/data_model/image_parser.py`.
- **What's needed:** the OCR-text → schema-candidate extractor must recognize ER-diagram table
  blocks (e.g. a table title line followed by column lines) and emit those table names. Read the
  test's OCR fixture text first to see the exact shape it must parse; keep it generic (pattern-based,
  no hardcoded "Patient"/"Department" — those are just the fixture's values).
- Effort: medium (parsing/NLP heuristics). Regression check: `tests/test_data_model_image_parser.py`.

### 3. `test_kpi_proof_packet` — `data_engineering_evidence` section
- Test: `tests/test_kpi_proof_packet.py:247-278` (setup starts ~line 150).
- Producer: `core/onboarding/kpi/proof_packet.py` — `KPIProofPacketBuilder.run()` (line 65) builds
  the packet dict; `_render_markdown()` (line 543) renders it.
- **What's needed:** add a `data_engineering_evidence` block to the packet payload that aggregates,
  from artifacts the test pre-writes:
  - `catalog_contract.json` → `catalog_object_count`, `table_format` (e.g. `local_parquet`)
  - `data_engineering_route.json` → `selected_track` (`medallion`)
  - pipeline plan / `layered_pipeline_harness/current.json` → `pipeline_status` (`blocked`),
    `layered_harness_status` (`failed`), and findings → `blockers` list (e.g.
    `"pipeline_plan_blocked: Pipeline plan status is blocked."`)
  - `pipeline_execution_harness/current.json` → `{status, passed, failed}`
  - `data_quality_harness/current.json` + duplicate review → `{status, duplicate_finding_count,
    unresolved_finding_count}`
  - a `blockers` entry `"percentage_denominator_scope_unresolved: Denominator scope is unresolved."`
    (see how the packet already computes denominator-scope blockers)
  - top-level `payload["artifacts"][...]["exists"]` flags for the new artifacts
  - markdown: a "Data Engineering Evidence" section containing `local_parquet`, `pipeline_plan_blocked`,
    `2 passed / 1 failed`, `2 duplicate / 1 unresolved`.
- Read lines 150-278 of the test for the EXACT keys/strings asserted — they are precise.
- Effort: medium-large (pure aggregation + render, but many exact-match assertions). Regression
  check: `tests/test_kpi_proof_packet.py`.

### 4. `test_result_view_builder` — mismatched-grain percentage via window function
- Test: `tests/test_result_view_builder.py:312`
  `test_mismatched_grain_percentage_now_emits_window_function_instead_of_fallback` — asserts the
  generated SQL contains `PARTITION BY "departement"` (a window function) and NOT
  `-- Generic builder fallback`.
- Producer: `core/onboarding/kpi/result_view_builder.py`. Relevant seams:
  `_detect_window_intent()` (line 461) returns `{"kind": "mismatched_grain_percentage", "partition": …}`;
  the build path that consumes it is around line 813 (`if window_intent.get("kind") == "mismatched_grain_percentage"`).
  Today that path falls through to the single-attribution fallback (`__attribution_rn`, see test's
  actual output) instead of emitting `OVER (PARTITION BY <partition>)`.
- **What's needed:** when intent is `mismatched_grain_percentage`, emit the percentage as a window
  function over the partition column(s) — `... / SUM(...) OVER (PARTITION BY "<partition>")` — rather
  than the attribution-row fallback. Keep banding/quoting consistent with the rest of the file
  (`quote_ident`).
- Effort: medium-deep (share/percentage SQL generation; verify cross-engine parity tests
  `tests/test_engine_parity*.py` and `tests/test_parser_parity.py` still pass).
- Regression check: `tests/test_result_view_builder.py` (note one OTHER test in this file was already
  passing — don't break it) + the parity tests.

---

## Suggested order
Item 1 first (it's a 5-minute product decision with the user — likely delete the test). Then 4 → 2 → 3
by ascending ambiguity, or farm 2/3/4 to three disjoint-file agents in parallel. After all land,
run the full gate; target is **0 failed / 1644 passed** (the 4 move to passing; +new regression
tests). Update the `REMEDIATION_PLAN.md` ledger and this file when done.

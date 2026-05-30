# HANDOFF — autoresearch / KPI platform

State (2026-05-30): the data/KPI engine is working, tested, and proven across SQL/Polars/PySpark,
workspace-agnostic, with reliability gates and a clean panel contract. This doc captures only what a
fresh session can't re-derive — the **weak points** and how to continue. Durable architecture facts
live in the auto-loaded memory index (`C:\Users\shubh\.claude\projects\C--Users-shubh-OneDrive-Desktop-interns\memory\MEMORY.md`).

## Weak points / risks (the important part)

1. **EVERYTHING IS UNCOMMITTED.** A very large change set sits on `main` with no commit — the single
   biggest risk. New modules: `core/onboarding/kpi/{verify_kpi_output,kpi_intent,engine_recommender,
   generate_kpi_engines,polars_generator,pyspark_generator,local_warehouse}.py`,
   `core/onboarding/{panel_contract.py, harness/engine_generation_harness.py, workspace/handoff_cli.py}`,
   `skills/data-model-creation/`, `.github/workflows/ci.yml`, and ~15 `tests/test_*.py`. Modified:
   `sql_generator.py, result_view_builder.py, delegation.py, flow.py, project_harness.py,
   workflow_guard_harness.py, source_to_target_planner.py, generation_workflow.py (kpi + data_model),
   generation_quality.py, contracts.py, pyproject.toml`. Also git-tracked DELETIONS from cleanup
   (rel_*.json, mapping_check.json, s2t_plan_copy.json, final_kpis.sql, get_baseline_metrics.py,
   run_hospital_kpis.py, summarize_results.py, workspace_scenarios.md, state/, .memory/).
   ACTION: branch, review `git status` / `git diff`, commit.

2. **4 pre-existing `test_enterprise_optimization` failures were NEVER fixed** (only confirmed not
   *added* to): `test_build_metadata_store_defaults_to_delta`, `test_kpi_feature_resolver_writes_mapping_and_blockers`
   (`proven_alias` vs `proven_direct`), `test_workspace_onboarding_generates_fresh_workspace_artifacts`
   (delta_metadata `_delta_log`), `test_workspace_presentation_exports_svg_xlsx_and_manifest`. They block a
   single "all green" signal. Likely env/optional-dep (deltalake metadata store, openpyxl) + a resolver
   state-name drift.

3. **CI has never actually run.** `.github/workflows/ci.yml` exists (a curated `tests` job + a
   `pyspark-parity` job with JDK17 + `CI_RUN_SPARK=1`) but no runner has executed it. "Proven in CI" is
   aspirational until a push triggers it.

4. **Complexity grew, not shrank.** ~55 CLI commands in pyproject + meta-infra added this session
   (routing tables, panel contracts, handoff docs, session-monitoring, coverage tests, 5 new commands).
   The turn-1 overengineering concern is MORE live now. Do a deliberate pass with the
   `overengineering-auditor` skill before adding more — decide which commands/layers earn their keep.

5. **No live demo state.** `workspaces/Healthcare-RCM-Data-Platform/interns/` was deleted in cleanup.
   To get a working demo back: `uv run onboard-workspace --workspace workspaces/Healthcare-RCM-Data-Platform`
   then `resolve-kpi-features` → `build-relationship-contracts` (+ approve PatientID/DeptID rels) →
   `generate-kpi-sql`. kpi_002 also needs the `departement→departments.Name` feature confirmed.

6. **Unexplained workspace wipe (unreproduced).** Mid-session the workspace `interns/**` was emptied
   once; a protected repro proved the project harness does NOT cause it, and no core `rmtree` targets it.
   Root cause unknown. Watch for it; the `session_not_monitored` guard helps surface lost sessions.

7. **Full suite not all-green locally.** `test_genericity_audit.py` and `test_result_view_builder.py`
   import `pytest` (not installed here) so they only run under pytest/CI; some legacy files have ruff debt.

## How to work in this repo (non-obvious)

- **Run tests/tools with `.venv\Scripts\python.exe`, NOT `uv run`** — `uv run` resyncs and reinstalls
  pre-release `pyspark 4.1.1` (no Delta), breaking PySpark. pyproject is pinned `pyspark>=3.5.1,<4`.
- **PySpark local run needs JDK 8/11/17 + winutils** (box has Java 24; JDK17 at `C:\Program Files\Java\jdk-17`,
  winutils at `C:\hadoop`). See memory `project_pyspark_local_run.md`. SQL/Polars need no JVM.
- **The green gate** is the curated suite in `ci.yml` (~99 tests). Run it before claiming done.
- **Verify a KPI workspace**: `verify-kpi-output --workspace <ws> [--cross-engine]` (self-grill;
  `--cross-engine` runs Polars/PySpark vs SQL).
- **No emojis** in any output (memory `feedback_no_emojis.md`) — use `[ok]/[~]/[x]/[blocked]`.
- **Panels** = `current.json` (machine) + `current.md` (lean human card, rendered verbatim). Family-A
  decision panels go through `core/onboarding/panel_contract.normalize_decision_panel`.
- **Agent/skill routing** is `core/onboarding/workspace/delegation.STAGE_ROUTING` (locked by
  `tests/test_agent_skill_routing.py`). Delegations write handoff docs to `interns/state/handoffs/`;
  fetch via `uv run handoff latest|render`.

## Suggested next steps (priority order)
1. `git` branch + commit the working tree (protect the work).
2. Run the curated CI suite via venv python; confirm green.
3. Fix or quarantine the 4 enterprise failures so "all green" means something.
4. `overengineering-auditor` pass on the ~55 CLIs + meta-layers.
5. Re-onboard the demo workspace if you need a live example.
6. Optional follow-ups noted earlier: Family-B render standardization; machine-envelope pass; wire the
   remaining idle-stage emitters (remote_execution/notification/flow_entry already have routing in the table).

## Suggested skills
`overengineering-auditor` (complexity pass), `gitagent`/`git-guardrails` (commit safely),
`auditor` (validate state vs intent), `maintainer` (fix the 4 failing tests).

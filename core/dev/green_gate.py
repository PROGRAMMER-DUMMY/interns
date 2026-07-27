"""green-gate: the project's portable test gate.

One command, runnable from any CLI (Claude, Codex, Gemini) or a shell, that runs
the curated CI suite plus the enterprise suite the same way `.github/workflows/ci.yml`
does -- and, with ``--sweep``, the broader resolver/pipeline modules whose
blast radius a change can reach.

Why a console script instead of a per-CLI skill: the logic lives in the repo once
and every agent just calls ``green-gate``. The CLI-specific skill/agent wrappers
only need to invoke this command.

Runtime note: this script must run under the project venv interpreter
(``.venv\\Scripts\\python.exe``), NOT ``uv run`` -- ``uv run`` resyncs and reinstalls
pre-release pyspark 4.1.1 (no Delta), which breaks the pyspark-backed tests. The
``green-gate`` console script is wired to the venv interpreter by install.

Output uses ASCII status markers only ([ok]/[x]/[~]); no emojis (repo rule).
"""
from __future__ import annotations

import argparse
import io
import json
import unittest
from pathlib import Path

# Curated, deterministic suite -- mirrors the `tests` job in ci.yml. This is THE
# green gate: any failure here is a hard stop.
CURATED_MODULES: tuple[str, ...] = (
    "tests.test_parser_parity",
    "tests.test_engine_parity",
    "tests.test_engine_parity_aggregate",
    "tests.test_kpi_definition_bulk",
    "tests.test_cli_deprecation_redirect",
    "tests.test_base_source_selector",
    "tests.test_kpi_engine_generators",
    "tests.test_verify_kpi_output",
    "tests.test_engine_recommender",
    "tests.test_generate_kpi_engines",
    "tests.test_engine_generation_harness",
    "tests.test_kpi_generation_quality",
    "tests.test_data_model_generation",
    "tests.test_agent_skill_routing",
    "tests.test_delegation_handoff",
    "tests.test_handoff_cli",
    "tests.test_workflow_guard_reliability",
    "tests.test_workflow_guard_roster",
    "tests.test_workflow_guard_monitoring",
    "tests.test_project_harness_gates",
    "tests.test_intent_coverage",
    "tests.test_kpi_extraction_determinism",
    "tests.test_document_ingestion",
    "tests.test_document_candidate_apply",
    "tests.test_document_kpi_merge",
    "tests.test_document_candidate_consumption",
    "tests.test_document_type_classification",
    "tests.test_intent_contract_routing",
    "tests.test_phi_gate",
    "tests.test_workflow_guard_specialist_firing",
    # Dashboard + tooling suites added 2026-06 (data-driven panels, verify gate,
    # DESIGN.md, token report, wiki lineage). Previously passed on demand but were
    # not gated — wiring them in so they protect against regressions.
    "tests.test_dashboard_inference",
    "tests.test_dashboard_profile",
    "tests.test_dashboard_design_md",
    "tests.test_dashboard_nested",
    "tests.test_dashboard_verify",
    "tests.test_token_report",
    "tests.test_wiki_writer",
    # Pipeline-SQL failure contracts: green again after the validator now rejects
    # empty pipeline SQL + raw dataset paths outside CATALOG BOOTSTRAP (2026-06-11).
    "tests.test_failure_contracts",
    # Phase 2.2 hostile-workspace fixes: prose KPI ingestion (F2), blocked-KPI
    # honesty invariant + validator dead-end errors (F1/F3), documented-join
    # relationship evidence (F4a), collision-blocks-not-resolves (F4b).
    "tests.test_prose_kpi_ingestion",
    "tests.test_blocked_kpi_invariant",
    "tests.test_documented_join_evidence",
    "tests.test_collision_blocking",
    # Phase 3 track A: incremental onboarding fingerprints + DuckDB pushdown profiling.
    "tests.test_onboarding_incremental",
    "tests.test_profile_pushdown",
    # Phase 3 track C: incremental medallion refresh + deploy-plan schema.
    "tests.test_medallion_incremental",
    # User-authored workspace data policy (custom PII/PHI + allowlist + tier).
    "tests.test_workspace_data_policy",
    # data-to-viz chart-selection knowledge base + new chart types.
    "tests.test_chart_knowledge",
    # Databricks deployment gates (PRD section 7) + apply-deploy boundary.
    "tests.test_deploy_gates",
    # Workspace-lock re-entrancy: nested same-process acquisition must not
    # self-deadlock (prepare-kpi-blocker-panel --force-onboard regression);
    # cross-thread/cross-process exclusion unchanged.
    "tests.test_workspace_lock_reentrancy",
    # Medallion UC deployer: approval consumption, G4/G5 re-checks, dry-run seam.
    "tests.test_workspace_deployer",
    # Slice 3 hostile refinement 1c: no_supporting_evidence blocker labeling
    # (zero prose-term anchors -> confirm-missing-data-or-point-at-source ask).
    "tests.test_no_supporting_evidence",
    # Slice 3 hostile refinement 1b: dictionary-vs-profile reconciliation
    # (documented claims contradicted by profile evidence become structured
    # dictionary_conflicts + an answerable blocker on tainted KPIs).
    "tests.test_dictionary_reconciliation",
    # Slice 3 hostile refinement 1d: derived-feature option synthesis
    # (a quantity in mixed units yields a JSON-backed UOM-normalization option;
    # honest blocker stays when no mixed-unit evidence exists).
    "tests.test_derived_option_synthesis",
    # Session 2026-06: dashboard de-RCM + medallion production hardening +
    # storage measurement/strategy + pipeline orchestration + shape gate.
    # Registered so the gate protects them against regression.
    "tests.test_dashboard_measure_semantics",
    "tests.test_workspace_shape_matrix",
    "tests.test_medallion_production_hardening",
    "tests.test_medallion_p2_p3",
    "tests.test_referential_integrity",
    "tests.test_storage_report",
    "tests.test_compute_decision_executable",
    "tests.test_pipeline_orchestration",
    "tests.test_kpi_secondary_measure",
    "tests.test_blocker_panel_renderer_previews",
    "tests.test_export_decimal_safety",
    "tests.test_layout_planner",
    "tests.test_dashboard_card_density",
    "tests.test_screener_selftest",
    # Phase 1a.1: agent-token cost-ledger ANCHOR half (env-native session join key,
    # honest-empty schema, zero-anchor/over-threshold liveness gate).
    "tests.test_cost_ledger",
    # Phase 1a.2b: transcript INGEST (privacy boundary, loud liveness, run-level
    # attribution, logged dedupe).
    "tests.test_cost_ingest",
    # Priority 1.1: PHI/PII review-and-consent panel (disposition capture,
    # not_sensitive_columns override with confirmed_by provenance, refuses
    # agent-asserted confirmers outright).
    "tests.test_phi_review_panel",
    # Priority 3 substrate work: profiling a Unity Catalog table via the SQL
    # warehouse (DatabricksClient.execute_query), not local DuckDB/direct-S3.
    "tests.test_databricks_table_profiler",
    # Generic, additive "databricks_source" discovery in onboarding.py --
    # workspace_settings.json-driven, zero hardcoded workspace/catalog/table
    # names in core/ (locked in by an explicit genericity-guard test).
    "tests.test_onboarding_databricks_source",
    # Generic Databricks-first-class onboarding (dynamic-cooking-firefly plan):
    # Path(uc_fqn).stem/.name silently mangled a `catalog`.`schema`.`table`
    # identifier -- corrupting dedup keys, staging-view table refs, and
    # dataset-file resolution for any UC-sourced profile. Plus the explicit,
    # human-confirmed data-source-mode panel (local_files/additive/exclusive)
    # and the golden-fixture proof that a local-only workspace (the
    # overwhelming majority, no databricks_source declared at all) is
    # completely unaffected by any of it.
    "tests.test_dataset_identity",
    "tests.test_local_only_workspace_regression",
    "tests.test_databricks_source_mode",
    "tests.test_data_source_panel",
    # Phase C (single-statement rewrite): parse_kpi() bakes column references
    # into Dimension/Aggregation.predicate/WindowSpec expressions at PARSE
    # time, before dialect-aware rendering -- most of those call sites never
    # threaded the real dialect through, silently keeping a duckdb-style
    # double-quoted identifier for databricks-dialect SQL. Spark SQL treats a
    # double-quoted string as a STRING LITERAL, not an identifier, by
    # default -- this doesn't just look wrong, it silently changes what a
    # predicate/GROUP BY/PARTITION BY computes. Promoted from SWEEP_MODULES:
    # a correctness bug in the exact capability this work builds (real,
    # executable Databricks KPI SQL) must always be enforced, not swept.
    "tests.test_result_view_builder",
    # Phase A/C: UC-sourced (format="delta") staging correctness and the
    # single-statement CTE rewrite that depends on it -- also promoted from
    # SWEEP_MODULES for the same reason.
    "tests.test_pipeline_sql_generator",
    # Phase C: real KPI execution against Databricks -- the remote-approval
    # gate, PHI-gate denial, mocked-success provenance, and warehouse-error
    # handling for _execute_records_databricks(). No live warehouse
    # dependency (DatabricksClient mocked), so always safe to enforce.
    "tests.test_kpi_execution_harness",
    # Found live via a real end-to-end walkthrough against the healthcare_rcm
    # Unity Catalog: blocker_question_panel.py's human-facing candidate
    # labels used Path(uc_fqn).stem in 16 places, rendering THREE DISTINCT
    # candidate tables as the identical, useless `catalog`.`schema`.column
    # label -- a human reading the panel could not tell them apart.
    "tests.test_blocker_panel_uc_sourced_labels",
    # Priority 2.1/3.6 minimal slice: medallion design-panel ratification.
    # Requires a real, non-blank --reasoning per item so a ratification
    # record can't collapse into a name-only rubber stamp.
    "tests.test_design_ratify",
    # KPI metric-expression feature extraction: free-text filler words ("a",
    # "no") and possessive-apostrophe fragments ("KPI2's" -> "s") must never
    # become candidate features -- found live via Hostile_Synthetic KPI
    # resolution, where the validator correctly rejected them but the fix
    # belongs in extraction, not the downstream denylist.
    "tests.test_feature_expression",
    # apply-kpi-panel-answer idempotency must key on WHICH blocker was
    # answered, not just the literal --answer text -- found live via
    # Hostile_Synthetic KPI resolution: two different blockers both
    # answered `option_a` collided on the same op_id, silently skipping
    # the second application.
    "tests.test_blocker_cli_op_id",
    # A --custom-definition answer's source_columns must never come back
    # empty for non-empty text -- an empty list silently no-ops the merge
    # into kpi_feature_mapping.json, leaving a feature's stale pre-override
    # candidate dataset in place forever even though state flips to
    # user_confirmed. Found live via Hostile_Synthetic KPI resolution.
    "tests.test_blocker_workflow_custom_definition",
    # _dedupe_features_by_physical_column collapsed two UNRELATED, still-
    # unresolved features into one whenever they happened to share the same
    # generic candidate-column SET (not a real resolution) -- silently
    # dropping a real, distinct blocker question. A significant, broadly-
    # impactful correctness bug in feature resolution generally, not
    # specific to any one workspace; promoted here from SWEEP_MODULES so
    # it's always enforced, not just occasionally swept.
    "tests.test_contextual_dictionary_mapping",
    # dbt+Airflow production-backend Phase D1: the per-enterprise config
    # resolution seam (core.config.resolve_databricks_config /
    # WorkspaceLayout.enterprise_id) and the dbt project generator itself --
    # the piece every later phase of that plan depends on.
    "tests.test_resolve_databricks_config",
    "tests.test_dbt_project_generator",
    # dbt+Airflow production-backend Phase D2: data-quality rules authored
    # the same human-in-the-loop way KPI features already are, generating
    # real dbt schema tests with tiered severity from confirmed answers.
    "tests.test_data_quality_panel",
    # dbt+Airflow production-backend Phase D3: workspace-aware stage
    # topology (dbt_build supersedes medallion_build+kpi_results) and the
    # Cosmos wiring that runs it via DBT_RUNNER instead of a subprocess.
    "tests.test_cosmos_dag",
    # dbt+Airflow production-backend Phase D3: bounded/dry-run backfill --
    # refuses an over-threshold date-range span without a human-confirmed
    # override (Human-Gate Provenance Rule), never automatic.
    "tests.test_dbt_backfill",
    # Phase 1.4: `dbt show` verification after generation -- a generated project
    # that compiles but returns zero rows is a blank deliverable, not a pass,
    # and a run without the remote-execution gate is refused, never "verified".
    "tests.test_dbt_verify",
    # Phase 1.2: offline dbt lineage + blast radius. Answers "what breaks
    # if I change this" with no warehouse -- the question nobody could
    # answer when an agent deleted a safety check blind.
    "tests.test_dbt_index",
    # Phase 3: warehouse spend read back out of system.query/system.billing
    # and attributed to one run. Warehouse dollars stay in their OWN
    # artifact -- AnchorEntry.cost_usd is agent-token cost, a different basis.
    "tests.test_warehouse_cost",
    # dbt+Airflow production-backend Phase D4: the dashboard's gold read
    # dispatches local-first between local Delta and a dbt-built Databricks
    # mart, gated by the same remote-execution approval as every other
    # Databricks path -- and the "data as of X" staleness banner.
    "tests.test_dashboard_layers",
    "tests.test_dashboard_staleness_banner",
    # Platform readiness: read-only Databricks/dbt/Airflow status check
    # (check-platform-readiness) surfaced at conversation start -- dbt/
    # Airflow absence is a capability gap for the cloud-native path, never
    # a blocker, since the local-DuckDB KPI flow works without either.
    "tests.test_platform_readiness",
)

# Enterprise optimization suite -- broad behavioral coverage, also expected green.
ENTERPRISE_MODULES: tuple[str, ...] = ("tests.test_enterprise_optimization",)

# Broader blast-radius modules only swept with --sweep. These are NOT part of the
# strict gate; some carry known pre-existing failures (see KNOWN_BASELINE).
SWEEP_MODULES: tuple[str, ...] = (
    "tests.test_kpi_proof_packet",
    "tests.test_workspace_definitions",
    "tests.test_workspace_flow",
    "tests.test_kpi_format_detector",
    "tests.test_workbook_structure",
    "tests.test_kpi_confirmation_panel",
    "tests.test_blocker_panel_renderer_previews",
    "tests.test_catalog_contract",
    "tests.test_contract_versioning",
    "tests.test_workflow_cli_modules",
    "tests.test_ai_app_harness",
    "tests.test_layered_pipeline_harness",
    "tests.test_pipeline_execution_harness",
    "tests.test_pipeline_plan",
    "tests.test_relationship_contracts",
    "tests.test_dashboard_spec_fidelity",
    "tests.test_kpi_intent_contract",
    "tests.test_metric_derivation",
    "tests.test_kpi_nl_chain_e2e",
)

# Pre-existing failures present on the clean tree as of 2026-05-30. A sweep failure
# in this set is "known" (reported, not a regression); anything else is a regression.
KNOWN_BASELINE: frozenset[str] = frozenset(
    {
        "tests.test_kpi_proof_packet.KPIProofPacketTests."
        "test_packet_includes_catalog_route_pipeline_and_layered_harness_evidence_when_present",
    }
)


def regression_modules() -> tuple[str, ...]:
    """Every `tests/regressions/*.py` module, discovered -- never curated.

    A regression test exists to stop one specific defect coming back. If it has to
    be hand-added to a list to run, it protects nothing until someone remembers.
    Discovered on purpose so a NEW regression file is gated the moment it lands.

    This was found live (2026-07-26): the whole `tests/regressions/` tree -- 37
    files, including the fan-trap detector suite -- was in neither CURATED_MODULES
    nor SWEEP_MODULES, so the gate reported "all green" while two of those tests
    were failing on a tree whose safety check had been deleted.
    """
    here = Path(__file__).resolve().parents[2] / "tests" / "regressions"
    if not here.is_dir():
        return ()
    return tuple(
        f"tests.regressions.{p.stem}"
        for p in sorted(here.glob("test_*.py"))
    )


def _run(modules: tuple[str, ...]) -> tuple[unittest.TestResult, list[tuple[str, str]]]:
    """Load and run the given test modules in-process. Returns (result, load_errors)."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    load_errors: list[tuple[str, str]] = []
    for module in modules:
        try:
            suite.addTests(loader.loadTestsFromName(module))
        except Exception as exc:  # import-time failure (e.g. missing optional dep)
            load_errors.append((module, f"{type(exc).__name__}: {exc}"))
    runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
    result = runner.run(suite)
    return result, load_errors


def _failing_ids(result: unittest.TestResult) -> set[str]:
    return {test.id() for test, _ in result.failures} | {test.id() for test, _ in result.errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the project's green gate (curated + enterprise suites)."
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Also run broader blast-radius modules and classify new vs. known-baseline failures.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable JSON summary.")
    args = parser.parse_args(argv)

    gate_modules = CURATED_MODULES + ENTERPRISE_MODULES + regression_modules()
    gate_result, gate_load_errors = _run(gate_modules)
    gate_failures = _failing_ids(gate_result)

    sweep_failures: set[str] = set()
    sweep_load_errors: list[tuple[str, str]] = []
    regressions: set[str] = set()
    known: set[str] = set()
    if args.sweep:
        sweep_result, sweep_load_errors = _run(SWEEP_MODULES)
        sweep_failures = _failing_ids(sweep_result)
        regressions = sweep_failures - KNOWN_BASELINE
        known = sweep_failures & KNOWN_BASELINE

    load_errors = gate_load_errors + sweep_load_errors
    gate_ran = gate_result.testsRun
    gate_ok = not gate_failures and not gate_load_errors
    ok = gate_ok and not regressions

    if args.json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "gate_ok": gate_ok,
                    "gate_tests_run": gate_ran,
                    "gate_failures": sorted(gate_failures),
                    "sweep_regressions": sorted(regressions),
                    "sweep_known_baseline": sorted(known),
                    "load_errors": [{"module": m, "error": e} for m, e in load_errors],
                },
                indent=2,
            )
        )
        return 0 if ok else 1

    mark = "[ok]" if gate_ok else "[x]"
    print(f"{mark} Green gate: {gate_ran} tests, {len(gate_failures)} failing")
    for fid in sorted(gate_failures):
        print(f"  [x] {fid}")
    for module, err in gate_load_errors:
        print(f"  [x] could not load {module}: {err}")

    if args.sweep:
        smark = "[ok]" if not regressions else "[x]"
        print(f"{smark} Sweep: {len(regressions)} regression(s), {len(known)} known-baseline")
        for fid in sorted(regressions):
            print(f"  [x] REGRESSION {fid}")
        for fid in sorted(known):
            print(f"  [~] known {fid}")
        for module, err in sweep_load_errors:
            print(f"  [x] could not load {module}: {err}")

    print("[ok] all green" if ok else "[x] not green -- see failures above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
